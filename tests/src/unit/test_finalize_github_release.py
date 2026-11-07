"""Stable aliases/releases cannot expose missing package or integration assets."""

import functools
import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import ClassVar

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
_ASSET = {
    "name": "ha_mcp_tools.zip",
    "state": "uploaded",
    "size": 42,
    "digest": "sha256:" + "a" * 64,
}
_COMPONENT = {"version": "9.1.1", "requirements": ["mcp>=1.24.0", "ha-mcp==9.1.1"]}
_PROJECT = {
    "requires-python": ">=3.13,<3.15",
    "classifiers": [
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
    ],
}
_finalize = functools.partial(publisher.finalize_release, project=_PROJECT)


@pytest.mark.parametrize(
    "case", ["available", "no-files", "wrong-python", "removed-file"]
)
def test_pypi_metadata_cannot_expose_an_uninstallable_pin(monkeypatch, case):
    distribution = {
        "filename": "ha_mcp-9.1.1-py3-none-any.whl",
        "packagetype": "bdist_wheel",
        "url": "https://files.pythonhosted.org/packages/test/ha_mcp-9.1.1-py3-none-any.whl",
        "requires_python": ">=3.13,<3.14" if case == "wrong-python" else ">=3.13,<3.15",
        "size": 42,
        "digests": {"sha256": "a" * 64},
        "yanked": False,
    }
    metadata = {
        "info": {"version": "9.1.1"},
        "urls": [] if case == "no-files" else [distribution],
    }

    class Response(io.BytesIO):
        status = 200
        headers: ClassVar[dict] = {"Content-Length": "42"}

    def urlopen(request, timeout):
        if request.get_method() == "HEAD":
            if case == "removed-file":
                raise OSError("distribution no longer served")
            return Response(b"")
        return Response(json.dumps(metadata).encode())

    monkeypatch.setattr(publisher.urllib.request, "urlopen", urlopen)
    assert publisher._pypi_ready("9.1.1", publisher._supported_pythons(_PROJECT)) is (
        case == "available"
    )


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
    annotated=True,
    target="version-tag-object",
    release_asset=_ASSET,
):
    calls = []
    monkeypatch.setattr(publisher, "_pypi_ready", lambda version, pythons: ready)
    monkeypatch.setattr(publisher, "_verify_release_zip", lambda *args: None)

    def push(remote, checkout, target, expected):
        calls.append(("git push", "PUSH", {"sha": target, "expected": expected}))

    monkeypatch.setattr(publisher, "_push_stable_ref", push)

    def gh(endpoint, *, method="GET", fields=None, paginate=False):
        calls.append((endpoint, method, fields))
        if method != "GET":
            if fail_publish and "/releases/" in endpoint:
                raise RuntimeError("publication failed")
            return {}
        if "/compare/" in endpoint:
            suffix = "-commit" if annotated else ""
            assert endpoint == f"repos/o/r/compare/{current}{suffix}...{target}{suffix}"
            return {"status": comparison}
        if "/git/tags/" in endpoint:
            return {
                "object": {
                    "type": "commit",
                    "sha": endpoint.rsplit("/", 1)[1] + "-commit",
                }
            }
        if endpoint.endswith("/releases?per_page=100"):
            release = {
                "id": 123,
                "tag_name": "v9.1.1",
                "draft": draft,
                "prerelease": False,
                "assets": [release_asset] if asset else [],
            }
            assert paginate
            return [[release]]
        if "/git/ref/tags/" in endpoint:
            return {
                "object": {
                    "sha": target,
                    "type": "tag" if annotated else "commit",
                }
            }
        if "/matching-refs/" in endpoint:
            return (
                []
                if missing_ref
                else [
                    {
                        "ref": "refs/tags/stable",
                        "object": {
                            "sha": current,
                            "type": "tag" if annotated else "commit",
                        },
                    }
                ]
            )
        raise AssertionError(endpoint)

    monkeypatch.setattr(publisher, "_gh_json", gh)
    return calls


