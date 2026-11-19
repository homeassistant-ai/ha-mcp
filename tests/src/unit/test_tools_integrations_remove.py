"""ha_remove_helpers_integrations on real Home Assistant Core.

The tool runs on the server's own client against a real ``hass``: config
entries, registry entries, storage and YAML helpers and every reply are Core's.
A lingering state is restored in Core as the removal wait begins.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    MockUser,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import ws_waiters
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._command_faults import dropped_on, pooled_socket
from ._held_command import held_command
from ._receipt_faults import at_entity_wait
from ._tool_errors import rejected_call_body


@pytest.fixture(autouse=True)
def _immediate_registry_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core answers at once; the retry backoff only adds time."""
    monkeypatch.setattr("ha_mcp.tools.tools_integrations.REGISTRY_RETRY_BASE_DELAY", 0)


@pytest.fixture
async def tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> IntegrationTools:
    # ``config`` serves the registry commands and the config entry delete.
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "input_button", {})
    return IntegrationTools(core_client)


async def _button(hass: HomeAssistant, client: HomeAssistantClient) -> tuple[str, str]:
    """A stored input_button, freshly named: its storage id and entity_id."""
    created = await client.send_websocket_message(
        {"type": "input_button/create", "name": f"Button {uuid.uuid4().hex[:8]}"}
    )
    assert created["success"], created
    storage_id = str(created["result"]["id"])
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "input_button", "input_button", storage_id
    )
    assert entity_id is not None
    return storage_id, entity_id


