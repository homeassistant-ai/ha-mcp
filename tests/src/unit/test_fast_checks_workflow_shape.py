"""Shape pins for pr.yml's Fast Checks lane and the repo-wide setup-uv pins.

The lane folds seven required checks into one job (#2311), and its job comment
declares two invariants load-bearing: the clean-tree validators (HACS,
Hassfest) run before any step that executes PR-controlled code, and every step
after the first check carries ``if: success() || failure()`` so one red check
cannot hide another. A comment is not a gate — these tests read the workflow
YAML directly, pass on arrival, and fire only when an edit drops a clause.

The setup-uv tests are a ratchet: ``version: "latest"`` is setup-uv's default
when the key is omitted, so the 27-site pinning sweep (#2311) regresses one
copy-paste at a time unless the pinned shape is asserted. The Renovate test
executes renovate.json's matchStrings against the real workflow text because a
custom regex manager that silently stops matching freezes every pin forever
with no red check — the exact failure the pins exist to prevent.
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_PR_YML = _WORKFLOW_DIR / "pr.yml"
_AGENTS_MD = _REPO_ROOT / "AGENTS.md"

# Steps allowed to precede the validators: the checkout, the docs-size check
# (reads AGENTS.md, executes nothing from the tree), and the fixture handling
# Hassfest itself needs. Everything else executes PR-controlled code.
_PRE_VALIDATOR_ALLOWLIST = {
    "(checkout)",
    "Docs Size Check",
    "HACS validation",
    "Remove vendored test fixtures",
    "Hassfest",
    "Restore vendored test fixtures",
}


def _fast_checks_job() -> dict[str, Any]:
    data = yaml.safe_load(_PR_YML.read_text(encoding="utf-8"))
    return data["jobs"]["fast-checks"]


def _steps() -> list[dict[str, Any]]:
    return _fast_checks_job()["steps"]


def _step_name(step: dict[str, Any]) -> str:
    return step.get("name") or "(checkout)"


def test_docs_size_check_enforces_root_instruction_budgets() -> None:
    """Workflow limits, root prose, and the current file stay synchronized."""
    step = next(step for step in _steps() if _step_name(step) == "Docs Size Check")
    run = str(step["run"])
    agents = _AGENTS_MD.read_text(encoding="utf-8")

    assert "LC_ALL=C.UTF-8 wc -m < AGENTS.md" in run
    assert "wc -l < AGENTS.md" in run
    char_limit = re.search(r"^\s*max_chars=(\d+)$", run, re.MULTILINE)
    line_limit = re.search(r"^\s*max_lines=(\d+)$", run, re.MULTILINE)
    prose_limits = re.search(
        r"Hard limit: ([\d,]+) Unicode characters and ([\d,]+) lines",
        agents,
    )

    assert char_limit and line_limit and prose_limits
    max_chars = int(char_limit.group(1))
    max_lines = int(line_limit.group(1))
    assert (max_chars, max_lines) == tuple(
        int(value.replace(",", "")) for value in prose_limits.groups()
    )
    assert '[ "$chars" -gt "$max_chars" ]' in run
    assert '[ "$lines" -gt "$max_lines" ]' in run
    assert "::error file=AGENTS.md" in run
    assert "exit 1" in run
    assert len(agents) <= max_chars
    assert len(agents.splitlines()) <= max_lines


def test_clean_tree_validators_precede_pr_controlled_execution() -> None:
    """ORDER IS LOAD-BEARING: uv sync runs the PR's own PEP 517 backend."""
    names = [_step_name(step) for step in _steps()]
    hassfest = names.index("Hassfest")
    hacs = names.index("HACS validation")
    assert hacs < hassfest
    for index, step in enumerate(_steps()):
        text = str(step.get("run", "")) + str(step.get("uses", ""))
        executes_tree = "uv" in text.split("#")[0] or "setup-uv" in text
        if executes_tree:
            assert index > hassfest, (
                f"step {_step_name(step)!r} executes PR-controlled code before "
                "the clean-tree validators - a PR-supplied build backend or "
                "script could rewrite the tree HACS/Hassfest certify "
                "(pr.yml ORDER IS LOAD-BEARING, Codex review on #2314)"
            )
    for name in names[: hassfest + 1]:
        assert name in _PRE_VALIDATOR_ALLOWLIST, (
            f"step {name!r} was inserted above Hassfest - only steps that "
            "execute nothing from the tree may precede the validators"
        )