@pytest.mark.parametrize("fault", [None, "digest", "members", "checkout"])
def test_incorrect_uploaded_zip_cannot_publish_or_move_stable(
    tmp_path, monkeypatch, fault
):
    checkout = tmp_path / "source"
    component = checkout / "custom_components/ha_mcp_tools"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text(
        json.dumps({"domain": "ha_mcp_tools", "version": "9.1.1"})
    )
    (component / "__init__.py").write_text("VALUE = 1\n")
    for args in (
        ("init", "-q"),
        ("config", "user.name", "test"),
        ("config", "user.email", "test@example.invalid"),
        ("add", "."),
        ("commit", "-qm", "release snapshot"),
    ):
        subprocess.run(["git", "-C", str(checkout), *args], check=True)
    commit = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if fault == "checkout":
        (component / "__init__.py").write_text("VALUE = 2\n")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as target:
        for path in component.iterdir():
            target.writestr(
                path.name,
                b"VALUE = 2\n"
                if fault == "members" and path.name == "__init__.py"
                else path.read_bytes(),
            )
    payload = archive.getvalue()
    metadata = {
        "id": 456,
        "name": "ha_mcp_tools.zip",
        "state": "uploaded",
        "size": len(payload),
        "digest": "sha256:"
        + ("a" * 64 if fault == "digest" else hashlib.sha256(payload).hexdigest()),
    }
    verifier = publisher._verify_release_zip
    calls = _world(
        monkeypatch,
        target=commit,
        annotated=False,
        release_asset=metadata,
    )
    monkeypatch.setattr(publisher, "_verify_release_zip", verifier)
    original = subprocess.run

    def download(command, **options):
        if command[:3] == ["gh", "api", "repos/o/r/releases/assets/456"]:
            options["stdout"].write(payload)
            return subprocess.CompletedProcess(command, 0)
        return original(command, **options)

    monkeypatch.setattr(publisher.subprocess, "run", download)
    if fault is None:
        _finalize("o/r", "v9.1.1", _HACS, _COMPONENT, checkout=checkout)
        assert any(method != "GET" for _, method, _ in calls)
    else:
        with pytest.raises(ValueError, match="ZIP"):
            _finalize("o/r", "v9.1.1", _HACS, _COMPONENT, checkout=checkout)
        assert all(method == "GET" for _, method, _ in calls)


def test_missing_pypi_performs_no_github_write(monkeypatch):
    calls = _world(monkeypatch, ready=False)
    with pytest.raises(TimeoutError):
        _finalize("o/r", "v9.1.1", _HACS, _COMPONENT, attempts=1, delay=0)
    assert calls == []


def test_missing_zip_leaves_release_and_stable_alias_untouched(monkeypatch):
    calls = _world(monkeypatch, asset=False)
    with pytest.raises(ValueError, match="ZIP is not ready"):
        _finalize("o/r", "v9.1.1", _HACS, _COMPONENT)
    assert all(method == "GET" for _, method, _ in calls)


@pytest.mark.parametrize("annotated", [False, True])
def test_publish_follows_asset_gate_and_retains_the_version_tag_object(
    monkeypatch, annotated
):
    calls = _world(monkeypatch, annotated=annotated)
    verified_commits = []
    monkeypatch.setattr(
        publisher,
        "_verify_release_zip",
        lambda repository, asset, checkout, commit: verified_commits.append(commit),
    )
    _finalize("o/r", "v9.1.1", _HACS, _COMPONENT)
    assert verified_commits == ["version-tag-object" + ("-commit" if annotated else "")]
    writes = [
        (endpoint, fields) for endpoint, method, fields in calls if method != "GET"
    ]
    assert writes == [
        ("repos/o/r/releases/123", {"draft": False}),
        (
            "git push",
            {"sha": "version-tag-object", "expected": "previous"},
        ),
    ]


