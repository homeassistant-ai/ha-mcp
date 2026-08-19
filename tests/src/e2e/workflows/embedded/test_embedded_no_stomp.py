"""The embedded install must never replace a package HA governs.

Issues #2135/#2146: ha-mcp's runtime install into Home Assistant's own
site-packages is a plain pip run — non-atomic uninstall-then-extract. A
dependency spec that forces pip to replace a package HA relies on opens a
tear window: interrupt the replacement (restart, watchdog, OOM during HA
startup) and the package is left with files from two releases, which for
``websockets`` killed every WS connect with ImportError until something
rewrote site-packages.

The policy this test enforces is the linked/unlinked split:

- **Governed packages** — anything HA itself speaks for, i.e. named in the
  image's own ``package_constraints.txt`` (copied out of the container by
  the entrypoint) or a direct integration requirement in the HA release's
  ``requirements_all.txt`` (fetched for the exact image version under
  test) — must NEVER be replaced or removed by our install. ``websockets``
  (floor-constrained) and ``mcp`` (a real integration dependency with no
  constraints entry) are both protected here.
- **Ungoverned transitives** — packages present in the image only as a
  side-effect, with no HA constraint and no integration requiring them
  (e.g. ``rich``, which fastmcp floors at >=13.9.4 while the image happens
  to carry 10.16.2) — may legitimately upgrade; they are OUR dependencies
  to keep current. Such upgrades are printed loudly so a new one never
  lands unnoticed.
- **Removals** are never legitimate for either class.

The snapshots come from the session container every embedded-lane test
already boots (see ``_build_ha_testcontainer`` in the e2e conftest), so the
guard also covers dependencies we don't declare directly — a transitive
floor-bump into governed territory trips it the same way the old exact
websockets pin would have.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
import requests
from test_constants import HA_TEST_IMAGE

from ...conftest import (
    EMBEDDED_FREEZE_AFTER,
    EMBEDDED_FREEZE_BEFORE,
    EMBEDDED_HA_CONSTRAINTS_COPY,
)

_REQUIREMENTS_ALL_URL = (
    "https://raw.githubusercontent.com/home-assistant/core/"
    "{version}/requirements_all.txt"
)


def _component_constant(name: str) -> str:
    """Read a string constant out of the component's const.py by source.

    Deliberately NOT an import: importing anything under
    ``custom_components.ha_mcp_tools`` runs the package __init__, which
    imports ``homeassistant`` — absent from the e2e runner's environment,
    so the import would break COLLECTION of this whole suite. Reading the
    source still fails loudly if the constant is renamed, which is the
    property an inlined literal would lose.
    """
    source = (_REPO_ROOT / "custom_components" / "ha_mcp_tools" / "const.py").read_text(
        encoding="utf-8"
    )
    match = re.search(rf'^{name} = "([^"]+)"', source, re.MULTILINE)
    assert match, f"{name} is gone from const.py — update this guard"
    return match.group(1)


_REPO_ROOT = Path(__file__).resolve().parents[5]
DATA_LAST_PIP_SPEC = _component_constant("DATA_LAST_PIP_SPEC")


def _canonical(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _parse_freeze(text: str) -> dict[str, str]:
    """Parse ``pip list --format=freeze`` output into {canonical_name: version}.

    Lines without ``==`` (editable installs, direct URLs) are recorded with
    the raw remainder as the version so a change in their shape still trips
    the comparison.
    """
    packages: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, version = line.partition("==")
        packages[_canonical(name)] = version.strip() if sep else line
    return packages


def _parse_requirement_names(text: str) -> set[str]:
    """Extract canonical package names from a requirements/constraints file."""
    names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        match = re.match(r"([A-Za-z0-9._-]+)", line)
        if match:
            names.add(_canonical(match.group(1)))
    return names


def _fetch_requirements_all(ha_version: str) -> set[str]:
    """Fetch the HA release's direct integration requirements (fail-closed).

    Three attempts against raw.githubusercontent; a total failure FAILS the
    test rather than silently weakening the governed set — a guard that
    can quietly shrink its protection is worse than a loud fetch error.
    """
    url = _REQUIREMENTS_ALL_URL.format(version=ha_version)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return _parse_requirement_names(response.text)
        except requests.exceptions.RequestException as err:
            last_error = err
            if attempt < 2:  # no pointless backoff after the final attempt
                time.sleep(5 * (attempt + 1))
    raise AssertionError(
        f"could not fetch {url} to build the governed-package set "
        f"(last error: {last_error}); the no-stomp guard cannot run "
        "without it"
    )


_KNOWN_BACKENDS = {
    "container",
    "embedded",
    "haos",
    "haos_stdio",
    "haos_inaddon",
    "haos_embedded",
}


def _skip_unless_embedded(info) -> None:
    """Runtime skip with a drift tripwire.

    Runtime skips are invisible to the mass-skip detector (it counts only
    collection-time markers), so a silently changed backend label would
    disable this guard forever with nothing noticing. An UNKNOWN label
    therefore fails loudly instead of skipping.
    """
    backend = info.get("backend")
    assert backend in _KNOWN_BACKENDS, (
        f"unknown backend label {backend!r} — the conftest's backend names "
        "changed; update this guard so it keeps running on the embedded lane"
    )
    if backend != "embedded":
        pytest.skip(
            "freeze snapshots are written only by the embedded testcontainer "
            "backend's preinstall entrypoint (E2E_BACKEND=embedded)"
        )


def _governed_names(config_path: Path) -> set[str]:
    constraints_file = config_path / EMBEDDED_HA_CONSTRAINTS_COPY
    assert constraints_file.is_file(), (
        f"missing constraints copy {constraints_file} — the entrypoint could "
        "not find the image's package_constraints.txt"
    )
    ha_version = HA_TEST_IMAGE.rsplit(":", 1)[-1]
    return _parse_requirement_names(
        constraints_file.read_text(encoding="utf-8")
    ) | _fetch_requirements_all(ha_version)


def _assert_no_governed_changes(
    before: dict[str, str],
    after: dict[str, str],
    governed: set[str],
    context: str,
) -> None:
    replaced = {
        name: (version, after[name])
        for name, version in before.items()
        if name in after and after[name] != version
    }
    removed = sorted(name for name in before if name not in after)
    assert not removed, (
        f"[{context}] packages present before the install are GONE after it: "
        f"{removed}. A removal is never a legitimate side effect — find the "
        "dist conflict and resolve it in pyproject.toml."
    )
    governed_replaced = {
        name: change for name, change in replaced.items() if name in governed
    }
    ungoverned_replaced = {
        name: change for name, change in replaced.items() if name not in governed
    }
    if ungoverned_replaced:
        # Legitimate: our stack's requirement on a package HA does not
        # govern. Loud on purpose — a NEW name appearing here should be
        # noticed and traced, not discovered in an issue report.
        print(
            f"no-stomp guard [{context}]: ungoverned transitive upgrades "
            f"(ours to keep current): {ungoverned_replaced}"
        )
    assert not governed_replaced, (
        f"[{context}] the install replaced packages HA GOVERNS "
        f"(package_constraints.txt or requirements_all.txt): "
        f"{governed_replaced}. A forced in-place replacement of an "
        "HA-governed package is the #2135/#2146 torn-install window: loosen "
        "the conflicting dependency spec (pyproject.toml) to admit HA's "
        "shipped version instead of pinning past it."
    )
    # ADDED packages matter too: `replaced` is built from before.items() and
    # is blind to a governed package the install PULLS IN fresh.
    added_governed = {
        name: after[name] for name in set(after) - set(before) if name in governed
    }
    assert not added_governed, (
        f"[{context}] the install ADDED packages HA governs: {added_governed}. "
        "HA's own requirement processing owns those; installing one behind "
        "its back means our resolution, not HA's, decided the version."
    )
    # websockets is the package the whole fix is about, so its presence in
    # the baseline is asserted rather than assumed — an image that stops
    # shipping it would otherwise turn the check below into a silent no-op.
    assert "websockets" in before, (
        f"[{context}] the HA image no longer ships websockets, so the "
        "check below proved nothing. Confirm how the shared copy reaches "
        "this environment before relaxing this assertion (#2135/#2146)."
    )
    assert after.get("websockets") == before["websockets"], (
        f"[{context}] the install replaced HA's shipped websockets "
        f"({before['websockets']} -> {after.get('websockets')}) — the "
        "exact #2146 failure shape (ha-mcp no longer even declares it; "
        "the vendored copy is the only one we import)"
    )


def test_embedded_preinstall_replaces_nothing_ha_governs(
    ha_container_with_fresh_config,
):
    info = ha_container_with_fresh_config
    _skip_unless_embedded(info)

    config_path = Path(info["config_path"])
    before_file = config_path / EMBEDDED_FREEZE_BEFORE
    after_file = config_path / EMBEDDED_FREEZE_AFTER
    # The entrypoint chain writes these before /init; a booted HA (which
    # the session fixture guarantees) means they must exist.
    assert before_file.is_file(), f"missing preinstall snapshot {before_file}"
    assert after_file.is_file(), f"missing postinstall snapshot {after_file}"

    before = _parse_freeze(before_file.read_text(encoding="utf-8"))
    after = _parse_freeze(after_file.read_text(encoding="utf-8"))
    assert before, "empty pip freeze snapshot — entrypoint capture broke"
    # The bracketed install must have actually DONE something, or the guard
    # is green while proving nothing (e.g. the install moved outside the
    # freeze bracket): the wheel adds ha-mcp itself plus its tree.
    added = set(after) - set(before)
    assert "ha-mcp" in added or "ha-mcp-dev" in added, (
        f"the preinstall added no ha-mcp distribution (added: {sorted(added)[:10]}"
        " ...) — the freeze bracket no longer surrounds the wheel install"
    )

    _assert_no_governed_changes(
        before, after, _governed_names(config_path), context="entrypoint preinstall"
    )


def test_embedded_runtime_install_replaces_nothing_ha_governs(
    ha_container_with_fresh_config,
):
    """The RUNTIME install path is guarded too, not just the preinstall.

    The component's bring-up runs its own force-install inside the live HA
    (the path that carried the eager --upgrade bug the review caught: the
    preinstall bracket alone would have stayed green while the runtime
    install replaced HA's websockets). By the time any test runs, the
    session fixture guarantees that bring-up completed — so a live
    ``pip list`` from the container compared against the pre-install
    snapshot covers everything every install path did.
    """
    info = ha_container_with_fresh_config
    _skip_unless_embedded(info)

    config_path = Path(info["config_path"])
    before_file = config_path / EMBEDDED_FREEZE_BEFORE
    assert before_file.is_file(), f"missing preinstall snapshot {before_file}"
    before = _parse_freeze(before_file.read_text(encoding="utf-8"))
    assert before, "empty pip freeze snapshot — entrypoint capture broke"

    container = info["container"]
    result = container.get_wrapped_container().exec_run(
        ["pip", "list", "--format=freeze"]
    )
    output = (result.output or b"").decode("utf-8", "replace")
    assert result.exit_code == 0, f"in-container pip list failed: {output[:500]}"
    runtime = _parse_freeze(output)
    assert "ha-mcp" in runtime or "ha-mcp-dev" in runtime, (
        "ha-mcp missing from the live container environment — the bring-up "
        "never installed the server at all"
    )
    # POSITIVE marker that the runtime force-install actually executed.
    # Without it this guard passes vacuously whenever bring-up takes the
    # fast path or defers mutations: ha-mcp is in the live `pip list`
    # either way (the entrypoint preinstalled it), so the before/after diff
    # would be empty and the test would be green having exercised nothing.
    #
    # The marker is the STORED spec rather than the bring-up log line: HA
    # rotates home-assistant.log on every start, and the session container
    # is restarted by other tests long before this one runs, so the log is
    # not durable evidence. `last_pip_spec` is, and it is written by
    # exactly one branch — `_store_installed_spec()` runs only when the
    # stored spec DIFFERS from the target, which the fast path (entered
    # only when they are equal) can never satisfy. The conftest seeds the
    # entry with no `last_pip_spec` at all, so its presence here means the
    # component ran its own force-install inside the live HA.
    entries = json.loads(
        (config_path / ".storage" / "core.config_entries").read_text(encoding="utf-8")
    )
    server_entries = [
        entry
        for entry in entries["data"]["entries"]
        if entry.get("data", {}).get("entry_type") == "server"
    ]
    assert len(server_entries) == 1, (
        f"expected exactly one in-process server config entry, got "
        f"{len(server_entries)} — the conftest seeding changed"
    )
    assert server_entries[0]["data"].get(DATA_LAST_PIP_SPEC), (
        "the component's force-install never ran (no last_pip_spec recorded "
        "on the server config entry), so this guard exercised no runtime "
        "install — if bring-up now takes the fast path by design, this test "
        "needs a new way to reach the install path rather than being allowed "
        "to pass vacuously"
    )

    _assert_no_governed_changes(
        before, runtime, _governed_names(config_path), context="runtime install"
    )
