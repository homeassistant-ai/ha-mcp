"""The guard that fails a test leaving a stand-in for a Home Assistant module."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import cast

import homeassistant.core
import pytest

from tests import core_stand_in_guard as guard

_STAND_IN = "homeassistant.helpers.a_stand_in"
# A file the guard still exempts; this test follows that list.
_EXEMPT_FILE = "tests/src/unit/test_config_flow.py"


def test_unit_conftest_registers_the_hook(pytestconfig: pytest.Config) -> None:
    assert guard.is_registered(pytestconfig)


def test_an_object_in_place_of_a_core_module_is_reported() -> None:
    sys.modules[_STAND_IN] = cast(ModuleType, SimpleNamespace())
    try:
        assert _STAND_IN in guard.core_module_stand_ins()
    finally:
        del sys.modules[_STAND_IN]


def test_an_imported_core_module_is_not_reported() -> None:
    assert homeassistant.core.__name__ not in guard.core_module_stand_ins()


def test_hook_fails_a_test_whose_stand_in_is_still_in_place(
    request: pytest.FixtureRequest,
) -> None:
    hook = guard.pytest_runtest_call(request.node)
    next(hook)
    sys.modules[_STAND_IN] = cast(ModuleType, SimpleNamespace())
    try:
        with pytest.raises(pytest.fail.Exception, match=_STAND_IN):
            hook.send(None)
    finally:
        del sys.modules[_STAND_IN]


def test_hook_passes_the_result_of_a_test_that_left_none(
    request: pytest.FixtureRequest,
) -> None:
    hook = guard.pytest_runtest_call(request.node)
    next(hook)
    with pytest.raises(StopIteration) as stopped:
        hook.send("result")
    assert stopped.value.value == "result"


def test_hook_leaves_a_grandfathered_file_alone() -> None:
    """Its stand-ins are still in place when the hook looks, and it passes."""
    item = cast(pytest.Item, SimpleNamespace(nodeid=f"{_EXEMPT_FILE}::test_x"))
    hook = guard.pytest_runtest_call(item)
    next(hook)
    sys.modules[_STAND_IN] = cast(ModuleType, SimpleNamespace())
    try:
        with pytest.raises(StopIteration):
            hook.send(None)
    finally:
        del sys.modules[_STAND_IN]
