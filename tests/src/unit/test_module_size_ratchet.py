"""Module-size ratchet: a source file over the line limit may not grow.

``AGENTS.md`` asks for modules of about 1,000 lines. ``module_size_baseline.json``
lists every tracked source file above that limit with its exact line count. A
listed file must match its count, and no other file may cross the limit. When a
listed file shrinks, ``python scripts/module_size_ratchet.py`` lowers its entry;
that command can lower or drop an entry but never raise or add one.

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


def test_listed_file_that_shrank_must_lower_its_entry() -> None:
    """A stale high entry would let the file grow back to its old size."""
    violations = ratchet.find_violations(
        {"src/big.py": 1500}, {"src/big.py": 2000}, LIMIT
    )

    assert len(violations) == 1
    assert "scripts/module_size_ratchet.py" in violations[0]


def test_entry_for_a_file_that_is_gone_is_rejected() -> None:
    """A leftover entry would let a new file reuse the path at the old size."""
    violations = ratchet.find_violations({}, {"src/deleted.py": 2000}, LIMIT)

    assert len(violations) == 1
    assert "src/deleted.py" in violations[0]


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


def test_staged_measurement_ignores_an_unstaged_shrink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit hook stages a baseline lowered to the measured sizes. If it
    measured the working tree, a shrink left out of the commit would lower the
    entry, and the committed file would no longer match it in CI."""
    # Inside a git hook these point every git call at the real repository.
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_PREFIX",
        "GIT_COMMON_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nextend-exclude = []\n", encoding="utf-8"
    )
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * (LIMIT + 2), encoding="utf-8")
    subprocess.run(["git", "add", "big.py"], cwd=tmp_path, check=True)
    big.write_text("x = 1\n" * (LIMIT + 1), encoding="utf-8")

    assert ratchet.measure(tmp_path, staged=True) == {"big.py": LIMIT + 2}
    assert ratchet.measure(tmp_path) == {"big.py": LIMIT + 1}


def test_repository_matches_the_baseline() -> None:
    """Fails when a source file crosses the limit or a listed file changes size."""
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    violations = ratchet.find_violations(
        ratchet.measure(REPO_ROOT), baseline, ratchet.LINE_LIMIT
    )

    assert not violations, "\n" + "\n".join(violations)
