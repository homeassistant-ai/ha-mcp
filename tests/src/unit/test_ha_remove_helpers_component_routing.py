"""Routing of ``ha_remove_helpers_integrations`` over the component's
``registry_lookup``.

Deleting a storage helper by an entity_id that is not its storage id needs the
entity's ``unique_id``: the legacy path reads ``config/entity_registry/get`` in a
three-attempt retry loop, and with the component one
``registry_lookup(entity_ids=[...])`` read replaces it. Deleting a flow helper
needs every entity of its config entry: the legacy path reads the whole
``config/entity_registry/list``, and ``registry_lookup(config_entry_id=...)``
returns them scoped in one frame. Every miss of the component (no capability, a
command it no longer knows, a timeout, a socket that cannot be opened) runs the
legacy read for the same result.

The server's own client runs on a real ``hass``. Storage helpers are created
through Core's commands, flow helpers are ``MockConfigEntry`` rows with their
registry entities, and Core answers every read and performs the delete. A spy
on the pooled socket records which reads the tool sent.
"""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import component_api, component_registry_lookup
from ha_mcp.tools.component_registry_lookup import (
    fetch_entities_for_config_entry_via_component,
    resolve_entities_via_component,
)
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._client_spies import sent_commands
from ._routing_core import (
    held_command,
    invalidations,
    pooled_socket,
    sent_frames,
    sent_types,
)
from ._tool_errors import error_of

LOOKUP = "ha_mcp_tools/registry_lookup"
ENTITY_GET = "config/entity_registry/get"
ENTITY_DUMP = "config/entity_registry/list"


class Button(NamedTuple):
    storage_id: str
    entity_id: str


class FlowHelper(NamedTuple):
    entry_id: str
    entity_ids: set[str]
    target: str


