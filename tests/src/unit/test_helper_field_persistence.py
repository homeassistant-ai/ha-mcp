"""Helper fields persist through ha_config_set_helper — tracking issue #1150.

A write that drops a field from the payload can still be answered with
``success``, so these tests read back what Core stored: the field a create or
update was given is in the stored item, and Core's whole-item update keeps
every field a rename did not touch. Both write paths run: Core's own
``<type>/update`` (no component) and the component's in-process write.
"""

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


async def _set_up(hass: HomeAssistant, helper_type: str) -> None:
    # ``config`` serves the entity-registry reads of the tool.
    for domain in ("config", helper_type):
        assert await async_setup_component(hass, domain, {})


def _name() -> str:
    """A fresh name, so no registry entry of an earlier helper is restored."""
    return f"Helper {uuid.uuid4().hex[:8]}"


async def _stored(client: HomeAssistantClient, helper_type: str, item_id: str) -> Any:
    listed = await client.send_websocket_message({"type": f"{helper_type}/list"})
    assert listed["success"], listed
    return next(i for i in listed["result"] if i["id"] == item_id)


async def _create(
    client: HomeAssistantClient, helper_type: str, fields: dict[str, Any]
) -> str:
    created = await client.send_websocket_message(
        {"type": f"{helper_type}/create", "name": _name(), **fields}
    )
    assert created["success"], created
    return str(created["result"]["id"])


async def test_input_number_create_stores_initial(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """Bug 1: an input_number created with ``initial`` must store it."""
    await _set_up(hass, "input_number")

    result = await tools.ha_config_set_helper(
        helper_type="input_number",
        name=_name(),
        config={"min": 60, "max": 85, "initial": 72},
    )

    stored = await _stored(core_client, "input_number", result["data"]["id"])
    assert stored["initial"] == 72


async def test_input_number_update_stores_initial(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """Bug 1b: an input_number update of ``initial`` must store it."""
    await _set_up(hass, "input_number")
    item_id = await _create(
        core_client, "input_number", {"min": 60, "max": 85, "initial": 72}
    )

    await tools.ha_config_set_helper(
        helper_type="input_number", helper_id=item_id, config={"initial": 80}
    )

    assert (await _stored(core_client, "input_number", item_id))["initial"] == 80


@pytest.mark.parametrize(
    ("helper_type", "fields"),
    [
        (
            "input_number",
            {
                "min": 60,
                "max": 85,
                "step": 0.5,
                "mode": "box",
                "initial": 72,
                "unit_of_measurement": "°F",
                "icon": "mdi:thermometer",
            },
        ),
        (
            "input_text",
            {
                "min": 5,
                "max": 50,
                "mode": "password",
                "initial": "starter",
                "icon": "mdi:note-text",
            },
        ),
        (
            "counter",
            {
                "initial": 5,
                "minimum": 0,
                "maximum": 100,
                "step": 2,
                "restore": True,
                "icon": "mdi:counter",
            },
        ),
        ("timer", {"duration": "0:30:00", "restore": True, "icon": "mdi:timer"}),
        ("input_boolean", {"initial": True, "icon": "mdi:toggle-switch-on"}),
        (
            "input_datetime",
            {
                "has_date": True,
                "has_time": True,
                "initial": "2026-12-31 23:59:59",
                "icon": "mdi:calendar",
            },
        ),
        (
            "input_select",
            {
                "options": ["Alpha", "Beta", "Gamma"],
                "initial": "Beta",
                "icon": "mdi:menu",
            },
        ),
    ],
)
async def test_a_rename_keeps_every_other_stored_field(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: HelperConfigTools,
    helper_type: str,
    fields: dict[str, Any],
) -> None:
    """Bug 8: Core's update replaces the whole item, so a rename that sent only
    the name would wipe every other field."""
    await _set_up(hass, helper_type)
    item_id = await _create(core_client, helper_type, fields)
    before = await _stored(core_client, helper_type, item_id)
    new_name = _name()

    await tools.ha_config_set_helper(
        helper_type=helper_type, helper_id=item_id, name=new_name
    )

    assert await _stored(core_client, helper_type, item_id) == {
        **before,
        "name": new_name,
    }
