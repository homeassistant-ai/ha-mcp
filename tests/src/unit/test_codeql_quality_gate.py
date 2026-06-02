"""Unit tests for scripts/codeql_quality_gate.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "codeql_quality_gate.py"
_spec = importlib.util.spec_from_file_location("codeql_quality_gate", _SCRIPT)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _write_sarif(tmp_path: Path, results: list[dict]) -> Path:
    sarif = {
        "runs": [
            {
                "tool": {"driver": {"name": "CodeQL", "rules": [{"id": "py/rule-0"}]}},
                "results": results,
            }
        ]
    }
    path = tmp_path / "quality.sarif"
    path.write_text(json.dumps(sarif), encoding="utf-8")
    return path


def _result(rule_id: str | None, uri: str, line: int, text: str) -> dict:
    result: dict = {
        "message": {"text": text},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": uri},
                    "region": {"startLine": line},
                }
            }
        ],
    }
    if rule_id is None:
        result["rule"] = {"index": 0}
    else:
        result["ruleId"] = rule_id
    return result


def test_load_findings_sorted_and_parsed(tmp_path: Path) -> None:
    path = _write_sarif(
        tmp_path,
        [
            _result("py/empty-except", "src/z.py", 5, "Empty except"),
            _result("py/unused", "src/a.py", 10, "Unused a"),
            _result("py/unused", "src/a.py", 2, "Unused a earlier"),
        ],
    )
    findings = gate.load_findings(path)
    # Sorted by (rule, file, line): empty-except first, then unused by line.
    assert findings == [
        ("src/z.py", 5, "py/empty-except", "Empty except"),
        ("src/a.py", 2, "py/unused", "Unused a earlier"),
        ("src/a.py", 10, "py/unused", "Unused a"),
    ]


def test_rule_id_resolved_from_rules_table(tmp_path: Path) -> None:
    """A result without ruleId falls back to the driver rules table by index."""
    path = _write_sarif(tmp_path, [_result(None, "src/a.py", 1, "x")])
    findings = gate.load_findings(path)
    assert findings[0][2] == "py/rule-0"


def test_main_exit_codes(tmp_path: Path) -> None:
    with_findings = _write_sarif(tmp_path, [_result("py/x", "src/a.py", 1, "x")])
    assert gate.main(["prog", str(with_findings)]) == 1

    empty = _write_sarif(tmp_path, [])
    assert gate.main(["prog", str(empty)]) == 0


def test_main_missing_file_returns_2(tmp_path: Path) -> None:
    assert gate.main(["prog", str(tmp_path / "nope.sarif")]) == 2


def test_render_groups_and_counts(tmp_path: Path) -> None:
    path = _write_sarif(
        tmp_path,
        [
            _result("py/x", "src/a.py", 1, "first"),
            _result("py/x", "src/b.py", 2, "second"),
            _result("py/y", "src/c.py", 3, "third"),
        ],
    )
    report = gate.render(gate.load_findings(path))
    assert "CodeQL code-quality findings: 3" in report
    assert "2  py/x" in report
    assert "## py/y (1)" in report


def test_render_empty() -> None:
    assert "No CodeQL code-quality findings" in gate.render([])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
