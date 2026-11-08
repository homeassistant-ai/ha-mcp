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

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
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


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("zip_state", ["missing", "empty", "uploading", "uploaded"])
@pytest.mark.parametrize("zip_release", [False, True])
def test_stable_publication_waits_for_the_pinned_package(
    tmp_path, available, zip_state, zip_release
):
    release = yaml.safe_load(
        (_WORKFLOW_DIR / "release-publish.yml").read_text(encoding="utf-8")
    )
    steps = release["jobs"]["publish_pypi"]["steps"]
    names = [step.get("name") for step in steps]
    assert "Publish GitHub release" not in [
        step.get("name") for step in _workflow()["jobs"]["semantic-release"]["steps"]
    ]
    assert names.index("Publish to PyPI") < names.index("Wait for the build on PyPI")
    assert names.index("Wait for the build on PyPI") < names.index(
        "Publish GitHub release"
    )
    calls = tmp_path / "calls"
    assets = tmp_path / "assets.json"
    (tmp_path / "hacs.json").write_text(json.dumps({"zip_release": zip_release}))
    assets.write_text(
        json.dumps(
            {
                "assets": []
                if zip_state == "missing"
                else [
                    {
                        "name": "ha_mcp_tools.zip",
                        "state": "uploaded" if zip_state != "uploading" else "new",
                        "size": 0 if zip_state == "empty" else 100,
                    }
                ]
            }
        )
    )
    stubs = (
        'curl() { [ "$AVAILABLE" = 1 ]; }\n'
        "sleep() { :; }\n"
        'gh() { if [ "$1 $2" = "release view" ]; then '
        'while [ "$#" -gt 0 ]; do if [ "$1" = --jq ]; then jq -r "$2" "$ASSETS"; return; fi; shift; done; return 99; fi; '
        'printf "%s\\n" "$*" >> "$CALLS"; }\n'
        'git() { if [ "$1" = rev-parse ]; then echo release-commit; else printf "%s\\n" "$*" >> "$CALLS"; fi; }\n'
    )
    for name in ("Wait for the build on PyPI", "Publish GitHub release"):
        step = next(step for step in steps if step.get("name") == name)
        assert step.get("if") is None, (
            "The default success gate must protect visibility and publication together"
        )
        result = subprocess.run(
            [
                shutil.which("bash") or "bash",
                "-e",
                "-c",
                stubs
                + step["run"].replace(
                    "python3 ", shlex.quote(Path(sys.executable).as_posix()) + " "
                ),
            ],
            cwd=tmp_path,
            env={
                **os.environ,
                "BASH_ENV": os.devnull,
                "AVAILABLE": "1" if available else "0",
                "VERSION": "1.2.3",
                "TAG": "v1.2.3",
                "CALLS": calls.as_posix(),
                "ASSETS": assets.as_posix(),
                "GITHUB_REPOSITORY": "owner/repo",
                "GH_TOKEN": "test-token",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if result.returncode:
            break
    ready = available and (not zip_release or zip_state == "uploaded")
    assert (result.returncode == 0) is ready, result.stderr
    if ready:
        assert calls.read_text().splitlines() == [
            "release edit v1.2.3 --draft=false",
            "push --force https://x-access-token:test-token@github.com/owner/repo.git release-commit:refs/tags/stable",
        ]
    else:
        assert not calls.exists(), (
            "An unavailable package or ZIP must leave the release and stable ref unchanged"
        )


def test_stable_alias_moves_only_after_the_release_becomes_public():
    assert "Update stable git tag" not in [
        step.get("name") for step in _workflow()["jobs"]["semantic-release"]["steps"]
    ]
    release = yaml.safe_load((_WORKFLOW_DIR / "release-publish.yml").read_text())
    publish = next(
        step["run"]
        for step in release["jobs"]["publish_pypi"]["steps"]
        if step.get("name") == "Publish GitHub release"
    )
    assert publish.index("--draft=false") < publish.index("refs/tags/stable")


@pytest.mark.parametrize("trigger", ["release", "noop", "at_tag", "manual"])
def test_new_master_commits_do_not_hide_a_release_cut_by_semver(tmp_path, trigger):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q", "-b", "master")
    git("config", "user.name", "Release test")
    git("config", "user.email", "release@example.invalid")
    (tmp_path / "pyproject.toml").write_text('version = "1.2.3"\n')
    git("add", ".")
    git("commit", "-qm", "fix: release input")
    release_input = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "chore(release): 1.2.3")
    git("tag", "v1.2.3")
    release_commit = git("rev-parse", "HEAD")
    git(
        "commit", "--allow-empty", "-qm", "fix: ordinary PR merged during the app build"
    )
    step = next(
        s
        for s in yaml.safe_load((_WORKFLOW_DIR / "release-publish.yml").read_text())[
            "jobs"
        ]["prepare"]["steps"]
        if s.get("id") == "version"
    )
    output = tmp_path / "output"
    run = step["run"].replace(
        "${{ github.event_name }}",
        "workflow_dispatch" if trigger == "manual" else "workflow_run",
    )
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", run],
        check=False,
        cwd=tmp_path,
        env={
            **os.environ,
            "BASH_ENV": os.devnull,
            "MANUAL": "true" if trigger == "manual" else "false",
            "TRIGGER_SHA": {
                "release": release_input,
                "noop": git("rev-parse", "HEAD"),
                "at_tag": release_commit,
                "manual": "",
            }[trigger],
            "GITHUB_OUTPUT": output.as_posix(),
            "GITHUB_STEP_SUMMARY": (tmp_path / "summary").as_posix(),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert (
        f"should_publish={'true' if trigger in {'release', 'manual'} else 'false'}"
        in output.read_text()
    )
    assert "release_tag=v1.2.3" in output.read_text()


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


def test_release_publish_uses_the_same_releasable_commit_matcher() -> None:
    publish = yaml.safe_load(
        (_WORKFLOW_DIR / "release-publish.yml").read_text(encoding="utf-8")
    )
    steps = publish["jobs"]["prepare"]["steps"]
    matches = [step for step in steps if step.get("id") == "version"]
    assert len(matches) == 1, (
        "release-publish.yml must have exactly one 'version' step in job 'prepare'"
    )

    run = matches[0]["run"]
    for pattern in (
        "^(feat|fix|perf|refactor)(\\(.+\\))?!?:",
        "^[a-z]+(\\(.+\\))?!:",
        "^BREAKING[ -]CHANGE:",
    ):
        assert f"--grep='{pattern}'" in run


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
