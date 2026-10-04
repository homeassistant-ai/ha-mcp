"""Module-size ratchet: a source file over the line limit may not grow.

``AGENTS.md`` asks for modules of about 1,000 lines. ``module_size_baseline.json``
lists every tracked source file above that limit with its line count. A listed
file may not grow past its count, and no other file may cross the limit. A
listed file that shrank passes; after the merge,
``python scripts/module_size_ratchet.py`` lowers its entry on master. That
command can lower or drop an entry but never raise or add one.

The repository pin at the bottom passes on arrival and fails only when a file
grows. The tests above it drive the rules with made-up sizes.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from ._ratchet_repo import commit, make_ratchet_repo

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "module_size_ratchet.py"
BASELINE_PATH = Path(__file__).with_name("module_size_baseline.json")


def _load_ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("module_size_ratchet", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ratchet = _load_ratchet()
LIMIT = 1000


def test_new_file_over_the_limit_is_rejected() -> None:
    """Without this a new oversized module lands with no baseline entry."""
    violations = ratchet.find_violations({"src/new.py": LIMIT + 1}, {}, LIMIT)

    assert len(violations) == 1
    assert "src/new.py" in violations[0]


def test_listed_file_that_grew_is_rejected() -> None:
    """Without this a file already over the limit keeps growing."""
    violations = ratchet.find_violations(
        {"src/big.py": 2001}, {"src/big.py": 2000}, LIMIT
    )

    assert len(violations) == 1
    assert "src/big.py" in violations[0]


def test_listed_file_that_shrank_passes() -> None:
    """A pull request that shrinks a file must not have to edit the shared
    baseline: two such edits conflict. The post-merge run lowers the entry."""
    sizes = {"src/big.py": 1500}

    assert ratchet.find_violations(sizes, {"src/big.py": 2000}, LIMIT) == []


def test_entry_for_a_file_that_is_gone_passes() -> None:
    """Splitting a listed module deletes it; the post-merge run drops it."""
    assert ratchet.find_violations({}, {"src/deleted.py": 2000}, LIMIT) == []


def test_matching_baseline_and_small_files_pass() -> None:
    sizes = {"src/big.py": 2000, "src/small.py": LIMIT}

    assert ratchet.find_violations(sizes, {"src/big.py": 2000}, LIMIT) == []


def test_every_violation_is_reported_in_one_run() -> None:
    """Stopping at the first one costs a full test run per offending file."""
    sizes = {"src/new.py": LIMIT + 1, "src/big.py": 2001}

    violations = ratchet.find_violations(sizes, {"src/big.py": 2000}, LIMIT)

    assert len(violations) == 2


def test_lowering_the_baseline_cannot_raise_or_add_an_entry() -> None:
    """Otherwise rerunning the command would accept any growth."""
    sizes = {"src/big.py": 2500, "src/new.py": LIMIT + 1}

    lowered = ratchet.lowered_baseline(sizes, {"src/big.py": 2000}, LIMIT)

    assert lowered == {"src/big.py": 2000}


def test_lowering_the_baseline_follows_shrunk_and_removed_files() -> None:
    sizes = {"src/big.py": 1500, "src/fixed.py": LIMIT}
    baseline = {"src/big.py": 2000, "src/fixed.py": 1200, "src/deleted.py": 3000}

    lowered = ratchet.lowered_baseline(sizes, baseline, LIMIT)

    assert lowered == {"src/big.py": 1500}


def test_stable_proxy_copy_is_not_counted() -> None:
    """The stable proxy tree is copied from the dev tree by the promote
    workflow. Counting it would fail every promote of a dev file that changed
    size, although the dev file already passed this check."""
    excluded = ratchet.excluded_prefixes(REPO_ROOT)

    assert not ratchet.in_scope("homeassistant-addon-webhook-proxy/start.py", excluded)
    assert ratchet.in_scope("homeassistant-addon-webhook-proxy-dev/start.py", excluded)


def test_vendored_code_is_not_counted() -> None:
    """Vendored trees are upstream's files; a version bump must not fail here."""
    excluded = ratchet.excluded_prefixes(REPO_ROOT)

    assert not ratchet.in_scope("src/ha_mcp/_vendor/fastmcp/server.py", excluded)


