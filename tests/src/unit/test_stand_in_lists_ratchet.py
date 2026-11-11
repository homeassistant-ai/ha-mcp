"""A pull request cannot exempt a test from the real-Core checks.

The lists of older test files are plain files. ``stand_in_lists_ratchet.py``
compares them with the pull request's base, and these tests run it on a small
repository: one commit is the base, the working tree is the pull request.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from ._ratchet_repo import commit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "stand_in_lists_ratchet.py"

_RULE_PATH = "rules/no-hand-made-core-replies.yml"
_RULE = """id: no-hand-made-core-replies
language: python
rule:
  kind: assignment
files:
  - "tests/src/unit/**"
  - "tests/addon/**"
ignores:
  - "tests/src/unit/test_old.py"
message: refused
"""
_GUARD_PATH = "tests/core_stand_in_guard.py"
_GUARD = '_GRANDFATHERED = frozenset({"tests/src/unit/test_old.py"})\n'
_RUFF_PATH = "tests/src/unit/ruff.toml"
_RUFF = (
    '[lint]\nextend-select = ["SLF001"]\n\n'
    '[lint.extend-per-file-ignores]\n"test_old.py" = ["SLF001"]\n'
)
_ADDON_RUFF = '[lint]\nextend-select = ["SLF001"]\n'
_PYPROJECT = """[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disable_error_code = ["import-untyped"]

[[tool.mypy.overrides]]
module = ["tests.src.unit.test_old"]
ignore_errors = true
"""
_WORKFLOW = "run: uv run python scripts/mypy_tests_exact.py\n"

_BASE = {
    _RULE_PATH: _RULE,
    _GUARD_PATH: _GUARD,
    _RUFF_PATH: _RUFF,
    "tests/addon/ruff.toml": _ADDON_RUFF,
    "pyproject.toml": _PYPROJECT,
    ".github/workflows/pr.yml": _WORKFLOW,
    "tests/src/unit/test_old.py": "",
    # An older file that is on no list.
    "tests/src/unit/test_clean.py": "",
}
_NEW = "tests/src/unit/test_new.py"
_OLD = "tests/src/unit/test_old.py"
_CLEAN = "tests/src/unit/test_clean.py"


@pytest.fixture(scope="module")
def ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stand_in_lists_ratchet", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(repo: Path, files: dict[str, str | None]) -> None:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _pull_request(
    tmp_path: Path, changes: dict[str, str | None], base: dict[str, str] | None = None
) -> Path:
    """A repository whose HEAD is the base and whose working tree has ``changes``."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _write(tmp_path, dict(_BASE if base is None else base))
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    commit(tmp_path)
    _write(tmp_path, {_NEW: "", **changes})
    return tmp_path


def _listed(rule: str, entry: str) -> str:
    return rule.replace("ignores:\n", f'ignores:\n  - "{entry}"\n')


@pytest.mark.parametrize(
    ("changes", "reported"),
    [
        pytest.param(
            {_RULE_PATH: _listed(_RULE, _NEW)},
            "lists the new file tests/src/unit/test_new.py",
            id="new-test-on-an-ast-grep-list",
        ),
        pytest.param(
            {
                _RULE_PATH: _RULE.replace(
                    "ignores:\n",
                    "ignores:\n  # converted later\n  - tests/src/unit/test_new.py\n",
                )
            },
            "lists the new file tests/src/unit/test_new.py",
            id="unquoted-entry-under-a-comment",
        ),
        pytest.param(
            {
                _RULE_PATH: _listed(_RULE, _NEW).replace(
                    "kind: assignment", "kind: call"
                )
            },
            "lists the new file tests/src/unit/test_new.py",
            id="new-test-listed-while-the-rule-is-widened",
        ),
        pytest.param(
            {
                _RULE_PATH: _listed(_RULE, "tests/src/unit").replace(
                    "kind: assignment", "kind: call"
                )
            },
            "lists the new file tests/src/unit",
            id="whole-directory-listed-while-the-rule-is-widened",
        ),
        pytest.param(
            {
                _RULE_PATH: _listed(_RULE, _CLEAN).replace(
                    "message: refused", "message: no"
                )
            },
            "test_clean.py was added to no-hand-made-core-replies's list",
            id="older-file-listed-behind-a-message-edit",
        ),
        pytest.param(
            {
                _RULE_PATH: None,
                "rules/replies-v2.yml": _listed(_RULE, _NEW),
            },
            "lists the new file tests/src/unit/test_new.py",
            id="new-test-listed-in-a-renamed-rule-file",
        ),
        pytest.param(
            {_RULE_PATH: None},
            "no-hand-made-core-replies was removed or renamed",
            id="rule-removed",
        ),
        pytest.param(
            {_RULE_PATH: _RULE.replace('  - "tests/addon/**"\n', "")},
            "scans fewer files than at the base",
            id="rule-no-longer-scans-a-tree",
        ),
        pytest.param(
            {_GUARD_PATH: _GUARD.replace("})", f', "{_NEW}"}})')},
            "tests/core_stand_in_guard.py: lists the new file",
            id="new-test-on-the-module-guard-list",
        ),
        pytest.param(
            {
                _GUARD_PATH: "_GRANDFATHERED: frozenset[str] = frozenset("
                f'{{"tests/src/unit/test_old.py", "{_CLEAN}"}})\n'
            },
            "test_clean.py was added to the list",
            id="older-file-on-an-annotated-guard-list",
        ),
        pytest.param(
            {_RUFF_PATH: _RUFF + '"test_new.py" = ["SLF001"]\n'},
            "tests/src/unit/ruff.toml: lists the new file",
            id="new-test-on-the-private-access-list",
        ),
        pytest.param(
            {_RUFF_PATH: _RUFF + '"test_clean.py" = ["SLF001"]\n'},
            "test_clean.py was added to the list",
            id="older-file-on-the-private-access-list",
        ),
        pytest.param(
            {
                _RUFF_PATH: _RUFF.replace(
                    "[lint]\n", '[lint]\nextend-ignore = ["SLF001"]\n'
                )
            },
            "extend-ignore turns SLF001 off for every test",
            id="private-access-check-switched-off",
        ),
        pytest.param(
            {
                "pyproject.toml": _PYPROJECT
                + '\n[[tool.mypy.overrides]]\nmodule = "tests.src.unit.test_new"\n'
                "ignore_errors = true\n"
            },
            "tests.src.unit.test_new is new and exempt from mypy",
            id="new-test-in-a-string-module-override",
        ),
        pytest.param(
            {
                "pyproject.toml": _PYPROJECT
                + '\n[[tool.mypy.overrides]]\nmodule = ["tests.src.unit.test_new"]\n'
                'disable_error_code = ["assignment"]\n'
            },
            "tests.src.unit.test_new is new and exempt from mypy",
            id="new-test-with-error-codes-disabled",
        ),
        pytest.param(
            {
                "pyproject.toml": _PYPROJECT.replace(
                    '["tests.src.unit.test_old"]',
                    '["tests.src.unit.test_old", "tests.src.unit.test_clean"]',
                )
            },
            "tests.src.unit.test_clean was added to the mypy list",
            id="older-file-on-the-mypy-list",
        ),
        pytest.param(
            {
                "pyproject.toml": '[tool.mypy]\nexclude = ["^tests/src/unit/test_new"]\n'
                + _PYPROJECT
            },
            "mypy exclude gained",
            id="new-test-excluded-from-mypy",
        ),
        pytest.param(
            {
                "pyproject.toml": _PYPROJECT.replace(
                    '["import-untyped"]', '["import-untyped", "assignment"]'
                )
            },
            "mypy no longer reports assignment in tests",
            id="mypy-loosened-for-every-test",
        ),
        pytest.param(
            {
                "pyproject.toml": '[tool.mypy]\ndisable_error_code = ["assignment"]\n\n'
                + _PYPROJECT
            },
            "mypy no longer reports assignment in tests",
            id="mypy-loosened-for-every-module",
        ),
    ],
)
def test_an_exempted_test_is_reported(
    ratchet: ModuleType, tmp_path: Path, changes: dict[str, str | None], reported: str
) -> None:
    """Each way of exempting a test from a check fails the pull request."""
    problems = ratchet.problems_since(_pull_request(tmp_path, changes), "HEAD")

    assert any(reported in problem for problem in problems), problems


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({}, id="nothing-listed"),
        pytest.param(
            {
                _RULE_PATH: _RULE.replace('  - "tests/src/unit/test_old.py"\n', ""),
                _GUARD_PATH: "_GRANDFATHERED = frozenset()\n",
                _RUFF_PATH: _ADDON_RUFF,
                "pyproject.toml": _PYPROJECT.split("\n\n", maxsplit=1)[0] + "\n",
            },
            id="converted-file-removed-from-every-list",
        ),
        pytest.param(
            {
                _RULE_PATH: _listed(_RULE, _CLEAN).replace(
                    "kind: assignment", "kind: call"
                )
            },
            id="widened-rule-lists-an-older-file-it-now-matches",
        ),
    ],
)
def test_converting_a_file_or_widening_a_rule_passes(
    ratchet: ModuleType, tmp_path: Path, changes: dict[str, str | None]
) -> None:
    """The ratchet stays out of the way of the changes it is meant to allow."""
    assert ratchet.problems_since(_pull_request(tmp_path, changes), "HEAD") == []


