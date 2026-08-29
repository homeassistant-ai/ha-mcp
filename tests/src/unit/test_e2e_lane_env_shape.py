"""Pin the no-tools lane env (#2292) — the workflow half and the parser half.

A dropped env var here degenerates a required lane into a green duplicate of
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
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_TOPOLOGY_PATH = (
    Path(__file__).resolve().parents[1] / "e2e" / "utilities" / "topology.py"
)


def _load_topology():
    spec = importlib.util.spec_from_file_location("e2e_topology", _TOPOLOGY_PATH)
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
    for name in ("E2E_NO_TOOLS_ENTRY", "E2E_BACKEND", "HAOS_TEST_MODE"):
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


# --------------------------------------------------------------------------
# The workflow half: which lane carries which env
# --------------------------------------------------------------------------

_PR = "pr.yml"
_E2E = "e2e-tests.yml"
_HAOS = "haos-e2e-tests.yml"

# (workflow, job) -> the backend selectors that step's env must carry, exactly.
# An absent selector is as load-bearing as a present one: the two no-component
# lanes must NOT name a backend, or they run the embedded topology instead of
# the plain-container one they exist to cover.
_NO_TOOLS_LANES: dict[tuple[str, str], dict[str, str]] = {
    (_PR, "e2e-validation-no-component"): {},
    (_PR, "e2e-validation-embedded-server-only"): {"E2E_BACKEND": "embedded"},
    (_E2E, "e2e-tests-no-component"): {},
    (_E2E, "e2e-tests-embedded-server-only"): {"E2E_BACKEND": "embedded"},
    (_HAOS, "haos-e2e-embedded-no-tools"): {"HAOS_TEST_MODE": "embedded"},
    (_HAOS, "haos-e2e-inaddon-no-tools"): {"HAOS_TEST_MODE": "inaddon"},
}

# The component-present siblings each no-tools lane is paired against. If one of
# these ever grows E2E_NO_TOOLS_ENTRY, the pair stops being a comparison.
_ORDINARY_LANES: tuple[tuple[str, str], ...] = (
    (_PR, "e2e-validation"),
    (_PR, "e2e-validation-embedded"),
    (_PR, "e2e-validation-update-path"),
    (_E2E, "e2e-tests"),
    (_E2E, "e2e-tests-embedded"),
    (_E2E, "e2e-tests-update-path"),
    (_HAOS, "haos-e2e"),
    (_HAOS, "haos-e2e-embedded"),
    (_HAOS, "haos-e2e-inaddon"),
    (_HAOS, "haos-e2e-stdio"),
)

# The container no-tools lanes are whole-topology audits: pytest.ini's
# --maxfail=3 would stop them after three failures, which is exactly the run
# that needs the full list. (The HAOS lanes already carry --maxfail=0 for their
# own triage reasons, stated in their step comments.)
_AUDIT_LANES: tuple[tuple[str, str], ...] = (
    (_PR, "e2e-validation-no-component"),
    (_PR, "e2e-validation-embedded-server-only"),
    (_E2E, "e2e-tests-no-component"),
    (_E2E, "e2e-tests-embedded-server-only"),
)

_SELECTOR_NAMES = ("E2E_BACKEND", "HAOS_TEST_MODE")


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
    """The component-present sibling must stay component-present."""
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


@pytest.mark.parametrize(("workflow", "job_id"), _AUDIT_LANES, ids=lambda v: v)
def test_container_audit_lane_reports_the_full_failure_surface(
    workflow: str, job_id: str
) -> None:
    """--maxfail=0 overrides pytest.ini's --maxfail=3 for the topology audits."""
    run = str(_pytest_step(workflow, job_id).get("run", ""))
    assert "--maxfail=0" in run, (
        f"{workflow}::{job_id} stops after pytest.ini's --maxfail=3, so a "
        "topology audit reports three failures instead of the full surface"
    )


def test_every_beta_cron_is_claimed_by_exactly_one_lane_gate() -> None:
    """Each beta cron must be claimed by exactly one job's ``if:`` gate.

    The merged beta workflow declares both nightly crons at the workflow
    level, and each lane claims its own inside its ``if:``. The two copies of
    each cron string must stay in lockstep: editing one side leaves a lane
    whose gate matches no declared cron, so it silently stops running nightly
    — the lane reports nothing, and nothing else notices (#2302 review).
    """
    workflow = yaml.safe_load(
        (_WORKFLOW_DIR / "haos-e2e-beta-tests.yml").read_text(encoding="utf-8")
    )
    triggers = workflow.get("on") or workflow.get(True) or {}
    declared = [entry["cron"] for entry in triggers.get("schedule", [])]
    assert declared, "the beta workflow must keep its nightly schedule"

    lane_conditions = [
        str(job.get("if") or "")
        for job_id, job in (workflow.get("jobs") or {}).items()
        if isinstance(job, dict) and job_id != "changes"
    ]
    for cron in declared:
        claimants = [cond for cond in lane_conditions if cron in cond]
        assert len(claimants) == 1, (
            f"cron {cron!r} must be claimed by exactly ONE beta lane gate; "
            f"found {len(claimants)}. A cron no lane claims runs nothing on "
            f"its slot; one claimed twice starts both lanes on it."
        )
    # And the reverse direction: a lane gating on a cron string the workflow
    # never declares (a typo'd edit of one copy) matches no schedule event and
    # silently skips its nightly run, while every declared cron still finds
    # its one claimant above. Each gate must contain exactly one cron literal,
    # and it must be a declared one.
    cron_literal = re.compile(r"'([^']*(?:\*|\d)[^']*\*[^']*)'")
    for cond in lane_conditions:
        crons_in_gate = [
            lit
            for lit in cron_literal.findall(cond)
            if lit.count(" ") == 4  # five cron fields
        ]
        assert len(crons_in_gate) == 1, (
            f"each beta lane gate must name exactly one cron; {cond!r} names "
            f"{crons_in_gate}"
        )
        assert crons_in_gate[0] in declared, (
            f"lane gate claims undeclared cron {crons_in_gate[0]!r}; the "
            f"workflow schedules {declared} — the two copies drifted"
        )
