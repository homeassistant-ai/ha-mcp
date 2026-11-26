"""Guard shared HAOS workflow contracts without restructuring proven lanes."""

import ast
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
# Stable and beta HAOS lanes each live in one consolidated workflow file
# (#2292/#2302). Stable PR lanes share one `changes` classifier; both beta
# lanes participate in every surviving workflow event.
_STABLE_WORKFLOW = "haos-e2e-tests.yml"
_BETA_WORKFLOW = "haos-e2e-beta-tests.yml"
_CONTAINER_BETA_WORKFLOW = "e2e-beta-tests.yml"
# 6 actual consumers (the four HAOS lanes, the image builder and the dev env);
# the floor is what catches a lane that silently loses its image-cache step.
_CACHE_KEY_CONSUMER_FLOOR = 6
_CACHE_KEY_OUTPUT_MARKER = "cache-key=haos-image-"
_HAOS_IMAGE_CACHE_PATH = "/tmp/haos-test-image.qcow2"
_CACHE_ACTIONS = {
    "actions/cache",
    "actions/cache/restore",
    "actions/cache/save",
}
_CACHE_KEY_COMMAND = """hash=$(git ls-tree -r HEAD \\
  tests/haos_image_build \\
  tests/initial_test_state \\
  custom_components/ha_mcp_tools \\
  homeassistant-addon-webhook-proxy \\
  | grep -v '\\.md$' | sha256sum | cut -d' ' -f1 | head -c16)
echo "cache-key=haos-image-$hash" >> "$GITHUB_OUTPUT"
"""


def _workflow(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _cache_key_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in _job_steps(job)
        if _CACHE_KEY_OUTPUT_MARKER in str(step.get("run", ""))
    ]


def _uses_haos_image_cache(job: dict[str, Any]) -> bool:
    return any(
        str(step.get("uses", "")).partition("@")[0] in _CACHE_ACTIONS
        and _HAOS_IMAGE_CACHE_PATH in _cache_paths(step)
        for step in _job_steps(job)
    )


def _cache_key_consumers(
    workflow_dir: Path = _WORKFLOW_DIR,
) -> list[tuple[Path, str]]:
    workflow_paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    consumers = [
        (path, str(job_id))
        for path in workflow_paths
        for job_id, job in _workflow(path)["jobs"].items()
        if isinstance(job, dict) and _uses_haos_image_cache(job)
    ]
    assert len(consumers) >= _CACHE_KEY_CONSUMER_FLOOR, (
        "expected at least "
        f"{_CACHE_KEY_CONSUMER_FLOOR} HAOS image cache-key consumers, found "
        f"{[(path.name, job_id) for path, job_id in consumers]}"
    )
    return consumers


# Every HAOS lane boots the shared image, so each one restores it before it
# would build; a lost restore leaves the lane rebuilding on every run while the
# consumer count above still passes on its key or save step.
_IMAGE_RESTORING_LANES = (
    (_STABLE_WORKFLOW, "haos-e2e"),
    (_STABLE_WORKFLOW, "haos-e2e-embedded"),
    (_STABLE_WORKFLOW, "haos-e2e-inaddon"),
    ("haos-e2e-stdio.yml", "haos-e2e-stdio"),
)


def _cache_paths(step: dict[str, Any]) -> list[str]:
    """The paths as actions/cache reads them: one per line, trimmed, in order.
    The order goes into the cache version, so it is kept."""
    paths = str(step.get("with", {}).get("path", "")).splitlines()
    return [path.strip() for path in paths if path.strip()]