@pytest.mark.parametrize(
    ("before", "after", "reported"),
    [
        pytest.param("a = 1\n", "a = 1\nb = 2\n", True, id="gains-a-match"),
        pytest.param("a = 1\nb = 2\n", "b = 2\nc = 3\n", False, id="same-count"),
        pytest.param("a = 1\nb = 2\n", "a = 1\n", False, id="loses-a-match"),
        pytest.param("a = 1\n", "a = 1\nprint()\n", False, id="unmatched-edit"),
    ],
)
def test_a_listed_file_cannot_gain_what_its_list_excuses(
    ratchet: ModuleType, tmp_path: Path, before: str, after: str, reported: bool
) -> None:
    """The list exempts what the file held, not a faked test added to it."""
    repo = _pull_request(tmp_path, {_OLD: after}, {**_BASE, _OLD: before})

    expected = [
        f"rules: {_OLD} has 2 of what no-hand-made-core-replies refuses, "
        "1 at the base; a listed file may not gain any"
    ]
    assert ratchet.problems_since(repo, "HEAD") == (expected if reported else [])


def test_an_unlisted_file_is_left_to_the_rule_itself(
    ratchet: ModuleType, tmp_path: Path
) -> None:
    """ast-grep already refuses a match in a file that is on no list."""
    repo = _pull_request(tmp_path, {_CLEAN: "a = 1\n", _NEW: "b = 2\n"})

    assert ratchet.problems_since(repo, "HEAD") == []


