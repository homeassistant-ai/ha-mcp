"""Real Core logbook fixtures and transport spies."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import LogTools

from ._log_tool_calls import call_kwargs


@pytest.fixture(autouse=True)
def _recorder(recorder_mock: Recorder) -> None:
    """Core's recorder, requested before ``hass`` is built as it requires."""


@pytest.fixture
async def logbook(hass: HomeAssistant) -> None:
    """Core's logbook on its recorder, with a kitchen light that turned on."""
    assert await async_setup_component(hass, "logbook", {})
    attributes = {"friendly_name": "Kitchen Light"}
    hall = {"friendly_name": "Hall"}
    hass.states.async_set("light.kitchen", "off", attributes)
    hass.states.async_set("light.hall", "off", hall)
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "on", attributes)
    hass.states.async_set("light.hall", "on", hall)
    await async_wait_recording_done(hass)


@contextmanager
def rest_reads(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """Spy on the client's REST logbook read; Core still answers it."""
    with patch.object(client, "get_logbook", wraps=client.get_logbook) as spy:
        yield spy


def sent_types(sent: AsyncMock) -> list[str]:
    """The command types the client sent over its WebSocket bridge."""
    return [call.args[0]["type"] for call in sent.await_args_list]


async def logbook_error(tools: LogTools, **kwargs: Any) -> dict[str, Any]:
    """The structured error ``ha_get_logs`` raised for these arguments."""
    with pytest.raises(ToolError) as exc_info:
        await tools.get_logs(**call_kwargs(source="logbook", **kwargs))
    payload: dict[str, Any] = json.loads(str(exc_info.value))
    error: dict[str, Any] = payload["error"]
    return error
