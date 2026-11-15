"""Real Core behind ``ha_get_logs`` in the log ordering tests.

The logbook comes from Core's ``logbook`` and ``recorder``, the system log from
its ``system_log``, the error log from the file its ``api`` serves, and the
logger levels from its ``logger``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import LogTools

from ._log_tool_calls import call_kwargs

# The per-message cap the tool's ``compact`` description promises.
SYSTEM_MESSAGE_CAP = 2000


async def get_logs(client: HomeAssistantClient, **overrides: Any) -> dict[str, Any]:
    """``ha_get_logs`` through the server's client, defaults for what is not given."""
    return await LogTools(client).get_logs(**call_kwargs(**overrides))


async def record_states(hass: HomeAssistant, states: dict[str, list[str]]) -> None:
    """Have Core's recorder write each entity's states, in order, into its logbook."""
    # The logbook registers a sidebar panel; the real frontend needs the
    # frontend build, which is not installed with Core.
    mock_integration(hass, MockModule("frontend"))
    assert await async_setup_component(hass, "logbook", {})
    for step in range(max(len(values) for values in states.values())):
        for entity_id, values in states.items():
            if step < len(values):
                hass.states.async_set(entity_id, values[step])
                await hass.async_block_till_done()
    await async_wait_recording_done(hass)


def logbook_states(result: dict[str, Any]) -> list[str]:
    """The ``state`` of each logbook row ``ha_get_logs`` returned, in order."""
    return [row["state"] for row in result["data"]["entries"]]


async def set_up_system_log(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "system_log", {})


def log_record(
    name: str, message: str = "m", level: int = logging.ERROR, **extra: Any
) -> None:
    """One record on logger ``name``, as an integration writes it."""
    logging.getLogger(name).log(level, "%s", message, **extra)


@pytest.fixture
def error_log_file(hass: HomeAssistant, tmp_path: Path) -> Path:
    """The file Core serves as its error log; asked for before ``core_client``,
    whose ``api`` setup only serves the log when Core knows the file."""
    path = tmp_path / "home-assistant.log"
    path.write_text(
        "\n".join(
            f"2026-05-27 10:00:0{i}.000 ERROR (MainThread) [a.b.c] l{i}"
            for i in range(4)
        )
        + "\n",
        encoding="utf-8",
    )
    hass.data[KEY_DATA_LOGGING] = str(path)
    return path
