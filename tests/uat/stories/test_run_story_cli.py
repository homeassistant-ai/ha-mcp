"""CLI tests for run_story.py story selection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

RUN_STORY = Path(__file__).resolve().parent / "run_story.py"
CATALOG = Path(__file__).resolve().parent / "catalog"


def test_several_story_paths_run_in_one_invocation():
    """Passing several story files runs them all in one invocation, so a BAT
    comparison shares one HA container instead of starting one per story."""
    stories = sorted(CATALOG.glob("t0[12]_*.yaml"))
    assert len(stories) == 2
    result = subprocess.run(
        [sys.executable, str(RUN_STORY), *map(str, stories), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "# t01:" in result.stdout
    assert "# t02:" in result.stdout


def test_arguments_after_double_dash_reach_run_uat(monkeypatch):
    """Arguments after ``--`` are passed to run_uat.py, not read as story
    paths (e.g. ``story.yaml -- --timeout 60``)."""
    from uat.stories import run_story

    story = next(CATALOG.glob("t01_*.yaml"))
    captured = {}

    async def _fake_run_stories(args, filtered):
        captured["args"] = args
        captured["stories"] = [path for path, _ in filtered]
        return 0

    monkeypatch.setattr(run_story, "run_stories", _fake_run_stories)
    monkeypatch.setattr(
        sys, "argv", ["run_story.py", str(story), "--", "--timeout", "60"]
    )
    with pytest.raises(SystemExit) as exc:
        run_story.main()

    assert exc.value.code == 0
    assert captured["stories"] == [story.resolve()]
    assert captured["args"].extra_args == ["--timeout", "60"]
