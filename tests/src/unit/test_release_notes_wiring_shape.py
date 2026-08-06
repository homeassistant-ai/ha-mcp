"""Pin the release workflows to the release-note extractor they call.

``semver-release.yml`` and ``hotfix-release.yml`` cannot run in PR CI, so
nothing else checks that the three filenames in each release step still agree:
the script's ``--out``, the ``[ ! -s ... ]`` emptiness guard, and ``gh release
create --notes-file``. A missing script is loud (the step runs under ``bash
-e``), but a drifting ``--out`` name is silent — the guard finds no file, the
fallback fires, and the release ships ``Release vX.Y.Z`` as its entire body on
a green run, for that release and every one after it. These tests catch that
before merge, the only coverage a workflow that cannot run pre-merge can have.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = "scripts/extract_release_notes.py"
_WORKFLOWS = ["semver-release.yml", "hotfix-release.yml"]


def _workflow(name: str) -> str:
    return (_REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_the_extractor_the_workflows_call_exists() -> None:
    assert (_REPO_ROOT / _SCRIPT).is_file(), (
        f"{_SCRIPT} is gone, but the release workflows still invoke it"
    )


@pytest.mark.parametrize("workflow", _WORKFLOWS)
def test_the_release_step_calls_the_extractor(workflow: str) -> None:
    assert re.search(rf"python3\s+{re.escape(_SCRIPT)}\s", _workflow(workflow)), (
        f"{workflow} no longer calls {_SCRIPT}"
    )


@pytest.mark.parametrize("workflow", _WORKFLOWS)
def test_out_guard_and_notes_file_all_name_the_same_file(workflow: str) -> None:
    text = _workflow(workflow)
    out = re.search(r"extract_release_notes\.py[^\n]*--out\s+(\S+)", text)
    assert out, f"{workflow} does not pass --out to the extractor"
    notes = out.group(1)

    assert re.search(rf"\[\s*!\s*-s\s+{re.escape(notes)}\s*\]", text), (
        f"{workflow} writes notes to {notes} but its emptiness guard checks something else"
    )
    assert re.search(rf"--notes-file\s+{re.escape(notes)}\b", text), (
        f"{workflow} writes notes to {notes} but publishes something else"
    )


@pytest.mark.parametrize("workflow", _WORKFLOWS)
def test_the_empty_extract_fallback_warns(workflow: str) -> None:
    """Without the warning, a degraded release reads exactly like a healthy one."""
    assert "::warning::no changelog section extracted" in _workflow(workflow), (
        f"{workflow} falls back to generic notes without saying so in the run log"
    )
