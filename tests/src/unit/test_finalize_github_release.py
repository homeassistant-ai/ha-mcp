"""Stable aliases/releases cannot expose missing package or integration assets."""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "finalize_release", _ROOT / "scripts/finalize_github_release.py"
)
assert _SPEC and _SPEC.loader
publisher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(publisher)

_HACS = {"zip_release": True, "filename": "ha_mcp_tools.zip"}
_COMPONENT = {"version": "9.1.1", "requirements": ["mcp>=1.24.0", "ha-mcp==9.1.1"]}


def _world(
    monkeypatch,
    *,
    ready=True,
    asset=True,
    draft=True,
    current="previous",
    missing_ref=False,
    comparison="ahead",
    fail_publish=False,
):
    calls = []
    monkeypatch.setattr(publisher, "_pypi_ready", lambda version: ready)

    def gh(endpoint, *, method="GET", fields=None):
        calls.append((endpoint, method, fields))
        if method != "GET":
            if fail_publish and "/releases/" in endpoint:
                raise RuntimeError("publication failed")
            return {}
        if "/compare/" in endpoint:
            return {"status": comparison}
        if "/releases/tags/" in endpoint:
            return {
                "id": 123,
                "tag_name": "v9.1.1",
                "draft": draft,
                "prerelease": False,
                "assets": [
                    {
                        "name": "ha_mcp_tools.zip",
                        "state": "uploaded",
                        "size": 42,
                        "digest": "sha256:" + "a" * 64,
                    }
                ]
                if asset
                else [],
            }
        if "/git/ref/tags/" in endpoint:
            return {"object": {"sha": "version-tag-object"}}
        if "/matching-refs/" in endpoint:
            return (
                []
                if missing_ref
                else [{"ref": "refs/tags/stable", "object": {"sha": current}}]
            )
        raise AssertionError(endpoint)

    monkeypatch.setattr(publisher, "_gh_json", gh)
    return calls


def test_missing_pypi_performs_no_github_write(monkeypatch):
    calls = _world(monkeypatch, ready=False)
    with pytest.raises(TimeoutError, match="release stays draft"):
        publisher.finalize_release(
            "o/r", "v9.1.1", _HACS, _COMPONENT, attempts=1, delay=0
        )
    assert calls == []


def test_missing_zip_leaves_release_and_stable_alias_untouched(monkeypatch):
    calls = _world(monkeypatch, asset=False)
    with pytest.raises(ValueError, match="ZIP is not ready"):
        publisher.finalize_release("o/r", "v9.1.1", _HACS, _COMPONENT)
    assert all(method == "GET" for _, method, _ in calls)


def test_publish_follows_asset_gate_and_retains_the_version_tag_object(monkeypatch):
    calls = _world(monkeypatch)
    publisher.finalize_release("o/r", "v9.1.1", _HACS, _COMPONENT)
    writes = [
        (endpoint, fields) for endpoint, method, fields in calls if method != "GET"
    ]
    assert writes == [
        ("repos/o/r/releases/123", {"draft": False}),
        (
            "repos/o/r/git/refs/tags/stable",
            {"sha": "version-tag-object", "force": True},
        ),
    ]


def test_an_old_retry_cannot_roll_back_the_stable_alias(monkeypatch):
    calls = _world(monkeypatch, comparison="behind")
    with pytest.raises(ValueError, match="advance"):
        publisher.finalize_release("o/r", "v9.1.1", _HACS, _COMPONENT)
    assert all(method == "GET" for _, method, _ in calls)


def test_failed_publication_keeps_the_existing_stable_alias(monkeypatch):
    calls = _world(monkeypatch, fail_publish=True)
    with pytest.raises(RuntimeError, match="publication failed"):
        publisher.finalize_release("o/r", "v9.1.1", _HACS, _COMPONENT)
    assert not any(
        "/git/" in endpoint and method != "GET" for endpoint, method, _ in calls
    )


def test_recovery_of_an_already_published_release_is_idempotent(monkeypatch):
    calls = _world(monkeypatch, draft=False, current="version-tag-object")
    publisher.finalize_release("o/r", "v9.1.1", _HACS, _COMPONENT)
    assert all(method == "GET" for _, method, _ in calls)


def test_legacy_archive_release_does_not_require_a_new_zip(monkeypatch):
    calls = _world(monkeypatch, asset=False, missing_ref=True)
    result = publisher.finalize_release("o/r", "v9.1.1", {}, {})
    assert result["zip_required"] is False
    assert any(
        method == "POST" and fields["ref"] == "refs/tags/stable"
        for _, method, fields in calls
    )


