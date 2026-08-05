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

    Uses a constraints sample frozen from the HA release the suite pins
    (HA_TEST_IMAGE / HA_IMAGE_GHCR), covering the packages ha-mcp actually
    shares with HA — the live check against that same version runs in CI
    (lockfile job), where drift on either side should fail the PR that
    introduces it, not this offline test.
    """

    def test_current_pyproject_has_no_violations(self):
        dependencies = checker.direct_dependencies(
            (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        # Verbatim from homeassistant/package_constraints.txt @ 2026.8.0.
        frozen_2026_8_0 = "\n".join(
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
            dependencies, checker.parse_requirement_lines(frozen_2026_8_0)
        )
        assert violations == []


class TestMarkerAndOperatorHandling:
    def test_false_marker_clause_is_excluded(self):
        constraints = checker.parse_requirement_lines(
            "fakelib==1.0;python_version<'3.0'\nreallib==2.0\n"
        )
        assert "fakelib" not in constraints
        assert str(constraints["reallib"]) == "==2.0"

    def test_true_marker_clause_is_kept(self):
        # python_version >= 3.11 is true on every interpreter this repo runs.
        constraints = checker.parse_requirement_lines(
            "grpcio==1.72.1;python_version>='3.11'"
        )
        assert str(constraints["grpcio"]) == "==1.72.1"

    def test_duplicate_applicable_entries_combine(self):
        constraints = checker.parse_requirement_lines("multidict>=6.0\nmultidict<7.0\n")
        combined = constraints["multidict"]
        assert combined.contains("6.5")
        assert not combined.contains("7.1")

    def test_arbitrary_equality_pin_counts_as_exact(self):
        # PEP 440 ``===`` must not evade rule 2's exact-pin detection.
        violations = checker.check_alignment(
            [Requirement("websockets===17.0")],
            checker.parse_requirement_lines("websockets>=15.0.1"),
        )
        assert len(violations) == 1


class TestMainExitCodes:
    """The lockfile CI job invokes main(); its exit codes are the contract."""

    def test_clean_alignment_exits_zero(self, tmp_path):
        constraints = tmp_path / "cons.txt"
        constraints.write_text("websockets>=15.0.1\nhttpx==0.28.1\n", encoding="utf-8")
        assert checker.main(["--constraints-file", str(constraints)]) == 0

    def test_violation_exits_one(self, tmp_path):
        # HA pinning fastmcp to a version our exact pin excludes -> rule 1.
        constraints = tmp_path / "cons.txt"
        constraints.write_text("fastmcp==0.0.1\n", encoding="utf-8")
        assert checker.main(["--constraints-file", str(constraints)]) == 1

    def test_unparseable_constraints_exit_two(self, tmp_path):
        constraints = tmp_path / "cons.txt"
        constraints.write_text("# nothing but comments\n", encoding="utf-8")
        assert checker.main(["--constraints-file", str(constraints)]) == 2
