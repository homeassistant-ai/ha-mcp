"""The flow-helper entity → config entry lookup, on a real ``hass``.

Every registry row, state and refusal below is Core's own, read through the
server's client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.flow_helper_lookup import get_entry_id_for_flow_helper

from ._addon_tool_helpers import _parse_tool_error


@pytest.fixture(autouse=True)
async def _registry_commands(hass: HomeAssistant) -> None:
    """``config`` answers the entity registry reads."""
    assert await async_setup_component(hass, "config", {})


def _registered(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    platform: str,
    *,
    with_entry: bool = True,
) -> er.RegistryEntry:
    entry = None
    if with_entry:
        entry = MockConfigEntry(domain=platform)
        entry.add_to_hass(hass)
    return entity_registry.async_get_or_create(
        "sensor", platform, f"{platform}-unique", config_entry=entry
    )


async def test_a_helpers_entity_resolves_to_its_config_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    meter = _registered(hass, entity_registry, "utility_meter")

    assert await get_entry_id_for_flow_helper(
        core_client, "utility_meter", meter.entity_id
    ) == (meter.config_entry_id, "ok")


@pytest.mark.parametrize(
    ("helper_type", "target", "reason"),
    [
        # Flow helpers need the full entity_id: helper_type often differs
        # from the entity's domain (utility_meter → sensor.*).
        ("template", "my_sensor", "bare_id_not_supported"),
        ("input_button", "my_button", "wrong_helper_type"),  # not a flow helper
    ],
    ids=["bare_id", "not_a_flow_helper"],
)
async def test_a_call_shape_the_lookup_cannot_use_reads_nothing(
    core_client: HomeAssistantClient, helper_type: str, target: str, reason: str
) -> None:
    with patch.object(
        core_client, "send_websocket_message", wraps=core_client.send_websocket_message
    ) as sent:
        assert await get_entry_id_for_flow_helper(core_client, helper_type, target) == (
            None,
            reason,
        )

    sent.assert_not_awaited()


async def test_an_entity_with_neither_registry_entry_nor_state_is_missing(
    core_client: HomeAssistantClient,
) -> None:
    assert await get_entry_id_for_flow_helper(
        core_client, "template", "sensor.ghost"
    ) == (None, "not_in_registry")


async def test_entity_with_a_state_but_no_registry_entry_is_not_missing(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core keeps no registry entry for an entity without a unique_id (a YAML
    template sensor without one, zone.home); it exists, so it must not read
    as missing."""
    hass.states.async_set("zone.home", "zoning")

    assert await get_entry_id_for_flow_helper(core_client, "template", "zone.home") == (
        None,
        "not_registry_managed",
    )


async def test_a_registered_entity_without_a_config_entry_is_not_a_stored_helper(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    # A YAML-defined template sensor with a unique_id.
    sensor = _registered(hass, entity_registry, "template", with_entry=False)

    assert await get_entry_id_for_flow_helper(
        core_client, "template", sensor.entity_id
    ) == (None, "no_config_entry")


async def test_a_registry_read_core_refuses_is_not_reported_as_missing(
    core_client: HomeAssistantClient,
) -> None:
    """A refusal other than not_found is no evidence the entity is absent."""
    with pytest.raises(ToolError) as exc_info:
        await get_entry_id_for_flow_helper(core_client, "template", "sensor.not valid")

    error = _parse_tool_error(exc_info)["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "sensor.not valid" in error["message"]


async def test_a_state_read_without_an_answer_is_not_evidence_of_absence(
    core_client: HomeAssistantClient,
) -> None:
    with (
        patch.object(
            core_client,
            "get_entity_state",
            AsyncMock(side_effect=HomeAssistantConnectionError("network down")),
        ),
        pytest.raises(HomeAssistantConnectionError),
    ):
        await get_entry_id_for_flow_helper(core_client, "template", "sensor.ghost")


async def test_a_registry_read_without_an_answer_propagates(
    core_client: HomeAssistantClient,
) -> None:
    # The caller's handler reports the lost connection.
    with (
        patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("network down")),
        ),
        pytest.raises(HomeAssistantConnectionError),
    ):
        await get_entry_id_for_flow_helper(core_client, "utility_meter", "sensor.x")


@pytest.mark.parametrize(
    ("platform", "retry_hint"),
    [("sun", "ha_remove_entity()"), ("input_boolean", "helper_type='input_boolean'")],
    ids=["not_a_helper", "other_helper"],
)
async def test_wrong_helper_type_refusal_names_the_owning_integration(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    platform: str,
    retry_hint: str,
) -> None:
    """The refusal must say which integration owns the entity, so omitting
    helper_type is suggested only where it can work."""
    owned = _registered(hass, entity_registry, platform)

    with pytest.raises(ToolError) as exc_info:
        await get_entry_id_for_flow_helper(core_client, "group", owned.entity_id)

    response = _parse_tool_error(exc_info)
    err = response["error"]
    assert err["code"] == "VALIDATION_INVALID_PARAMETER"
    assert f"'{platform}'" in err["message"]
    assert response["platform"] == platform
    assert retry_hint in err["suggestion"]
