"""CLI tests for run_story.py story selection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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
