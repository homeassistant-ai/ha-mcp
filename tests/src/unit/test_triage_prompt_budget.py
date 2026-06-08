"""Guards the issue-triage prompt-budgeting algorithm.

The triage workflow (``.github/workflows/issue-triage.yml``) budgets the
assembled LLM prompt under the GitHub Models 8000-token input cap (issue
#1514). That step runs in a checkout-less workflow, so its inline JS can't be
imported; ``scripts/verify_triage_prompt_budget.mjs`` deliberately mirrors the
trim logic. This module runs that harness in CI and asserts the budget
constants in the two files have not drifted apart — drift is the precise risk a
mirrored copy introduces.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS = _REPO_ROOT / "scripts" / "verify_triage_prompt_budget.mjs"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "issue-triage.yml"

# Budget knobs that must stay identical in both files.
_CONSTANTS = ("TOKEN_BUDGET", "BODY_FLOOR", "CHANGELOG_FLOOR", "AUTHOR_FLOOR")

# The priority order sections are trimmed in. A reorder in one file but not the
# other would change behaviour silently, so assert both match this sequence.
_CANONICAL_TRIM_ORDER = ["duplicate", "changelog", "author", "body"]
_WORKFLOW_TRIM_MARKERS = {
    "duplicate": r"duplicateSection = ''",
    "changelog": r"changelog = trimTo\(",
    "author": r"authorSection = trimTo\(",
    "body": r"issueBody = trimTo\(",
}
_HARNESS_TRIM_MARKERS = {
    "duplicate": r'dup = ""',
    "changelog": r"log = trimTo\(",
    "author": r"author = trimTo\(",
    "body": r"body = trimTo\(",
}


def _node_binary() -> str:
    return os.environ.get("NODE_BINARY", "node")


def _extract_constants(text: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for name in _CONSTANTS:
        match = re.search(rf"\b{name}\s*=\s*(\d+)", text)
        if match:
            found[name] = int(match.group(1))
    return found


@pytest.mark.skipif(shutil.which(_node_binary()) is None, reason="node not available")
def test_budget_harness_passes() -> None:
    """The mirrored budget algorithm keeps worst-case prompts under the cap."""
    result = subprocess.run(
        [_node_binary(), str(_HARNESS)],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"verify_triage_prompt_budget.mjs failed:\n{result.stdout}\n{result.stderr}"
    )


def test_budget_constants_in_sync() -> None:
    """Fail if the budget constants drift between workflow and harness."""
    workflow_consts = _extract_constants(_WORKFLOW.read_text())
    harness_consts = _extract_constants(_HARNESS.read_text())
    for name in _CONSTANTS:
        assert name in workflow_consts, f"{name} missing from {_WORKFLOW.name}"
        assert name in harness_consts, f"{name} missing from {_HARNESS.name}"
        assert workflow_consts[name] == harness_consts[name], (
            f"{name} drifted: workflow={workflow_consts[name]} "
            f"harness={harness_consts[name]}"
        )


def _trim_order(text: str, markers: dict[str, str]) -> list[str]:
    positions: list[tuple[int, str]] = []
    for kind, pattern in markers.items():
        match = re.search(pattern, text)
        assert match, f"trim marker for {kind!r} not found"
        positions.append((match.start(), kind))
    return [kind for _, kind in sorted(positions)]


def test_trim_order_in_sync() -> None:
    """Fail if the section trim/drop order drifts between the two files."""
    workflow_order = _trim_order(_WORKFLOW.read_text(), _WORKFLOW_TRIM_MARKERS)
    harness_order = _trim_order(_HARNESS.read_text(), _HARNESS_TRIM_MARKERS)
    assert workflow_order == _CANONICAL_TRIM_ORDER, workflow_order
    assert harness_order == _CANONICAL_TRIM_ORDER, harness_order
