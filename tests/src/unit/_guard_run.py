"""Running one of the repository's check scripts on a checkout a test built."""

from pathlib import Path
from types import ModuleType

import pytest


def run_guard(
    guard: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    """The script's exit status and what it printed, run on ``repo``."""
    status = guard.main(["--repo", str(repo)])
    return status, capsys.readouterr().out
