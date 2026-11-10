"""The lists of tests exempt from the real-Core checks can only shrink.

A pull request that adds its own new test to a list would pass every other
check, which is how a guessed-Core test gets in. ``stand_in_lists_ratchet.py``
compares each list with the base commit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "stand_in_lists_ratchet.py"

_RULE = """id: no-hand-made-core-replies
rule:
  kind: assignment
files:
  - "tests/src/unit/**"
ignores:
  - "tests/src/unit/test_old.py"
message: no
"""
_RUFF = '[lint.extend-per-file-ignores]\n"test_old.py" = ["SLF001"]\n'
_MYPY = (
    "[[tool.mypy.overrides]]\n"
    'module = ["tests.src.unit.test_old"]\n'
    "ignore_errors = true\n"
)
_GUARD = '_GRANDFATHERED = frozenset({"tests/src/unit/test_old.py"})\n'


@pytest.fixture(scope="module")
def ratchet() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stand_in_lists_ratchet", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("path", "base", "head", "added"),
    [
        pytest.param(
            "rules/no-hand-made-core-replies.yml",
            _RULE,
            _RULE.replace(
                '  - "tests/src/unit/test_old.py"\n',
                '  - "tests/src/unit/test_new.py"\n  - "tests/src/unit/test_old.py"\n',
            ),
            {"tests/src/unit/test_new.py"},
            id="ast-grep-ignores",
        ),
        pytest.param(
            "tests/src/unit/ruff.toml",
            _RUFF,
            _RUFF + '"test_new.py" = ["SLF001"]\n',
            {"test_new.py"},
            id="private-access-list",
        ),
        pytest.param(
            "pyproject.toml",
            _MYPY,
            _MYPY.replace('"]', '", "tests.src.unit.test_new"]'),
            {"tests.src.unit.test_new"},
            id="mypy-list",
        ),
        pytest.param(
            "tests/core_stand_in_guard.py",
            _GUARD,
            _GUARD.replace("})", ', "tests/src/unit/test_new.py"})'),
            {"tests/src/unit/test_new.py"},
            id="module-stand-in-guard",
        ),
    ],
)
def test_a_new_test_parked_on_a_list_is_reported(
    ratchet: ModuleType, path: str, base: str, head: str, added: set[str]
) -> None:
    """A pull request cannot exempt its own new test from a check."""
    assert ratchet.added_entries(path, base, head) == added


@pytest.mark.parametrize(
    ("path", "base", "head"),
    [
        pytest.param(
            "rules/no-hand-made-core-replies.yml",
            _RULE,
            _RULE.replace('  - "tests/src/unit/test_old.py"\n', ""),
            id="converted-file-removed",
        ),
        pytest.param(
            "rules/no-hand-made-core-replies.yml",
            _RULE,
            _RULE.replace("kind: assignment", "kind: call").replace(
                '  - "tests/src/unit/test_old.py"\n',
                '  - "tests/src/unit/test_older.py"\n  - "tests/src/unit/test_old.py"\n',
            ),
            id="widened-rule-lists-the-older-files-it-now-matches",
        ),
        pytest.param("tests/src/unit/ruff.toml", None, _RUFF, id="list-is-new"),
        pytest.param("pyproject.toml", "[tool.mypy]\n", _MYPY, id="mypy-list-is-new"),
    ],
)
def test_a_shrinking_or_newly_created_list_is_allowed(
    ratchet: ModuleType, path: str, base: str | None, head: str
) -> None:
    """Converting a file, widening a rule and introducing a check all pass."""
    assert ratchet.added_entries(path, base, head) == set()
