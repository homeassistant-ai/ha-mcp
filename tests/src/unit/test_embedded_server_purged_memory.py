"""A purged generation of the server's code is freed, not kept (#3131).

A start that replaces the server's code drops the ``ha_mcp`` modules from
``sys.modules``. Caches and loggers elsewhere in the process used to keep every
one of them alive, so each such start left a whole copy of the server behind.
The tests start the real server twice in this process and look at what is left
of the first copy.
"""

from __future__ import annotations

import gc
import logging
import sys
import weakref
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools.const import OPT_PIP_SPEC

from ._embedded_server_core import embedded_server_env as embedded_server_env
from ._embedded_server_core import package_index as package_index
from ._embedded_server_core import running_server

pytestmark = pytest.mark.usefixtures("embedded_server_env", "socket_enabled")

# An override makes every start load the server's code again.
OVERRIDE = {OPT_PIP_SPEC: "ha-mcp==99.0.0"}


def _server_modules() -> list[weakref.ref[ModuleType]]:
    return [
        weakref.ref(module)
        for name, module in list(sys.modules.items())
        if name == "ha_mcp" or name.startswith("ha_mcp.")
    ]


def _alive(modules: list[weakref.ref[ModuleType]]) -> list[str]:
    gc.collect()
    return sorted(module.__name__ for ref in modules if (module := ref()) is not None)


async def test_the_code_a_restart_replaced_is_freed(hass: HomeAssistant) -> None:
    async with running_server(hass, options=OVERRIDE):
        first = _server_modules()
    assert len(first) > 100

    async with running_server(hass, options=OVERRIDE):
        assert _alive(first) == []


async def test_a_restart_leaves_other_loggers_handlers_and_filters_alone(
    hass: HomeAssistant,
) -> None:
    logger = logging.getLogger("a_logger_of_someone_else")
    handler = logging.NullHandler()
    log_filter = logging.Filter("a_logger_of_someone_else")
    logger.addHandler(handler)
    logger.addFilter(log_filter)
    try:
        async with running_server(hass, options=OVERRIDE):
            pass
        async with running_server(hass, options=OVERRIDE):
            pass

        assert handler in logger.handlers
        assert log_filter in logger.filters
    finally:
        logger.removeHandler(handler)
        logger.removeFilter(log_filter)