def _image_cache_steps(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [
        step
        for step in _job_steps(job)
        if str(step.get("uses", "")).partition("@")[0] == action
        and _HAOS_IMAGE_CACHE_PATH in _cache_paths(step)
    ]


def _cache_identity(step: dict[str, Any]) -> tuple[str, list[str]]:
    """What Actions matches a restore against a save by: the key, and the
    path list, which goes into the cache version."""
    return step["with"]["key"], _cache_paths(step)


def test_every_haos_lane_restores_the_image_the_external_lane_saves() -> None:
    external = _workflow(_WORKFLOW_DIR / _STABLE_WORKFLOW)["jobs"]["haos-e2e"]
    saves = _image_cache_steps(external, "actions/cache/save")
    assert len(saves) == 1, "the external HAOS lane must save the image once"
    saved = _cache_identity(saves[0])
    for workflow, job_id in _IMAGE_RESTORING_LANES:
        job = _workflow(_WORKFLOW_DIR / workflow)["jobs"][job_id]
        restores = _image_cache_steps(job, "actions/cache/restore")
        assert restores, f"{workflow}::{job_id} no longer restores the HAOS image"
        assert all(_cache_identity(step) == saved for step in restores), (
            f"{workflow}::{job_id} restores a key or path list the external "
            "lane does not save, so it rebuilds the image on every run"
        )


_TWO_PHASE_LANES = (
    (_STABLE_WORKFLOW, "haos-e2e-inaddon"),
    ("haos-e2e-stdio.yml", "haos-e2e-stdio"),
    (_BETA_WORKFLOW, "haos-e2e-inaddon-beta"),
)


def _run_two_phase_step(
    tmp_path: Path,
    workflow: str,
    job_id: str,
    pytest_args: str,
    exits: str = "",
    pytest_paths: str = "src/e2e/",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run a two-phase lane's real shell with a ``uv`` that records each call's
    arguments and answers with the next code in ``exits`` (0 once they run out)."""
    job = _workflow(_WORKFLOW_DIR / workflow)["jobs"][job_id]
    step = next(
        step
        for step in _job_steps(job)
        if "not addon_disruptive" in step.get("run", "")
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'n=$(($(ls "$UV_CALLS" | wc -l) + 1))\n'
        'printf "%s\\n" "$@" > "$UV_CALLS/$n"\n'
        'code=$(echo "$UV_EXITS" | cut -d, -f"$n")\n'
        'exit "${code:-0}"\n'
    )
    uv.chmod(0o755)
    calls = tmp_path / "calls"
    calls.mkdir()
    (tmp_path / "tests" / "src" / "e2e").mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PYTEST_ARGS": pytest_args,
            "PYTEST_PATHS": pytest_paths,
            "UV_CALLS": str(calls),
            "UV_EXITS": exits,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, [
        (calls / name).read_text().splitlines()
        for name in sorted(os.listdir(calls), key=int)
    ]


def _selection(call: list[str]) -> tuple[str, str]:
    """The marker expression and worker count a pytest call runs with."""
    last = max(index for index, arg in enumerate(call) if arg == "-m")
    marker = call[last + 1]
    workers = next(arg for arg in reversed(call) if arg.startswith("-n"))
    return marker, workers


@pytest.mark.parametrize(("workflow", "job_id"), _TWO_PHASE_LANES)
@pytest.mark.parametrize(
    "pytest_args",
    [
        "-m haos_stdio_only",
        "-k visibility\t-m addon_disruptive",
        "-k visibility -n2",
        "-vn2",
        "-k visibility @selection.args",
    ],
)
def test_a_dispatch_cannot_reset_a_phase_marker_or_worker_count(
    tmp_path: Path, workflow: str, job_id: str, pytest_args: str
) -> None:
    """pytest keeps the last ``-m`` and ``-n``, so one in ``pytest_args`` would
    replace a phase's own: both phases would run the same tests, or the
    self-restarting app tests would run in parallel."""
    result, calls = _run_two_phase_step(tmp_path, workflow, job_id, pytest_args)

    assert result.returncode != 0
    assert calls == [], "a phase ran with the dispatch's marker or worker count"


@pytest.mark.parametrize(("workflow", "job_id"), _TWO_PHASE_LANES)
@pytest.mark.parametrize(
    ("exits", "passes"),
    [("0,0", True), ("5,0", True), ("0,5", True), ("5,5", False), ("1,0", False)],
    ids=["both", "serial-only", "parallel-only", "empty", "parallel-failed"],
)
def test_one_empty_phase_does_not_stop_the_other(
    tmp_path: Path, workflow: str, job_id: str, exits: str, passes: bool
) -> None:
    """A ``-k`` can select only the self-restarting app tests, leaving the
    parallel phase empty (pytest exit 5); the serial phase must still run.
    A selection matching nothing at all, or a failed phase, still fails."""
    result, calls = _run_two_phase_step(
        tmp_path, workflow, job_id, "-vv -k visibility", exits
    )

    assert (result.returncode == 0) is passes, result.stdout + result.stderr
    assert _selection(calls[0]) == ("not addon_disruptive", "-n2")
    if exits == "1,0":
        assert len(calls) == 1, "the serial phase booted HAOS after a failure"
    else:
        assert len(calls) == 2
        assert _selection(calls[1]) == ("addon_disruptive", "-n0")


@pytest.mark.parametrize(("workflow", "job_id"), _TWO_PHASE_LANES)
def test_pytest_paths_keep_their_filename_patterns(
    tmp_path: Path, workflow: str, job_id: str
) -> None:
    """A dispatch may name test modules by pattern, expanded after ``cd tests``."""
    module = tmp_path / "tests" / "src" / "e2e" / "test_search_x.py"
    module.parent.mkdir(parents=True)
    module.touch()

    result, calls = _run_two_phase_step(
        tmp_path,
        workflow,
        job_id,
        "-k visibility",
        pytest_paths="src/e2e/test_search*.py",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert all("src/e2e/test_search_x.py" in call for call in calls)


def _cache_key_command(path: Path, job_id: str) -> str:
    consumer = f"{path.name}:{job_id}"
    job = _workflow(path)["jobs"][job_id]
    assert isinstance(job, dict), f"{consumer} must be a job mapping"
    steps = _cache_key_steps(job)
    assert len(steps) == 1, f"{consumer} must have one image cache-key step"
    script = str(steps[0]["run"])
    start_marker = "hash=$(git ls-tree -r HEAD"
    end_marker = 'echo "cache-key=haos-image-$hash" >> "$GITHUB_OUTPUT"\n'
    assert script.count(start_marker) == 1, (
        f"{consumer} must have one cache-key command"
    )
    assert script.count(end_marker) == 1, (
        f"{consumer} must emit one HAOS image cache key"
    )
    start = script.index(start_marker)
    end = script.index(end_marker, start) + len(end_marker)
    return script[start:end]


def _beta_lane_jobs(
    workflow_dir: Path = _WORKFLOW_DIR,
) -> set[tuple[str, str, str]]:
    """Discover beta-image jobs independently from runtime attestation."""
    beta_jobs: set[tuple[str, str, str]] = set()
    workflow_paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    for path in workflow_paths:
        for job_id, job in _workflow(path)["jobs"].items():
            if not isinstance(job, dict):
                continue
            beta_signal = False
            mode: str | None = None
            for step in _job_steps(job):
                env = step.get("env", {})
                run = str(step.get("run", ""))
                step_text = str(step)
                if isinstance(env, dict):
                    if env.get("HAOS_BUILD_SUPERVISOR_CHANNEL") == "beta":
                        beta_signal = True
                    if isinstance(env.get("HAOS_TEST_MODE"), str):
                        mode = env["HAOS_TEST_MODE"]
                if (
                    "version.home-assistant.io/beta.json" in run
                    or "haos-beta-image-" in step_text
                    or "/tmp/haos-beta-test-image.qcow2" in step_text
                ):
                    beta_signal = True
            if beta_signal:
                beta_jobs.add((path.name, str(job_id), str(mode)))
    return beta_jobs


def test_haos_image_cache_key_command_matches_every_consumer() -> None:
    for path, job_id in _cache_key_consumers():
        consumer = f"{path.name}:{job_id}"
        assert _cache_key_command(path, job_id) == _CACHE_KEY_COMMAND, consumer


def test_cache_key_consumer_discovery_is_marker_independent(tmp_path: Path) -> None:
    workflow = """jobs:
  lane:
    steps:
      - uses: actions/cache/restore@pinned
        with:
          path: /tmp/haos-test-image.qcow2
          key: shared
"""
    expected = []
    for index in range(_CACHE_KEY_CONSUMER_FLOOR):
        path = tmp_path / f"lane-{index}.yaml"
        path.write_text(workflow, encoding="utf-8")
        expected.append((path, "lane"))

    assert _cache_key_consumers(tmp_path) == expected


def test_cache_key_consumer_discovery_tracks_jobs_individually(
    tmp_path: Path,
) -> None:
    path = tmp_path / "multi-lane.yaml"
    jobs = {
        f"lane-{index}": {
            "steps": [
                {
                    "uses": "actions/cache/restore@pinned",
                    "with": {"path": _HAOS_IMAGE_CACHE_PATH, "key": "shared"},
                }
            ]
        }
        for index in range(_CACHE_KEY_CONSUMER_FLOOR)
    }
    path.write_text(yaml.safe_dump({"jobs": jobs}), encoding="utf-8")

    assert _cache_key_consumers(tmp_path) == [
        (path, f"lane-{index}") for index in range(_CACHE_KEY_CONSUMER_FLOOR)
    ]


def test_beta_lane_discovery_does_not_depend_on_attestation(tmp_path: Path) -> None:
    """A beta build is discovered even when runtime attestation is missing."""
    workflow = {
        "jobs": {
            "beta": {
                "steps": [
                    {
                        "run": "curl https://version.home-assistant.io/beta.json",
                    },
                    {
                        "run": "pytest src/e2e",
                        "env": {"HAOS_TEST_MODE": "inaddon"},
                    },
                ]
            }
        }
    }
    path = tmp_path / "beta.yml"
    path.write_text(yaml.safe_dump(workflow), encoding="utf-8")

    assert _beta_lane_jobs(tmp_path) == {("beta.yml", "beta", "inaddon")}


def test_beta_lanes_share_a_current_supervisor_and_core_image() -> None:  # noqa: PLR0915
    """Both beta lanes bake the same beta OS, Supervisor, and Core.

    The lanes live in a single workflow file for each user-originated master
    update, nightly schedule, or manual dispatch, and each bakes its own qcow2.
    Neither touches the Actions cache: a ~4.9 GB image in a 10 GB repository
    budget evicted the stable qcow2 and the Home Assistant container images
    that the pull-request lanes restore (#2311).
    """
    lane_specs = (
        ("haos-e2e-inaddon-beta", "inaddon", "haos-e2e-inaddon"),
        ("haos-e2e-embedded-beta", "embedded", "haos-e2e-embedded"),
    )
    beta_resolvers: list[str] = []
    # The other beta.json consumers have no HAOS mode: the container beta
    # workflow's resolver (test_container_beta_lane_resolves_the_beta_core_image_once)
    # and pr.yml's beta unit lane (test_pr_unit_beta_lane_runs_the_suites_on_the_beta_core).
    assert _beta_lane_jobs() == {
        (_BETA_WORKFLOW, beta_job_id, mode)
        for beta_job_id, mode, _stable_job_id in lane_specs
    } | {
        (_BETA_WORKFLOW, "resolve-beta", "None"),
        (_CONTAINER_BETA_WORKFLOW, "resolve-beta", "None"),
        ("pr.yml", "unit-tests-beta", "None"),
    }

    beta_path = _WORKFLOW_DIR / _BETA_WORKFLOW
    assert beta_path.is_file(), "both beta lanes live in one workflow file"
    workflow = _workflow(beta_path)

    triggers = workflow[True]
    assert "pull_request" not in triggers, (
        "beta compatibility runs after changes land on master, not on every "
        "push to an open PR"
    )
    assert triggers["push"]["branches"] == ["master"]
    assert triggers["schedule"] == [{"cron": "17 3 * * *"}]
    assert workflow["concurrency"] == {
        "group": (
            "${{ github.workflow }}-${{ github.event_name == "
            "'workflow_dispatch' && github.run_id || 'full' }}"
        ),
        "cancel-in-progress": True,
    }
    # No `changes` classifier job (#2311): with pull_request gone every
    # surviving trigger is a trusted ref, so a docs-only classifier could only
    # ever compute run=true while burning a runner per nightly and per merge.
    assert "changes" not in workflow["jobs"], (
        "a docs-only classifier is dead weight here - no pull_request trigger "
        "means it can never authorize a skip (#2311)"
    )

    # The skip gate; its rationale is the resolve-beta job comment in the
    # workflow itself. Pinned here: both channels read, every value validated
    # (a failed parse must fail the job, never skip the lanes), and a manual
    # dispatch always runs.
    resolver = workflow["jobs"]["resolve-beta"]
    assert resolver["outputs"] == {
        "superfluous": "${{ steps.compare.outputs.superfluous }}"
    }
    compare = next(step for step in _job_steps(resolver) if step.get("id") == "compare")
    assert "version.home-assistant.io/beta.json" in compare["run"]
    assert "version.home-assistant.io/stable.json" in compare["run"]
    assert '["supervisor"]' in compare["run"]
    assert '["homeassistant"]["qemux86-64"]' in compare["run"]
    assert compare["run"].count("|| exit 1") == 6
    assert "::error::" in compare["run"]
    skip_guard = (
        "github.event_name == 'workflow_dispatch' || "
        "needs.resolve-beta.outputs.superfluous != 'true'"
    )

    for beta_job_id, mode, stable_job_id in lane_specs:
        job = workflow["jobs"][beta_job_id]

        if mode == "inaddon":
            assert job["needs"] == "resolve-beta"
            assert job["if"] == skip_guard
        else:
            assert job["needs"] == ["resolve-beta"], (
                "the embedded lane bakes its own image, so it must not wait "
                "on the inaddon lane (#2311)"
            )
            assert job["if"] == (
                "${{ !cancelled() && needs.resolve-beta.result == 'success' "
                f"&& ({skip_guard}) }}}}"
            ), (
                "the embedded lane must continue after a writer failure, but "
                "not after a superseding run cancels the workflow, and never "
                "when the compare job failed or found beta equal to stable"
            )
        steps = _job_steps(job)

        resolve = next(
            step
            for step in steps
            if step.get("name")
            == "Resolve current beta OS, Supervisor, and Core versions"
        )
        assert "version.home-assistant.io/beta.json" in resolve["run"]
        assert '["supervisor"]' in resolve["run"]
        assert '["homeassistant"]["qemux86-64"]' in resolve["run"]
        beta_resolvers.append(resolve["run"])

        assert not any(
            str(step.get("uses", "")).partition("@")[0] in _CACHE_ACTIONS
            for step in steps
        ), (
            f"{beta_job_id} must not use the Actions cache: a ~4.9 GB beta "
            "image evicts the stable qcow2 and the HA container images that "
            "the pull-request lanes restore (#2311)"
        )

        build = next(
            step for step in steps if step.get("name") == "Build the beta image"
        )
        assert "if" not in build, (
            f"{beta_job_id} bakes its own image on every run: a conditional here "
            "would leave the lane with no qcow2 (#2311)"
        )
        assert build["env"] == {
            "HAOS_BUILD_OS_VERSION": "${{ steps.versions.outputs.os_version }}",
            "HAOS_BUILD_SUPERVISOR_CHANNEL": "beta",
            "HAOS_BUILD_SUPERVISOR_MIN_VERSION": (
                "${{ steps.versions.outputs.supervisor_version }}"
            ),
            "HAOS_BUILD_CORE_VERSION": "${{ steps.versions.outputs.core_version }}",
        }

        run_step = next(
            step for step in steps if step.get("env", {}).get("HAOS_TEST_MODE")
        )
        run_env = run_step["env"]
        assert run_env["HAOS_TEST_MODE"] == mode
        assert run_env["HAOS_TEST_IMAGE_PATH"] == "/tmp/haos-beta-test-image.qcow2"
        assert run_env["HAOS_EXPECTED_OS_VERSION"] == (
            "${{ steps.versions.outputs.os_version }}"
        )
        assert run_env["HAOS_EXPECTED_SUPERVISOR_CHANNEL"] == "beta"
        assert run_env["HAOS_EXPECTED_SUPERVISOR_MIN_VERSION"] == (
            "${{ steps.versions.outputs.supervisor_version }}"
        )
        assert run_env["HAOS_EXPECTED_CORE_VERSION"] == (
            "${{ steps.versions.outputs.core_version }}"
        )
        assert run_env["PYTEST_PATHS"] == (
            "${{ github.event.inputs.pytest_paths || 'src/e2e/' }}"
        )
        assert run_env["PYTEST_ARGS"] == "${{ github.event.inputs.pytest_args }}"
        assert "$PYTEST_PATHS" in run_step["run"]
        assert "$PYTEST_ARGS" in run_step["run"]
        assert "${{ github.event.inputs.pytest_paths" not in run_step["run"]
        assert "${{ github.event.inputs.pytest_args" not in run_step["run"]
        assert "src/e2e/" in run_env["PYTEST_PATHS"]
        assert (
            workflow[True]["workflow_dispatch"]["inputs"]["pytest_paths"]["default"]
            == "src/e2e/"
        )

        if mode == "inaddon":
            diagnostics = next(
                step
                for step in steps
                if step.get("name", "").startswith("Extract HAOS")
            )
            diagnostics_script = diagnostics["run"]
            assert "targets=(/tmp/haos-beta-test-image-gw*.qcow2)" in diagnostics_script
            assert "targets+=(/tmp/haos-beta-test-image.qcow2)" in diagnostics_script

        stable = _workflow(_WORKFLOW_DIR / _STABLE_WORKFLOW)
        stable_steps = _job_steps(stable["jobs"][stable_job_id])
        stable_build = next(
            step
            for step in stable_steps
            if step.get("name") == "Build image locally (cache miss or forced rebuild)"
        )
        assert "env" not in stable_build

    assert beta_resolvers[0] == beta_resolvers[1]


def test_container_beta_lane_resolves_the_beta_core_image_once() -> None:
    """The container beta lanes run the current beta Core image (#2361).

    One resolver job reads the beta Core version and every test job pins its
    HA image to that job's output, so all five lanes test the same image and
    the stable renovate pin never leaks into a beta run.
    """
    path = _WORKFLOW_DIR / _CONTAINER_BETA_WORKFLOW
    assert path.is_file()
    workflow = _workflow(path)

    triggers = workflow[True]
    pr_jobs = _workflow(_WORKFLOW_DIR / "pr.yml")["jobs"]
    caller = pr_jobs["e2e-validation-beta"]
    assert caller["uses"] == "./.github/workflows/e2e-beta-tests.yml"
    assert caller["with"] == {"pr_lane": True}
    assert caller["if"] == pr_jobs["e2e-validation"]["if"]
    assert caller["needs"] == "changes"
    assert triggers["workflow_call"]["inputs"]["pr_lane"]["type"] == "boolean"
    assert triggers["push"] == {"branches": ["master"]}, (
        "no paths filter: a nightly must run regardless of the last commit"
    )
    assert triggers["schedule"] == [{"cron": "37 3 * * *"}]
    assert set(triggers["workflow_dispatch"]["inputs"]) == {
        "pytest_args",
        "pytest_paths",
    }
    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert "changes" not in workflow["jobs"]
    assert "HA_IMAGE_GHCR" not in workflow.get("env", {}), (
        "the beta workflow must not carry the stable image pin"
    )

    resolver = workflow["jobs"]["resolve-beta"]
    resolve = next(
        step
        for step in _job_steps(resolver)
        if "version.home-assistant.io/beta.json" in str(step.get("run", ""))
    )
    assert '["homeassistant"]["default"]' in resolve["run"], (
        "the container lanes want the multi-arch docker tag, not qemux86-64"
    )
    assert "image=ghcr.io/home-assistant/home-assistant:$core_version" in resolve["run"]
    assert resolver["outputs"]["core_version"] == (
        "${{ steps.versions.outputs.core_version }}"
    )
    assert resolver["outputs"]["image"] == "${{ steps.versions.outputs.image }}"
    # The skip compares against the image the E2E fixtures actually build
    # from (tests/test_constants.HA_TEST_IMAGE's default), not stable.json:
    # while the Renovate bump lags a release, beta equals the new stable and
    # this lane is the only container lane on it. The resolver reads that
    # literal with sed and aborts on an empty result, so a reformatted pin
    # can never reach the comparison; pinning the extraction here surfaces
    # such a reformat on the pull request instead of as a failed resolver on
    # the next push or nightly.
    assert "tests/test_constants.py" in resolve["run"]
    assert "superfluous=$superfluous" in resolve["run"]
    constants = (_REPO_ROOT / "tests" / "test_constants.py").read_text(encoding="utf-8")
    pins = re.findall(
        r'^_DEFAULT_HA_TEST_IMAGE = "ghcr\.io/home-assistant/home-assistant:(.*)"$',
        constants,
        flags=re.MULTILINE,
    )
    assert len(pins) == 1 and re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", pins[0]), (
        f"the resolver's sed over tests/test_constants.py would not find one pin: {pins}"
    )
    # The workflow-level pins are pre-pull and cache-key inputs for the same
    # image; Renovate moves all four together, and the skip's premise (the
    # stable lanes ran exactly this image) holds only while they agree.
    for pinned_workflow in ("pr.yml", "e2e-tests.yml", "performance-tests.yml"):
        text = (_WORKFLOW_DIR / pinned_workflow).read_text(encoding="utf-8")
        workflow_pins = re.findall(
            r'^  HA_IMAGE_GHCR: "ghcr\.io/home-assistant/home-assistant:(.*)"$',
            text,
            flags=re.MULTILINE,
        )
        assert workflow_pins == pins, (
            f"{pinned_workflow} pins {workflow_pins} but the fixtures run {pins}"
        )
    checkout = _job_steps(resolver)[0]
    assert str(checkout.get("uses", "")).startswith("actions/checkout@")
    assert checkout["with"]["sparse-checkout"] == "tests/test_constants.py"
    # A single-file pattern needs non-cone mode; actions/checkout defaults
    # cone mode to true, which would leave the file out of the checkout, the
    # sed would find nothing, and the resolver's empty-pin check would abort
    # the job. The pin protects the job, not the comparison.
    assert checkout["with"]["sparse-checkout-cone-mode"] is False

    image_ref = "${{ needs.resolve-beta.outputs.image }}"
    test_jobs = {
        job_id: job
        for job_id, job in workflow["jobs"].items()
        if job_id != "resolve-beta"
    }
    assert set(test_jobs) == {
        "e2e-tests",
        "e2e-tests-no-component",
        "e2e-tests-embedded",
        "e2e-tests-embedded-server-only",
        "e2e-tests-update-path",
    }, "the beta workflow mirrors e2e-tests.yml job for job"
    for job_id, job in test_jobs.items():
        assert job["needs"] == "resolve-beta", job_id
        assert job["env"]["HA_IMAGE_GHCR"] == image_ref, job_id
        assert job["env"]["HA_TEST_IMAGE"] == image_ref, (
            f"{job_id}: the E2E fixtures build their container from "
            "tests/test_constants.HA_TEST_IMAGE, which only the environment "
            "can override"
        )
        assert not any(
            "version.home-assistant.io/beta.json" in str(step.get("run", ""))
            for step in _job_steps(job)
        ), f"{job_id} must not resolve the beta version a second time"


@pytest.mark.parametrize(
    ("channel", "pin", "active", "duplicate"),
    [
        ("2026.12.0b1", "2026.11.3", "true", "false"),
        ("2026.12.0", "2026.11.3", "false", "false"),
        ("2026.12.0", "2026.12.0", "false", "true"),
        ("2027.1.0b1", "2026.12.0", "true", "false"),
        ("2027.1.0.dev202612010300", "2026.12.0", "true", "false"),
    ],
)
def test_container_beta_resolver_across_stable_promotion(
    tmp_path: Path, channel: str, pin: str, active: str, duplicate: str
) -> None:
    """Pull requests run the beta Docker job only for an actual beta, and the
    full lanes skip only the image the stable lanes already run, through the
    weeks a stable release spends in the beta channel."""
    workflow = _workflow(_WORKFLOW_DIR / _CONTAINER_BETA_WORKFLOW)
    steps = _job_steps(workflow["jobs"]["resolve-beta"])
    resolver = next(step["run"] for step in steps if step.get("id") == "versions")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_constants.py").write_text(
        f'_DEFAULT_HA_TEST_IMAGE = "ghcr.io/home-assistant/home-assistant:{pin}"\n'
    )
    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-ec", 'curl() { echo "$CHANNEL"; }\n' + resolver],
        cwd=tmp_path,
        env={
            **os.environ,
            "CHANNEL": json.dumps({"homeassistant": {"default": channel}}),
            "GITHUB_OUTPUT": str(output),
        },
        check=True,
    )
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
        "core_version": channel,
        "image": f"ghcr.io/home-assistant/home-assistant:{channel}",
        "active_beta": active,
        "superfluous": duplicate,
    }


def _actions_value(node: ast.expr, context: dict[str, Any]) -> Any:
    """One node of a GitHub Actions expression, parsed as Python."""
    if isinstance(node, ast.BoolOp):
        value = _actions_value(node.values[0], context)
        for operand in node.values[1:]:
            if bool(value) == isinstance(node.op, ast.Or):
                return value
            value = _actions_value(operand, context)
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _actions_value(node.operand, context)
    if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.Eq):
        left, right = node.left, node.comparators[0]
        return _actions_value(left, context) == _actions_value(right, context)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return context.get(node.id)
    raise AssertionError(f"unsupported expression node {ast.dump(node)}")


def _actions_expression(text: str, context: dict[str, Any]) -> Any:
    """Evaluate the expressions these workflows use (``&&``, ``||``, ``!``,
    ``==``, quoted strings and dotted context names; a name not in
    ``context`` is null, as an unset input is)."""
    names: dict[str, str] = {}

    def name(match: re.Match[str]) -> str:
        return names.setdefault(match.group(), f"v{len(names)}")

    body = text.strip().removeprefix("${{").removesuffix("}}")
    body = re.sub(r"(?<!')\b[A-Za-z_][\w-]*(?:\.[\w-]+)+", name, body)
    body = body.replace("&&", " and ").replace("||", " or ")
    body = re.sub(r"!(?!=)", " not ", body)
    values = {alias: context.get(dotted) for dotted, alias in names.items()}
    return _actions_value(ast.parse(body.strip(), mode="eval").body, values)


@pytest.mark.parametrize(
    ("event", "pr_lane", "active", "duplicate", "running"),
    [
        ("pull_request", True, "true", "false", {"e2e-tests"}),
        ("pull_request", True, "false", "false", set()),
        ("pull_request", True, "true", "true", set()),
        ("workflow_dispatch", True, "true", "false", {"e2e-tests"}),
        ("push", None, "true", "false", "all"),
        ("schedule", None, "false", "false", "all"),
        ("push", None, "false", "true", set()),
        ("workflow_dispatch", None, "false", "true", "all"),
    ],
)
def test_container_beta_jobs_a_pull_request_and_a_full_run_start(
    event: str, pr_lane: bool | None, active: str, duplicate: str, running: Any
) -> None:
    """A pull request runs only the external Docker job and only for an
    actual beta the stable lanes do not run; push, nightly and dispatch runs
    keep the whole suite, and a pull request's run cancels only its own."""
    workflow = _workflow(_WORKFLOW_DIR / _CONTAINER_BETA_WORKFLOW)
    context: dict[str, Any] = {
        "github.event_name": event,
        "github.run_id": "4242",
        "inputs.pr_lane": pr_lane,
        "steps.versions.outputs.active_beta": active,
        "steps.versions.outputs.superfluous": duplicate,
    }
    outputs = workflow["jobs"]["resolve-beta"]["outputs"]
    for output in ("run_full", "run_pr_lane"):
        value = _actions_expression(outputs[output], context)
        context[f"needs.resolve-beta.outputs.{output}"] = str(value).lower()
    jobs = {
        job_id
        for job_id, job in workflow["jobs"].items()
        if job_id != "resolve-beta" and _actions_expression(job["if"], context)
    }
    test_jobs = set(workflow["jobs"]) - {"resolve-beta"}
    assert jobs == (test_jobs if running == "all" else running)

    group = workflow["concurrency"]["group"]
    prefix, expression = group.split("${{", 1)
    full_run = event in ("push", "schedule")
    assert prefix + str(_actions_expression(expression, context)) == (
        f"{prefix}full" if full_run else f"{prefix}4242"
    )


def test_pr_unit_beta_lane_runs_the_suites_on_the_beta_core() -> None:
    """The beta unit lane tests the beta Core, and only while it differs.

    One resolver step reads the beta Core and compares it with the dev group's
    Core pin; every later step is gated on that comparison and installs or
    runs against the resolved version. The suites run with --no-sync: a plain
    `uv run` re-syncs the venv to the lock and would test the stable Core
    again while reporting a beta run.
    """
    job = _workflow(_WORKFLOW_DIR / "pr.yml")["jobs"]["unit-tests-beta"]
    steps = _job_steps(job)
    resolve = next(step for step in steps if step.get("id") == "beta")
    assert "version.home-assistant.io/beta.json" in resolve["run"]
    assert "pyproject.toml" in resolve["run"], (
        "the skip compares with the dev group's pin"
    )

    later = steps[steps.index(resolve) + 1 :]
    skip_guard = (
        "github.event_name == 'workflow_dispatch' || "
        "steps.beta.outputs.superfluous != 'true'"
    )
    install = next(step for step in later if step.get("id") == "install")
    assert install["if"] == skip_guard
    assert install["env"]["BETA_CORE"] == "${{ steps.beta.outputs.version }}"
    for package in ("homeassistant", "pytest-homeassistant"):
        assert f'"{package}==$BETA_CORE"' in install["run"], package

    suites = [step for step in later if "pytest tests/" in str(step.get("run", ""))]
    assert {step["run"].split()[4] for step in suites} == {
        "tests/src/unit/",
        "tests/addon/",
    }
    for step in suites:
        assert step["run"].startswith("uv run --no-sync pytest "), step["name"]
        assert step["if"] in (
            skip_guard,
            "${{ !cancelled() && steps.install.outcome == 'success' "
            "&& matrix.shard == '1/2' }}",
        ), step["name"]
