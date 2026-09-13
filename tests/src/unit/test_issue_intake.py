"""Run the dependency-free intake behavior suite in the normal unit-test lane."""

import shutil
import subprocess
from pathlib import Path


def test_issue_intake_behavior() -> None:
    root = Path(__file__).resolve().parents[3]
    node = shutil.which("node")
    assert node, "Node is required for issue-intake workflow regression tests"
    completed = subprocess.run(
        [node, "--test", str(root / "tests/js/issue-intake.test.mjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