def test_an_old_retry_cannot_roll_back_the_stable_alias(monkeypatch):
    calls = _world(monkeypatch, comparison="behind")
    with pytest.raises(ValueError, match="advance"):
        _finalize("o/r", "v9.1.1", _HACS, _COMPONENT)
    assert all(method == "GET" for _, method, _ in calls)


def test_failed_publication_keeps_the_existing_stable_alias(monkeypatch):
    calls = _world(monkeypatch, fail_publish=True)
    with pytest.raises(RuntimeError, match="publication failed"):
        _finalize("o/r", "v9.1.1", _HACS, _COMPONENT)
    assert not any(method == "PUSH" for _, method, _ in calls)


def test_ref_failure_retains_the_verified_public_release_for_recovery(monkeypatch):
    calls = _world(monkeypatch)

    def rejected(*args):
        raise RuntimeError("stable changed concurrently")

    monkeypatch.setattr(publisher, "_push_stable_ref", rejected)
    with pytest.raises(RuntimeError, match="concurrently"):
        _finalize("o/r", "v9.1.1", _HACS, _COMPONENT)
    assert [fields for _, method, fields in calls if method != "GET"] == [
        {"draft": False}
    ]


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("concurrent", [False, True])
def test_stable_lease_preserves_tag_objects_and_operator_changes(
    tmp_path, existing, concurrent
):
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    checkout.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "master")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.com")
    tags = []
    for version in ("1", "2", "operator"):
        git("commit", "--allow-empty", "-qm", version)
        git("tag", "-a", version, "-m", version)
        tags.append(git("rev-parse", version))
    expected = tags[0] if existing else ""
    if existing:
        git("push", str(remote), f"{tags[0]}:refs/tags/stable")
    if concurrent:
        git("push", "--force", str(remote), f"{tags[2]}:refs/tags/stable")
        with pytest.raises(subprocess.CalledProcessError):
            publisher._push_stable_ref(str(remote), checkout, tags[1], expected)
    else:
        publisher._push_stable_ref(str(remote), checkout, tags[1], expected)
    found = subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/tags/stable"], text=True
    ).strip()
    assert found == (tags[2] if concurrent else tags[1])


def test_recovery_of_an_already_published_release_is_idempotent(monkeypatch):
    calls = _world(monkeypatch, draft=False, current="version-tag-object")
    _finalize("o/r", "v9.1.1", _HACS, _COMPONENT)
    assert all(method == "GET" for _, method, _ in calls)


def test_draft_lookup_uses_all_pages_when_the_tag_endpoint_cannot_find_it(monkeypatch):
    calls = _world(monkeypatch)
    original = publisher._gh_json
    draft = {"id": 123, "tag_name": "v9.1.1", "draft": True, "prerelease": False}
    pages = [[{"tag_name": "another-release"}] * 100 for _ in range(10)] + [[draft]]

    def gh(endpoint, *, method="GET", fields=None, paginate=False):
        if "/releases/tags/" in endpoint:
            raise subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 404")
        if endpoint.endswith("/releases?per_page=100"):
            assert paginate
            return pages
        return original(endpoint, method=method, fields=fields)

    monkeypatch.setattr(publisher, "_gh_json", gh)
    _finalize("o/r", "v9.1.1", {}, {})
    assert any(
        method == "PATCH" and fields == {"draft": False} for _, method, fields in calls
    )


def test_legacy_archive_release_does_not_require_a_new_zip(monkeypatch):
    calls = _world(monkeypatch, asset=False, missing_ref=True)
    monkeypatch.setattr(
        publisher,
        "_verify_release_zip",
        lambda *args: pytest.fail("legacy release must not download a new ZIP"),
    )
    result = _finalize("o/r", "v9.1.1", {}, {})
    assert result["zip_required"] is False
    assert any(
        method == "PUSH" and fields["expected"] == "" for _, method, fields in calls
    )


