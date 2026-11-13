"""Fixtures and helpers the entity registry tests on a real ``hass`` share."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.errors import DEFAULT_SUGGESTIONS, ErrorCode


@pytest.fixture(params=["component", "core"])
async def core_client(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> HomeAssistantClient:
    """The shared ``core_client``, with and without the component's commands."""
    # ``config`` serves the registry commands the tools send.
    assert await async_setup_component(hass, "config", {})
    if request.param == "component":
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    return core_client


@pytest.fixture
async def lamp(hass: HomeAssistant, core_client: HomeAssistantClient) -> str:
    """A registry entry whose entity_id the user changed."""
    assert await async_setup_component(hass, "homeassistant", {})
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("light", "demo", "lamp")
    registry.async_update_entity(entry.entity_id, new_entity_id="light.my_lamp")
    return "light.my_lamp"


def sent_types(sent: AsyncMock) -> list[str]:
    return [call.args[0]["type"] for call in sent.call_args_list]


def assert_cores_refusal(
    exc_info: pytest.ExceptionInfo[ToolError], ha_error_code: str
) -> None:
    """Core's refusal keeps its code as ``ha_error_code`` and its message
    without the client's ``Command failed:`` prefix."""
    payload = json.loads(str(exc_info.value))
    assert "Command failed" not in payload["error"]["message"]
    assert payload["ha_error_code"] == ha_error_code


def device_entity(hass: HomeAssistant, original_name: str | None) -> er.RegistryEntry:
    """A sensor on a device, the shape the main-entity switch applies to."""
    config_entry = MockConfigEntry(domain="demo")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("demo", "sun")}
    )
    return er.async_get(hass).async_get_or_create(
        "sensor", "demo", "dawn", device_id=device.id, original_name=original_name
    )


def assert_default_suggestions(error: dict[str, Any]) -> None:
    """The tool adds no suggestions of its own to Core's refusal: what it
    carries is the error code's default list."""
    expected = DEFAULT_SUGGESTIONS.get(ErrorCode(error["code"]), [])
    given = [error["suggestion"], *error.get("suggestions", [])[1:]]
    assert given == expected
