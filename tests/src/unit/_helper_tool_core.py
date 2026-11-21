"""ha_config_set_helper on real Core, on both write paths, for the helper tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools


@pytest.fixture(params=[False, True], ids=["core", "component"])
async def tools(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> AsyncIterator[HelperConfigTools]:
    if request.param:
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    yield HelperConfigTools(core_client)
    await hass.async_block_till_done()


async def set_up(hass: HomeAssistant, helper_type: str) -> None:
    # ``config`` serves the entity-registry reads of the tool.
    for domain in ("config", helper_type):
        assert await async_setup_component(hass, domain, {})


def fresh_name() -> str:
    """A fresh name, so no registry entry of an earlier helper is restored."""
    return f"Helper {uuid.uuid4().hex[:8]}"


async def stored_item(
    client: HomeAssistantClient, helper_type: str, item_id: str
) -> Any:
    listed = await client.send_websocket_message({"type": f"{helper_type}/list"})
    assert listed["success"], listed
    return next(i for i in listed["result"] if i["id"] == item_id)


async def create_item(
    client: HomeAssistantClient, helper_type: str, fields: dict[str, Any]
) -> str:
    created = await client.send_websocket_message(
        {"type": f"{helper_type}/create", "name": fresh_name(), **fields}
    )
    assert created["success"], created
    return str(created["result"]["id"])
