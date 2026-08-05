"""Unit tests for scripts/check_ha_constraint_alignment.py (#2135/#2146).

The script is the mechanical link between ha-mcp's direct dependencies and
HA core's ``package_constraints.txt``: where HA pins exactly we must admit
the pin, and where HA is loose we must not pin exactly (the forced in-place
replacement that tears packages). These tests pin the two rules offline; the
lockfile CI job runs the script against the live constraints of the HA
version the e2e lanes test.
"""

from __future__ import annotations

import sys
from pathlib import Path

from packaging.requirements import Requirement

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import check_ha_constraint_alignment as checker  # noqa: E402

_CONSTRAINTS_SAMPLE = """
# comment line
--extra-index-url https://example.invalid
websockets>=15.0.1
httpx==0.28.1
PyJWT==2.12.1
grpcio==1.72.1;python_version<'3.14'
not a requirement line
"""


class TestParseRequirementLines:
    def test_parses_names_canonically_and_skips_noise(self):
        constraints = checker.parse_requirement_lines(_CONSTRAINTS_SAMPLE)
        assert set(constraints) == {"websockets", "httpx", "pyjwt", "grpcio"}
        assert str(constraints["websockets"]) == ">=15.0.1"
        assert str(constraints["httpx"]) == "==0.28.1"


class TestCheckAlignment:
    def test_exact_ha_pin_admitted_by_matching_pin_passes(self):
        violations = checker.check_alignment(
            [Requirement("httpx[socks]==0.28.1")],
            checker.parse_requirement_lines("httpx==0.28.1"),
        )
        assert violations == []

    def test_exact_ha_pin_admitted_by_range_passes(self):
        violations = checker.check_alignment(
            [Requirement("cryptography>=48.0.0,<51")],
            checker.parse_requirement_lines("cryptography==48.0.1"),
        )
        assert violations == []

    def test_exact_ha_pin_excluded_by_our_pin_fails(self):
        violations = checker.check_alignment(
            [Requirement("pydantic==2.13.5")],
            checker.parse_requirement_lines("pydantic==2.13.4"),
        )
        assert len(violations) == 1
        assert "pydantic" in violations[0]
        assert "realign" in violations[0]

    def test_loose_ha_constraint_with_our_exact_pin_fails(self):
        # The literal #2146 shape: HA floors websockets, ha-mcp pinned it.
        violations = checker.check_alignment(
            [Requirement("websockets==17.0")],
            checker.parse_requirement_lines("websockets>=15.0.1"),
        )
        assert len(violations) == 1
        assert "websockets" in violations[0]
        assert "torn-install" in violations[0]

    def test_loose_ha_constraint_with_our_range_passes(self):
        violations = checker.check_alignment(
            [Requirement("websockets>=15.0.1,<18")],
            checker.parse_requirement_lines("websockets>=15.0.1"),
        )
        assert violations == []

    def test_dependency_ha_does_not_constrain_is_ignored(self):
        violations = checker.check_alignment(
            [Requirement("fastmcp==3.4.5")],
            checker.parse_requirement_lines("websockets>=15.0.1"),
        )
        assert violations == []


class TestRepoPyprojectIsAligned:
    """The repo's own dependency list passes both rules offline.

    Uses a constraints sample frozen from HA 2026.7.4 for the packages ha-mcp
    actually shares with HA — the live check against the current HA version
    runs in CI (lockfile job), where drift on either side should fail the PR
    that introduces it, not this offline test.
    """

    def test_current_pyproject_has_no_violations(self):
        dependencies = checker.direct_dependencies(
            (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        frozen_2026_7_4 = "\n".join(
            [
                "websockets>=15.0.1",
                "httpx==0.28.1",
                "pydantic==2.13.4",
                "cryptography==48.0.1",
                "packaging>=23.1",
                "typing-extensions>=4.15.0,<5.0",
            ]
        )
        violations = checker.check_alignment(
            dependencies, checker.parse_requirement_lines(frozen_2026_7_4)
        )
        assert violations == []
