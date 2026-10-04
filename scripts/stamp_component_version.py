"""Stamp the HACS component's version and the ha-mcp build it pins.

A stable release needs no stamping: semantic-release writes the release version
into ``manifest.json`` (``version`` and the ``ha-mcp==`` requirement) and
``const.py`` in the release commit. A development pre-release is cut from a
master snapshot, so the mirror sync stamps the snapshot with:

- ``--version``: the plain next-release number (``9.0.0``). Released servers
  parse the component version segment by segment as integers, so a component
  must never report a dev suffix; the suffix lives in the pre-release tag.
- ``--pin``: the development build this pre-release runs (``9.0.0.dev2901``).
- ``--dist``: the distribution that build is published as. Publish Dev Channel
  uploads development builds as ``ha-mcp-dev``, so a pre-release pins
  ``ha-mcp-dev==9.0.0.dev2901``; a stable release keeps ``ha-mcp``.

Usage::

    python scripts/stamp_component_version.py \\
        --component-dir /tmp/mirror/custom_components/ha_mcp_tools \\
        --version 9.0.0 --pin 9.0.0.dev2901 --dist ha-mcp-dev
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SERVER_DISTS = ("ha-mcp", "ha-mcp-dev")
_PLAIN_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_PIN_VERSION = re.compile(r"^\d+\.\d+\.\d+(\.dev\d+)?$")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_COMPONENT_VERSION = re.compile(r'^COMPONENT_VERSION = "[^"]*"$', re.MULTILINE)


def _is_server_requirement(requirement: str) -> bool:
    match = _REQUIREMENT_NAME.match(requirement)
    return bool(match) and match.group(1).lower().replace("_", "-") in SERVER_DISTS


def stamp(component_dir: Path, version: str, pin: str, dist: str = "ha-mcp") -> None:
    """Rewrite ``manifest.json`` and ``const.py`` under ``component_dir``."""
    if not _PLAIN_VERSION.match(version):
        raise ValueError(f"component version must be X.Y.Z, got {version!r}")
    if not _PIN_VERSION.match(pin):
        raise ValueError(f"pin must be X.Y.Z or X.Y.Z.devN, got {pin!r}")
    if dist not in SERVER_DISTS:
        raise ValueError(f"dist must be one of {SERVER_DISTS}, got {dist!r}")

    manifest_path = component_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    requirements = list(manifest.get("requirements", []))
    pinned = [r for r in requirements if _is_server_requirement(r)]
    if len(pinned) != 1:
        raise ValueError(
            f"expected exactly one ha-mcp requirement in "
            f"{manifest_path}, found {pinned}"
        )
    manifest["requirements"] = [
        f"{dist}=={pin}" if _is_server_requirement(r) else r for r in requirements
    ]
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=4) + "\n", encoding="utf-8")

    const_path = component_dir / "const.py"
    source = const_path.read_text(encoding="utf-8")
    stamped, count = _COMPONENT_VERSION.subn(f'COMPONENT_VERSION = "{version}"', source)
    if count != 1:
        raise ValueError(f"expected one COMPONENT_VERSION in {const_path}")
    const_path.write_text(stamped, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--component-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--pin", required=True)
    parser.add_argument("--dist", default="ha-mcp", choices=SERVER_DISTS)
    args = parser.parse_args(argv)
    try:
        stamp(args.component_dir, args.version, args.pin, args.dist)
    except ValueError as err:
        print(f"stamp_component_version: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