@pytest.fixture
async def core(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Core's registry and config entry API and storage input_buttons."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "input_button", {})


@pytest.fixture
async def client(
    core: None, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return component_core_client


async def _button(hass: HomeAssistant, client: HomeAssistantClient) -> Button:
    """A stored input_button whose entity was renamed away from its storage id,
    so deleting it by entity_id needs the registry's unique_id."""
    suffix = uuid.uuid4().hex[:8]
    created = await client.send_websocket_message(
        {"type": "input_button/create", "name": f"My Button {suffix}"}
    )
    storage_id = created["result"]["id"]
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("input_button", "input_button", storage_id)
    assert entity_id is not None
    renamed = f"input_button.renamed_{suffix}"
    registry.async_update_entity(entity_id, new_entity_id=renamed)
    await hass.async_block_till_done()
    return Button(storage_id, renamed)


def _flow_helper(hass: HomeAssistant) -> FlowHelper:
    """A min_max helper entry with three entities, and an entity of another entry."""
    entry = MockConfigEntry(domain="min_max", title="Energy")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entity_ids = {
        registry.async_get_or_create(
            "sensor",
            "min_max",
            object_id,
            config_entry=entry,
            suggested_object_id=f"energy_{object_id}",
        ).entity_id
        for object_id in ("peak", "offpeak", "total")
    }
    other = MockConfigEntry(domain="min_max", title="Other")
    other.add_to_hass(hass)
    registry.async_get_or_create("sensor", "min_max", "other", config_entry=other)
    return FlowHelper(entry.entry_id, entity_ids, "sensor.energy_peak")


async def _remove_button(client: HomeAssistantClient, target: str) -> dict[str, Any]:
    result: dict[str, Any] = await IntegrationTools(
        client
    ).ha_remove_helpers_integrations(
        target=target, helper_type="input_button", confirm=True, wait=False
    )
    return result


async def _remove_flow(client: HomeAssistantClient, target: str) -> dict[str, Any]:
    result: dict[str, Any] = await IntegrationTools(
        client
    ).ha_remove_helpers_integrations(
        target=target, helper_type="min_max", confirm=True, wait=False
    )
    return result


async def _stored_buttons(client: HomeAssistantClient) -> list[str]:
    listed = await client.send_websocket_message({"type": "input_button/list"})
    return [item["id"] for item in listed["result"]]


async def _assert_deleted_by_the_legacy_resolve(
    client: HomeAssistantClient, button: Button, types: list[str]
) -> None:
    assert types.count(ENTITY_GET) == 1
    assert button.storage_id not in await _stored_buttons(client)


# --- storage helper: registry_lookup(entity_ids=...) --------------------------------


async def test_a_storage_helper_is_resolved_by_one_component_read_and_deleted(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    button = await _button(hass, client)
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await _remove_button(client, button.entity_id)

    assert resp["success"] is True
    assert resp["method"] == "websocket_delete"
    assert resp["unique_id"] == button.storage_id
    assert resp["entity_ids"] == [button.entity_id]
    assert ENTITY_GET not in sent_types(sent)
    assert sent_frames(sent, LOOKUP) == [{"entity_ids": [button.entity_id]}]
    assert button.storage_id not in await _stored_buttons(client)


async def test_without_the_component_the_registry_resolves_the_storage_helper(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    button = await _button(hass, core_client)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        resp = await _remove_button(core_client, button.entity_id)

    assert resp["unique_id"] == button.storage_id
    assert LOOKUP not in sent_types(sent)
    await _assert_deleted_by_the_legacy_resolve(core_client, button, sent_types(sent))


async def test_a_component_without_the_command_is_invalidated_and_core_resolves(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    button = await _button(hass, client)
    assert await component_api.get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], LOOKUP)
    socket = await pooled_socket(client)

    with (
        sent_commands(socket) as sent,
        invalidations(component_registry_lookup) as invalidate,
    ):
        resp = await _remove_button(client, button.entity_id)

    assert resp["unique_id"] == button.storage_id
    await _assert_deleted_by_the_legacy_resolve(client, button, sent_types(sent))
    invalidate.assert_called_once_with(client)


async def test_a_timed_out_component_read_falls_back_and_keeps_the_capability(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    button = await _button(hass, client)
    socket = await pooled_socket(client)

    async with held_command(hass, client, LOOKUP):
        with (
            sent_commands(socket) as sent,
            invalidations(component_registry_lookup) as invalidate,
        ):
            resp = await _remove_button(client, button.entity_id)

    assert resp["unique_id"] == button.storage_id
    await _assert_deleted_by_the_legacy_resolve(client, button, sent_types(sent))
    invalidate.assert_not_called()


async def test_an_unreachable_component_socket_falls_back_to_the_retry_loop(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """An escaping error would skip the legacy resolve entirely; the legacy
    read rides the WebSocket bridge, not the component's socket."""
    button = await _button(hass, client)
    assert await component_api.get_component_caps(client) is not None
    socket = await pooled_socket(client)

    with (
        patch.object(
            component_registry_lookup,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
        ),
        sent_commands(socket) as sent,
        invalidations(component_registry_lookup) as invalidate,
    ):
        resp = await _remove_button(client, button.entity_id)

    assert resp["unique_id"] == button.storage_id
    await _assert_deleted_by_the_legacy_resolve(client, button, sent_types(sent))
    invalidate.assert_not_called()


async def test_a_registered_entity_without_a_unique_id_is_never_deleted_by_guess(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The component finds the entity but no unique_id to delete it by: the
    error names the one component read, not a three-attempt retry loop, and
    no helper is deleted by its object id."""
    stored = await _button(hass, client)
    entity = er.async_get(hass).async_get_or_create(
        "input_button", "flow_test", "", suggested_object_id="my_button"
    )
    hass.states.async_set(entity.entity_id, "unknown")
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent, pytest.raises(ToolError) as exc:
        await _remove_button(client, entity.entity_id)

    error = error_of(exc)
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "Component registry lookup" in error["message"]
    assert "3 attempts" not in error["message"]
    assert ENTITY_GET not in sent_types(sent)
    assert await _stored_buttons(client) == [stored.storage_id]


async def test_an_entity_the_component_misses_but_core_has_is_not_reported_missing(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """zone.home has a state but no registry entry: the caller learns it is
    not registry-managed, not that it was already deleted."""
    assert await async_setup_component(hass, "zone", {})
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent, pytest.raises(ToolError) as exc:
        await IntegrationTools(client).ha_remove_helpers_integrations(
            target="zone.home", helper_type="zone", confirm=True, wait=False
        )

    assert error_of(exc)["code"] == "RESOURCE_NOT_FOUND"
    assert ENTITY_GET not in sent_types(sent)


# --- flow helper: registry_lookup(config_entry_id=...) ------------------------------


async def test_a_flow_helpers_entities_come_from_one_component_read(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    helper = _flow_helper(hass)
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await _remove_flow(client, helper.target)

    assert resp["success"] is True
    assert resp["method"] == "config_flow_delete"
    assert resp["entry_id"] == helper.entry_id
    assert set(resp["entity_ids"]) == helper.entity_ids
    assert ENTITY_DUMP not in sent_types(sent)
    assert sent_frames(sent, LOOKUP) == [{"config_entry_id": helper.entry_id}]
    assert hass.config_entries.async_get_entry(helper.entry_id) is None


async def test_without_the_component_a_flow_helpers_entities_come_from_the_registry(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    """The whole registry is read and only the entry's own entities kept."""
    helper = _flow_helper(hass)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        resp = await _remove_flow(core_client, helper.target)

    assert set(resp["entity_ids"]) == helper.entity_ids
    types = sent_types(sent)
    assert types.count(ENTITY_DUMP) == 1
    assert LOOKUP not in types


async def test_an_unreachable_component_socket_reads_the_registry_without_a_warning(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The flow-helper delete proceeds exactly as on an install without the
    component."""
    helper = _flow_helper(hass)
    assert await component_api.get_component_caps(client) is not None
    socket = await pooled_socket(client)

    with (
        patch.object(
            component_registry_lookup,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
        ),
        sent_commands(socket) as sent,
    ):
        resp = await _remove_flow(client, helper.target)

    assert set(resp["entity_ids"]) == helper.entity_ids
    assert sent_types(sent).count(ENTITY_DUMP) == 1
    assert not resp.get("warnings")


async def test_an_unexpected_entity_read_failure_is_a_warning_naming_that_read(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """A fault that escapes the component read does not abort the delete; the
    warning names ``registry_lookup``, the read that failed (F8)."""
    helper = _flow_helper(hass)
    socket = await pooled_socket(client)

    with (
        patch(
            "ha_mcp.tools.config_helpers.registry"
            ".fetch_entities_for_config_entry_via_component",
            side_effect=RuntimeError("boom"),
        ),
        sent_commands(socket) as sent,
    ):
        resp = await _remove_flow(client, helper.target)

    assert resp["success"] is True
    assert any(
        f"registry_lookup failed for config_entry_id={helper.entry_id}" in w
        for w in resp["warnings"]
    )
    assert ENTITY_DUMP not in sent_types(sent)
    assert hass.config_entries.async_get_entry(helper.entry_id) is None


# --- the component_registry_lookup reads ----------------------------------------------


@pytest.mark.parametrize("read", ["config_entry", "entities"])
async def test_a_component_without_the_command_is_invalidated(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    read: str,
) -> None:
    helper = _flow_helper(hass)
    assert await component_api.get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], LOOKUP)

    result: object
    with invalidations(component_registry_lookup) as invalidate:
        if read == "config_entry":
            result = await fetch_entities_for_config_entry_via_component(
                client, helper.entry_id
            )
        else:
            result = await resolve_entities_via_component(client, [helper.target])

    assert result is None
    invalidate.assert_called_once_with(client)


async def test_a_timed_out_config_entry_read_keeps_the_capability(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    helper = _flow_helper(hass)

    async with held_command(hass, client, LOOKUP):
        with invalidations(component_registry_lookup) as invalidate:
            result = await fetch_entities_for_config_entry_via_component(
                client, helper.entry_id
            )

    assert result is None
    invalidate.assert_not_called()


async def test_an_unreachable_socket_is_no_entity_answer(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The caller's legacy per-id resolve runs instead."""
    helper = _flow_helper(hass)
    assert await component_api.get_component_caps(client) is not None

    with patch.object(
        component_registry_lookup,
        "get_websocket_client",
        AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
    ):
        assert await resolve_entities_via_component(client, [helper.target]) is None
