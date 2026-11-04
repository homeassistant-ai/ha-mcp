"""Temporary runner-only proof; this file is not part of the proposed PR."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path.cwd()
BASE = "12ffbe26"
PRODUCT = "88a6758bf6654ae14309ffb97abd73ab1d62c2df"


def run(*args, cwd=ROOT, **kwargs):
    return subprocess.run(args, cwd=cwd, text=True, check=True, **kwargs)


def git(repo, *args):
    return run(
        "git", "-c", "user.name=Release proof", "-c",
        "user.email=release-proof@example.invalid", *args, cwd=repo,
        capture_output=True,
    ).stdout.strip()


with tempfile.TemporaryDirectory() as temporary:
    for label, message, expected in [
        ("maintenance", "chore: maintenance after stable", "9.1.1.dev2"),
        ("docs", "docs: clarify usage", "9.1.1.dev2"),
        ("refactor", "refactor: simplify implementation", "9.1.1.dev2"),
        ("fix", "fix: repair a defect", "9.1.1.dev2"),
        ("feature", "feat: add a capability", "9.2.0.dev2"),
        ("breaking", "feat: replace the protocol\n\nBREAKING CHANGE: replace old protocol", "10.0.0.dev2"),
    ]:
        repo = Path(temporary) / label
        repo.mkdir()
        git(repo, "init", "-q", "-b", "master")
        git(repo, "remote", "add", "origin", "https://github.com/homeassistant-ai/ha-mcp.git")
        for relative in [
            "pyproject.toml", "custom_components/ha_mcp_tools/manifest.json",
            "custom_components/ha_mcp_tools/const.py",
        ]:
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / relative, target)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "chore: seed stable release")
        git(repo, "tag", "v9.1.0")
        git(repo, "commit", "-qm", message, "--allow-empty")
        git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
        before = git(repo, "rev-parse", "HEAD")
        result = run(
            "bash", str(ROOT / "scripts/dev_version.sh"), cwd=repo,
            env={**os.environ, "GITHUB_EVENT_NAME": "push"}, capture_output=True,
        )
        actual = result.stdout.strip()
        print(f"{label}: {actual}; expected {expected}", flush=True)
        assert actual == expected, result.stderr
        assert git(repo, "status", "--porcelain") == ""
        assert git(repo, "rev-parse", "HEAD") == before
        assert git(repo, "tag", "--list") == "v9.1.0"

    selected = [
        "tests/src/unit/test_mirror_sync_workflow_shape.py::TestDevVersionScript::test_dev_builds_sort_after_stable_and_count_the_selected_commit",
        "tests/src/unit/test_mirror_sync_workflow_shape.py::TestStampComponentVersion::test_stamps_a_dev_pre_release",
        "tests/src/unit/test_caller_token_auth.py::TestGetCallerTokenManifestVersion::test_bootstrap_version_remains_parseable_by_old_servers",
        "tests/src/unit/test_embedded_setup.py::TestComponentCompat::test_satisfied_component_clears_issue",
    ]
    run("uv", "run", "pytest", *selected, "-q", "--maxfail=0", "--tb=short")
    reverted = [
        "scripts/dev_version.sh", "scripts/stamp_component_version.py",
        "custom_components/ha_mcp_tools/__init__.py",
        "custom_components/ha_mcp_tools/embedded_setup.py",
    ]
    report = Path(temporary) / "before.xml"
    try:
        git(ROOT, "restore", f"--source={BASE}", "--", *reverted)
        failed = subprocess.run(
            ["uv", "run", "pytest", *selected, "-q", "--maxfail=0", "--tb=short", f"--junitxml={report}"],
            cwd=ROOT, text=True, check=False,
        )
        assert failed.returncode == 1, f"Expected assertion failures, got {failed.returncode}"
        cases = ET.parse(report).findall(".//testcase")
        assert not any(case.find("error") is not None for case in cases)
        failures = [case.attrib["name"] for case in cases if case.find("failure") is not None]
        for target in selected:
            name = target.rsplit("::", 1)[-1]
            assert any(failure.startswith(name) for failure in failures), (name, failures)
        print(f"Confirmed regressions against the original code: {failures}", flush=True)
    finally:
        git(ROOT, "restore", f"--source={PRODUCT}", "--", *reverted)
