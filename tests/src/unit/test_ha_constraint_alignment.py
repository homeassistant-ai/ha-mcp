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

import pytest
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


class TestFetchFailurePath:
    """The fetch budget the CI job's timeout is sized against.

    pr.yml raised the lockfile job to 5 minutes and treats exit 2 as a
    fail-open warning on the reasoning that a fetch failure resolves FAST
    and distinctly. Restoring a long timeout or a trailing sleep would push
    the worst case past the job budget, turning that clean exit into an
    opaque runner kill — the exact outcome the comments say must not happen.
    """

    def _patch_fetch(self, monkeypatch, *, sleeps, attempts):
        def boom(*_args, **_kwargs):
            attempts.append(1)
            raise checker.urllib.error.URLError("network down")

        monkeypatch.setattr(checker.urllib.request, "urlopen", boom)
        monkeypatch.setattr(checker.time, "sleep", sleeps.append)

    def test_unreachable_constraints_exit_is_the_fail_open_sentinel(self, monkeypatch):
        """Only THIS code may fail open, and it must not be 2.

        pr.yml warns-and-passes on the sentinel alone. Were it 2, argparse's
        usage-error exit and uv's own exit 2 would land in the same branch,
        leaving the gate permanently green while checking nothing.
        """
        sleeps: list[float] = []
        attempts: list[int] = []
        self._patch_fetch(monkeypatch, sleeps=sleeps, attempts=attempts)
        assert (
            checker.main(["--ha-version", "2026.8.0"])
            == checker.EXIT_CONSTRAINTS_UNREACHABLE
        )
        assert checker.EXIT_CONSTRAINTS_UNREACHABLE != 2

    def test_permanent_http_error_fails_hard_instead_of_failing_open(
        self, monkeypatch
    ):
        """A moved constraints file must red-light the PR, not warn.

        A 404 is not an outage — it means HA renamed or moved
        package_constraints.txt, or the version ref does not exist. Retrying
        cannot fix it, and returning the network sentinel would let CI
        swallow it silently on every future PR.
        """
        attempts: list[int] = []

        def gone(*_args, **_kwargs):
            attempts.append(1)
            raise checker.urllib.error.HTTPError(
                "https://example.invalid/c.txt", 404, "Not Found", {}, None
            )

        monkeypatch.setattr(checker.urllib.request, "urlopen", gone)
        monkeypatch.setattr(checker.time, "sleep", lambda _s: None)

        with pytest.raises(SystemExit) as exc:
            checker.main(["--ha-version", "2026.8.0"])

        assert exc.value.code == 1
        assert len(attempts) == 1, "a permanent 4xx must not be retried"

    def test_exactly_three_attempts_with_no_trailing_sleep(self, monkeypatch):
        sleeps: list[float] = []
        attempts: list[int] = []
        self._patch_fetch(monkeypatch, sleeps=sleeps, attempts=attempts)
        checker.main(["--ha-version", "2026.8.0"])
        assert len(attempts) == checker._FETCH_ATTEMPTS == 3
        assert len(sleeps) == checker._FETCH_ATTEMPTS - 1, (
            "a sleep after the final attempt is pure waste and pushes the "
            "worst case toward the job timeout"
        )

    def test_worst_case_fits_well_inside_the_job_timeout(self):
        budget = checker._FETCH_ATTEMPTS * checker._FETCH_TIMEOUT_SECONDS + sum(
            checker._FETCH_BACKOFF_SECONDS * (n + 1)
            for n in range(checker._FETCH_ATTEMPTS - 1)
        )
        assert budget <= 60, (
            f"worst-case fetch is {budget}s; the lockfile job also checks the "
            "lockfile and resolves packages, so keep this well under its "
            "timeout-minutes or exit 2 becomes an opaque kill"
        )

    def test_unreadable_local_file_exits_two(self, tmp_path):
        missing = tmp_path / "nope.txt"
        assert checker.main(["--constraints-file", str(missing)]) == 2


class TestMarkerSkipIsAnnounced:
    def test_skipped_marker_clause_is_reported_on_stderr(self, capsys):
        checker.parse_requirement_lines("fakelib==1.0;python_version<'3.0'\n")
        assert "fakelib" in capsys.readouterr().err
