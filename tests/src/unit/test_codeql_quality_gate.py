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


def test_vendored_paths_are_ignored(tmp_path: Path) -> None:
    path = _write_sarif(
        tmp_path,
        [
            _result("py/empty-except", "tests/initial_test_state/x.py", 1, "vendored"),
            _result("py/empty-except", "src/a.py", 1, "first-party"),
        ],
    )
    findings = gate.load_findings(path)
    assert [f[0] for f in findings] == ["src/a.py"]


def test_syntax_errors_are_not_suppressed(tmp_path: Path) -> None:
    """py/syntax-error is no longer blanket-ignored; any parse failure gates."""
    path = _write_sarif(
        tmp_path,
        [_result("py/syntax-error", "tests/src/e2e/conftest.py", 1, "invalid syntax")],
    )
    findings, suppressed = gate.classify(path)
    assert len(findings) == 1
    assert not suppressed


def test_allowlisted_finding_is_suppressed_not_gated(tmp_path: Path) -> None:
    """An allowlisted false positive is reported as suppressed, not gated."""
    path = _write_sarif(
        tmp_path,
        [
            _result(
                "py/unused-global-variable",
                "src/ha_mcp/__main__.py",
                580,
                "The global variable '_shutdown_in_progress' is not used.",
            ),
            _result("py/empty-except", "src/a.py", 1, "real finding"),
        ],
    )
    findings, suppressed = gate.classify(path)
    assert [f[2] for f in findings] == ["py/empty-except"]
    assert len(suppressed) == 1
    assert suppressed[0][2] == "py/unused-global-variable"
    assert suppressed[0][4]  # a non-empty reason string
    # The gate still fails because a real finding remains.
    assert gate.main(["prog", str(path)]) == 1


def test_allowlist_requires_all_three_of_rule_path_message(tmp_path: Path) -> None:
    """Same rule+path but a different symbol must NOT be suppressed."""
    path = _write_sarif(
        tmp_path,
        [
            _result(
                "py/unused-global-variable",
                "src/ha_mcp/__main__.py",
                99,
                "The global variable 'something_else' is not used.",
            ),
        ],
    )
    findings, suppressed = gate.classify(path)
    assert len(findings) == 1
    assert not suppressed


def test_allowlist_wrong_rule_is_not_suppressed(tmp_path: Path) -> None:
    """Right path + right symbol but a different rule must still gate."""
    path = _write_sarif(
        tmp_path,
        [
            _result(
                "py/empty-except",  # not the allowlisted rule for this symbol
                "src/ha_mcp/__main__.py",
                580,
                "The global variable '_shutdown_in_progress' is not used.",
            ),
        ],
    )
    findings, suppressed = gate.classify(path)
    assert len(findings) == 1
    assert not suppressed


def test_allowlist_wrong_path_is_not_suppressed(tmp_path: Path) -> None:
    """Right rule + right symbol but a different file must still gate."""
    path = _write_sarif(
        tmp_path,
        [
            _result(
                "py/unused-global-variable",
                "src/ha_mcp/tools/somewhere_else.py",
                10,
                "The global variable '_shutdown_in_progress' is not used.",
            ),
        ],
    )
    findings, suppressed = gate.classify(path)
    assert len(findings) == 1
    assert not suppressed


def test_result_without_location_still_gates(tmp_path: Path) -> None:
    """A result with no physical location is kept with a placeholder file/line."""
    sarif = {
        "runs": [
            {
                "tool": {"driver": {"name": "CodeQL"}},
                "results": [{"ruleId": "py/no-loc", "message": {"text": "x"}}],
            }
        ]
    }
    path = tmp_path / "q.sarif"
    path.write_text(json.dumps(sarif), encoding="utf-8")
    findings = gate.load_findings(path)
    assert findings == [("<no-location>", 0, "py/no-loc", "x")]


def test_missing_region_and_message_default_cleanly(tmp_path: Path) -> None:
    """Absent region.startLine and message text fall back to 0 / empty string."""
    sarif = {
        "runs": [
            {
                "tool": {"driver": {"name": "CodeQL"}},
                "results": [
                    {
                        "ruleId": "py/x",
                        "locations": [
                            {"physicalLocation": {"artifactLocation": {"uri": "a.py"}}}
                        ],
                    }
                ],
            }
        ]
    }
    path = tmp_path / "q.sarif"
    path.write_text(json.dumps(sarif), encoding="utf-8")
    findings = gate.load_findings(path)
    assert findings == [("a.py", 0, "py/x", "")]


def test_malformed_sarif_returns_exit_2(tmp_path: Path) -> None:
    path = tmp_path / "bad.sarif"
    path.write_text("{not valid json", encoding="utf-8")
    assert gate.main(["prog", str(path)]) == 2


def test_suppressed_findings_are_reported_to_stdout(tmp_path: Path, capsys) -> None:
    """Allowlisted findings must be printed, never silently dropped."""
    path = _write_sarif(
        tmp_path,
        [
            _result(
                "py/unused-import",
                "packaging/binary/pyinstaller_hooks/runtime_hook.py",
                7,
                "Import of 'idna' is not used.",
            ),
        ],
    )
    assert gate.main(["prog", str(path)]) == 0  # only a suppressed finding
    out = capsys.readouterr().out
    assert "Suppressed (allowlisted false positives): 1" in out
    assert "py/unused-import" in out


def test_github_step_summary_is_written(tmp_path: Path, monkeypatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    path = _write_sarif(tmp_path, [_result("py/x", "src/a.py", 1, "boom")])
    gate.main(["prog", str(path)])
    assert "CodeQL Code Quality" in summary.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
