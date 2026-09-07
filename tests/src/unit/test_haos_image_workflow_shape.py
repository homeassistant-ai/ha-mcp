"""Guard shared HAOS workflow contracts without restructuring proven lanes."""

import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
# Stable and beta HAOS lanes each live in one consolidated workflow file
# (#2292/#2302). Stable PR lanes share one `changes` classifier; both beta
# lanes participate in every surviving workflow event.
_STABLE_WORKFLOW = "haos-e2e-tests.yml"
_BETA_WORKFLOW = "haos-e2e-beta-tests.yml"
_CONTAINER_BETA_WORKFLOW = "e2e-beta-tests.yml"
# 7 actual consumers (the six HAOS lanes + the image builder); the floor is what
# catches a lane that silently loses its image-cache step.
_CACHE_KEY_CONSUMER_FLOOR = 7
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
  | sha256sum | cut -d' ' -f1 | head -c16)
echo "cache-key=haos-image-$hash" >> "$GITHUB_OUTPUT"
"""


def _workflow(path: Path) -> dict[str, Any]:
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
        and _HAOS_IMAGE_CACHE_PATH
        in str(step.get("with", {}).get("path", "")).splitlines()
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


def test_beta_lanes_share_a_current_supervisor_and_core_image() -> None:
    """Both beta lanes share one workflow and one manifest-keyed qcow2 cache.

    The lanes live in a single workflow file for each user-originated master
    update, nightly schedule, or manual dispatch. The cache-writing inaddon
    lane runs first, then the embedded lane consumes its image.
    """
    lane_specs = (
        ("haos-e2e-inaddon-beta", "inaddon", "haos-e2e-inaddon"),
        ("haos-e2e-embedded-beta", "embedded", "haos-e2e-embedded"),
    )
    beta_resolvers: list[str] = []
    beta_cache_keys: list[str] = []
    # The container beta workflow's resolver job is the only other beta.json
    # consumer; it has no HAOS mode and is pinned down by
    # test_container_beta_lane_resolves_the_beta_core_image_once below.
    assert _beta_lane_jobs() == {
        (_BETA_WORKFLOW, beta_job_id, mode)
        for beta_job_id, mode, _stable_job_id in lane_specs
    } | {
        (_BETA_WORKFLOW, "resolve-beta", "None"),
        (_CONTAINER_BETA_WORKFLOW, "resolve-beta", "None"),
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
            assert job["needs"] == ["resolve-beta", "haos-e2e-inaddon-beta"], (
                "the embedded lane must wait for the sole cache writer"
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

        cache_key = next(
            step for step in steps if step.get("name") == "Compute beta image cache key"
        )
        cache_script = cache_key["run"]
        assert "haos-beta-image-" in cache_script
        assert "steps.versions.outputs.supervisor_version" in cache_script
        assert "steps.versions.outputs.core_version" in cache_script
        assert "steps.versions.outputs.os_version" in cache_script
        beta_cache_keys.append(cache_script)

        build = next(
            step
            for step in steps
            if step.get("name") == "Build image locally (cache miss or forced rebuild)"
        )
        assert build["env"] == {
            "HAOS_BUILD_OS_VERSION": "${{ steps.versions.outputs.os_version }}",
            "HAOS_BUILD_SUPERVISOR_CHANNEL": "beta",
            "HAOS_BUILD_SUPERVISOR_MIN_VERSION": (
                "${{ steps.versions.outputs.supervisor_version }}"
            ),
            "HAOS_BUILD_CORE_VERSION": "${{ steps.versions.outputs.core_version }}",
        }

        restore = next(
            step for step in steps if step.get("name") == "Restore image from cache"
        )
        assert restore["with"]["path"] == "/tmp/haos-beta-test-image.qcow2"

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
    assert beta_cache_keys[0] == beta_cache_keys[1]


def test_container_beta_lane_resolves_the_beta_core_image_once() -> None:
    """The container beta lanes mirror the HAOS beta trigger shape (#2361).

    One resolver job reads the beta Core version and every test job pins its
    HA image to that job's output, so all five lanes test the same image and
    the stable renovate pin never leaks into a beta run.
    """
    path = _WORKFLOW_DIR / _CONTAINER_BETA_WORKFLOW
    assert path.is_file()
    workflow = _workflow(path)

    triggers = workflow[True]
    assert "pull_request" not in triggers, (
        "beta compatibility runs after changes land on master, not on every "
        "push to an open PR"
    )
    assert triggers["push"] == {"branches": ["master"]}, (
        "no paths filter: a nightly must run regardless of the last commit"
    )
    assert triggers["schedule"] == [{"cron": "37 3 * * *"}]
    assert set(triggers["workflow_dispatch"]["inputs"]) == {
        "pytest_args",
        "pytest_paths",
    }
    assert workflow["concurrency"] == {
        "group": (
            "${{ github.workflow }}-${{ github.event_name == "
            "'workflow_dispatch' && github.run_id || 'full' }}"
        ),
        "cancel-in-progress": True,
    }
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
    assert resolver["outputs"] == {
        "core_version": "${{ steps.versions.outputs.core_version }}",
        "image": "${{ steps.versions.outputs.image }}",
        "superfluous": "${{ steps.versions.outputs.superfluous }}",
    }
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
    skip_guard = (
        "github.event_name == 'workflow_dispatch' || "
        "needs.resolve-beta.outputs.superfluous != 'true'"
    )
    for job_id, job in test_jobs.items():
        assert job["needs"] == "resolve-beta", job_id
        assert job["if"] == skip_guard, (
            f"{job_id} must be skipped when beta equals the stable pin, except "
            "on a manual dispatch"
        )
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
