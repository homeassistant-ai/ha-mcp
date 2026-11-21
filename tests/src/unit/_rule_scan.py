"""Run one ast-grep rule over the repository or over a sample test file."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
RULES = REPO / "rules"


def _ast_grep() -> str:
    found = shutil.which(
        "ast-grep", path=str(Path(sys.executable).parent)
    ) or shutil.which("ast-grep")
    assert found, "ast-grep-cli (dev group) is not installed in this environment"
    return found


def scan(rule_file: Path, cwd: Path, target: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        [_ast_grep(), "scan", "--rule", str(rule_file), "--json=compact", target],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout.strip(), proc.stderr
    rows: list[dict[str, Any]] = json.loads(proc.stdout)
    return rows


def flagged(rule: str, source: str, tmp_path: Path) -> bool:
    """Whether ``rule`` reports ``source`` written as a new unit test file."""
    sample = tmp_path / "tests" / "src" / "unit" / "test_sample.py"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(source, encoding="utf-8")
    rows = scan(RULES / f"{rule}.yml", tmp_path, "tests/src/unit/test_sample.py")
    return any(row["ruleId"] == rule for row in rows)
