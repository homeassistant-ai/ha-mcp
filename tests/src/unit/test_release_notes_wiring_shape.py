"""Pin the stable release workflow's force and release-note wiring.

``semver-release.yml`` cannot run in PR CI, so nothing else checks that the
filenames in its release step still agree: the
script's ``--out``, the ``[ ! -s ... ]`` emptiness guard, the fallback write,
and ``gh release create --notes-file``. A missing script is loud (the step runs
under ``bash -e``), but a drifting ``--out`` name is silent — the guard finds no
file, the fallback fires, and the release ships ``Release vX.Y.Z`` as its entire
body on a green run, for that release and every one after it. These tests catch
that before merge, the only coverage a workflow that cannot run pre-merge can
have.

The assertions are scoped to the one step that builds and publishes the body,
so a match somewhere else in the file cannot stand in for that step's wiring.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_SCRIPT = "scripts/extract_release_notes.py"
_STEP = "Create draft GitHub release"
_WORKFLOW = "semver-release.yml"
_OUT_RE = re.compile(r"extract_release_notes\.py[^\n]*--out\s+(\S+)")
_RUNS_OF_BLANKS = re.compile(r"[ \t]+")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOW_DIR / _WORKFLOW).read_text(encoding="utf-8"))


def _step(job: str, step_id: str) -> dict[str, Any]:
    steps = _workflow()["jobs"][job]["steps"]
    matches = [step for step in steps if step.get("id") == step_id]
    assert len(matches) == 1, (
        f"{_WORKFLOW} must have exactly one {step_id!r} step in job {job!r}"
    )
    return matches[0]


def _release_step_run() -> str:
    """The `run` body of the single step that writes and publishes the notes."""
    data = _workflow()
    steps = [
        step
        for job in data["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == _STEP
    ]
    assert len(steps) == 1, f"{_WORKFLOW} must have exactly one {_STEP!r} step"
    run = steps[0].get("run")
    assert run, f"{_WORKFLOW}'s {_STEP!r} step has no run body"
    # Comment lines are dropped first: the step is commented, and a commented-out
    # command must not be able to satisfy an assertion about what it does.
    active = "\n".join(
        line for line in run.splitlines() if not line.lstrip().startswith("#")
    )
    return _RUNS_OF_BLANKS.sub(" ", active)


def test_manual_force_requests_a_patch_release() -> None:
    data = _workflow()
    force_input = data[True]["workflow_dispatch"]["inputs"]["force"]
    check_run = _step("check-changes", "check")["run"]
    semantic = _step("semantic-release", "semantic")

    assert force_input["type"] == "boolean"
    assert "Force a patch" in force_input["description"]
    assert '[ "${{ inputs.force }}" = "true" ]' in check_run
    assert '[ -n "$CHANGES" ] && [ "${{ inputs.force }}" = "true" ]' in check_run
    for pattern in (
        "^(feat|fix|perf|refactor)(\\(.+\\))?!?:",
        "^[a-z]+(\\(.+\\))?!:",
        "^BREAKING[ -]CHANGE:",
    ):
        assert f'--grep="{pattern}"' in check_run
    assert semantic["with"]["force"] == "${{ inputs.force && 'patch' || '' }}"


def test_release_publish_consumes_its_producing_run_instead_of_moving_master() -> None:
    publish = yaml.safe_load(
        (_WORKFLOW_DIR / "release-publish.yml").read_text(encoding="utf-8")
    )
    steps = publish["jobs"]["prepare"]["steps"]
    source = next(step for step in steps if step.get("id") == "source")
    version = next(step for step in steps if step.get("id") == "version")
    assert "github.event.workflow_run.id" in source["env"]["TRIGGER_RUN_ID"]
    assert 'gh run download "$TRIGGER_RUN_ID"' in source["run"]
    assert version["env"]["RELEASE_TAG"] == "${{ steps.source.outputs.tag }}"
    assert 'git show "$RELEASE_TAG:pyproject.toml"' in version["run"]
    semver = yaml.safe_load((_WORKFLOW_DIR / "semver-release.yml").read_text())
    produced = next(
        step["with"]["name"]
        for step in semver["jobs"]["semantic-release"]["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert f"--name {produced}" in source["run"]


def _workflow_expression(expression: str, context: dict[str, Any]) -> Any:
    """Evaluate this wiring's equality/and/or subset on literal test contexts."""
    expression = expression.strip().removeprefix("${{").removesuffix("}}").strip()
    expression = re.sub(
        r"\b(?:github|inputs)\.[\w.]+", lambda m: repr(context[m[0]]), expression
    )
    tree = ast.parse(
        expression.replace("&&", " and ").replace("||", " or "), mode="eval"
    )

    def value(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp):
            result = None
            for term in node.values:
                result = value(term)
                if isinstance(node.op, ast.And) and not result:
                    break
                if isinstance(node.op, ast.Or) and result:
                    break
            return result
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = value(node.left), value(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise AssertionError(f"Unsupported workflow expression: {ast.dump(node)}")

    return value(tree.body)


@pytest.mark.parametrize(
    "event,producer,channel,manual_id,reads,validates,expected_id",
    [
        ("workflow_run", "stable", "", "202", True, False, 101),
        ("workflow_dispatch", "", "stable", "202", True, True, "202"),
        ("workflow_dispatch", "", "stable", "", True, True, ""),
        ("workflow_dispatch", "", "stable", "0", True, True, "0"),
        ("workflow_run", "dev", "", "202", False, False, None),
        ("workflow_dispatch", "", "dev", "202", False, False, None),
        ("push", "", "", "202", False, False, None),
    ],
)
def test_finalized_reader_routes_only_stable_events_through_the_right_run(
    event, producer, channel, manual_id, reads, validates, expected_id
) -> None:
    workflow = yaml.safe_load(
        (_WORKFLOW_DIR / "sync-integration-mirror.yml").read_text()
    )
    gate = workflow["jobs"]["gate"]
    reader = next(s for s in gate["steps"] if s.get("id") == "finalized")
    validation = next(
        s for s in gate["steps"] if s.get("name") == "Validate stable recovery input"
    )
    publishers = {
        kind: yaml.safe_load((_WORKFLOW_DIR / path).read_text())["name"]
        for kind, path in (
            ("stable", "release-publish.yml"),
            ("dev", "publish-dev.yml"),
        )
    }
    context = {
        "github.event_name": event,
        "github.event.workflow_run.name": publishers.get(producer, ""),
        "github.event.workflow_run.id": 101 if event == "workflow_run" else None,
        "inputs.channel": channel,
        "inputs.release_publish_run_id": manual_id,
    }
    assert gate["permissions"]["actions"] == "read"
    assert gate["steps"].index(validation) < gate["steps"].index(reader)
    assert bool(_workflow_expression(reader["if"], context)) is reads
    assert bool(_workflow_expression(validation["if"], context)) is validates
    if reads:
        assert str(_workflow_expression(reader["with"]["run-id"], context)) == str(
            expected_id
        )
    if validates:
        assert (
            _workflow_expression(validation["env"]["RECOVERY_RUN_ID"], context)
            == expected_id
        )


def test_the_extractor_the_workflow_calls_exists() -> None:
    assert (_REPO_ROOT / _SCRIPT).is_file(), (
        f"{_SCRIPT} is gone, but the release workflow still invokes it"
    )


def test_the_release_step_wires_one_filename_end_to_end() -> None:
    run = _release_step_run()

    out = _OUT_RE.search(run)
    assert out, f"{_WORKFLOW}'s {_STEP!r} step does not call {_SCRIPT} with --out"
    notes = out.group(1)

    stages = [
        (f"python3 {_SCRIPT}", "call the extractor"),
        (f"[ ! -s {notes} ]", f"guard {notes} for emptiness"),
        ("::warning::no changelog section extracted", "warn when the extract is empty"),
        (f"> {notes}", f"write its fallback body to {notes}"),
        (f"--notes-file {notes}", f"publish {notes}"),
    ]
    for needle, what in stages:
        assert needle in run, f"{_WORKFLOW}'s {_STEP!r} step does not {what}"

    positions = [run.index(needle) for needle, _ in stages]
    assert positions == sorted(positions), (
        f"{_WORKFLOW}'s {_STEP!r} step must extract, guard the empty file, warn, "
        "write the fallback, then publish that same file — in that order"
    )
