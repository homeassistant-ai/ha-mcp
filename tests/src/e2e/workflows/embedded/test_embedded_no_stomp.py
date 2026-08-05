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
            time.sleep(5 * (attempt + 1))
    pytest.fail(
        f"could not fetch {url} to build the governed-package set "
        f"(last error: {last_error}); the no-stomp guard cannot run "
        "without it"
    )


def test_embedded_preinstall_replaces_nothing_ha_governs(
    ha_container_with_fresh_config,
):
    info = ha_container_with_fresh_config
    if info.get("backend") != "embedded":
        pytest.skip(
            "freeze snapshots are written only by the embedded testcontainer "
            "backend's preinstall entrypoint (E2E_BACKEND=embedded)"
        )

    config_path = Path(info["config_path"])
    before_file = config_path / EMBEDDED_FREEZE_BEFORE
    after_file = config_path / EMBEDDED_FREEZE_AFTER
    constraints_file = config_path / EMBEDDED_HA_CONSTRAINTS_COPY
    # The entrypoint chain writes all three before /init; a booted HA (which
    # the session fixture guarantees) means they must exist.
    assert before_file.is_file(), f"missing preinstall snapshot {before_file}"
    assert after_file.is_file(), f"missing postinstall snapshot {after_file}"
    assert constraints_file.is_file(), (
        f"missing constraints copy {constraints_file} — the entrypoint could "
        "not find the image's package_constraints.txt"
    )

    before = _parse_freeze(before_file.read_text(encoding="utf-8"))
    after = _parse_freeze(after_file.read_text(encoding="utf-8"))
    assert before, "empty pip freeze snapshot — entrypoint capture broke"

    replaced = {
        name: (version, after[name])
        for name, version in before.items()
        if name in after and after[name] != version
    }
    removed = sorted(name for name in before if name not in after)

    # Removing anything the image shipped is never legitimate.
    assert not removed, (
        f"Installing the ha-mcp wheel REMOVED image-shipped packages: "
        f"{removed}. A removal is never a legitimate side effect of the "
        "embedded install — find the dist conflict and resolve it in "
        "pyproject.toml."
    )
    if not replaced:
        return

    ha_version = HA_TEST_IMAGE.rsplit(":", 1)[-1]
    governed = _parse_requirement_names(
        constraints_file.read_text(encoding="utf-8")
    ) | _fetch_requirements_all(ha_version)

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
            "no-stomp guard: ungoverned transitive upgrades by the embedded "
            f"install (ours to keep current): {ungoverned_replaced}"
        )

    assert not governed_replaced, (
        "Installing the ha-mcp wheel replaced packages HA GOVERNS "
        f"(package_constraints.txt or requirements_all.txt): "
        f"{governed_replaced}. A forced in-place replacement of an "
        "HA-governed package is the #2135/#2146 torn-install window: loosen "
        "the conflicting dependency spec (pyproject.toml) to admit HA's "
        "shipped version instead of pinning past it."
    )

    # websockets is the dependency that bit (#2146); assert it explicitly so
    # a regression names the culprit even if the general message changes.
    if "websockets" in before:
        assert after.get("websockets") == before["websockets"], (
            "the embedded install replaced HA's shipped websockets "
            f"({before['websockets']} -> {after.get('websockets')}) — "
            "the exact #2146 failure shape"
        )
