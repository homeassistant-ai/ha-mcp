"""A storage helper's icon follows the running Core's own field lists.

The field lists are Core's own: the component serves the create and update
schemas of the collections set up on the real ``hass``. Without the component
(``core_client``) no list can be read.
"""

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_helpers import icon_route
from ha_mcp.tools.config_helpers.icon_route import icon_targets


async def _set_up(hass: HomeAssistant, *domains: str) -> None:
    for domain in domains:
        assert await async_setup_component(hass, domain, {})


@pytest.mark.parametrize(
    ("helper_type", "in_item"),
    [("zone", True), ("input_boolean", True), ("person", False), ("tag", False)],
)
@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("icon", ["mdi:x", ""], ids=["set", "cleared"])
async def test_core_s_own_fields_route_the_icon(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    helper_type: str,
    in_item: bool,
    action: str,
    icon: str,
) -> None:
    """A zone or input_boolean keeps its icon where its dialog writes it, and a
    stored or cleared icon never becomes a registry override, which would hide
    every later change made in those dialogs. A person or tag, which Core gives
    no icon field, keeps it in the entity registry."""
    await _set_up(hass, helper_type)

    targets = await icon_targets(component_core_client, helper_type, action, icon)

    assert targets == ((icon, None) if in_item else (None, icon))


@pytest.mark.parametrize("with_component", [False, True], ids=["unreadable", "unset"])
async def test_unknown_fields_send_the_icon_to_the_registry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    with_component: bool,
) -> None:
    """Without the component no field list can be read; with it, a type whose
    collection Core has not set up is in no list. Nothing is guessed."""
    if with_component:
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)

    targets = await icon_targets(core_client, "input_boolean", "create", "mdi:star")

    assert targets == (None, "mdi:star")


async def test_a_cleared_icon_of_unknown_fields_clears_both(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Clearing only the registry would bring an older stored icon back."""
    await _set_up(hass, "input_boolean")

    assert await icon_targets(core_client, "input_boolean", "update", "") == ("", "")


async def test_a_zone_icon_stays_in_the_zone_without_the_component(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without the component no schema can be read, and a registry override
    would leave the Zones editor showing no icon (#3001). Core's zone fields
    declare the icon (the first test), so the fallback follows Core."""
    await _set_up(hass, "zone")

    assert await icon_targets(core_client, "zone", "create", "mdi:home") == (
        "mdi:home",
        None,
    )


@pytest.mark.parametrize(("icon", "reads"), [(None, 0), ("mdi:x", 1)])
async def test_no_icon_reads_and_writes_nothing(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    icon: str | None,
    reads: int,
) -> None:
    """No icon needs no field list; an icon does, so the read is counted."""
    await _set_up(hass, "zone")

    with patch.object(
        icon_route, "fetch_helper_schemas", wraps=icon_route.fetch_helper_schemas
    ) as fetch:
        targets = await icon_targets(component_core_client, "zone", "update", icon)

    assert targets == (icon, None)
    assert fetch.await_count == reads
