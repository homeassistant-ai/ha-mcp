"""Routing tests for ``ha_list_services`` over the ``ha_mcp_tools`` gate.

The legacy path pays for a REST ``get_services()`` catalog fetch plus a WS
``frontend/get_translations`` round-trip on every call, regardless of the
``domain``/``query`` filters. When the component advertises ``services_list``
one ``ha_mcp_tools/services_list`` frame returns both in a single round-trip;
the server still runs its own filter and pagination over that payload. These
tests pin: the component-preferred path (both legacy calls skipped), a missing
component falls back, ``unknown_command`` invalidates caps and falls back, a
command error falls back, and a connection error ALSO falls back to the REST
legacy, because this tool's legacy path is REST + a per-request WS bridge, NOT
the shared pooled WS, so a WS outage must not kill the tool.

The tool runs on the server's own client against a real ``hass`` with
``counter`` set up; Core answers every request.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import service as core_service
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools import tools_services
from ha_mcp.tools.component_api import component_supports, get_component_caps
from ha_mcp.tools.tools_services import ServiceDiscoveryTools

from ._client_spies import sent_commands, sent_messages


@pytest.fixture
async def client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on a ``hass`` with ``counter`` and Core's translations
    command (the frontend cannot be set up here: its assets are not installed)."""
    assert await async_setup_component(hass, "counter", {})
    await hass.async_block_till_done()
    websocket_api.async_register_command(hass, websocket_get_translations)
    return core_client


@pytest.fixture
async def socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    """The pooled WebSocket client the component frame goes through."""
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


@pytest.fixture
def component(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The same client with the component's commands registered."""
    return component_core_client


@contextmanager
def _legacy_fetches(client: HomeAssistantClient) -> Iterator[tuple[AsyncMock, Any]]:
    """Spies on the REST catalog read and the WS bridge (translations)."""
    with (
        patch.object(client, "get_services", wraps=client.get_services) as rest,
        sent_messages(client) as bridge,
    ):
        yield rest, bridge


def _translation_reads(bridge: AsyncMock) -> int:
    return sum(
        1
        for call in bridge.await_args_list
        if call.args[0].get("type") == "frontend/get_translations"
    )


def _services_list_frames(spy: AsyncMock) -> list[Any]:
    return [
        c for c in spy.call_args_list if c.args[0] == tools_services.WS_SERVICES_LIST
    ]


def _served_by_legacy(rest: AsyncMock, bridge: AsyncMock) -> None:
    rest.assert_awaited_once()
    assert _translation_reads(bridge) == 1


async def test_component_preferred_skips_legacy_fetches(
    client: HomeAssistantClient,
    component: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """Capability present → one component frame, both legacy calls skipped."""
    with _legacy_fetches(client) as (rest, bridge), sent_commands(socket) as sent:
        resp = await ServiceDiscoveryTools(client).ha_list_services(domain="counter")

    assert resp["success"] is True
    assert "counter.increment" in resp["services"]
    rest.assert_not_awaited()
    assert _translation_reads(bridge) == 0
    (call,) = _services_list_frames(sent)
    assert call.kwargs["domain"] == "counter"
    # The component reads hass.config.language in-process, so the server does
    # not force a language over the wire.
    assert "language" not in call.kwargs
    assert "query" not in call.kwargs


async def test_query_is_never_forwarded_but_still_filters(
    client: HomeAssistantClient,
    component: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """``query`` is filtered server-side, never sent to the component's frame.

    Forwarding ``query`` would let a coarse per-domain pass drop a whole domain
    from the payload when nothing under it matches, while ``domains`` is
    populated from ``domain_filter`` alone. The server's exact filter runs over
    the full payload instead.
    """
    with _legacy_fetches(client) as (rest, _), sent_commands(socket) as sent:
        resp = await ServiceDiscoveryTools(client).ha_list_services(query="set_value")

    assert set(resp["services"]) == {"counter.set_value"}
    rest.assert_not_awaited()
    (call,) = _services_list_frames(sent)
    assert "query" not in call.kwargs


async def test_without_the_component_uses_legacy_fetches(
    client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
) -> None:
    """No component → legacy REST catalog + WS translations, no component frame."""
    with _legacy_fetches(client) as (rest, bridge), sent_commands(socket) as sent:
        resp = await ServiceDiscoveryTools(client).ha_list_services()

    assert resp["success"] is True
    assert "counter.increment" in resp["services"]
    _served_by_legacy(rest, bridge)
    assert not _services_list_frames(sent)


async def test_unknown_command_invalidates_and_falls_back(
    client: HomeAssistantClient,
    component: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``unknown_command`` on the services_list frame → invalidate caps + legacy.

    Core answers ``unknown_command`` for a command name it never registered,
    as it does for a component that predates the command.
    """
    monkeypatch.setattr(
        tools_services, "WS_SERVICES_LIST", "ha_mcp_tools/services_list_retired"
    )
    invalidated = MagicMock(wraps=tools_services.invalidate_caps)
    monkeypatch.setattr(tools_services, "invalidate_caps", invalidated)

    with _legacy_fetches(client) as (rest, bridge):
        resp = await ServiceDiscoveryTools(client).ha_list_services()

    assert resp["success"] is True
    _served_by_legacy(rest, bridge)
    invalidated.assert_called_once_with(client)


async def test_command_error_falls_back_and_keeps_the_capability(
    client: HomeAssistantClient,
    component: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command error that is not ``unknown_command`` → legacy fetches, caps
    stay cached. The component refuses a catalog Core could not describe; Core's
    REST view holds its own binding of the function and still answers."""
    monkeypatch.setattr(
        core_service,
        "async_get_all_descriptions",
        AsyncMock(side_effect=HomeAssistantError("descriptions unavailable")),
    )
    invalidated = MagicMock(wraps=tools_services.invalidate_caps)
    monkeypatch.setattr(tools_services, "invalidate_caps", invalidated)

    with _legacy_fetches(client) as (rest, bridge):
        resp = await ServiceDiscoveryTools(client).ha_list_services()

    assert resp["success"] is True
    assert "counter.increment" in resp["services"]
    _served_by_legacy(rest, bridge)
    invalidated.assert_not_called()


async def test_connection_error_falls_back_to_legacy(
    client: HomeAssistantClient,
    component: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """A WS-down error on the component frame falls back to the REST legacy.

    This tool's legacy path is the REST ``get_services()`` catalog + a
    per-request WS bridge for translations, NOT the shared pooled WS, so the
    REST fallback serves the catalog rather than the tool erroring out, and the
    transient error keeps the positive capability cached.
    """
    assert component_supports(await get_component_caps(client), "services_list")

    with (
        patch.object(
            socket,
            "send_command",
            AsyncMock(side_effect=HomeAssistantConnectionError("down")),
        ),
        _legacy_fetches(client) as (rest, bridge),
    ):
        resp = await ServiceDiscoveryTools(client).ha_list_services()

    assert resp["success"] is True
    assert "counter.increment" in resp["services"]
    _served_by_legacy(rest, bridge)
    assert component_supports(await get_component_caps(client), "services_list")
