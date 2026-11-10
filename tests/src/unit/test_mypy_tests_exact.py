"""The list of test modules exempt from mypy stays exact.

``scripts/mypy_tests_exact.py`` runs mypy on the tests without the list and
compares. These tests pin the two halves that need no mypy run: taking the
list out of ``pyproject.toml``, and comparing it with mypy's output.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "mypy_tests_exact.py"

_PYPROJECT = """[project]
name = "x"

[tool.mypy]
strict = true

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false

[[tool.mypy.overrides]]
module = [
    "tests.src.unit.test_old",
    "tests.src.e2e.helper",
]
ignore_errors = true

[[tool.mypy.overrides]]
module = ["vendor.*"]
ignore_errors = true

[tool.ruff]
line-length = 88
"""
_EXEMPT = {"tests.src.unit.test_old", "tests.src.e2e.helper"}
_OLD = "tests/src/unit/test_old.py:3: error: Incompatible types  [assignment]\n"
_HELPER = "tests/src/e2e/helper.py:9:5: error: Missing return  [return]\n"
_NEW = "tests/src/unit/test_new.py:7: error: Incompatible types  [assignment]\n"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mypy_tests_exact", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_the_exempt_test_modules_leave_the_config(script: ModuleType) -> None:
    """mypy runs with every other setting, the ``tests.*`` override included."""
    config, exempt = script.split_exempt(_PYPROJECT)

    assert exempt == _EXEMPT
    overrides = tomllib.loads(config)["tool"]["mypy"]["overrides"]
    assert [override["module"] for override in overrides] == [["tests.*"], ["vendor.*"]]
    assert tomllib.loads(config)["tool"]["ruff"] == {"line-length": 88}


def test_the_repository_list_is_found(script: ModuleType) -> None:
    """The real ``pyproject.toml`` still has the list where the script looks."""
    text = (script.REPO / "pyproject.toml").read_text(encoding="utf-8")

    config, exempt = script.split_exempt(text)

    assert exempt
    assert not any(
        override.get("ignore_errors") and "tests." in str(override["module"])
        for override in tomllib.loads(config)["tool"]["mypy"]["overrides"]
    )


@pytest.mark.parametrize(
    ("output", "reported"),
    [
        pytest.param(_OLD + _HELPER, [], id="errors-only-in-listed-modules"),
        pytest.param(
            _OLD + _HELPER + _NEW,
            [_NEW.strip()],
            id="error-in-an-unlisted-test",
        ),
        pytest.param(
            _OLD,
            [
                "tests.src.e2e.helper has no mypy errors left: remove it from the "
                "ignore_errors list in pyproject.toml"
            ],
            id="listed-module-that-is-clean",
        ),
    ],
)
def test_the_list_is_compared_with_what_mypy_reports(
    script: ModuleType, output: str, reported: list[str]
) -> None:
    """An unlisted error fails, and so does a listed module with none left."""
    assert script.compare(_EXEMPT, output) == reported