def test_last_line_without_a_newline_is_counted() -> None:
    """Otherwise a 1,001-line file with no final newline passes as 1,000."""
    assert ratchet.count_lines(b"a\nb\nc") == 3
    assert ratchet.count_lines(b"a\nb\nc\n") == 3


@pytest.fixture
def temp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty git repository with no exclusions and an empty baseline."""
    return make_ratchet_repo(tmp_path, monkeypatch, ratchet.BASELINE_NAME)


def _stage(repo: Path, path: str, lines: int) -> None:
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("x = 1\n" * lines, encoding="utf-8")
    subprocess.run(["git", "add", path], cwd=repo, check=True)


def _stage_baseline(repo: Path, text: str) -> None:
    (repo / ratchet.BASELINE_NAME).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", ratchet.BASELINE_NAME], cwd=repo, check=True)


def test_staged_measurement_ignores_an_unstaged_shrink(temp_repo: Path) -> None:
    """The commit hook judges what the commit holds. Measuring the working
    tree would let an unstaged shrink hide growth the commit stages."""
    _stage(temp_repo, "big.py", LIMIT + 2)
    (temp_repo / "big.py").write_text("x = 1\n" * (LIMIT + 1), encoding="utf-8")

    assert ratchet.measure(temp_repo, staged=True) == {"big.py": LIMIT + 2}
    assert ratchet.measure(temp_repo) == {"big.py": LIMIT + 1}


def test_staged_measurement_uses_the_staged_exclusions(temp_repo: Path) -> None:
    """An exclusion left unstaged must not hide a file the commit contains."""
    _stage(temp_repo, "vendor/big.py", LIMIT + 1)
    (temp_repo / "pyproject.toml").write_text(
        '[tool.ruff]\nextend-exclude = ["vendor"]\n', encoding="utf-8"
    )

    assert ratchet.measure(temp_repo, staged=True) == {"vendor/big.py": LIMIT + 1}


def test_hook_rejects_a_staged_oversized_file(temp_repo: Path) -> None:
    """A commit that stages only a .js or .astro file runs no unit tests, so
    the hook command itself must fail on a new file over the limit."""
    _stage(temp_repo, "page.astro", LIMIT + 1)

    assert ratchet.main(["--staged", "--check"], repo_root=temp_repo) == 1


def test_staged_run_ignores_an_unstaged_baseline_edit(temp_repo: Path) -> None:
    """Growth staged for the commit must not pass because an unstaged edit
    raises the entry."""
    _stage(temp_repo, "big.py", LIMIT + 2)
    (temp_repo / ratchet.BASELINE_NAME).write_text(
        json.dumps({"big.py": LIMIT + 1}), encoding="utf-8"
    )
    subprocess.run(["git", "add", ratchet.BASELINE_NAME], cwd=temp_repo, check=True)
    (temp_repo / ratchet.BASELINE_NAME).write_text(
        json.dumps({"big.py": LIMIT + 2}), encoding="utf-8"
    )

    assert ratchet.main(["--staged", "--check"], repo_root=temp_repo) == 1


def test_hook_never_writes_the_baseline(temp_repo: Path) -> None:
    """The hook runs on every commit. If it lowered the baseline, every pull
    request touching a listed file would carry an edit to the one shared file,
    and those edits conflict with each other."""
    _stage(temp_repo, "big.py", LIMIT + 1)
    listed = json.dumps({"big.py": LIMIT + 5})
    _stage_baseline(temp_repo, listed)

    assert ratchet.main(["--staged", "--check"], repo_root=temp_repo) == 0
    assert (temp_repo / ratchet.BASELINE_NAME).read_text(encoding="utf-8") == listed


def test_post_merge_run_lowers_the_baseline(temp_repo: Path) -> None:
    """The sync workflow's run is what keeps an entry from staying high
    enough to let its file grow back."""
    _stage(temp_repo, "big.py", LIMIT + 1)
    _stage_baseline(temp_repo, json.dumps({"big.py": LIMIT + 5, "gone.py": 2000}))

    assert ratchet.main([], repo_root=temp_repo) == 0
    assert json.loads((temp_repo / ratchet.BASELINE_NAME).read_text()) == {
        "big.py": LIMIT + 1
    }


def test_baseline_raised_or_added_over_the_base_is_rejected() -> None:
    """The baseline is a plain file. Without this check an agent blocked by
    the ratchet could raise its own entry and every other check would pass."""
    growth = ratchet.find_growth(
        {"src/big.py": 2001, "src/new.py": 1500}, {"src/big.py": 2000}
    )

    assert len(growth) == 2


def test_baseline_lowered_or_dropped_against_the_base_passes() -> None:
    base = {"src/big.py": 2000, "src/fixed.py": 3000}

    assert ratchet.find_growth({"src/big.py": 1500}, base) == []


def test_base_check_rejects_a_hand_raised_entry(temp_repo: Path) -> None:
    """The CI step compares the checked-out baseline with the base branch's."""
    (temp_repo / ratchet.BASELINE_NAME).write_text(
        json.dumps({"big.py": LIMIT + 1}), encoding="utf-8"
    )
    subprocess.run(["git", "add", ratchet.BASELINE_NAME], cwd=temp_repo, check=True)
    commit(temp_repo)
    (temp_repo / ratchet.BASELINE_NAME).write_text(
        json.dumps({"big.py": LIMIT + 5}), encoding="utf-8"
    )

    assert ratchet.main(["--base", "HEAD"], repo_root=temp_repo) == 1


