"""Guard the always-reported CodeQL merge gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "codeql-quality.yml"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_pull_requests_always_emit_codeql_gate() -> None:
    workflow = _workflow()
    pull_request = workflow[True]["pull_request"]
    gate = workflow["jobs"]["code-quality-gate"]

    assert "paths" not in pull_request
    assert gate["name"] == "CodeQL Gate"
    assert gate["needs"] == "code-quality"
    assert gate["if"] == "${{ always() }}"
    assert gate["steps"][0]["run"] == (
        'test "${{ needs.code-quality.result }}" = "success"'
    )