def test_a_dev_or_mismatched_pin_is_not_finalized_as_stable(monkeypatch):
    calls = _world(monkeypatch)
    component = {"version": "9.1.1", "requirements": ["ha-mcp-dev==9.1.1.dev4"]}
    with pytest.raises(ValueError, match="server pin"):
        _finalize("o/r", "v9.1.1", _HACS, component)
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
    assert "conclusion" not in release["jobs"]["prepare"]["if"]
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
    semver = yaml.safe_load(
        (_ROOT / ".github/workflows/semver-release.yml").read_text()
    )
    producer_name = next(
        s["with"]["name"]
        for s in semver["jobs"]["semantic-release"]["steps"]
        if s.get("uses", "").startswith("actions/upload-artifact@")
    )
    artifacts = {
        "artifacts": [] if scenario == "no-release" else [{"name": producer_name}]
    }
    stub = (
        'gh() { case "$1 $2" in\n'
        f'"api repos/o/r/actions/runs/42/artifacts") printf "%s\\n" {shlex.quote(json.dumps(artifacts))} | jq "$4";;\n'
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


@pytest.mark.parametrize("scenario", ["docker-failed", "no-finalization", "superseded"])
def test_docs_and_mirror_follow_finalization_even_when_later_jobs_fail(
    tmp_path, scenario
):
    """A failed Docker job cannot strand an already usable integration release."""
    workflows = _ROOT / ".github/workflows"
    release = yaml.safe_load((workflows / "release-publish.yml").read_text())
    steps = release["jobs"]["finalize_github_release"]["steps"]
    producer = next(
        s["with"]["name"]
        for s in steps
        if s.get("uses", "").startswith("actions/upload-artifact@")
    )
    artifacts = {
        "artifacts": [] if scenario == "no-finalization" else [{"name": producer}]
    }
    current = "newer" if scenario == "superseded" else "same-tag-object"
    stub = (
        'gh() { case "$1 $2" in\n'
        f'"api repos/o/r/actions/runs/42/artifacts") printf "%s\\n" {shlex.quote(json.dumps(artifacts))} | jq "$4";;\n'
        '"run download") DEST="${@: -1}"; mkdir -p "$DEST"; '
        'printf "v1.1.0\\n" > "$DEST/finalized-release-tag.txt";;\n'
        f'"api repos/o/r/git/ref/tags/stable") echo {current};;\n'
        '"api repos/o/r/git/ref/tags/v1.1.0") echo same-tag-object;;\n'
        "*) return 2;; esac; }\n"
    )
    action = yaml.safe_load(
        (_ROOT / ".github/actions/read-finalized-release/action.yml").read_text()
    )
    output = tmp_path / "output"
    env = {
        **os.environ,
        "GITHUB_OUTPUT": output.as_posix(),
        "RELEASE_RUN_ID": "42",
        "GITHUB_REPOSITORY": "o/r",
        "TRIGGER_CONCLUSION": "failure",
    }
    result = subprocess.run(
        [
            shutil.which("bash") or "bash",
            "-e",
            "-c",
            stub + action["runs"]["steps"][0]["run"],
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["tag"] == ("v1.1.0" if scenario == "docker-failed" else "")
    mirror = yaml.safe_load((workflows / "sync-integration-mirror.yml").read_text())
    pick = next(s for s in mirror["jobs"]["gate"]["steps"] if s.get("id") == "decide")
    env.update(
        {
            "EVENT": "workflow_run",
            "TRIGGER_WORKFLOW": "Release Publish",
            "TRIGGER_BRANCH": "master",
            "FINALIZED_TAG": values["tag"],
        }
    )
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", stub + pick["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["channel"] == ("stable" if scenario == "docker-failed" else "skip")
    deploy = yaml.safe_load((workflows / "deploy.yml").read_text())
    assert "conclusion" not in deploy["jobs"]["build"]["if"]
    assert "needs.finalized.outputs.tag" in deploy["jobs"]["build"]["if"]
