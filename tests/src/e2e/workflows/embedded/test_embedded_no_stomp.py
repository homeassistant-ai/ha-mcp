"""The embedded install must never replace a package HA already ships.

Issues #2135/#2146: ha-mcp's runtime install into Home Assistant's own
site-packages is a plain pip run — non-atomic uninstall-then-extract. Any
dependency spec that forces pip to replace an image-shipped package opens a
tear window: interrupt the replacement (restart, watchdog, OOM during HA
startup) and the package is left with files from two releases, which for
``websockets`` kills every WS connect with ImportError until something
rewrites site-packages. The fix is to never force the replacement (deps that
overlap HA's environment are ranges admitting HA's copy, not exact pins).

This test enforces that mechanically, against the real install: the embedded
backend's container entrypoint brackets its wheel preinstall with
``pip list --format=freeze`` snapshots (see ``_build_ha_testcontainer`` in
the e2e conftest). Comparing them proves the install only ADDED packages —
nothing the HA image shipped was upgraded, downgraded, or removed. Because
the snapshots come from the session container every embedded-lane test
already boots, the guard also covers dependencies we don't declare directly
(a transitive floor-bump by fastmcp would trip it the same way).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ...conftest import EMBEDDED_FREEZE_AFTER, EMBEDDED_FREEZE_BEFORE


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
        key = name.strip().lower().replace("_", "-")
        packages[key] = version.strip() if sep else line
    return packages


def test_embedded_preinstall_replaces_nothing(ha_container_with_fresh_config):
    info = ha_container_with_fresh_config
    if info.get("backend") != "embedded":
        pytest.skip(
            "freeze snapshots are written only by the embedded testcontainer "
            "backend's preinstall entrypoint (E2E_BACKEND=embedded)"
        )

    config_path = Path(info["config_path"])
    before_file = config_path / EMBEDDED_FREEZE_BEFORE
    after_file = config_path / EMBEDDED_FREEZE_AFTER
    # The entrypoint chain writes both files before /init; a booted HA (which
    # the session fixture guarantees) means they must exist.
    assert before_file.is_file(), f"missing preinstall snapshot {before_file}"
    assert after_file.is_file(), f"missing postinstall snapshot {after_file}"

    before = _parse_freeze(before_file.read_text(encoding="utf-8"))
    after = _parse_freeze(after_file.read_text(encoding="utf-8"))
    assert before, "empty pip freeze snapshot — entrypoint capture broke"

    replaced = {
        name: (version, after[name])
        for name, version in before.items()
        if name in after and after[name] != version
    }
    removed = sorted(name for name in before if name not in after)

    assert not replaced and not removed, (
        "Installing the ha-mcp wheel into the HA image changed packages the "
        f"image already shipped — replaced: {replaced or 'none'}; removed: "
        f"{removed or 'none'}. A forced in-place replacement of an HA-shipped "
        "package is the #2135/#2146 torn-install window: loosen the "
        "conflicting dependency spec (pyproject.toml) to admit HA's shipped "
        "version instead of pinning past it."
    )

    # websockets is the dependency that bit (#2146); assert it explicitly so a
    # regression names the culprit even if the general diff message changes.
    if "websockets" in before:
        assert after.get("websockets") == before["websockets"], (
            "the embedded install replaced HA's shipped websockets "
            f"({before['websockets']} -> {after.get('websockets')}) — "
            "the exact #2146 failure shape"
        )
