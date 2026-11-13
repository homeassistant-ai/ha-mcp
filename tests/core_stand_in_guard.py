"""Fail a test that leaves a stand-in where a Home Assistant module belongs.

The unit and app (add-on) trees run against real Home Assistant Core. A test
that swaps a fake into ``sys.modules`` for ``homeassistant.*`` (or for the
libraries Core is built on) asserts against its own guess of what Core does
instead of Core.

The check runs after each test body, so it sees what is in ``sys.modules`` at
that moment: a ``monkeypatch`` swap, which is undone later at fixture teardown,
or one the test left behind. A swap the test itself has already undone
(``patch.dict``, ``try``/``finally``) is gone by then;
``rules/no-core-module-stand-ins.yml`` refuses those forms in the source.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Generator
from typing import Any

import pytest

_CORE_ROOTS = frozenset({"homeassistant", "aiohttp", "voluptuous", "probatio", "yarl"})

# Test files written before the unit tests ran on real Home Assistant Core.
# They are to be converted and removed from this list; never add a file to it.
_GRANDFATHERED = frozenset(
    {
        "tests/src/unit/test_component_config_entries_contract.py",
        "tests/src/unit/test_component_template_diagnose.py",
        "tests/src/unit/test_component_ws_phase2_async.py",
        "tests/src/unit/test_config_flow.py",
    }
)


def core_module_stand_ins() -> list[str]:
    """Names in ``sys.modules`` under Core's roots that are not imported modules."""
    return sorted(
        name
        for name, module in list(sys.modules.items())
        if name.partition(".")[0] in _CORE_ROOTS
        and (
            not isinstance(module, types.ModuleType)
            or getattr(module, "__spec__", None) is None
        )
    )


def is_registered(config: pytest.Config) -> bool:
    """Whether this session runs the check: each tree's conftest must load it."""
    impls = config.pluginmanager.hook.pytest_runtest_call.get_hookimpls()
    return any(impl.function is pytest_runtest_call for impl in impls)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, Any, Any]:
    result = yield
    if item.nodeid.partition("::")[0] in _GRANDFATHERED:
        return result
    if stand_ins := core_module_stand_ins():
        pytest.fail(
            "The test replaced Home Assistant modules with stand-ins: "
            f"{', '.join(stand_ins)}. Import the real module and patch the one "
            "function the test needs to control (see tests/AGENTS.md, 'Unit "
            "Tests Run on Real Home Assistant Core').",
            pytrace=False,
        )
    return result