def test_a_dev_or_mismatched_pin_is_not_finalized_as_stable(monkeypatch):
    calls = _world(monkeypatch)
    component = {"version": "9.1.1", "requirements": ["ha-mcp-dev==9.1.1.dev4"]}
    with pytest.raises(ValueError, match="server pin"):
        publisher.finalize_release("o/r", "v9.1.1", _HACS, component)
    assert calls == []


def test_finalization_is_downstream_of_pypi_without_a_semver_wait_cycle():
    workflows = _ROOT / ".github/workflows"
    semver = yaml.safe_load((workflows / "semver-release.yml").read_text())
    release = yaml.safe_load((workflows / "release-publish.yml").read_text())
    steps = semver["jobs"]["semantic-release"]["steps"]
    assert not any(
        s.get("name") in {"Update stable git tag", "Publish GitHub release"}
        for s in steps
    )
    finalize = release["jobs"]["finalize_github_release"]
    assert "publish_pypi" in finalize["needs"]
    assert "refs/heads/master" in finalize["if"]
    assert "homeassistant-ai/ha-mcp" in finalize["if"]
    assert release[True]["workflow_run"]["workflows"] == ["SemVer Release"]
    # The helper belongs to the workflow revision, not a legacy release tag
    # which predates this script; manifests still come from the released tag.
    checkouts = [
        s["with"]
        for s in finalize["steps"]
        if s.get("uses", "").startswith("actions/checkout@")
    ]
    assert any("github.workflow_sha" in c["ref"] for c in checkouts)
    assert any("release_tag" in c["ref"] for c in checkouts)
    deploy = yaml.safe_load((workflows / "deploy.yml").read_text())
    assert deploy[True]["workflow_run"]["workflows"] == ["Release Publish"]


def test_both_hacs_manifests_select_the_published_component_asset():
    import json

    for name in ("hacs.json", "custom_components/ha_mcp_tools/hacs.json"):
        manifest = json.loads((_ROOT / name).read_text())
        assert manifest["zip_release"]
        assert manifest["filename"] == "ha_mcp_tools.zip"


@pytest.mark.parametrize("scenario", ["master-advanced", "no-release", "old-retry"])
def test_release_prepare_uses_the_producing_run_and_rejects_old_retries(
    tmp_path, scenario
):
    """A later source commit cannot strand a draft or select another release."""

    def git(*args):
        subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
        )

    git("init", "-q", "-b", "master")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.com")
    for version in ("1.0.0", "1.1.0", "1.2.0"):
        (tmp_path / "pyproject.toml").write_text(f'version = "{version}"\n')
        git("add", "pyproject.toml")
        git("commit", "-qm", "feat: advance master")
        git("tag", f"v{version}")
    git("tag", "stable", "v1.2.0" if scenario == "old-retry" else "v1.0.0")
    output = tmp_path / "output"
    env = {
        **os.environ,
        "GITHUB_OUTPUT": output.as_posix(),
        "RUNNER_TEMP": tmp_path.as_posix(),
        "GITHUB_REPOSITORY": "o/r",
        "EVENT_NAME": "workflow_run",
        "TRIGGER_RUN_ID": "42",
    }
    stub = (
        'gh() { case "$1 $2" in\n'
        f'"api repos/o/r/actions/runs/42/artifacts") echo {0 if scenario == "no-release" else 1};;\n'
        '"run download") mkdir -p "$RUNNER_TEMP/release-metadata"; '
        'printf "v1.1.0\\n" > "$RUNNER_TEMP/release-metadata/release-tag.txt";;\n'
        "*) return 2;; esac; }\n"
    )
    workflow = yaml.safe_load(
        (_ROOT / ".github/workflows/release-publish.yml").read_text()
    )
    for step_id in ("source", "version"):
        step = next(
            s for s in workflow["jobs"]["prepare"]["steps"] if s.get("id") == step_id
        )
        result = subprocess.run(
            [shutil.which("bash") or "bash", "-e", "-c", stub + step["run"]],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        env["RELEASE_TAG"] = values.get("tag", "")
    assert values["should_publish"] == (
        "true" if scenario == "master-advanced" else "false"
    )
    if scenario == "master-advanced":
        assert values["release_tag"] == "v1.1.0" and values["version"] == "1.1.0"
