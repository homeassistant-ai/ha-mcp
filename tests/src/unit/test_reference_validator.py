"""Core's verdict on a config's references reaches the write response.

The component's ``config_references`` validates a proposed automation, script
or scene with Core, checks its references against Core's registries and puts
each entity target to Core's target lookups. These tests pin how the reference
validator reports that answer, and that it says so when nothing was checked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_config_reads
from ha_mcp.tools.component_api import ComponentCaps
from ha_mcp.tools.component_config_reads import CONFIG_REFERENCES
from ha_mcp.tools.reference_validator import validate_config_references

_CONFIG: dict[str, Any] = {
    "triggers": [{"trigger": "event", "event_type": "arrived"}],
    "actions": [
        {
            "action": "select.select_option",
            "target": {"entity_id": "input_select.mode_of_travel"},
            "data": {"option": "cycling"},
        }
    ],
}


async def _input_select_and_select_services(hass: HomeAssistant) -> None:
    """The entity the config aims at, and the select services Core describes."""
    assert await async_setup_component(
        hass,
        "input_select",
        {
            "input_select": {
                "mode_of_travel": {"options": ["walking", "cycling"]},
            }
        },
    )
    assert await async_setup_component(hass, "select", {})


@pytest.mark.anyio
async def test_select_option_on_an_input_select_is_reported_with_cores_offer(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """#2989: the write saved silently and the helper never changed. The
    warning carries what Core offers for the entity, the right action in it."""
    await _input_select_and_select_services(hass)
    result = await validate_config_references(
        component_core_client, _CONFIG, "automation"
    )
    (warning,) = result["warnings"]
    assert warning["path"] == "actions[0]"
    assert warning["value"] == "input_select.mode_of_travel"
    assert warning["target_of"] == "select.select_option"
    assert "input_select.select_option" in warning["offered"]
    assert "references_unchecked" not in result


@pytest.mark.anyio
async def test_the_same_entity_with_the_action_core_offers_is_not_reported(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The twin of the case above: the entity is right, only the action differs."""
    await _input_select_and_select_services(hass)
    config = {
        **_CONFIG,
        "actions": [
            {
                "action": "input_select.select_option",
                "target": {"entity_id": "input_select.mode_of_travel"},
                "data": {"option": "cycling"},
            }
        ],
    }
    result = await validate_config_references(
        component_core_client, config, "automation"
    )
    assert result == {"warnings": []}


@pytest.mark.anyio
async def test_a_service_home_assistant_does_not_have_is_reported(
    component_core_client: HomeAssistantClient,
) -> None:
    """#940: a hallucinated notify service was accepted silently."""
    config = {
        "triggers": [{"trigger": "event", "event_type": "arrived"}],
        "actions": [
            {
                "action": "notify.mobile_app_andrew_phone",
                "data": {"message": "home"},
            }
        ],
    }
    result = await validate_config_references(
        component_core_client, config, "automation"
    )
    assert [(w["kind"], w["value"]) for w in result["warnings"]] == [
        ("service", "notify.mobile_app_andrew_phone")
    ]


@pytest.mark.anyio
async def test_a_config_core_refuses_leaves_the_references_unchecked(
    component_core_client: HomeAssistantClient,
) -> None:
    config = {
        "triggers": [{"trigger": "no_such_trigger_platform"}],
        "actions": [{"action": "light.turn_on"}],
    }
    result = await validate_config_references(
        component_core_client, config, "automation"
    )
    assert result["warnings"] == []
    assert result["references_unchecked"]


@pytest.mark.anyio
async def test_without_the_component_the_references_are_reported_unchecked(
    core_client: HomeAssistantClient,
) -> None:
    """No server-side copy of Core's rules stands in for the component."""
    result = await validate_config_references(core_client, _CONFIG, "automation")
    assert result["warnings"] == []
    assert "not installed" in result["references_unchecked"]


@pytest.mark.anyio
async def test_an_outdated_component_is_told_apart_from_an_absent_one(
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An installed component from before the check was told it was missing.

    The component's handshake lists the capabilities it registered; this one
    lists every capability but the reference check.
    """
    advertised = [c for c in system.CAPABILITIES if c != "config_references"]
    monkeypatch.setattr(system, "CAPABILITIES", advertised)
    result = await validate_config_references(
        component_core_client, _CONFIG, "automation"
    )
    assert result["warnings"] == []
    assert "update it" in result["references_unchecked"]


@pytest.mark.anyio
async def test_a_component_that_does_not_know_the_check_is_asked_again_after_update(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capabilities cached as offering the check, but Core answers the command
    unknown_command: the check is reported as needing an update, and the cached
    capabilities are dropped so the next check probes the component again."""
    served = ComponentCaps(1, "2.2.2", frozenset({CONFIG_REFERENCES}), {})
    monkeypatch.setattr(
        component_config_reads, "get_component_caps", AsyncMock(return_value=served)
    )
    invalidated = MagicMock(wraps=component_config_reads.invalidate_caps)
    monkeypatch.setattr(component_config_reads, "invalidate_caps", invalidated)

    result = await validate_config_references(core_client, _CONFIG, "automation")

    assert result["warnings"] == []
    assert "update it" in result["references_unchecked"]
    invalidated.assert_called_once_with(core_client)


@pytest.mark.anyio
async def test_a_dropped_connection_never_aborts_the_write_and_says_why(
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check runs before the write; an error escaping it failed the write."""
    monkeypatch.setattr(
        component_config_reads,
        "get_websocket_client",
        AsyncMock(side_effect=HomeAssistantConnectionError("closed")),
    )
    result = await validate_config_references(
        component_core_client, _CONFIG, "automation"
    )
    assert result == {"warnings": [], "references_unchecked": "closed"}


@pytest.mark.anyio
async def test_a_timed_out_check_never_aborts_the_write_and_says_why(
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check Core does not answer in time leaves the write standing."""
    monkeypatch.setattr(
        component_config_reads,
        "get_websocket_client",
        AsyncMock(side_effect=HomeAssistantCommandTimeout("slow")),
    )
    result = await validate_config_references(
        component_core_client, _CONFIG, "automation"
    )
    assert result == {"warnings": [], "references_unchecked": "slow"}
