"""Routing tests for radio's ``resolve_entry_id`` over the config_entries gate.

``ha_manage_radio``'s single-instance resolvers map an integration ``domain`` to
its config ``entry_id`` via ``resolve_entry_id``, which dumped EVERY config entry
over the legacy ``config_entries/get`` WS read and filtered client-side. When the
component advertises ``config_entries`` the domain-scoped read now rides one
``ha_mcp_tools/config_entries`` frame (shared ``fetch_config_entries``);
the legacy ``config_entries/get`` dump stays the fallback. These pin the seam:
component-hit (no legacy dump), legacy fallback on no-caps, ``unknown_command`` →
invalidate caps + legacy, an establish failure → legacy, and
"integration not configured" → None.

Every reply is real Core's: the server's client on a real ``hass``, with the
component's commands registered where the component is installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.websocket_api.const import DOMAIN as WS_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools import component_config_entries
from ha_mcp.tools.component_api import get_component_caps
from ha_mcp.tools.radio.base import resolve_entry_id

WS_CONFIG_ENTRIES = "ha_mcp_tools/config_entries"


async def _zwave_entry(hass: HomeAssistant) -> None:
    """Core's config entries API, holding one ``zwave_js`` entry."""
    assert await async_setup_component(hass, "config", {})
    MockConfigEntry(
        domain="zwave_js", entry_id="cfg-zwave", title="Title zwave_js"
    ).add_to_hass(hass)


@contextmanager
def _routes(
    client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
) -> Iterator[tuple[AsyncMock, AsyncMock]]:
    """Spies on the legacy bridge and the pooled component socket; both call through."""
    with (
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ) as legacy,
        patch.object(socket, "send_command", wraps=socket.send_command) as component,
    ):
        yield legacy, component


async def _socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


def _legacy_dumps(legacy: AsyncMock) -> int:
    return sum(
        1
        for call in legacy.call_args_list
        if call.args[0].get("type") == "config_entries/get"
    )


def _entries_calls(component: AsyncMock) -> list[Any]:
    return [
        call for call in component.call_args_list if call.args[0] == WS_CONFIG_ENTRIES
    ]


async def test_resolve_entry_id_served_by_component(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The domain read rides one config_entries frame; the legacy dump never runs."""
    await _zwave_entry(hass)
    socket = await _socket(component_core_client)

    with _routes(component_core_client, socket) as (legacy, component):
        entry_id = await resolve_entry_id(component_core_client, "zwave_js")

    assert entry_id == "cfg-zwave"
    assert _legacy_dumps(legacy) == 0
    calls = _entries_calls(component)
    assert len(calls) == 1
    assert calls[0].kwargs["domain"] == "zwave_js"


async def test_resolve_entry_id_capsless_uses_legacy_dump(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without the component, Core rejects its info probe: legacy dump."""
    await _zwave_entry(hass)
    socket = await _socket(core_client)

    with _routes(core_client, socket) as (legacy, component):
        entry_id = await resolve_entry_id(core_client, "zwave_js")

    assert entry_id == "cfg-zwave"
    assert _legacy_dumps(legacy) == 1
    assert not _entries_calls(component)


async def test_resolve_entry_id_unknown_command_invalidates_and_falls_back(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caps cached from a component that has since lost the command."""
    await _zwave_entry(hass)
    caps = await get_component_caps(component_core_client)
    assert caps is not None
    # A downgraded component: Core no longer knows the command and answers
    # unknown_command itself.
    hass.data[WS_DOMAIN].pop(WS_CONFIG_ENTRIES)
    invalidated = MagicMock(wraps=component_config_entries.invalidate_caps)
    monkeypatch.setattr(component_config_entries, "invalidate_caps", invalidated)
    socket = await _socket(component_core_client)

    with _routes(component_core_client, socket) as (legacy, _component):
        entry_id = await resolve_entry_id(component_core_client, "zwave_js")

    assert entry_id == "cfg-zwave"
    assert _legacy_dumps(legacy) == 1
    invalidated.assert_called_once_with(component_core_client)


async def test_resolve_entry_id_establish_failure_falls_back_to_legacy(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A WS establish failure after caps cached → legacy dump.

    The config_entries legacy path is the ``send_websocket_message`` bridge, which
    acquires the pooled socket again itself, so the establish failure must route
    to legacy.
    """
    await _zwave_entry(hass)
    assert await get_component_caps(component_core_client) is not None

    with (
        patch.object(
            component_config_entries,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("WS establish failed")),
        ),
        patch.object(
            component_core_client,
            "send_websocket_message",
            wraps=component_core_client.send_websocket_message,
        ) as legacy,
    ):
        entry_id = await resolve_entry_id(component_core_client, "zwave_js")

    assert entry_id == "cfg-zwave"
    assert _legacy_dumps(legacy) == 1


async def test_resolve_entry_id_not_configured_returns_none(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Component serves an authoritative empty entries list → no match → None."""
    assert await async_setup_component(hass, "config", {})
    socket = await _socket(component_core_client)

    with _routes(component_core_client, socket) as (legacy, component):
        entry_id = await resolve_entry_id(component_core_client, "zwave_js")

    assert entry_id is None
    assert _legacy_dumps(legacy) == 0
    assert len(_entries_calls(component)) == 1
