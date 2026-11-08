"""Consumers select the artifact built from the stamped release checkout."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("mismatch", [None, "version", "pin", "constant"])
def test_stable_snapshot_preserves_tagged_unicode_bytes_and_rejects_bad_pins(
    tmp_path, mismatch
):
    source = tmp_path / "source"
    component = source / "custom_components/ha_mcp_tools"
    component.mkdir(parents=True)
    manifest = {
        "domain": "ha_mcp_tools",
        "name": "HA-MCP — Manage Home Assistant with AI",
        "version": "1.0.1" if mismatch == "version" else "1.0.0",
        "requirements": ["ha-mcp==1.0.1" if mismatch == "pin" else "ha-mcp==1.0.0"],
    }
    (component / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    version = "1.0.1" if mismatch == "constant" else "1.0.0"
    (component / "const.py").write_text(f'COMPONENT_VERSION = "{version}"\n')
    (source / "hacs.json").write_text("{}\n")
    (source / "LICENSE").write_text("test\n")
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    step = next(
        s
        for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
        if s.get("name") == "Stage snapshot"
    )
    script = (
        step["run"]
        .replace("/tmp/mirror", mirror.as_posix())
        .replace("python3 ", shlex.quote(Path(sys.executable).as_posix()) + " ")
    )
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", script],
        cwd=_ROOT,
        env={
            **os.environ,
            "SRC": source.as_posix(),
            "CHANNEL": "stable",
            "VER": "1.0.0",
            "PIN": "1.0.0",
            "DIST": "ha-mcp",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode == 0) is (mismatch is None), result.stdout + result.stderr
    if mismatch is None:
        for path in component.iterdir():
            assert (
                mirror / "custom_components/ha_mcp_tools" / path.name
            ).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("matches_snapshot", [True, False])
def test_the_verified_zip_digest_is_committed_with_its_snapshot(
    tmp_path, matches_snapshot
):
    mirror = tmp_path / "mirror"
    component = mirror / "custom_components/ha_mcp_tools"
    component.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(mirror)], check=True)
    for key, value in (("user.name", "test"), ("user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(mirror), "config", key, value], check=True)
    source = component / "__init__.py"
    source.write_text("VALUE = 1\n")
    (component / "manifest.json").write_text(
        json.dumps(
            {
                "domain": "ha_mcp_tools",
                "version": "1.0.0",
                "requirements": ["ha-mcp-dev==1.0.0.dev7"],
            }
        )
    )
    metadata = mirror / ".github/component-zip-source.json"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps({"repository": "upstream/repo", "tag": "v1.0.0.dev7"})
    )
    subprocess.run(["git", "-C", str(mirror), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(mirror), "commit", "-qm", "stamped snapshot"], check=True
    )
    if not matches_snapshot:
        source.write_text("VALUE = 2\n")
    payload = tmp_path / "upstream.zip"
    subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts/build_component_zip.py"),
            "--component-dir",
            str(component),
            "--output",
            str(payload),
        ],
        check=True,
    )
    source.write_text("VALUE = 1\n")
    digest = "sha256:" + hashlib.sha256(payload.read_bytes()).hexdigest()
    stub = (
        'git() { case "$*" in *ls-remote*) return 0;; *) command git "$@";; esac; }\n'
        'gh() { case "$1 $2" in "api repos/upstream/repo/releases/tags/v1.0.0.dev7")\n'
        f'printf "%s\\n" {shlex.quote(digest)};;\n'
        '"release download") while [ "$#" -gt 0 ]; do '
        'if [ "$1" = --dir ]; then DEST="$2"; break; fi; shift; done\n'
        'mkdir -p "$DEST"; cp "$PAYLOAD" "$DEST/ha_mcp_tools.zip";; '
        "*) return 2;; esac; }\n"
        "sleep() { :; }\n"
    )
    step = next(
        s
        for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
        if s.get("name") == "Wait for the upstream integration ZIP before tagging"
    )
    script = step["run"].replace("/tmp/mirror", mirror.as_posix())
    script = script.replace(
        "python3 ", shlex.quote(Path(sys.executable).as_posix()) + " "
    )
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", stub + script],
        cwd=_ROOT,
        env={
            **os.environ,
            "RUNNER_TEMP": tmp_path.as_posix(),
            "PAYLOAD": payload.as_posix(),
            "TAG": "v1.0.0-dev.7",
            "VER": "1.0.0",
            "CHANNEL": "dev",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode == 0) is matches_snapshot, result.stdout + result.stderr
    committed = json.loads(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(mirror),
                "show",
                "HEAD:.github/component-zip-source.json",
            ],
            text=True,
        )
    )
    if matches_snapshot:
        assert committed["digest"] == digest
    else:
        assert "digest" not in committed


@pytest.mark.parametrize("tag_state", ["exists", "superseded", "lookup-error"])
def test_sync_retry_does_not_fetch_a_pruned_zip_when_no_tag_is_needed(
    tmp_path, tag_state
):
    marker = tmp_path / ".github/component-zip-source.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"repository": "upstream/repo", "tag": "v1.0.0.dev7"}))
    stub = (
        'git() { case "$TAG_STATE" in\n'
        "exists) echo existing-tag;;\n"
        'superseded) case "$*" in *refs/tags/v1.0.0) echo stable-tag;; esac;;\n'
        "lookup-error) return 2;; esac; }\n"
        "gh() { touch upstream-requested; return 1; }\n"
        "seq() { echo 1; }; sleep() { :; }\n"
    )
    step = next(
        s
        for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
        if s.get("name") == "Wait for the upstream integration ZIP before tagging"
    )
    script = (
        step["run"]
        .replace("/tmp/mirror", tmp_path.as_posix())
        .replace("python3 ", shlex.quote(Path(sys.executable).as_posix()) + " ")
    )
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", stub + script],
        cwd=tmp_path,
        env={
            **os.environ,
            "TAG_STATE": tag_state,
            "GITHUB_ENV": (tmp_path / "env").as_posix(),
            "TAG": "v1.0.0-dev.7",
            "VER": "1.0.0",
            "CHANNEL": "dev",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode == 0) is (tag_state != "lookup-error"), result.stderr
    assert not (tmp_path / "upstream-requested").exists()
    if tag_state != "lookup-error":
        # Even if the ref disappears afterwards, skipping verification must
        # never turn into creation of an unverified tag in the next step.
        written_env = dict(
            line.split("=", 1) for line in (tmp_path / "env").read_text().splitlines()
        )
        tag_step = next(
            s
            for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
            if s.get("name") == "Tag the mirror"
        )
        result = subprocess.run(
            [
                shutil.which("bash") or "bash",
                "-e",
                "-c",
                "git() { touch tag-requested; return 2; }; gh() { touch tag-requested; return 2; };\n"
                + tag_step["run"],
            ],
            cwd=tmp_path,
            env={**os.environ, **written_env, "CHANNEL": "dev"},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "tag-requested").exists()


def _steps(path: str, job: str) -> list[dict]:
    return yaml.safe_load((_ROOT / path).read_text(encoding="utf-8"))["jobs"][job][
        "steps"
    ]


def test_each_release_uploads_the_zip_before_publishing() -> None:
    for path, job, create_name in [
        (
            ".github/workflows/semver-release.yml",
            "semantic-release",
            "Create draft GitHub release",
        ),
        (
            ".github/workflows/publish-dev.yml",
            "create-prerelease",
            "Create pre-release",
        ),
    ]:
        steps = _steps(path, job)
        build = next(
            i
            for i, s in enumerate(steps)
            if s.get("name") == "Build the stamped integration ZIP"
        )
        create = next(i for i, s in enumerate(steps) if s.get("name") == create_name)
        assert build < create
        assert "uv run -s scripts/build_component_zip.py" in steps[build]["run"]
        assert "ha_mcp_tools.zip" in steps[create]["run"]
        if job == "create-prerelease":
            # With an asset, gh owns the draft/upload/publish lifecycle and
            # attempts cleanup on failure; explicit --draft disables cleanup.
            assert "--draft" not in steps[create]["run"]
            assert "gh release edit" not in steps[create]["run"]
            stamp = next(
                i
                for i, s in enumerate(steps)
                if s.get("name") == "Stamp the component for this build"
            )
            assert stamp < build
        else:
            # Stable publishing is finalized separately, after its PyPI gate.
            assert "--draft" in steps[create]["run"]


def test_the_mirror_copies_the_verified_upstream_artifact_without_rebuilding() -> None:
    steps = _steps(".github/integration-mirror-workflows/release-on-tag.yml", "release")
    copied = next(
        i
        for i, s in enumerate(steps)
        if s.get("name") == "Copy the upstream integration ZIP"
    )
    create = next(
        i for i, s in enumerate(steps) if s.get("name") == "Create release for tag"
    )
    assert copied < create
    assert "gh release download" in steps[copied]["run"]
    assert "sha256sum --check" in steps[copied]["run"]
    assert "build_component_zip.py" not in steps[copied]["run"]
    assert "ha_mcp_tools.zip" in steps[create]["run"]
    stage = next(
        s
        for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
        if s.get("name") == "Stage snapshot"
    )["run"]
    assert "component-zip-source.json" in stage
    assert "os.environ['PIN']" in stage
    assert "SOURCE_REPOSITORY" in stage
    names = [
        s.get("name")
        for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
    ]
    assert names.index(
        "Wait for the upstream integration ZIP before tagging"
    ) < names.index("Tag the mirror")


def test_focused_e2e_dispatch_keeps_other_backend_runs_as_the_default() -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/e2e-tests.yml").read_text())
    selector = workflow[True]["workflow_dispatch"]["inputs"]["update_path_only"]
    assert selector["type"] == "boolean" and selector["default"] is False
    for name, job in workflow["jobs"].items():
        if name != "e2e-tests-update-path":
            for event, focused, expected in [
                ("push", False, True),
                ("push", True, True),
                ("workflow_dispatch", False, True),
                ("workflow_dispatch", True, False),
            ]:
                condition = (
                    job["if"]
                    .replace(
                        "github.event_name != 'workflow_dispatch'",
                        str(event != "workflow_dispatch").lower(),
                    )
                    .replace("!inputs.update_path_only", str(not focused).lower())
                )
                actual = any(
                    all(term.strip() == "true" for term in group.split("&&"))
                    for group in condition.split("||")
                )
                assert actual is expected, (event, focused, job["if"])
    target = workflow["jobs"]["e2e-tests-update-path"]
    assert "if" not in target
    assert any(s.get("env", {}).get("E2E_UPDATE_PATH") == "1" for s in target["steps"])


@pytest.mark.parametrize("source", ["published", "draft", "unavailable"])
def test_mirror_tag_requires_an_available_public_zip(tmp_path, source):
    """Reject API failures and defensive draft JSON before downloading."""
    assert shutil.which("jq"), "jq is required for this test"
    marker = tmp_path / ".github"
    marker.mkdir()
    (marker / "component-zip-source.json").write_text(
        json.dumps({"repository": "upstream/repo", "tag": "v9.1.1"})
    )
    payload = b"the verified integration artifact"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    release = {
        "draft": source == "draft",
        "assets": [
            {
                "name": "ha_mcp_tools.zip",
                "state": "uploaded",
                "size": 42,
                "digest": digest,
            }
        ],
    }
    stub = (
        'gh() { case "$1 $2" in\n'
        '"api "*) touch "$RUNNER_TEMP/api-requested"\n'
        'if [ "$SOURCE_STATE" = unavailable ]; then echo "HTTP 404: Not Found" >&2; return 1; fi\n'
        f'printf "%s\\n" {shlex.quote(json.dumps(release))} | jq -r "$4" '
        '&& touch "$RUNNER_TEMP/api-filter-succeeded";;\n'
        '"release download") touch "$RUNNER_TEMP/download-called"; '
        'mkdir -p "$RUNNER_TEMP/upstream-integration-zip"; '
        f'printf "%s" {shlex.quote(payload.decode())} > "$RUNNER_TEMP/upstream-integration-zip/ha_mcp_tools.zip";;\n'
        "*) return 2;; esac; }\n"
        # Snapshot comparison and commit are exercised above with real Git and
        # builder; this case isolates the public/draft source admission gate.
        f'python3() {{ case "$*" in scripts/build_component_zip.py*) echo {shlex.quote(digest)};; '
        f'*) {shlex.quote(Path(sys.executable).as_posix())} "$@";; esac; }}\n'
        "git() { return 0; }\n"
        "seq() { echo 1; }\nsleep() { :; }\n"
    )
    step = next(
        s
        for s in _steps(".github/workflows/sync-integration-mirror.yml", "sync")
        if s.get("name") == "Wait for the upstream integration ZIP before tagging"
    )
    script = step["run"].replace("/tmp/mirror", tmp_path.as_posix())
    result = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", stub + script],
        cwd=tmp_path,
        env={
            **os.environ,
            "RUNNER_TEMP": tmp_path.as_posix(),
            "TAG": "v9.1.1",
            "VER": "9.1.1",
            "CHANNEL": "stable",
            "SOURCE_STATE": source,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert (result.returncode == 0) is (source == "published"), result.stderr
    assert (tmp_path / "api-requested").exists()
    assert (tmp_path / "api-filter-succeeded").exists() is (source != "unavailable")
    if source != "published":
        assert "The upstream ZIP is unavailable" in result.stdout
        assert not (tmp_path / "download-called").exists()
    else:
        assert (tmp_path / "download-called").exists()
