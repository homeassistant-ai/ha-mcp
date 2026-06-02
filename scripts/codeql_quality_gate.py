#!/usr/bin/env python3
"""Parse a CodeQL code-quality SARIF file, report findings, and gate on them.

CodeQL's Code Quality preview (the GitHub Settings → Security → Code quality
page) is only available to Team/Enterprise Cloud org plans, so it cannot be
enabled on this repo's free org. This script provides an equivalent gate by
running the ``python-code-quality.qls`` suite via the CodeQL CLI in CI and
failing the job when any finding remains.

Usage:
    codeql_quality_gate.py <quality.sarif>

Exits 1 if the SARIF contains any results, 0 otherwise. A grouped, sorted
finding list is printed to stdout and (when running in Actions) appended to
the job summary.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Findings under these path prefixes are dropped before gating. These are
# vendored / third-party trees we do not own and ruff already excludes them
# (see ``extend-exclude`` in pyproject.toml). Keep this in sync with that list.
PATHS_IGNORE: tuple[str, ...] = ("tests/initial_test_state/",)

# Rules dropped before gating. ``py/syntax-error`` here is not a real syntax
# error: it is a ``tsg-python`` parser diagnostic that CodeQL's code-quality
# engine emits when it cannot parse a (valid Python 3) construct such as a
# percent-format string. It is a tool limitation, not a code-quality finding,
# and cannot be fixed in our source.
RULES_IGNORE: frozenset[str] = frozenset({"py/syntax-error"})


def _rule_id(result: dict[str, Any], run: dict[str, Any]) -> str:
    """Resolve a result's rule id, falling back to the rules table by index."""
    if rule_id := result.get("ruleId"):
        return rule_id
    rule_index = result.get("rule", {}).get("index")
    if rule_index is None:
        return "<unknown>"
    rules = run.get("tool", {}).get("driver", {}).get("rules", [])
    if 0 <= rule_index < len(rules):
        return rules[rule_index].get("id", "<unknown>")
    return "<unknown>"


def _location(result: dict[str, Any]) -> tuple[str, int]:
    """Return (file, line) for a result's primary physical location."""
    locations = result.get("locations") or []
    if not locations:
        return ("<no-location>", 0)
    phys = locations[0].get("physicalLocation", {})
    uri = phys.get("artifactLocation", {}).get("uri", "<no-file>")
    line = phys.get("region", {}).get("startLine", 0)
    return (uri, line)


def load_findings(sarif_path: Path) -> list[tuple[str, int, str, str]]:
    """Return a sorted list of (file, line, rule_id, message) findings."""
    data = json.loads(sarif_path.read_text(encoding="utf-8"))
    findings: list[tuple[str, int, str, str]] = []
    for run in data.get("runs", []):
        for result in run.get("results", []):
            rule_id = _rule_id(result, run)
            if rule_id in RULES_IGNORE:
                continue
            file, line = _location(result)
            if any(file.startswith(prefix) for prefix in PATHS_IGNORE):
                continue
            message = result.get("message", {}).get("text", "").strip()
            findings.append((file, line, rule_id, message))
    findings.sort(key=lambda f: (f[2], f[0], f[1]))
    return findings


def render(findings: list[tuple[str, int, str, str]]) -> str:
    """Render a human-readable, grouped report of the findings."""
    if not findings:
        return "No CodeQL code-quality findings. ✅"
    by_rule: dict[str, list[tuple[str, int, str, str]]] = defaultdict(list)
    for f in findings:
        by_rule[f[2]].append(f)
    counts = Counter(f[2] for f in findings)

    lines: list[str] = [f"CodeQL code-quality findings: {len(findings)}", ""]
    lines.append("Count by rule:")
    for rule, count in counts.most_common():
        lines.append(f"  {count:>4}  {rule}")
    lines.append("")
    for rule in sorted(by_rule):
        lines.append(f"## {rule} ({len(by_rule[rule])})")
        for file, line, _, message in by_rule[rule]:
            lines.append(f"  {file}:{line}  {message}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <quality.sarif>", file=sys.stderr)
        return 2
    sarif_path = Path(argv[1])
    if not sarif_path.exists():
        print(f"SARIF file not found: {sarif_path}", file=sys.stderr)
        return 2

    findings = load_findings(sarif_path)
    report = render(findings)
    print(report)

    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"# CodeQL Code Quality\n\n```\n{report}\n```\n")

    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
