"""Rewrite the Core integration pins the dev group and Dependabot hold.

The unit tests run the Core integrations in ``INTEGRATIONS``, so the dev group
installs their manifest requirements at Core's pins and Dependabot ignores
them. Renovate runs this after it bumps the dev group's Core and before
``uv lock``: it reads that Core version's manifests from GitHub and rewrites
the marked block in ``pyproject.toml`` and in ``.github/dependabot.yml``.
Everything between the markers is replaced.

Usage (after editing ``INTEGRATIONS``):
    python scripts/sync_dev_integration_pins.py
    uv lock
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_ha_constraint_alignment as checker

_REPO_ROOT = Path(__file__).resolve().parents[1]
_START = "# core-integration-pins:start"
_END = "# core-integration-pins:end"
INTEGRATIONS = (
    "otbr",
    "thread",
    "zwave_js",
    "matter",
    "zha",
    "usb",
    "hassio",
    "homeassistant_hardware",
    "infrared",
    "stream",
    "camera",
    "knx",
    "local_calendar",
    "remote_calendar",
    "conversation",
)


def manifest_requirements(manifests: Iterable[str]) -> dict[str, str]:
    """Map each requirement's canonical name to its pin, sorted by name.

    A library several manifests list gets one entry with their extras
    combined; two different pins of it are refused.
    """
    merged: dict[str, Requirement] = {}
    for manifest in manifests:
        for text in json.loads(manifest).get("requirements", []):
            requirement = Requirement(text)
            name = canonicalize_name(requirement.name)
            known = merged.setdefault(name, requirement)
            if (known.specifier, known.marker) != (
                requirement.specifier,
                requirement.marker,
            ):
                raise SystemExit(
                    f"ERROR: Core's manifests pin {name} two ways: "
                    f"'{known}' and '{requirement}'"
                )
            known.extras |= requirement.extras
    return {name: str(merged[name]) for name in sorted(merged)}


def _marker(lines: list[str], marker: str) -> int:
    for index, line in enumerate(lines):
        if line.strip() == marker:
            return index
    raise SystemExit(f"ERROR: no '{marker}' line")


def replace_block(text: str, entries: Iterable[str]) -> str:
    """Put ``entries`` between the marker lines, at the start marker's indent."""
    lines = text.splitlines(keepends=True)
    start, end = _marker(lines, _START), _marker(lines, _END)
    if end < start:
        raise SystemExit(f"ERROR: '{_END}' comes before '{_START}'")
    indent = lines[start][: -len(lines[start].lstrip())]
    lines[start + 1 : end] = [f"{indent}{entry}\n" for entry in entries]
    return "".join(lines)


def _fetch_manifests(version: str) -> list[str] | None:
    manifests: list[str] = []
    for domain in INTEGRATIONS:
        try:
            manifest = checker.fetch_core_file(
                version, f"homeassistant/components/{domain}/manifest.json"
            )
        except SystemExit:
            print(
                f"ERROR: Core {version} has no '{domain}' manifest; update "
                f"INTEGRATIONS in scripts/{Path(__file__).name}",
                file=sys.stderr,
            )
            raise
        if manifest is None:
            return None
        manifests.append(manifest)
    return manifests


def main(root: Path = _REPO_ROOT) -> int:
    pyproject = root / "pyproject.toml"
    dependabot = root / ".github" / "dependabot.yml"
    pyproject_text = pyproject.read_text(encoding="utf-8")
    manifests = _fetch_manifests(checker.dev_group_core_version(pyproject_text))
    if manifests is None:
        return 1
    requirements = manifest_requirements(manifests)
    updated = {
        pyproject: replace_block(
            pyproject_text, (f"{json.dumps(pin)}," for pin in requirements.values())
        ),
        dependabot: replace_block(
            dependabot.read_text(encoding="utf-8"),
            (f"- dependency-name: {json.dumps(name)}" for name in requirements),
        ),
    }
    for path, text in updated.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