def test_a_check_that_is_new_lists_older_files_but_no_new_one(
    ratchet: ModuleType, tmp_path: Path
) -> None:
    """Introducing a check may list what predates it, and nothing else."""
    base = {
        name: text
        for name, text in _BASE.items()
        if name not in (_RULE_PATH, _RUFF_PATH, ".github/workflows/pr.yml")
    }
    base["pyproject.toml"] = ""
    older = _listed(_RULE, _CLEAN)
    repo = _pull_request(
        tmp_path,
        {
            _RULE_PATH: older,
            _RUFF_PATH: _RUFF + '"test_clean.py" = ["SLF001"]\n',
            "pyproject.toml": _PYPROJECT,
            ".github/workflows/pr.yml": _WORKFLOW,
        },
        base,
    )
    assert ratchet.problems_since(repo, "HEAD") == []

    _write(repo, {_RULE_PATH: _listed(older, _NEW)})
    assert ratchet.problems_since(repo, "HEAD") == [
        f"rules: no-hand-made-core-replies lists the new file {_NEW}"
    ]


def test_the_command_fails_the_job_and_says_what_to_do(
    ratchet: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The step exits non-zero with the entry and the remedy in its output."""
    repo = _pull_request(tmp_path, {_RULE_PATH: _listed(_RULE, _NEW)})

    assert ratchet.main(["--base", "HEAD", "--repo", str(repo)]) == 1
    output = capsys.readouterr().out
    assert _NEW in output
    assert "core_client" in output


def test_an_unreadable_base_fails_instead_of_passing(ratchet: ModuleType) -> None:
    """A base commit git cannot read must not make every list look new."""
    assert ratchet.main(["--base", "0" * 40]) == 2


def test_a_base_file_git_cannot_read_fails_instead_of_counting_as_absent(
    ratchet: ModuleType, tmp_path: Path
) -> None:
    """A list that exists at the base but cannot be read is not a new list."""
    repo = _pull_request(tmp_path, {})
    blob = subprocess.run(
        ["git", "rev-parse", f"HEAD:{_RUFF_PATH}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()

    with pytest.raises(RuntimeError, match="cannot read"):
        ratchet.problems_since(repo, "HEAD")


def test_a_guard_list_that_names_a_file_indirectly_fails(
    ratchet: ModuleType, tmp_path: Path
) -> None:
    """A constant in the list would hide the file it stands for."""
    guard = f'_NEW = "{_NEW}"\n' + _GUARD.replace("})", ", _NEW})")
    repo = _pull_request(tmp_path, {_GUARD_PATH: guard})

    with pytest.raises(ValueError, match="string literals"):
        ratchet.problems_since(repo, "HEAD")
