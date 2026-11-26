"""Pin the no-tools lane env (#2292) — the workflow half and the parser half.

A dropped env var here degenerates a no-tools lane into a green duplicate of
its ordinary sibling: `E2E_NO_TOOLS_ENTRY` is what strips the component's
"File & YAML Tools" config entry, and the backend selector next to it is what
decides WHICH no-tools topology the lane runs. Every downstream guard keys off
that one variable — the staging in `e2e/conftest.py`, the
`requires_tools_entry` / `no_tools_only` markers, the per-topology skip
ceilings, and the lane-aware component assertions. Lose it and the lane still
passes, having re-tested the ordinary topology.

The parser tests below cover the other half: `tools_entry_absent()` refuses to
guess. An unrecognized spelling raises instead of reading as "off", for the
same reason — a silent false is indistinguishable from a lane that was never
asked to drop the entry.

`topology.py` is loaded by file path: its imports are stdlib-only, so this
collects without importing the e2e package and its Docker-shaped conftest.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest_plugins = ["pytester"]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_TOPOLOGY_PATH = (
    Path(__file__).resolve().parents[1] / "e2e" / "utilities" / "topology.py"
)


def _load_topology():
    spec = importlib.util.spec_from_file_location("e2e_topology", _TOPOLOGY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


topology = _load_topology()


# --------------------------------------------------------------------------
# topology.py: the env parser both halves of the suite share
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_lane_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every parser test from a lane-less environment."""
    for name in (
        "E2E_NO_TOOLS_ENTRY",
        "E2E_COMBINE_TOPOLOGIES",
        "E2E_BACKEND",
        "HAOS_TEST_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_unset_env_is_an_ordinary_component_present_lane() -> None:
    assert topology.tools_entry_absent() is False
    assert topology.component_surface_available() is True


@pytest.mark.parametrize("value", ["1", " TRUE ", "yes", "on"])
def test_recognized_true_spellings_select_the_no_tools_lane(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", value)
    assert topology.tools_entry_absent() is True


@pytest.mark.parametrize("value", ["", "0", "false", " Off "])
def test_recognized_false_spellings_stay_on_the_ordinary_lane(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", value)
    assert topology.tools_entry_absent() is False
    assert topology.component_surface_available() is True


@pytest.mark.parametrize("value", ["01", "ja", "yes please", "TRUE!"])
def test_unrecognized_values_raise_instead_of_guessing_off(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Guessing "off" would silently re-run the ordinary topology (#2292)."""
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", value)
    with pytest.raises(RuntimeError, match="E2E_NO_TOOLS_ENTRY"):
        topology.tools_entry_absent()


@pytest.mark.parametrize("value", ["01", "ja", "TRUE!"])
def test_unrecognized_combine_values_raise_instead_of_guessing_off(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Reading a typo as "off" would drop a lane's absent phase silently."""
    monkeypatch.setenv("E2E_COMBINE_TOPOLOGIES", value)
    with pytest.raises(RuntimeError, match="E2E_COMBINE_TOPOLOGIES"):
        topology.combined_topologies()


def test_no_tools_without_a_backend_has_no_component_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain container / HAOS inaddon: no active component entry at all."""
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", "1")
    assert topology.component_surface_available() is False


@pytest.mark.parametrize("value", ["embedded", " Embedded "])
def test_no_tools_embedded_container_keeps_the_component_surface(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The in-process server entry still registers ``ha_mcp_tools/*`` (#2291)."""
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", "1")
    monkeypatch.setenv("E2E_BACKEND", value)
    assert topology.component_surface_available() is True


@pytest.mark.parametrize("value", ["embedded", " Embedded "])
def test_no_tools_haos_embedded_keeps_the_component_surface(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", "1")
    monkeypatch.setenv("HAOS_TEST_MODE", value)
    assert topology.component_surface_available() is True


def test_no_tools_haos_inaddon_has_no_component_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", "1")
    monkeypatch.setenv("HAOS_TEST_MODE", "inaddon")
    assert topology.component_surface_available() is False


@pytest.mark.parametrize(
    ("lane_env", "gated_runs"),
    [
        pytest.param({}, True, id="ordinary"),
        pytest.param({"E2E_NO_TOOLS_ENTRY": "1"}, False, id="no-component"),
        pytest.param(
            {"E2E_NO_TOOLS_ENTRY": "1", "E2E_BACKEND": "embedded"},
            True,
            id="embedded-server-only",
        ),
    ],
)
def test_requires_component_surface_skips_only_without_the_surface(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    lane_env: dict[str, str],
    gated_runs: bool,
) -> None:
    """The E2E suite's collection hook skips a gated test exactly on the lanes
    without the surface; an inverted condition would skip it on every lane that
    has it, and an ungated test runs everywhere."""
    for name, value in lane_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("HAOS_TEST_IMAGE_PATH", raising=False)
    pytester.makeconftest(
        "from tests.src.e2e._conftest_collection import pytest_collection_modifyitems"
    )
    pytester.makeini(
        "[pytest]\nmarkers =\n    requires_component_surface: needs the surface\n"
    )
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.requires_component_surface
        def test_gated():
            pass

        def test_ordinary():
            pass
        """
    )

    # Only pytest and this conftest: the plugins installed for the unit tests
    # (Core's among them) are not the E2E suite's.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    result = pytester.runpytest("-p", "no:cacheprovider")

    result.assert_outcomes(
        passed=2 if gated_runs else 1, skipped=0 if gated_runs else 1
    )


# --------------------------------------------------------------------------
# The workflow half: which lane carries which env
# --------------------------------------------------------------------------

_PR = "pr.yml"
_E2E = "e2e-tests.yml"
_HAOS = "haos-e2e-tests.yml"
_STDIO = "haos-e2e-stdio.yml"
_BETA = "haos-e2e-beta-tests.yml"
_CBETA = "e2e-beta-tests.yml"

# (workflow, job) -> the backend selectors that step's env must carry, exactly.
# An absent selector is as load-bearing as a present one: the two no-component
# lanes must NOT name a backend, or they run the embedded topology instead of
# the plain-container one they exist to cover.
_NO_TOOLS_LANES: dict[tuple[str, str], dict[str, str]] = {
    (_E2E, "e2e-tests-no-component"): {},
    (_E2E, "e2e-tests-embedded-server-only"): {"E2E_BACKEND": "embedded"},
    (_CBETA, "e2e-tests-no-component"): {},
    (_CBETA, "e2e-tests-embedded-server-only"): {"E2E_BACKEND": "embedded"},
}

# Every component-present lane: the siblings each no-tools lane is paired
# against, plus the HAOS beta lanes, which have no no-tools counterpart but
# are component-present all the same. (The container beta workflow mirrors
# e2e-tests.yml job for job, so its no-tools lanes are paired the same way.) If one of these ever grows
# E2E_NO_TOOLS_ENTRY, a pair stops being a comparison and a beta lane stops
# covering the topology it was built to run on beta images.
_ORDINARY_LANES: tuple[tuple[str, str], ...] = (
    (_PR, "e2e-validation"),
    (_PR, "e2e-validation-embedded"),
    (_PR, "e2e-validation-update-path"),
    (_E2E, "e2e-tests"),
    (_E2E, "e2e-tests-embedded"),
    (_E2E, "e2e-tests-update-path"),
    (_HAOS, "haos-e2e"),
    (_STDIO, "haos-e2e-stdio"),
    (_BETA, "haos-e2e-inaddon-beta"),
    (_BETA, "haos-e2e-embedded-beta"),
    (_CBETA, "e2e-tests"),
    (_CBETA, "e2e-tests-embedded"),
    (_CBETA, "e2e-tests-update-path"),
)

# HAOS lanes whose whole-run topology a dispatch input picks: combined on pull
# requests, or without the tools entry throughout. They carry
# E2E_NO_TOOLS_ENTRY as an expression, so neither table above fits them.
_SELECTABLE_LANES: dict[tuple[str, str], dict[str, str]] = {
    (_HAOS, "haos-e2e-embedded"): {"HAOS_TEST_MODE": "embedded"},
    (_HAOS, "haos-e2e-inaddon"): {"HAOS_TEST_MODE": "inaddon"},
}


_SELECTOR_NAMES = ("E2E_BACKEND", "HAOS_TEST_MODE")

# Jobs that run the e2e suite but are deliberately NOT topology lanes, so they
# stay outside the two tables. Every OTHER e2e job in the directory is
# discovered by glob, so a topology lane introduced in a brand-new workflow
# file fails the completeness check until it is classified — hand-listing the
# lane files here would exempt the next file the same way the tables used to
# exempt the next lane (Codex review on the #2302 follow-up).
#
# Keyed by (workflow, job_id), not by workflow: excluding the whole FILE would
# exempt every job in it, including a topology lane added to it later, and that
# lane would then sit in neither table with this module still green — the same
# blanket-exemption shape the paragraph above rejects, applied to files
# instead of lanes (Codex review on #2309).
_NON_TOPOLOGY_JOBS: dict[tuple[str, str], str] = {
    ("performance-tests.yml", "performance-tests"): (
        "benchmark over src/e2e/performance/, not a topology lane paired "
        "against anything"
    ),
}

# A pytest step is an e2e lane when it targets the e2e suite: the path
# literally, or `$PYTEST_PATHS`, the HAOS workflows' dispatch-overridable input
# that defaults to `src/e2e/`. Matching `uv run pytest` alone would also sweep
# in pr.yml's unit-tests and docker-validation jobs.
_E2E_TARGET = re.compile(r"src/e2e/|\$\{?PYTEST_PATHS")

# What an e2e pytest step points at, with whatever follows `src/e2e/`
# captured: empty means the whole suite, non-empty means a subset.
_E2E_SUITE_TARGET = re.compile(r"(?:tests/)?src/e2e/([^\s\"']*)")

# Floor on the discovered count, so a predicate that quietly stops matching (a
# workflow restructure, a renamed input) fails here rather than reporting an
# empty sweep as full coverage.
_MIN_DISCOVERED_LANES = 19


def _job(workflow: str, job_id: str) -> dict[str, Any]:
    data = yaml.safe_load((_WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    job = data["jobs"][job_id]
    assert isinstance(job, dict), f"{workflow}::{job_id} must be a job mapping"
    return job


def _pytest_step(workflow: str, job_id: str) -> dict[str, Any]:
    """The one step in the job that invokes the e2e suite.

    Matched on the invocation rather than the bare word "pytest", which also
    appears in the diagnostics steps' prose about xdist workers.
    """
    job = _job(workflow, job_id)
    steps = [
        step
        for step in job.get("steps", [])
        if isinstance(step, dict) and "uv run pytest" in str(step.get("run", ""))
    ]
    assert len(steps) == 1, (
        f"{workflow}::{job_id} must have exactly one pytest step, found "
        f"{[step.get('name') for step in steps]}"
    )
    return steps[0]


def _uncommented(run: str) -> str:
    """A step's shell body with its comment-only lines dropped.

    A retired command left behind as `# uv run pytest src/e2e/...` is not an
    invocation, but it reads as one to any substring match — and the check
    that an excluded job still targets its subset would then be satisfied by
    the comment while the real command has moved elsewhere.
    """
    return "\n".join(
        line for line in run.splitlines() if not line.lstrip().startswith("#")
    )


def _job_pytest_run(workflow: str, job_id: str) -> str:
    """The pytest command a job runs, joined across its steps.

    Matched on the invocation, for the reason ``_pytest_step`` gives above: a
    diagnostics step's prose mentions pytest without running it, and joining
    that prose in would let a stale `src/e2e/...` string satisfy a check about
    what the job actually invokes. Comment lines go for the same reason.
    """
    return "\n".join(
        _uncommented(str(step.get("run", "")))
        for step in _job(workflow, job_id).get("steps", [])
        if isinstance(step, dict)
        and "uv run pytest" in _uncommented(str(step.get("run", "")))
    )


def _step_env(step: dict[str, Any]) -> dict[str, Any]:
    env = step.get("env") or {}
    assert isinstance(env, dict)
    return env


@pytest.mark.parametrize(
    ("workflow", "job_id"),
    list(_NO_TOOLS_LANES),
    ids=lambda value: value,
)
def test_no_tools_lane_carries_the_env_that_makes_it_a_no_tools_lane(
    workflow: str, job_id: str
) -> None:
    """Without E2E_NO_TOOLS_ENTRY the lane is a green copy of its sibling."""
    env = _step_env(_pytest_step(workflow, job_id))
    assert env.get("E2E_NO_TOOLS_ENTRY") == "1", (
        f"{workflow}::{job_id} runs the ORDINARY topology without "
        f"E2E_NO_TOOLS_ENTRY=1, and reports green doing it (#2292)"
    )


@pytest.mark.parametrize(
    ("workflow", "job_id"),
    list(_NO_TOOLS_LANES),
    ids=lambda value: value,
)
def test_no_tools_lane_selects_its_own_backend_topology(
    workflow: str, job_id: str
) -> None:
    """Each no-tools shape is one selector away from another lane's shape."""
    env = _step_env(_pytest_step(workflow, job_id))
    expected = _NO_TOOLS_LANES[(workflow, job_id)]
    actual = {name: env[name] for name in _SELECTOR_NAMES if name in env}
    assert actual == expected, (
        f"{workflow}::{job_id} selects {actual}, expected {expected} — the "
        "backend selector decides WHICH no-tools topology runs, and a wrong "
        "or missing one silently duplicates another lane"
    )


@pytest.mark.parametrize(("workflow", "job_id"), _ORDINARY_LANES, ids=lambda v: v)
def test_ordinary_lane_does_not_carry_the_no_tools_env(
    workflow: str, job_id: str
) -> None:
    """A retained lane must not be pinned permanently to its absent phase."""
    job = _job(workflow, job_id)
    job_env = job.get("env") or {}
    assert "E2E_NO_TOOLS_ENTRY" not in job_env, f"{workflow}::{job_id} job env"
    for step in job.get("steps", []):
        if not isinstance(step, dict):
            continue
        assert "E2E_NO_TOOLS_ENTRY" not in _step_env(step), (
            f"{workflow}::{job_id} step {step.get('name')!r} drops the "
            "component entry, so the lane no longer covers the ordinary "
            "topology it is paired against"
        )


# The expression shapes these workflows use for topology selection:
# ``${{ matrix.x }}`` and ``${{ <matrix|inputs>.x == 'v' && 'a' || 'b' }}``.
_EXPRESSION = re.compile(
    r"\$\{\{\s*(?P<ref>(?:matrix|inputs)\.\w+)"
    r"(?:\s*==\s*'(?P<eq>[^']*)'\s*&&\s*'(?P<yes>[^']*)'\s*\|\|\s*'(?P<no>[^']*)')?"
    r"\s*\}\}"
)


def _resolve(text: str, context: dict[str, str]) -> str:
    """Evaluate the expressions above as Actions would for one run.

    An absent context key is an unset input (a pull_request run has no
    inputs). ``cond && '' || 'b'`` yields ``'b'`` in Actions, so an empty
    ``yes`` branch falls through like it does there.
    """

    def evaluate(match: re.Match[str]) -> str:
        value = context.get(match["ref"])
        if match["eq"] is None:
            return value or ""
        return match["yes"] if value == match["eq"] and match["yes"] else match["no"]

    resolved = _EXPRESSION.sub(evaluate, text)
    assert "${{" not in resolved, f"unsupported expression in {text!r}"
    return resolved


@pytest.mark.parametrize("job_id", ["e2e-validation", "e2e-validation-embedded"])
def test_pull_requests_run_the_suite_in_both_tool_entry_states(
    monkeypatch: pytest.MonkeyPatch, job_id: str
) -> None:
    """Pull requests have no whole-run no-tools container lane any more.

    A combined row must cover the absent state, a component-present row must
    keep running the general suite with the entry.
    """
    step = _pytest_step(_PR, job_id)
    states = set()
    for row in _job(_PR, job_id)["strategy"]["matrix"]["include"]:
        context = {f"matrix.{key}": str(value) for key, value in row.items()}
        monkeypatch.setenv(
            "E2E_COMBINE_TOPOLOGIES",
            _resolve(str(_step_env(step)["E2E_COMBINE_TOPOLOGIES"]), context),
        )
        combined = topology.combined_topologies()
        states.add(combined)
    assert states == {True, False}, f"{job_id} covers only combined={states}"


@pytest.mark.parametrize(("workflow", "job_id"), list(_SELECTABLE_LANES))
@pytest.mark.parametrize(
    ("chosen", "combined", "absent"),
    [
        (None, True, False),
        ("combined", True, False),
        ("without-tools", False, True),
    ],
    ids=["pull_request", "dispatch-combined", "dispatch-without-tools"],
)
def test_haos_topology_input_selects_the_lane_topology(
    monkeypatch: pytest.MonkeyPatch,
    workflow: str,
    job_id: str,
    chosen: str | None,
    combined: bool,
    absent: bool,
) -> None:
    """Pull requests run combined; a dispatch can keep the entry absent.

    The whole-run no-tools HAOS lanes are this input now. If the expression
    stops resolving, a manual no-tools run silently re-tests the combined
    topology, or a pull request loses its absent phase.
    """
    data = yaml.safe_load((_WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    options = data[True]["workflow_dispatch"]["inputs"]["topology"]["options"]
    if chosen is not None:
        assert chosen in options
    context = {} if chosen is None else {"inputs.topology": chosen}
    env = _step_env(_pytest_step(workflow, job_id))
    for name in ("E2E_COMBINE_TOPOLOGIES", "E2E_NO_TOOLS_ENTRY"):
        monkeypatch.setenv(name, _resolve(str(env[name]), context))
    assert topology.combined_topologies() is combined
    assert topology.tools_entry_absent() is absent
    selectors = {name: env[name] for name in _SELECTOR_NAMES if name in env}
    assert selectors == _SELECTABLE_LANES[(workflow, job_id)]


def test_stdio_lane_never_allocates_a_pull_request_runner() -> None:
    data = yaml.safe_load((_WORKFLOW_DIR / _STDIO).read_text(encoding="utf-8"))
    assert set(data[True]) == {"workflow_dispatch"}


def _failure_cap(arguments: list[str]) -> str | None:
    """The ``--maxfail`` pytest applies: the last of ``--maxfail`` and ``-x``."""
    cap = None
    for index, argument in enumerate(arguments):
        if argument.startswith("--maxfail="):
            cap = argument.partition("=")[2]
        elif argument == "--maxfail":
            cap = arguments[index + 1]
        elif argument in ("-x", "--exitfirst"):
            cap = "1"
    return cap


def test_no_run_stops_before_reporting_every_failure() -> None:
    """A failure cap ends a run after its first failures, so a topology audit
    or a locale sweep reports a few of them and hides the rest."""
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    assert _failure_cap(pyproject["tool"]["pytest"]["ini_options"]["addopts"]) is None
    for path in sorted(_WORKFLOW_DIR.glob("*.yml")):
        jobs = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
        for job_id, job in jobs.items():
            for step in job.get("steps", []):
                body = _uncommented(str(step.get("run", ""))).replace("\\\n", " ")
                for line in body.splitlines():
                    if "uv run pytest" in line:
                        arguments = shlex.split(line, comments=True)
                        assert _failure_cap(arguments) in (None, "0"), (
                            f"{path.name}::{job_id}: {line.strip()}"
                        )


def _discover_e2e_lanes(directory: Path | None = None) -> set[tuple[str, str]]:
    """Every ``(workflow, job)`` in a workflow dir that runs the e2e suite.

    The directory is a parameter so the exclusion's job-scoping can be
    exercised against a fixture: proving that a non-excluded job in an
    EXCLUDED workflow is still discovered needs a second job in that file,
    which the real directory does not have.
    """
    root = _WORKFLOW_DIR if directory is None else directory
    discovered: set[tuple[str, str]] = set()
    for path in sorted((*root.glob("*.yml"), *root.glob("*.yaml"))):
        workflow = path.name
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        for job_id, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            if (workflow, job_id) in _NON_TOPOLOGY_JOBS:
                continue
            for step in job.get("steps", []):
                if not isinstance(step, dict):
                    continue
                run = str(step.get("run", ""))
                if "uv run pytest" in run and _E2E_TARGET.search(run):
                    discovered.add((workflow, job_id))
                    break
    return discovered


def test_every_e2e_lane_is_claimed_by_exactly_one_table() -> None:
    """The lane tables are hand-maintained, so a lane added later belongs to
    none and is exempt from every env-contract check above: it is
    parametrized nowhere, and nothing goes red for it (#2302 review).

    Completeness is all this test claims, and classification stays the tables'
    job deliberately. Deriving it from the presence of E2E_NO_TOOLS_ENTRY would
    make the contract vacuous in exactly the case #2292 is about: a no-tools
    lane that LOST the variable would re-classify itself as ordinary, and
    ``test_ordinary_lane_does_not_carry_the_no_tools_env`` would then cheerfully
    confirm the variable it just dropped is absent. So the author says which
    table a new lane belongs to; this test only insists that they say.
    """
    discovered = _discover_e2e_lanes()
    tables = {
        "_NO_TOOLS_LANES": set(_NO_TOOLS_LANES),
        "_ORDINARY_LANES": set(_ORDINARY_LANES),
        "_SELECTABLE_LANES": set(_SELECTABLE_LANES),
    }

    for lane in sorted(discovered):
        claimants = [name for name, table in tables.items() if lane in table]
        workflow, job_id = lane
        assert len(claimants) == 1, (
            f"{workflow}::{job_id} runs the e2e suite but is claimed by "
            f"{claimants or 'no table'}, not by exactly one. Add it to "
            "_NO_TOOLS_LANES (with the backend selectors its pytest step must "
            "carry) if it sets E2E_NO_TOOLS_ENTRY=1, to _SELECTABLE_LANES if "
            "a dispatch input picks its topology, otherwise to "
            "_ORDINARY_LANES — a lane in no table is checked by nothing "
            "in this module, and a lane in both is contradicting itself"
        )

    for name, table in tables.items():
        stale = sorted(lane for lane in table if lane not in discovered)
        assert not stale, (
            f"{name} names {stale}, which no longer runs the e2e suite — a "
            "renamed or deleted job. Fix the entry rather than dropping it: "
            "the lane it stood for may still exist under its new name"
        )

    assert len(discovered) >= _MIN_DISCOVERED_LANES, (
        f"discovered only {len(discovered)} e2e lanes across the workflow dir, "
        f"below the {_MIN_DISCOVERED_LANES} that exist today — the discovery "
        "predicate stopped matching, and an empty sweep passes the checks "
        "above vacuously"
    )


def test_every_non_topology_exclusion_names_a_job_that_exists() -> None:
    """An exclusion whose job is gone carries a rationale nobody can check.

    A misspelled key is caught by the completeness test above — the job it
    failed to exclude turns up unclaimed. A key for a job that no longer
    exists is caught by nothing: it excludes an empty set forever, and its
    stated reason quietly stops describing anything.
    """
    for (workflow, job_id), reason in _NON_TOPOLOGY_JOBS.items():
        path = _WORKFLOW_DIR / workflow
        assert path.is_file(), f"{workflow} no longer exists, but is excluded"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        jobs = (data or {}).get("jobs") or {}
        assert job_id in jobs, (
            f"{workflow}::{job_id} is excluded as {reason!r} but the workflow "
            f"has no such job — the exclusion now covers nothing"
        )
        # And it must be excludable on its merits, not on the author's
        # say-so: a topology lane hand-written into this dict would be hidden
        # from the completeness sweep as thoroughly as the file-scoped key
        # used to hide its whole file. What marks a lane as a topology lane is
        # the selectors it is parametrized with, so ask THIS job for them.
        #
        # Scoped to the job, never to the file: a sibling topology lane in the
        # same file legitimately carries these names, and that arrangement is
        # exactly what test_an_excluded_job_does_not_exempt_its_neighbours
        # exists to permit. A whole-file substring test would forbid the very
        # configuration this module blesses one test further down.
        job = _job(workflow, job_id)
        # Workflow-level env is inherited by every job in the file, so it
        # parametrizes this one as surely as its own. A sibling job's private
        # env does not, which is why this reads three scopes and not the file.
        workflow_env = (data or {}).get("env") or {}
        job_env = job.get("env") or {}
        env_keys = (
            set(workflow_env)
            | set(job_env)
            | {
                key
                for step in job.get("steps", [])
                if isinstance(step, dict)
                for key in _step_env(step)
            }
        )
        marks_a_lane = env_keys & {*_SELECTOR_NAMES, "E2E_NO_TOOLS_ENTRY"}
        assert not marks_a_lane, (
            f"{workflow}::{job_id} is parametrized with "
            f"{sorted(marks_a_lane)}, which is what makes a job a topology "
            "lane — an exclusion cannot be the thing that keeps it out of the "
            "tables"
        )

        # Absence of the selectors is not proof: the default container
        # topology carries none of them either, so a job repurposed to run
        # the ordinary suite would pass the check above and stay hidden
        # forever, since discovery skips it before it ever reads the command.
        # So require the positive property instead — the job must still run
        # only a subset of the e2e tree, which is what its rationale claims.
        targets = _E2E_SUITE_TARGET.findall(str(_job_pytest_run(workflow, job_id)))
        assert targets, (
            f"{workflow}::{job_id} no longer targets the e2e tree at all, so "
            "the exclusion is describing a job that is not what it was"
        )
        whole_suite = [target for target in targets if not target.strip("/")]
        assert not whole_suite, (
            f"{workflow}::{job_id} now runs the whole e2e suite, not the "
            f"subset {reason!r} describes — that is an ordinary lane wearing "
            "an exclusion, and discovery skips it before it reads the command"
        )


def test_an_excluded_job_does_not_exempt_its_neighbours(tmp_path: Path) -> None:
    """The exclusion is per job, and this is the case that proves it.

    Keyed by workflow, excluding one benchmark job would exempt the whole
    file: a topology lane added to it later would be discovered by nothing,
    sit in neither table, and leave this module green while every env contract
    it should carry goes unchecked (Codex review on #2309).

    The real workflow dir cannot show this — the excluded file holds exactly
    one job — so the fixture puts a topology lane next to the excluded one,
    under the excluded file's own name.
    """
    if not _NON_TOPOLOGY_JOBS:
        pytest.skip("nothing is excluded, so there is no scoping to check")
    workflow, excluded_job = next(iter(_NON_TOPOLOGY_JOBS))
    (tmp_path / workflow).write_text(
        "jobs:\n"
        f"  {excluded_job}:\n"
        "    steps:\n"
        "      - run: cd tests && uv run pytest src/e2e/performance/ -m perf\n"
        "  a-topology-lane-added-later:\n"
        "    steps:\n"
        "      - run: cd tests && uv run pytest src/e2e/ --tb=short\n",
        encoding="utf-8",
    )

    discovered = _discover_e2e_lanes(tmp_path)

    assert (workflow, "a-topology-lane-added-later") in discovered, (
        "a topology lane sharing a file with an excluded job was skipped — "
        "the exclusion is keyed by workflow, so it exempts the whole file"
    )
    assert (workflow, excluded_job) not in discovered, (
        "the excluded job itself must still be excluded"
    )


def test_beta_schedule_runs_every_lane_without_cron_gates() -> None:
    """The single shared beta cron must not be restricted to one lane."""
    workflow = yaml.safe_load(
        (_WORKFLOW_DIR / "haos-e2e-beta-tests.yml").read_text(encoding="utf-8")
    )
    triggers = workflow.get("on") or workflow.get(True) or {}
    declared = [entry["cron"] for entry in triggers.get("schedule", [])]
    assert len(declared) == 1, "the beta workflow must keep one nightly schedule"

    lanes = {
        job_id: job
        for job_id, job in (workflow.get("jobs") or {}).items()
        if isinstance(job, dict) and job_id != "changes"
    }
    assert lanes, "the beta workflow must retain its E2E lanes"
    for job_id, job in lanes.items():
        condition = str(job.get("if") or "")
        assert "github.event.schedule" not in condition, (
            f"{job_id} must run on the shared nightly schedule, not claim a "
            "lane-specific cron"
        )