def test_base_check_rejects_growth_under_a_stale_entry(temp_repo: Path) -> None:
    """Until the sync runs, the base's entry can sit above its file. Growing
    the file back under that entry must fail: the sync would lower the entry
    from the base and leave master over it."""
    _stage(temp_repo, "big.py", LIMIT + 1)
    _stage_baseline(temp_repo, json.dumps({"big.py": LIMIT + 5}))
    commit(temp_repo)
    (temp_repo / "big.py").write_text("x = 1\n" * (LIMIT + 3), encoding="utf-8")

    assert ratchet.main(["--base", "HEAD"], repo_root=temp_repo) == 1


def test_base_check_rejects_a_nan_entry(temp_repo: Path) -> None:
    """Every comparison with NaN is false, so a NaN entry would let its file
    grow past the base check and the repository pin alike."""
    (temp_repo / ratchet.BASELINE_NAME).write_text(
        json.dumps({"big.py": LIMIT + 1}), encoding="utf-8"
    )
    subprocess.run(["git", "add", ratchet.BASELINE_NAME], cwd=temp_repo, check=True)
    commit(temp_repo)
    (temp_repo / ratchet.BASELINE_NAME).write_text('{"big.py": NaN}', encoding="utf-8")

    with pytest.raises(ValueError, match="NaN"):
        ratchet.main(["--base", "HEAD"], repo_root=temp_repo)


def test_base_without_the_baseline_passes(temp_repo: Path) -> None:
    """The pull request that adds a baseline has nothing to compare it with."""
    subprocess.run(
        ["git", "rm", "-q", "--cached", ratchet.BASELINE_NAME],
        cwd=temp_repo,
        check=True,
    )
    commit(temp_repo)

    assert ratchet.main(["--base", "HEAD"], repo_root=temp_repo) == 0


def test_unknown_base_is_an_error(temp_repo: Path) -> None:
    """A mistyped ref in the workflow must fail the step, not skip the check."""
    commit(temp_repo)

    with pytest.raises(subprocess.CalledProcessError):
        ratchet.main(["--base", "no-such-ref"], repo_root=temp_repo)


def test_repository_matches_the_baseline() -> None:
    """Fails when a source file crosses the limit or a listed file grows."""
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    violations = ratchet.find_violations(
        ratchet.measure(REPO_ROOT), baseline, ratchet.LINE_LIMIT
    )

    assert not violations, "\n" + "\n".join(violations)