def test_every_step_after_the_first_check_reports_on_a_red_run() -> None:
    """The failure-attribution requirement: one red cannot hide another."""
    steps = _steps()
    for step in steps[2:]:
        assert step.get("if") == "success() || failure()", (
            f"step {_step_name(step)!r} would be skipped after an earlier red "
            "step, hiding its own result - and `always()` is deliberately "
            "rejected so a cancelled run stops (pr.yml failure-attribution "
            "comment)"
        )


def test_mypy_step_accumulates_instead_of_aborting() -> None:
    """Under `bash -e` the first failing tree would hide the other two."""
    mypy = next(step for step in _steps() if _step_name(step) == "Run mypy")
    run = str(mypy["run"])
    for line in run.splitlines():
        if "uv run mypy" in line:
            assert "|| status=1" in line, (
                "a mypy invocation without `|| status=1` aborts the block on "
                "its first error and hides every later tree's errors"
            )
    assert 'exit "$status"' in run


def test_fixture_restore_verifies_its_own_postcondition() -> None:
    """A silently failed restore shrinks ruff/ast-grep/format coverage."""
    names = [_step_name(step) for step in _steps()]
    restore = names.index("Restore vendored test fixtures")
    assert restore == names.index("Hassfest") + 1
    run = str(_steps()[restore]["run"])
    assert "git checkout HEAD -- tests/initial_test_state" in run
    assert "git diff --quiet HEAD -- tests/initial_test_state" in run, (
        "the restore must assert its postcondition: the fixture tree holds "
        "tracked .py files the later lint steps walk"
    )


def test_pr_time_hacs_and_hassfest_survive_in_the_lane() -> None:
    """validate.yml lost its pull_request trigger; this lane is the only
    PR-time run of both validators now."""
    uses = [str(step.get("uses", "")) for step in _steps()]
    assert any(u.startswith("hacs/action@") for u in uses)
    assert any(u.startswith("home-assistant/actions/hassfest@") for u in uses)


def test_lane_timeout_matches_the_fetch_budget_docstring() -> None:
    """test_ha_constraint_alignment sizes its 60s fetch budget against this
    number; keep the two in sync."""
    assert _fast_checks_job()["timeout-minutes"] == 15


def _setup_uv_sites() -> list[tuple[Path, dict[str, Any]]]:
    sites = []
    for path in sorted(_WORKFLOW_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        for job in (data.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            sites.extend(
                (path, step)
                for step in job.get("steps", [])
                if isinstance(step, dict)
                and str(step.get("uses", "")).startswith("astral-sh/setup-uv")
            )
    return sites


def test_every_setup_uv_site_pins_a_full_version() -> None:
    """`latest` is setup-uv's default when `version:` is omitted - the 28th
    site reintroduces the floating-tool problem the sweep removed (#2311)."""
    sites = _setup_uv_sites()
    assert len(sites) >= 20, "the setup-uv sweep matched almost nothing"
    for path, step in sites:
        version = (step.get("with") or {}).get("version")
        assert isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version), (
            f"{path.name}: setup-uv must pin a full x.y.z version, got "
            f"{version!r} - an omitted key or 'latest' floats with uv "
            "releases and can redden CI with zero repo change"
        )


def test_renovate_matchstrings_actually_match_the_setup_uv_pins() -> None:
    """A custom regex manager that stops matching fails open: no PR, no red
    check, every pin frozen forever."""
    config = json.loads((_REPO_ROOT / "renovate.json").read_text())
    patterns = [
        re.compile(match.replace("?<", "?P<"), re.DOTALL)
        for manager in config["customManagers"]
        for match in manager["matchStrings"]
    ]
    for path, step in _setup_uv_sites():
        version = (step.get("with") or {}).get("version")
        text = path.read_text(encoding="utf-8")
        captured = {
            found.group("currentValue")
            for pattern in patterns
            for found in pattern.finditer(text)
        }
        assert version in captured, (
            f"{path.name}: no renovate.json matchString captures the pin "
            f"{version!r} - Renovate would silently never bump it"
        )


def test_renovate_groups_the_uv_pin_with_the_uv_container_image() -> None:
    """pr.yml's comment promises the setup-uv pin and the unit-tests container
    image move in one Renovate PR; only a packageRules group delivers that."""
    config = json.loads((_REPO_ROOT / "renovate.json").read_text())
    for rule in config.get("packageRules", []):
        if set(rule.get("matchDepNames", [])) >= {
            "astral-sh/uv",
            "ghcr.io/astral-sh/uv",
        }:
            assert rule.get("groupName"), "the uv rule must set a groupName"
            return
    raise AssertionError(
        "renovate.json has no packageRules entry grouping astral-sh/uv "
        "(github-releases) with ghcr.io/astral-sh/uv (docker) - the two pins "
        "would drift across separate Renovate PRs"
    )
