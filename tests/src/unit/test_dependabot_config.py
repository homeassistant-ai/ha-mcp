"""Guard the split between Dependabot and Renovate over the vendored pins."""

from __future__ import annotations

import json
import posixpath
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _renovate_pypi_manifest_dirs() -> set[str]:
    renovate = json.loads((_REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    dirs = set()
    for manager in renovate["customManagers"]:
        if manager.get("datasourceTemplate") != "pypi":
            continue
        for pattern in manager["managerFilePatterns"]:
            path = pattern.removeprefix("/^").removesuffix("$/").replace("\\.", ".")
            dirs.add("/" + posixpath.dirname(path))
    return dirs


def test_dependabot_never_opens_pin_only_prs_for_renovate_vendored_trees() -> None:
    # Renovate regenerates a vendored tree in the same commit as its pin. A
    # Dependabot security PR for the same pin bumps the pin alone, fails the
    # vendored-tree tests and duplicates Renovate's own security PR.
    config = (_REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    updates = yaml.safe_load(config)["updates"]
    vendored_dirs = _renovate_pypi_manifest_dirs()

    assert vendored_dirs
    for directory in vendored_dirs:
        entries = [
            u
            for u in updates
            if u["package-ecosystem"] == "pip" and u.get("directory") == directory
        ]
        assert entries, f"no Dependabot pip entry ignores {directory}"
        assert all({"dependency-name": "*"} in e.get("ignore", []) for e in entries)
