"""Guard supply-chain hardening that cannot be exercised by PR workflows."""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"


def _workflow(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(data: dict[str, Any]) -> set[str]:
    on_node = data.get(True) or data.get("on") or {}
    if isinstance(on_node, str):
        return {on_node}
    if isinstance(on_node, list):
        return set(on_node)
    return set(on_node)


def _assert_checkout_credentials_are_not_persisted(
    path: Path, repo_root: Path, visited: set[Path] | None = None
) -> int:
    path = path.resolve()
    visited = visited or set()
    if path in visited:
        return 0
    visited.add(path)

    data = _workflow(path)
    checked = 0
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            if "actions/checkout" not in str(step.get("uses", "")):
                continue
            checked += 1
            persist_credentials = (step.get("with") or {}).get("persist-credentials")
            assert persist_credentials is False or persist_credentials == "false", (
                f"{path.relative_to(repo_root)} persists checkout credentials in a "
                "pull_request workflow"
            )

        uses = job.get("uses")
        if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
            called_path = repo_root / uses.removeprefix("./")
            checked += _assert_checkout_credentials_are_not_persisted(
                called_path, repo_root, visited
            )

    return checked


def test_pr_workflows_do_not_persist_checkout_credentials() -> None:
    checked_workflows = 0
    checked_checkouts = 0
    workflow_paths = sorted(
        path for path in _WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"}
    )
    for path in workflow_paths:
        data = _workflow(path)
        if "pull_request" not in _triggers(data):
            continue

        checked_workflows += 1
        checked_checkouts += _assert_checkout_credentials_are_not_persisted(
            path, _REPO_ROOT
        )

    assert checked_workflows, "trigger derivation matched no pull_request workflow"
    assert checked_checkouts, "pull_request workflows contained no checkout steps"


def test_pr_checkout_guard_follows_reusable_workflows(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    caller = workflow_dir / "caller.yml"
    caller.write_text(
        "jobs:\n  delegated:\n    uses: ./.github/workflows/reusable.yaml\n",
        encoding="utf-8",
    )
    reusable = workflow_dir / "reusable.yaml"
    reusable.write_text(
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n"
        '        with:\n          persist-credentials: "false"\n',
        encoding="utf-8",
    )
    assert _assert_checkout_credentials_are_not_persisted(caller, tmp_path) == 1

    reusable.write_text(
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=r"reusable\.yaml persists"):
        _assert_checkout_credentials_are_not_persisted(caller, tmp_path)


def test_renovate_token_can_read_vulnerability_alerts() -> None:
    steps = _workflow(_WORKFLOW_DIR / "renovate.yml")["jobs"]["renovate"]["steps"]
    token_step = next(
        step
        for step in steps
        if "actions/create-github-app-token" in str(step.get("uses", ""))
    )

    token_inputs = token_step["with"]
    assert token_inputs.get("client-id") == "${{ secrets.RENOVATE_APP_ID }}"
    assert "app-id" not in token_inputs
    assert token_inputs.get("permission-vulnerability-alerts") == "read", (
        "the Renovate token lists permission-* inputs and so drops every "
        "permission it does not name; without vulnerability_alerts read the "
        "vulnerabilityAlerts carve-out in renovate.json cannot fire"
    )


def test_renovate_engine_uses_a_full_version_pin() -> None:
    steps = _workflow(_WORKFLOW_DIR / "renovate.yml")["jobs"]["renovate"]["steps"]
    renovate_step = next(
        step
        for step in steps
        if "renovatebot/github-action" in str(step.get("uses", ""))
    )

    version = str((renovate_step.get("with") or {}).get("renovate-version", ""))
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        "the action version and Renovate engine version are separate pins; a "
        "major-only engine tag changes behavior without a workflow diff"
    )

    config = json.loads((_REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    engine_manager = next(
        manager
        for manager in config["customManagers"]
        if manager.get("depNameTemplate") == "renovatebot/renovate"
    )
    assert engine_manager.get("datasourceTemplate") == "github-releases", (
        "the engine self-update must use a datasource with release timestamps so "
        "the global age gate can eventually release it"
    )


def test_renovate_age_gate_does_not_freeze_timestamp_less_updates() -> None:
    config = json.loads((_REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))

    assert config.get("minimumReleaseAge") == "7 days", (
        "the release-age gate from #2196 is part of the supply-chain contract"
    )
    assert config.get("minimumReleaseAgeBehaviour") == "timestamp-optional", (
        "timestamp-required permanently freezes updates from registries that do "
        "not publish release timestamps"
    )
    required_rules = [
        rule
        for rule in config["packageRules"]
        if rule.get("minimumReleaseAgeBehaviour") == "timestamp-required"
    ]
    assert not required_rules, (
        "package-level timestamp requirements can recreate the permanent freeze "
        "fixed by #2200"
    )


def test_renovate_log_levels_make_failures_actionable_without_warning_noise() -> None:
    config = json.loads((_REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    remaps = config.get("logLevelRemap", [])

    host_error_levels = [
        remap.get("newLogLevel")
        for remap in remaps
        if "External host error causing abort - skipping"
        in str(remap.get("matchMessage", ""))
    ]
    assert host_error_levels == ["error"], (
        "an aborted repository scan must fail the workflow instead of finishing green"
    )

    git_error_levels = [
        remap.get("newLogLevel")
        for remap in remaps
        if remap.get("matchMessage") == "Git error - aborting"
    ]
    assert git_error_levels == ["error"], (
        "a Git 5xx abort also returns external-host-error and must fail the workflow"
    )

    timestamp_warning_levels = [
        remap.get("newLogLevel")
        for remap in remaps
        if "minimumReleaseAgeBehaviour=timestamp-optional"
        in str(remap.get("matchMessage", ""))
    ]
    assert timestamp_warning_levels == ["info"], (
        "the expected warning for timestamp-less sources should not pollute the "
        "dependency dashboard"
    )


def _tracked_dockerfiles() -> set[Path]:
    """Every Dockerfile Renovate can see, which is exactly the tracked ones.

    Enumerated through Git rather than walking the filesystem. This project's
    workflow tells contributors to keep worktrees under `worktree/` (AGENTS.md)
    and agents place theirs under `.claude/worktrees/`, so a walk finds whole
    nested copies of the repository and the assertion below fails on any
    machine following those instructions.

    Excluding those by directory NAME is the wrong instrument: `.claude` also
    holds the tracked `.claude/skills/` tree, so a Dockerfile added there later
    would be invisible to this guard while Renovate still applied its rules to
    it. Tracked-ness is the property that actually matters, so ask Git for it.

    Raises rather than degrading if Git cannot answer: silently scanning
    nothing would let this guard pass while asserting about an empty set.
    """
    listing = subprocess.run(
        [
            "git",
            # The unit-test job runs inside a container with the workspace
            # bind-mounted from the host, so the checkout is owned by a
            # different uid than the process and Git's dubious-ownership
            # check refuses to read the index at all. Scoped to this one
            # invocation; no Git config is written anywhere.
            "-c",
            "safe.directory=*",
            "-C",
            str(_REPO_ROOT),
            "ls-files",
            "-z",
            "Dockerfile",
            "*/Dockerfile",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # Carry Git's own stderr into the failure. ``check=True`` would raise
    # ``CalledProcessError`` naming only the exit status, which says nothing
    # about why Git refused.
    assert listing.returncode == 0, (
        "cannot enumerate tracked Dockerfiles, so this guard cannot run: "
        f"git exited {listing.returncode}: {listing.stderr.strip()}"
    )
    return {_REPO_ROOT / rel for rel in listing.stdout.split("\0") if rel}


def test_python_runtime_automation_is_digest_only() -> None:
    config = json.loads((_REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    package_rules = config["packageRules"]
    python_rules = [
        rule
        for rule in package_rules
        if "dockerfile" in rule.get("matchManagers", [])
        and "python" in rule.get("matchPackageNames", [])
    ]

    version_rule = next(
        rule
        for rule in python_rules
        if rule.get("allowedVersions") == "/^3\\.13-slim$/"
    )
    assert "matchFileNames" not in version_rule, (
        "every Python Dockerfile must inherit the coordinated 3.13-slim baseline"
    )

    proxy_rule = next(rule for rule in python_rules if rule.get("enabled") is False)
    assert set(proxy_rule.get("matchFileNames", [])) == {
        "homeassistant-addon-webhook-proxy/Dockerfile",
        "homeassistant-addon-webhook-proxy-dev/Dockerfile",
    }, "webhook-proxy images change only through the dev-first promotion workflow"

    python_dockerfiles = {
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _tracked_dockerfiles()
        if "FROM python:" in path.read_text(encoding="utf-8")
    }
    assert python_dockerfiles - set(proxy_rule["matchFileNames"]) == {
        "Dockerfile",
        "homeassistant-addon/Dockerfile",
        "homeassistant-addon-dev/Dockerfile",
        "tests/haos_image_build/screenshot_engine_mock/Dockerfile",
    }
    assert not any("matchUpdateTypes" in rule for rule in python_rules), (
        "a pre-lookup enabled=false rule cannot be overridden later for digest updates"
    )
    assert "asdf" not in config.get("enabledManagers", [])
    assert not any("asdf" in rule.get("matchManagers", []) for rule in package_rules), (
        "Python minor-version changes span independent runtime contracts and must "
        "not be rewritten by the old dead asdf task"
    )
    assert not any("postUpgradeTasks" in rule for rule in python_rules)


def test_dev_release_tag_cleanup_uses_authenticated_github_api() -> None:
    jobs = _workflow(_WORKFLOW_DIR / "publish-dev.yml")["jobs"]
    create_run = next(
        step["run"]
        for step in jobs["create-prerelease"]["steps"]
        if step.get("name") == "Create pre-release"
    )
    cleanup_run = next(
        step["run"]
        for step in jobs["cleanup-old-prereleases"]["steps"]
        if step.get("name") == "Delete old dev releases (keep last 5)"
    )

    assert (
        'gh api -X DELETE "repos/${GITHUB_REPOSITORY}/git/refs/tags/$TAG"' in create_run
    )
    assert (
        'gh api -X DELETE "repos/${GITHUB_REPOSITORY}/git/refs/tags/$tag"'
        in cleanup_run
    )
    assert "git push origin --delete" not in create_run + cleanup_run


def test_renovate_can_write_the_status_check_it_publishes() -> None:
    """Renovate must not abort its whole run right after writing a branch.

    ``minimumReleaseAge`` makes Renovate publish a stability status check on
    every branch it writes. GitHub answers a commit-status call from a token
    without that permission with 404, not 403, and Renovate reads 404 there as
    the repository having changed underneath it and aborts the ENTIRE run,
    before opening the PR, before updating the dependency dashboard, and
    before processing any other dependency. That is silent: the abort logs
    below error level, so the workflow still reports success.

    This pins the workflow half only. The app installation must grant
    ``statuses`` as well, which no test here can see: an installation token
    can only narrow the permissions the installation already holds.
    """
    steps = _workflow(_WORKFLOW_DIR / "renovate.yml")["jobs"]["renovate"]["steps"]
    token_step = next(
        step for step in steps if "create-github-app-token" in str(step.get("uses", ""))
    )
    assert token_step["with"].get("permission-statuses") == "write", (
        "Renovate needs commit-status write, and a token listing any "
        "permission-* input drops every permission it does not name"
    )


def test_a_renovate_abort_fails_the_workflow() -> None:
    """A run that dies mid-way must not report success.

    Renovate exits non-zero only when some record is logged at error level or
    above, so a fatal abort logged at info leaves the workflow green. This one
    hid two weeks of dead runs.
    """
    config = json.loads((_REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    promoted = {
        remap["matchMessage"]
        for remap in config["logLevelRemap"]
        if remap["newLogLevel"] == "error"
    }
    assert "Repository has changed during renovation - aborting" in promoted, (
        "this abort ends the whole repository run, so it cannot stay at info"
    )