async def _disable(hass: HomeAssistant, entity_id: str) -> None:
    er.async_get(hass).async_update_entity(
        entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None


async def _stored_buttons(client: HomeAssistantClient) -> set[str]:
    """The storage ids Core still holds."""
    listed = await client.send_websocket_message({"type": "input_button/list"})
    assert listed["success"], listed
    return {str(item["id"]) for item in listed["result"]}


async def _remove(tools: IntegrationTools, target: str, **kwargs: Any) -> Any:
    return await tools.ha_remove_helpers_integrations(
        target=target, confirm=True, **{"wait": False, **kwargs}
    )


async def _refusal(tools: IntegrationTools, target: str, **kwargs: Any) -> Any:
    return (await rejected_call_body(_remove(tools, target, **kwargs)))["error"]


def _meter_entry(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, *tariffs: str
) -> tuple[MockConfigEntry, list[str]]:
    """A utility_meter entry with one registry entity per tariff."""
    entry = MockConfigEntry(domain="utility_meter", title="Energy")
    entry.add_to_hass(hass)
    entity_ids = [
        entity_registry.async_get_or_create(
            "sensor",
            "utility_meter",
            f"{tariff}_{uuid.uuid4().hex[:8]}",
            config_entry=entry,
            suggested_object_id=f"energy_{tariff}",
        ).entity_id
        for tariff in tariffs
    ]
    return entry, entity_ids


@pytest.fixture
async def unloadable_meter(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> AsyncIterator[tuple[MockConfigEntry, list[str]]]:
    """A loaded utility_meter entry whose integration has no unload: Core can
    only delete it with a restart to follow."""
    mock_integration(
        hass,
        MockModule("utility_meter", async_setup_entry=AsyncMock(return_value=None)),
    )
    mock_platform(hass, "utility_meter.config_flow")
    entry, entity_ids = _meter_entry(hass, entity_registry, "peak")
    with mock_config_flow("utility_meter", type("MeterFlow", (ConfigFlow,), {})):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        yield entry, entity_ids


# --- an entry_id target ------------------------------------------------------------


async def test_an_entry_id_deletes_that_config_entry(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    entry = MockConfigEntry(domain="utility_meter")
    entry.add_to_hass(hass)

    result = await _remove(tools, entry.entry_id)

    assert result["success"] is True
    assert result["method"] == "config_entry_delete"
    assert result["helper_type"] == "config_entry"
    assert result["entry_id"] == entry.entry_id
    assert result["entity_ids"] == []
    assert result["require_restart"] is False
    assert hass.config_entries.async_get_entry(entry.entry_id) is None


async def test_an_entry_deleted_without_unloading_says_a_restart_is_needed(
    hass: HomeAssistant,
    tools: IntegrationTools,
    unloadable_meter: tuple[MockConfigEntry, list[str]],
) -> None:
    entry, _ = unloadable_meter

    result = await _remove(tools, entry.entry_id)

    assert result["require_restart"] is True
    assert "restart required" in result["message"].lower()
    assert hass.config_entries.async_get_entry(entry.entry_id) is None


async def test_an_unknown_entry_id_points_the_caller_at_ha_get_integration(
    tools: IntegrationTools,
) -> None:
    """A typo'd entry_id is reported missing, with where to look it up."""
    error = await _refusal(tools, "ghost_entry")

    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert "ghost_entry" in error["message"]
    assert "May indicate" in error["message"]
    assert "ha_get_integration" in error["message"]


async def test_a_refused_entry_delete_is_not_reported_as_a_missing_entry(
    hass: HomeAssistant, tools: IntegrationTools, read_only_client: HomeAssistantClient
) -> None:
    """Core refuses a non-admin's delete; that is not "not found"."""
    entry = MockConfigEntry(domain="utility_meter")
    entry.add_to_hass(hass)

    error = await _refusal(IntegrationTools(read_only_client), entry.entry_id)

    assert error["code"] == "AUTH_INVALID_TOKEN"
    assert hass.config_entries.async_get_entry(entry.entry_id) is not None


# --- a storage helper --------------------------------------------------------------


async def test_a_disabled_helper_is_deleted_through_its_registry_entry(
    hass: HomeAssistant, tools: IntegrationTools, core_client: HomeAssistantClient
) -> None:
    """#1057: a disabled helper has no state (Core answers its state read
    with 404), yet its registry entry still names what to delete."""
    storage_id, entity_id = await _button(hass, core_client)
    await _disable(hass, entity_id)

    result = await _remove(tools, entity_id, helper_type="input_button")

    assert result["method"] == "websocket_delete"
    assert result["unique_id"] == storage_id
    assert storage_id not in await _stored_buttons(core_client)


@pytest.mark.parametrize(
    "target",
    [f"input_button.never_{uuid.uuid4().hex[:8]}", "input_button.Not An Id"],
    ids=["not_found", "invalid_format"],
)
async def test_a_helper_in_neither_state_nor_registry_is_reported_missing(
    tools: IntegrationTools, target: str
) -> None:
    """Core answers the registry read with not_found, or invalid_format for an
    id that cannot name an entity; the caller is told to check with ha_search."""
    error = await _refusal(tools, target, helper_type="input_button")

    assert error["code"] == "ENTITY_NOT_FOUND"
    assert target in error["message"]
    assert "May indicate" in error["message"]
    assert "ha_search" in error["message"]


async def test_a_refused_state_read_is_not_reported_as_a_missing_helper(
    hass: HomeAssistant,
    tools: IntegrationTools,
    read_only_client: HomeAssistantClient,
    hass_read_only_user: MockUser,
) -> None:
    """Core refuses the state read to a user without entity access: only a 404
    may make the helper count as missing."""
    hass_read_only_user.mock_policy({})

    error = await _refusal(
        IntegrationTools(read_only_client),
        f"input_button.blocked_{uuid.uuid4().hex[:8]}",
        helper_type="input_button",
    )

    assert error["code"] == "AUTH_INVALID_TOKEN"


async def test_a_disabled_entry_without_a_unique_id_is_not_reported_removed(
    hass: HomeAssistant,
    tools: IntegrationTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """The registry still holds the entry, so the helper is not gone; nothing
    is deleted by a guessed id."""
    stored, _ = await _button(hass, core_client)
    entity = entity_registry.async_get_or_create(
        "input_button",
        "input_button",
        "",
        suggested_object_id=f"orphan_{uuid.uuid4().hex[:8]}",
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    error = await _refusal(tools, entity.entity_id, helper_type="input_button")

    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "registry entry exists" in error["message"]
    assert await _stored_buttons(core_client) == {stored}


@pytest.mark.parametrize("disabled", [False, True], ids=["state_present", "disabled"])
async def test_an_unreadable_registry_entry_is_not_reported_missing(
    hass: HomeAssistant,
    tools: IntegrationTools,
    core_client: HomeAssistantClient,
    disabled: bool,
) -> None:
    """#2699: Core fails the registry read. With a state the entity exists;
    without one a disabled helper keeps its registry entry. Only Core's
    not_found proves it missing, so the read failure is reported with its
    cause and nothing is deleted."""
    storage_id, entity_id = await _button(hass, core_client)
    if disabled:
        await _disable(hass, entity_id)

    with patch.object(
        er.RegistryEntry,
        "extended_dict",
        new_callable=PropertyMock,
        side_effect=HomeAssistantError("registry unavailable"),
    ):
        error = await _refusal(tools, entity_id, helper_type="input_button")

    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "registry unavailable" in error["message"]
    assert storage_id in await _stored_buttons(core_client)


async def test_an_entity_with_a_state_but_no_registry_entry_is_not_reported_missing(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    """zone.home has a state but no registry entry; the caller learns it is
    configured elsewhere, not that it is missing."""
    assert await async_setup_component(hass, "zone", {})

    error = await _refusal(tools, "zone.home", helper_type="zone")

    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert "no entity registry entry" in error["message"]


async def test_a_delete_core_refuses_is_reported_with_its_reason(
    hass: HomeAssistant,
    tools: IntegrationTools,
    core_client: HomeAssistantClient,
    read_only_client: HomeAssistantClient,
) -> None:
    storage_id, entity_id = await _button(hass, core_client)

    error = await _refusal(
        IntegrationTools(read_only_client), entity_id, helper_type="input_button"
    )

    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "Failed to delete helper" in error["message"]
    assert "Unauthorized" in error["message"]
    assert storage_id in await _stored_buttons(core_client)


async def test_a_yaml_helper_is_reported_as_not_deletable_here(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    """Core has its registry entry but no stored item, so the delete is not
    found; the caller learns it is a YAML helper."""
    object_id = f"yaml_switch_{uuid.uuid4().hex[:8]}"
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {object_id: {}}}
    )

    error = await _refusal(
        tools, f"input_boolean.{object_id}", helper_type="input_boolean"
    )

    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert "YAML" in error["message"]


async def test_a_dropped_registry_read_is_a_connection_error(
    hass: HomeAssistant, tools: IntegrationTools, core_client: HomeAssistantClient
) -> None:
    """No answer to the registry read is not evidence the helper is missing."""
    storage_id, entity_id = await _button(hass, core_client)
    socket = await pooled_socket(core_client)

    with dropped_on(hass, "config/entity_registry/get", socket):
        error = await _refusal(tools, entity_id, helper_type="input_button")

    assert error["code"] == "CONNECTION_FAILED"
    assert storage_id in await _stored_buttons(core_client)


async def test_waiting_for_a_removed_helper_adds_no_warning(
    hass: HomeAssistant, tools: IntegrationTools, core_client: HomeAssistantClient
) -> None:
    _, entity_id = await _button(hass, core_client)

    with patch.object(
        ws_waiters,
        "wait_for_entity_removed",
        wraps=ws_waiters.wait_for_entity_removed,
    ) as waited:
        result = await _remove(tools, entity_id, helper_type="input_button", wait=True)

    assert result["success"] is True
    assert not result.get("warnings")
    waited.assert_awaited_once()
    assert hass.states.get(entity_id) is None


async def test_a_helper_still_present_after_the_wait_is_a_warning(
    hass: HomeAssistant, tools: IntegrationTools, core_client: HomeAssistantClient
) -> None:
    _, entity_id = await _button(hass, core_client)

    with (
        at_entity_wait(hass, lambda: hass.states.async_set(entity_id, "unknown")),
        patch.object(
            ws_waiters,
            "wait_for_entity_removed",
            partial(ws_waiters.wait_for_entity_removed, timeout=0.05),
        ),
    ):
        result = await _remove(tools, entity_id, helper_type="input_button", wait=True)

    assert result["success"] is True
    assert any("still present" in w for w in result["warnings"])


# --- a flow helper -----------------------------------------------------------------


async def test_a_flow_helper_missing_from_registry_and_state_is_reported_missing(
    tools: IntegrationTools,
) -> None:
    error = await _refusal(tools, "template.ghost", helper_type="template")

    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "template.ghost" in error["message"]
    assert "May indicate" in error["message"]
    assert "ha_search" in error["message"]


async def test_a_yaml_flow_helper_is_reported_as_not_storage_based(
    tools: IntegrationTools, entity_registry: er.EntityRegistry
) -> None:
    """A template entity without a config entry is YAML-configured."""
    entity = entity_registry.async_get_or_create(
        "sensor", "template", f"yaml_{uuid.uuid4().hex[:8]}"
    )

    error = await _refusal(tools, entity.entity_id, helper_type="template")

    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert "storage-based" in error["message"]


async def test_a_helper_type_never_deletes_another_integrations_entry(
    hass: HomeAssistant, tools: IntegrationTools, entity_registry: er.EntityRegistry
) -> None:
    """group + a Hue light would otherwise remove the whole Hue integration."""
    hue = MockConfigEntry(domain="hue")
    hue.add_to_hass(hass)
    lamp = entity_registry.async_get_or_create("light", "hue", "lamp", config_entry=hue)

    error = await _refusal(tools, lamp.entity_id, helper_type="group")

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert hass.config_entries.async_get_entry(hue.entry_id) is not None


async def test_an_entry_removed_meanwhile_is_reported_as_a_concurrent_removal(
    hass: HomeAssistant, tools: IntegrationTools, entity_registry: er.EntityRegistry
) -> None:
    """The entry the registry named is gone when the delete reaches Core: the
    race is reported, not passed off as a removal this call made."""
    entry, entity_ids = _meter_entry(hass, entity_registry, "peak")

    async with held_command(hass, "config/entity_registry/list") as held:
        call = asyncio.ensure_future(
            rejected_call_body(
                _remove(tools, entity_ids[0], helper_type="utility_meter")
            )
        )
        await held.arrived.wait()
        await hass.config_entries.async_remove(entry.entry_id)
        held.release.set()
        error = (await call)["error"]

    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert entry.entry_id in error["message"]
    assert "concurrent removal" in error["message"]


async def test_a_flow_helper_deleted_without_unloading_says_a_restart_is_needed(
    tools: IntegrationTools, unloadable_meter: tuple[MockConfigEntry, list[str]]
) -> None:
    _, entity_ids = unloadable_meter

    result = await _remove(tools, entity_ids[0], helper_type="utility_meter")

    assert result["method"] == "config_flow_delete"
    assert result["require_restart"] is True


async def test_sub_entities_still_present_after_the_wait_are_named_in_a_warning(
    hass: HomeAssistant, tools: IntegrationTools, entity_registry: er.EntityRegistry
) -> None:
    """utility_meter: one sub-entity is gone, the other outlives the wait."""
    _, (peak, offpeak) = _meter_entry(hass, entity_registry, "peak", "offpeak")

    with (
        at_entity_wait(hass, lambda: hass.states.async_set(offpeak, "1")),
        patch(
            "ha_mcp.tools.tools_integrations.wait_for_entity_removed",
            new_callable=AsyncMock,
            wraps=partial(ws_waiters.wait_for_entity_removed, timeout=0.05),
        ) as waited,
    ):
        result = await _remove(tools, peak, helper_type="utility_meter", wait=True)

    assert result["success"] is True
    warnings = " ".join(result["warnings"])
    assert offpeak in warnings
    assert peak not in warnings
    assert waited.await_count == 2
