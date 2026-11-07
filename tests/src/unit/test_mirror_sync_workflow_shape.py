"""Guard the paired component/server release automation (#2427).

The mirror sync and the dev-version script only run after a merge, so PR CI
never executes them. These tests pin the workflow's ordering and run the
stamping script against a scratch copy of the component.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_MIRROR = _WORKFLOWS / "sync-integration-mirror.yml"
_RELEASE_ON_TAG = (
    _REPO_ROOT / ".github" / "integration-mirror-workflows" / "release-on-tag.yml"
)
_COMPONENT = _REPO_ROOT / "custom_components" / "ha_mcp_tools"
_STAMP = _REPO_ROOT / "scripts" / "stamp_component_version.py"
_DEV_VERSION = _REPO_ROOT / "scripts" / "dev_version.sh"


def _workflow(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _sync_step_names() -> list[str]:
    return [step.get("name") for step in _workflow(_MIRROR)["jobs"]["sync"]["steps"]]


class TestMirrorSyncShape:
    def test_both_tagging_legs_follow_their_publishing_workflow(self) -> None:
        # yaml reads the bare `on` key as True.
        triggers = _workflow(_MIRROR)[True]
        assert triggers["workflow_run"]["workflows"] == [
            _workflow(_WORKFLOWS / "release-publish.yml")["name"],
            _workflow(_WORKFLOWS / "publish-dev.yml")["name"],
        ]
        # A dispatched dev build from another branch must never be mirrored.
        assert triggers["workflow_run"]["branches"] == ["master"]
        gate = next(
            s
            for s in _workflow(_MIRROR)["jobs"]["gate"]["steps"]
            if s.get("id") == "decide"
        )
        assert "head_branch" in gate["env"]["TRIGGER_BRANCH"]
        assert '"$TRIGGER_BRANCH" != master' in gate["run"]

    def test_gate_follows_the_stable_publisher_and_the_dev_pypi_job(self) -> None:
        gate = next(
            s
            for s in _workflow(_MIRROR)["jobs"]["gate"]["steps"]
            if s.get("id") == "decide"
        )["run"]
        jobs = {
            job["name"]
            for job in _workflow(_WORKFLOWS / "publish-dev.yml")["jobs"].values()
            if "name" in job
        }
        assert "Publish PyPI (dev channel)" in jobs
        publisher = _workflow(_WORKFLOWS / "release-publish.yml")["name"]
        assert f'"$TRIGGER_WORKFLOW" = "{publisher}"' in gate
        assert '"finalized_tag=$FINALIZED_TAG"' in gate
        assert 'job="Publish PyPI (dev channel)"' in gate

    def test_dev_leg_pins_the_version_the_dev_build_uploaded(self) -> None:
        # Recomputing the number in the mirror could name a build PyPI never
        # got, or one built from a different commit.
        publish = _workflow(_WORKFLOWS / "publish-dev.yml")["jobs"]["prepare"]
        uploads = [
            step["with"]["name"]
            for step in publish["steps"]
            if "upload-artifact" in step.get("uses", "")
        ]
        mirror = _workflow(_MIRROR)["jobs"]["sync"]["steps"]
        downloads = [
            step["with"]["name"]
            for step in mirror
            if "download-artifact" in step.get("uses", "")
        ]
        assert uploads == downloads == ["dev-version"]
        resolve = next(
            s
            for s in mirror
            if s.get("name") == "Resolve the release this run publishes"
        )
        assert "dev_version.sh" not in resolve["run"]

    def test_tags_only_after_pypi_serves_the_pin(self) -> None:
        names = _sync_step_names()
        assert names.index("Stage snapshot") < names.index("Commit and push")
        assert names.index("Commit and push") < names.index(
            "Wait for the pinned server build on PyPI"
        )
        assert names.index("Wait for the pinned server build on PyPI") < names.index(
            "Tag the mirror"
        )

    def test_stable_leg_keeps_the_workflows_own_scripts(self) -> None:
        # A recovery dispatch runs this workflow from master against an older
        # tag; checking the tag out over the working tree would run that
        # tag's scripts with arguments only master's understand.
        steps = _workflow(_MIRROR)["jobs"]["sync"]["steps"]
        resolve = next(
            s
            for s in steps
            if s.get("name") == "Resolve the release this run publishes"
        )
        stage = next(s for s in steps if s.get("name") == "Stage snapshot")
        assert "git checkout" not in resolve["run"]
        assert '"$SRC/custom_components/ha_mcp_tools"' in stage["run"]
        # hacs.json's Home Assistant minimum ships with the snapshot's component.
        assert 'cp "$SRC/hacs.json" "$SRC/LICENSE" /tmp/mirror/' in stage["run"]

    @pytest.mark.parametrize(
        ("trigger", "matches_alias", "expected"),
        [("v1.0.0", True, "1.0.0"), ("", True, None), ("v1.0.0", False, None)],
    )
    def test_stable_leg_syncs_the_release_its_trigger_cut(
        self, tmp_path: Path, trigger: str, matches_alias: bool, expected: str | None
    ) -> None:
        # Recovery needs a finalized tag and its still-current raw stable alias;
        # a newer public release is not evidence of successful finalization.
        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*args: str) -> str:
            return subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=t",
                    "-c",
                    "user.email=t@t",
                    *args,
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("init", "-q", "-b", "master")
        shas = []
        for n, tag in enumerate(["", "v1.0.0", "", "v1.1.0"]):
            git("commit", "-q", "--allow-empty", "-m", str(n))
            shas.append(git("rev-parse", "HEAD"))
            if tag:
                git("tag", tag)
        resolve = next(
            s
            for s in _workflow(_MIRROR)["jobs"]["sync"]["steps"]
            if s.get("name") == "Resolve the release this run publishes"
        )
        output = tmp_path / "out"
        result = subprocess.run(
            [
                shutil.which("bash") or "bash",
                "-e",
                "-c",
                (
                    'gh() { case "$2" in\n'
                    "repos/o/r/releases/latest) echo v1.1.0;;\n"
                    'repos/o/r/git/ref/tags/stable) echo "$STABLE_OBJECT";;\n'
                    "repos/o/r/git/ref/tags/v1.0.0) echo finalized-object;;\n"
                    "repos/o/r/git/ref/tags/v1.1.0) echo newest-object;;\n"
                    "*) return 2;; esac; }\n"
                )
                + resolve["run"],
            ],
            cwd=repo,
            env={
                **os.environ,
                "CHANNEL": "stable",
                "FINALIZED_TAG": trigger,
                "STABLE_OBJECT": (
                    "newest-object"
                    if not trigger
                    else "finalized-object"
                    if matches_alias
                    else "other-object"
                ),
                "GITHUB_REPOSITORY": "o/r",
                "RUNNER_TEMP": tmp_path.as_posix(),
                "GITHUB_OUTPUT": output.as_posix(),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 0) is (expected is not None), result.stderr
        if expected is None:
            assert not (tmp_path / "release-src").exists()
        else:
            values = dict(
                line.split("=", 1) for line in output.read_text().splitlines()
            )
            assert values["version"] == expected
            assert values["tag"] == trigger
            assert git("-C", values["src"], "rev-parse", "HEAD") == shas[1]

    @pytest.mark.parametrize("finalized", ["", "v1.0.0"])
    def test_manual_stable_recovery_requires_the_publishers_marker(
        self, tmp_path: Path, finalized: str
    ) -> None:
        workflow = _workflow(_MIRROR)
        pick = next(
            s for s in workflow["jobs"]["gate"]["steps"] if s.get("id") == "decide"
        )
        output = tmp_path / "output"
        result = subprocess.run(
            [shutil.which("bash") or "bash", "-e", "-c", pick["run"]],
            cwd=tmp_path,
            env={
                **os.environ,
                "EVENT": "workflow_dispatch",
                "DISPATCH_CHANNEL": "stable",
                "TRIGGER_BRANCH": "master",
                "TRIGGER_CONCLUSION": "",
                "FINALIZED_TAG": finalized,
                "GITHUB_OUTPUT": output.as_posix(),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert (result.returncode == 0) is bool(finalized), result.stderr
        if finalized:
            values = output.read_text().splitlines()
            assert "channel=stable" in values and f"finalized_tag={finalized}" in values

    @pytest.mark.parametrize(
        "branch,run_id,run_kind,accepted",
        [
            ("master", "42", "workflow_run", True),
            ("feature", "42", "workflow_run", False),
            ("master", "abc", "workflow_run", False),
            ("master", "0", "workflow_run", False),
            ("master", "", "workflow_run", False),
            ("master", "42", "workflow_dispatch", True),
            ("master", "42", "other-workflow", False),
            ("master", "42", "other-branch", False),
            ("master", "42", "pull_request", False),
            ("master", "42", "pull_request_target", False),
            ("master", "42", "push", False),
            ("master", "42", "lookup-error", False),
        ],
    )
    def test_manual_recovery_requires_a_trusted_publisher_run(
        self, tmp_path: Path, branch: str, run_id: str, run_kind: str, accepted: bool
    ) -> None:
        step = next(
            s
            for s in _workflow(_MIRROR)["jobs"]["gate"]["steps"]
            if s.get("name") == "Validate stable recovery input"
        )
        run = {
            "id": 42,
            "workflow_id": 74 if run_kind == "other-workflow" else 73,
            "name": "Release Publish",
            "head_branch": "feature" if run_kind == "other-branch" else "master",
            "event": run_kind
            if run_kind
            in {"workflow_dispatch", "pull_request", "pull_request_target", "push"}
            else "workflow_run",
            # Finalization can have succeeded before a later Docker job failed.
            "conclusion": "failure",
        }
        stub = (
            'gh() { printf "%s\\n" "$2" >> api-called\n'
            'case "$2" in\n'
            'repos/o/r/actions/workflows/release-publish.yml) printf "%s\\n" \'{"id":73}\' | jq -r "$4";;\n'
            'repos/o/r/actions/runs/42) [ "$RUN_KIND" != lookup-error ] || return 2\n'
            'printf "%s\\n" "$RUN_JSON" | jq -r "$4";;\n'
            "*) return 2;; esac; }\n"
        )
        result = subprocess.run(
            [shutil.which("bash") or "bash", "-e", "-c", stub + step["run"]],
            cwd=tmp_path,
            env={
                **os.environ,
                "RECOVERY_BRANCH": branch,
                "RECOVERY_RUN_ID": run_id,
                "GITHUB_REPOSITORY": "o/r",
                "RUN_KIND": run_kind,
                "RUN_JSON": json.dumps(run),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert (result.returncode == 0) is accepted, result.stderr
        if branch != "master" or run_id != "42":
            assert (
                "Stable recovery requires master and a finalized Release Publish run ID"
                in result.stdout
            )
            assert not (tmp_path / "api-called").exists()

    @pytest.mark.parametrize("matches_alias", [False, True])
    def test_alias_change_after_resolution_blocks_the_mirror_tag(
        self, tmp_path, matches_alias
    ) -> None:
        step = next(
            s
            for s in _workflow(_MIRROR)["jobs"]["sync"]["steps"]
            if s.get("name") == "Tag the mirror"
        )
        stub = (
            'gh() { case "$2" in *tags/stable) echo "$ALIAS_OBJECT";; '
            "*tags/v1.0.0) echo finalized-object;; *) return 2;; esac; }\n"
            "git() { echo called > git-called; echo existing-tag; }\n"
        )
        result = subprocess.run(
            [shutil.which("bash") or "bash", "-e", "-c", stub + step["run"]],
            cwd=tmp_path,
            env={
                **os.environ,
                "CHANNEL": "stable",
                "TAG": "v1.0.0",
                "GITHUB_REPOSITORY": "o/r",
                "ALIAS_OBJECT": "finalized-object" if matches_alias else "other-object",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert (result.returncode == 0) is matches_alias, result.stderr
        if not matches_alias:
            assert "Stable changed after resolution" in result.stdout
        assert (tmp_path / "git-called").exists() is matches_alias

    def test_snapshot_is_stamped(self) -> None:
        stage = next(
            step
            for step in _workflow(_MIRROR)["jobs"]["sync"]["steps"]
            if step.get("name") == "Stage snapshot"
        )
        assert "scripts/stamp_component_version.py" in stage["run"]

    def test_a_dev_build_tags_its_stamp_and_leaves_main_on_masters_component(
        self, tmp_path: Path
    ) -> None:
        # The mirror's main matches master; only the dev tag's commit carries
        # the dev stamp, as publish-dev.yml does here.
        def git(*args: str, cwd: Path = tmp_path) -> str:
            return subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        remote = tmp_path / "remote.git"
        clone = tmp_path / "mirror"
        src = tmp_path / "src"
        git("init", "-q", "--bare", "-b", "main", str(remote))
        git("clone", "-q", f"file://{remote}", str(clone))
        git("commit", "-q", "--allow-empty", "-m", "previous", cwd=clone)
        git("push", "-q", "origin", "main", cwd=clone)
        shutil.copytree(_COMPONENT, src / "custom_components" / "ha_mcp_tools")
        for name in ("hacs.json", "LICENSE"):
            shutil.copy(_REPO_ROOT / name, src / name)
        # The tag step's release notes need the main repository's history.
        stub = tmp_path / "stub"
        stub.mkdir()
        (stub / "python3").write_text(
            '#!/bin/sh\nfor last; do :; done\necho notes > "$last"\n'
        )
        (stub / "python3").chmod(0o755)
        env = {
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CHANNEL": "dev",
            "GITHUB_REPOSITORY": "o/r",
            "SOURCE_SHA": "abc",
            "SRC": str(src),
            "VER": "99.0.0",
            "PIN": "99.0.0.dev7",
            "DIST": "ha-mcp-dev",
            "TAG": "v99.0.0-dev.7",
        }
        steps = {s.get("name"): s for s in _workflow(_MIRROR)["jobs"]["sync"]["steps"]}

        for name in (
            "Stage snapshot",
            "Commit and push",
            "Stamp the dev snapshot",
            "Tag the mirror",
        ):
            script = (
                steps[name]["run"]
                .replace("/tmp/mirror", str(clone))
                .replace("${{ steps.release.outputs.pin }}", env["PIN"])
                .replace("${{ steps.release.outputs.dist }}", env["DIST"])
            )
            step_env = env
            if name == "Tag the mirror":
                step_env = {**env, "PATH": f"{stub}:/usr/bin:/bin"}
            result = subprocess.run(
                ["bash", "-e", "-c", script],
                cwd=_REPO_ROOT,
                env=step_env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, f"{name}: {result.stderr}"

        manifest = "custom_components/ha_mcp_tools/manifest.json"
        on_main = json.loads(git("show", f"main:{manifest}", cwd=remote))
        tagged = json.loads(git("show", f"v99.0.0-dev.7:{manifest}", cwd=remote))
        assert on_main == json.loads((_COMPONENT / "manifest.json").read_text())
        assert tagged["version"] == "99.0.0"
        assert "ha-mcp-dev==99.0.0.dev7" in tagged["requirements"]


def _release_on_tag_step(name: str) -> dict[Any, Any]:
    return next(
        s
        for s in _workflow(_RELEASE_ON_TAG)["jobs"]["release"]["steps"]
        if s.get("name") == name
    )


# Newest first, as the releases API lists them.
_DEV_TAGS = ["v9.0.1-dev.1"]
_DEV_TAGS += [f"v9.0.0-dev.{n}" for n in (1001, 1000, 999, 998, 997, 996)]
_DEV_TAGS += ["v2.2.3-dev.423"]
_OLD_DEV_TAGS = ["v2.2.3-dev.423", "v9.0.0-dev.996", "v9.0.0-dev.997"]


class TestMirrorReleaseOnTag:
    @pytest.mark.parametrize(
        "scenario",
        [
            "new",
            "bad-digest",
            "upstream-replaced",
            "mirror-replaced",
            "draft-replaced-before-publish",
            "upstream-pruned",
            "incomplete-upload",
            "draft-empty",
            "draft-incomplete",
            "draft-complete-upstream-pruned",
        ],
    )
    def test_only_verified_or_already_uploaded_zips_can_be_published(
        self, tmp_path: Path, scenario: str
    ) -> None:
        """A retry recovers drafts and preserves uploaded assets after pruning."""
        marker = tmp_path / ".github"
        marker.mkdir()
        payload = tmp_path / "payload.zip"
        payload.write_bytes(b"the upstream integration artifact")
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        (marker / "component-zip-source.json").write_text(
            json.dumps(
                {
                    "repository": "upstream/repo",
                    "tag": "v9.1.0-dev.1",
                    "digest": "sha256:"
                    + ("0" * 64 if scenario == "bad-digest" else digest),
                }
            )
        )
        if scenario == "upstream-replaced":
            payload.write_bytes(b"a replacement artifact with its own valid digest")
            digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        calls = tmp_path / "calls"
        fake_gh = (
            'gh() { printf "%s\\n" "$*" >> calls\n'
            'case "$1 $2" in\n'
            '  "api repos/mirror/repo/releases/tags/v9.1.0-dev.1")\n'
            '    case "$SCENARIO" in draft-*) return 1;; esac\n'
            '    [ "$SCENARIO" = upstream-pruned ] && printf "sha256:%s\\n" "$ZIP_DIGEST"; return 0;;\n'
            '  "api repos/mirror/repo/releases/73")\n'
            '    case "$SCENARIO" in mirror-replaced) printf "sha256:%064d\\n" 0;;\n'
            '      draft-replaced-before-publish) if [ -f mirror-looked-up ]; then printf "sha256:%064d\\n" 0; else touch mirror-looked-up; fi;;\n'
            "    esac\n"
            '    case "$SCENARIO" in upstream-pruned|draft-complete-upstream-pruned)\n'
            '      printf "sha256:%s\\n" "$ZIP_DIGEST";; esac\n'
            "    return 0;;\n"
            '  "release view")\n'
            '    case "$SCENARIO" in upstream-pruned|incomplete-upload|mirror-replaced|draft-*) ;; *) return 1;; esac\n'
            '    case "$*" in *databaseId*) echo 73;; *isDraft*)\n'
            '      case "$SCENARIO" in draft-*) echo true;; *) echo false;; esac;;\n'
            "      *) echo ha_mcp_tools.zip;; esac;;\n"
            '  "api repos/upstream/repo/releases/tags/v9.1.0-dev.1")\n'
            '    case "$SCENARIO" in *upstream-pruned) return 1;; esac\n'
            '    printf "sha256:%s\\n" "$ZIP_DIGEST";;\n'
            '  "release download") mkdir -p dist/integration; cp payload.zip dist/integration/ha_mcp_tools.zip;;\n'
            '  "release create")\n'
            '    case "$*" in *--draft*) return 1;; esac\n'
            '    [ "$4" = dist/integration/ha_mcp_tools.zip ] && [ -s "$4" ] || return 1\n'
            "    touch mirror-uploaded;;\n"
            '  "release upload")\n'
            '    case "$SCENARIO" in *incomplete*)\n'
            '      case "$*" in *--clobber*) ;; *) return 1;; esac;; esac\n'
            "    touch mirror-uploaded;;\n"
            '  "release edit")\n'
            '    [ "$SCENARIO" = draft-complete-upstream-pruned ] || [ -f mirror-uploaded ] || return 1\n'
            "    touch draft-published;;\n"
            "  *) return 2;;\nesac\n}\nsleep() { :; }\n"
        )
        if sys.platform == "win32":
            # Native Git Bash needs the test interpreter, not Windows' Store alias.
            (bin_dir / "python3").write_text(
                f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n',
                newline="\n",
            )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "SCENARIO": scenario,
            "ZIP_DIGEST": "0" * 64 if scenario == "bad-digest" else digest,
            "GITHUB_REF_NAME": "v9.1.0-dev.1",
            "GITHUB_REPOSITORY": "mirror/repo",
        }
        for step in ("Copy the upstream integration ZIP", "Create release for tag"):
            result = subprocess.run(
                [
                    shutil.which("bash") or "bash",
                    "-e",
                    "-c",
                    fake_gh + _release_on_tag_step(step)["run"],
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=45,
            )
            if result.returncode != 0:
                break
        assert calls.exists(), result.stderr
        attempted = calls.read_text().splitlines()
        if scenario in {
            "bad-digest",
            "upstream-replaced",
            "mirror-replaced",
            "draft-replaced-before-publish",
        }:
            assert result.returncode != 0
            assert not any(
                c.startswith(("release create", "release edit", "release upload"))
                for c in attempted
            )
        else:
            assert result.returncode == 0, result.stderr
            if scenario.endswith("upstream-pruned"):
                assert not any(
                    c.startswith(
                        ("api repos/upstream/", "release download", "release upload")
                    )
                    for c in attempted
                )
            else:
                expected = (
                    "release upload"
                    if scenario
                    in {"incomplete-upload", "draft-empty", "draft-incomplete"}
                    else "release create"
                )
                assert any(c.startswith(expected) for c in attempted)
                assert (
                    tmp_path / "dist/integration/ha_mcp_tools.zip"
                ).read_bytes() == payload.read_bytes()
            if scenario.startswith("draft-"):
                assert (tmp_path / "draft-published").exists()

    def test_keeps_as_many_dev_pre_releases_as_the_main_repository(self) -> None:
        # Past HACS's 30-release window a stable release behind the dev
        # builds is no longer seen, so the mirror prunes like publish-dev.yml.
        prune = _release_on_tag_step("Keep the newest five dev pre-releases")
        retire = _release_on_tag_step("Retire superseded dev pre-releases")
        main_cleanup = (_WORKFLOWS / "publish-dev.yml").read_text(encoding="utf-8")
        keep = re.compile(r"tail -n \+(\d+)")
        assert keep.findall(prune["run"]) == keep.findall(main_cleanup)
        # Dev tags prune; stable tags retire.
        assert prune["if"] == retire["if"].replace("!", "")
        names = [
            s.get("name")
            for s in _workflow(_RELEASE_ON_TAG)["jobs"]["release"]["steps"]
        ]
        assert names.index("Create release for tag") < names.index(prune["name"])

    @pytest.mark.parametrize(
        ("tags", "fail", "attempted", "succeeds"),
        [
            pytest.param(_DEV_TAGS, {}, _OLD_DEV_TAGS, True, id="older-builds-pruned"),
            pytest.param(
                _DEV_TAGS[1:],
                {"GITHUB_REF_NAME": _DEV_TAGS[0]},
                _OLD_DEV_TAGS,
                True,
                id="listing-misses-the-new-release",
            ),
            pytest.param(
                [],
                {"GITHUB_REF_NAME": _DEV_TAGS[0]},
                [],
                True,
                id="first-dev-release",
            ),
            pytest.param(
                _DEV_TAGS,
                {"FAIL_DELETE": "v9.0.0-dev.996"},
                _OLD_DEV_TAGS,
                False,
                id="failed-delete-still-tries-the-rest",
            ),
            pytest.param(_DEV_TAGS, {"FAIL_API": "1"}, [], False, id="failed-listing"),
        ],
    )
    def test_prunes_the_older_dev_pre_releases(
        self,
        tmp_path: Path,
        tags: list[str],
        fail: dict[str, str],
        attempted: list[str],
        succeeds: bool,
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        listing = tmp_path / "listing"
        listing.write_text("".join(f"{t}\n" for t in tags))
        deleted = tmp_path / "deleted"
        deleted.touch()
        fake_gh = bin_dir / "gh"
        fake_gh.write_text(
            "#!/bin/sh\n"
            'case "$1 $2" in\n'
            f'  "api --paginate") [ -n "$FAIL_API" ] && exit 1; cat {listing} ;;\n'
            f'  "release delete") echo "$3" >> {deleted}\n'
            '    [ "$3" = "$FAIL_DELETE" ] && exit 1 ;;\n'
            "esac\n"
            "exit 0\n"
        )
        fake_gh.chmod(0o755)

        result = subprocess.run(
            [
                "bash",
                "-e",
                "-c",
                _release_on_tag_step("Keep the newest five dev pre-releases")["run"],
            ],
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "GITHUB_REPOSITORY": "o/r",
                **fail,
            },
            capture_output=True,
            text=True,
            check=False,
        )

        assert (result.returncode == 0) is succeeds, result.stderr
        assert sorted(deleted.read_text().split()) == attempted


class TestDevVersionScript:
    def test_uses_the_semantic_release_version_the_release_runs(self) -> None:
        # The dev builds' base must be what the real release would cut, so the
        # script's pinned CLI must match the release workflow's action pin.
        script = _DEV_VERSION.read_text(encoding="utf-8")
        release = (_WORKFLOWS / "semver-release.yml").read_text(encoding="utf-8")
        script_version = re.search(r'PSR_VERSION="([\d.]+)"', script)
        action_version = re.search(
            r"python-semantic-release/python-semantic-release@\S+ # v([\d.]+)", release
        )
        assert script_version and action_version
        assert script_version.group(1) == action_version.group(1)

    def test_server_pin_names_match_the_component(self) -> None:
        # const.py must import nothing (tests load it standalone), so it
        # repeats the distribution names server_pin.py matches on.
        def load(name: str) -> Any:
            spec = importlib.util.spec_from_file_location(
                f"_parity_{name}", _COMPONENT / f"{name}.py"
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        const = load("const")
        assert load("server_pin").SERVER_DISTS == (
            const.DIST_NAME_STABLE,
            const.DIST_NAME_DEV,
        )

    def test_the_pr_version_gate_uses_the_shared_pin_rule(self) -> None:
        # One rule for what a server pin is: the gate, the stamp script, the
        # E2E seed and the component all read server_pin.py.
        gate = (_WORKFLOWS / "pr.yml").read_text(encoding="utf-8")
        assert "from server_pin import server_pins" in gate
        assert "server_pin.py" in _STAMP.read_text(encoding="utf-8")

    @pytest.mark.parametrize("workflow", ["publish-dev.yml", "addon-publish-dev.yml"])
    def test_every_dev_surface_uses_the_script(self, workflow: str) -> None:
        assert "DEV_VERSION=$(scripts/dev_version.sh)" in (
            _WORKFLOWS / workflow
        ).read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        ("event", "expected"),
        [("push", "9.0.0.dev2"), ("workflow_dispatch", "9.0.0.dev3")],
    )
    def test_counts_the_built_commit_on_push(
        self, tmp_path: Path, event: str, expected: str
    ) -> None:
        # A push build whose checkout finds master already advanced must not
        # take the next push's number: PyPI keeps the first upload, so the
        # newer build would be dropped while the HACS pre-release pinned it.
        repo = tmp_path / "repo"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_uvx = bin_dir / "uvx"
        fake_uvx.write_text("#!/bin/sh\necho 9.0.0\n")
        fake_uvx.chmod(0o755)

        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(repo), *args], check=True, capture_output=True
            )

        repo.mkdir()
        git("init", "-q", "-b", "master")
        for n in range(3):
            git(
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                str(n),
            )
        git("update-ref", "refs/remotes/origin/master", "HEAD")
        git("reset", "-q", "--hard", "HEAD~1")

        result = subprocess.run(
            ["bash", str(_DEV_VERSION)],
            cwd=repo,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "GITHUB_EVENT_NAME": event},
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected

    def test_a_dispatch_from_another_branch_uses_masters_next_release(
        self, tmp_path: Path
    ) -> None:
        # semantic-release only versions the master release group: on any
        # other branch it prints nothing, so the script computes the next
        # release from origin/master and must leave no worktree behind.
        repo = tmp_path / "repo"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_uvx = bin_dir / "uvx"
        fake_uvx.write_text(
            "#!/bin/sh\n"
            'case "$(git symbolic-ref --short HEAD)" in master*) echo 9.0.0;; esac\n'
        )
        fake_uvx.chmod(0o755)

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        repo.mkdir()
        git("init", "-q", "-b", "master")
        for n in range(3):
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                "--allow-empty", "-m", str(n))  # fmt: skip
        git("update-ref", "refs/remotes/origin/master", "HEAD")
        git("checkout", "-q", "-b", "feature")

        result = subprocess.run(
            ["bash", str(_DEV_VERSION)],
            cwd=repo,
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "9.0.0.dev3"
        assert git("branch", "--list", "master-dev-version-*") == ""
        assert len(git("worktree", "list").splitlines()) == 1


class TestReleaseStamping:
    def test_semantic_release_stamps_the_component(self) -> None:
        pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for variable in (
            "custom_components/ha_mcp_tools/manifest.json:version",
            "custom_components/ha_mcp_tools/manifest.json:ha-mcp",
            "custom_components/ha_mcp_tools/const.py:COMPONENT_VERSION",
        ):
            assert f'"{variable}"' in pyproject


@pytest.fixture
def component(tmp_path: Path) -> Path:
    target = tmp_path / "ha_mcp_tools"
    target.mkdir()
    for name in ("manifest.json", "const.py"):
        shutil.copy(_COMPONENT / name, target / name)
    return target


def _stamp(
    component: Path, version: str, pin: str, dist: str = "ha-mcp"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(_STAMP),
            "--component-dir",
            str(component),
            "--version",
            version,
            "--pin",
            pin,
            "--dist",
            dist,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


class TestStampComponentVersion:
    def test_stamps_a_dev_pre_release(self, component: Path) -> None:
        result = _stamp(component, "9.0.0", "9.0.0.dev2901", dist="ha-mcp-dev")

        assert result.returncode == 0, result.stderr
        manifest = json.loads((component / "manifest.json").read_text())
        assert manifest["version"] == "9.0.0"
        # One server requirement: two would install both distributions over
        # the same ha_mcp package.
        assert [r for r in manifest["requirements"] if r.startswith("ha-mcp")] == [
            "ha-mcp-dev==9.0.0.dev2901"
        ]
        assert 'COMPONENT_VERSION = "9.0.0"' in (component / "const.py").read_text()

    def test_restamps_a_dev_snapshot(self, component: Path) -> None:
        _stamp(component, "9.0.0", "9.0.0.dev1", dist="ha-mcp-dev")

        result = _stamp(component, "9.0.0", "9.0.0.dev2", dist="ha-mcp-dev")

        assert result.returncode == 0, result.stderr
        manifest = json.loads((component / "manifest.json").read_text())
        assert "ha-mcp-dev==9.0.0.dev2" in manifest["requirements"]

    def test_keeps_the_other_requirements(self, component: Path) -> None:
        before = json.loads((component / "manifest.json").read_text())["requirements"]

        _stamp(component, "9.0.0", "9.0.0.dev1")

        after = json.loads((component / "manifest.json").read_text())["requirements"]
        assert [r for r in after if not r.startswith("ha-mcp")] == [
            r for r in before if not r.startswith("ha-mcp")
        ]

    def test_refuses_a_suffixed_component_version(self, component: Path) -> None:
        # Released servers parse the component version as integers.
        result = _stamp(component, "9.0.0.dev2901", "9.0.0.dev2901")

        assert result.returncode == 1
        assert "X.Y.Z" in result.stderr

    def test_refuses_a_manifest_without_the_server_pin(self, component: Path) -> None:
        manifest = json.loads((component / "manifest.json").read_text())
        manifest["requirements"] = [
            r for r in manifest["requirements"] if not r.startswith("ha-mcp")
        ]
        (component / "manifest.json").write_text(json.dumps(manifest))

        result = _stamp(component, "9.0.0", "9.0.0.dev1")

        assert result.returncode == 1
        assert "exactly one ha-mcp requirement" in result.stderr
