"""Routing tests for ``ha_dev_manage_server(update_source)`` over the component
``server_entry_update`` WRITE capability (issue #1813 Phase 3).

In EMBEDDED mode the tool prefers the component's one-hop direct write
(``ha_mcp_tools/server_entry_update`` → ``async_update_entry``) over the
options-flow start+submit round-trip. These pin the routing contract:

* component present → the component write is used, its reply is mapped, and
  no options flow is opened or submitted;
* component absent → the legacy (deferred) options-flow submit;
* ``unknown_command`` invalidates caps and a dropped connection falls back to
  the legacy submit; the write is idempotent, so a re-apply is safe;
* NON-embedded mode never routes to the component write and submits the
  options flow synchronously.

The tool runs on the server's own client against a real ``hass``: the server
entry is a real config entry, its options flow the component's own.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools import tools_dev

from ._client_spies import sent_commands
from ._component_frames import drop_connection_on, frames, pooled
from ._core_setup import with_config_component
from ._server_update import (
    EMBEDDED_TARGET,
    PIP_SPEC,
    SERVER_URL,
    applied,
    server_entry,
    update_source,
)

_RESTART_NOTE = (
    "The in-process server will reinstall and restart now; this "
    "connection will drop. Reconnect in 1-5 minutes and verify with "
    "ha_dev_manage_server('info')."
)


@pytest.fixture(autouse=True)
def _embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Embedded mode by default; a test clears it for the non-embedded path."""
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")


@pytest.fixture
async def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The server's client with the component; ``config`` serves the flows."""
    return await with_config_component(hass, component_core_client)


@pytest.fixture
async def socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled(client)


@contextmanager
def _flow_calls(client: HomeAssistantClient) -> Iterator[tuple[AsyncMock, AsyncMock]]:
    """Spies on the options-flow open and submit; Core still answers."""
    with (
        patch.object(
            client, "start_options_flow", wraps=client.start_options_flow
        ) as opened,
        patch.object(
            client, "submit_options_flow_step", wraps=client.submit_options_flow_step
        ) as submitted,
    ):
        yield opened, submitted


async def test_embedded_component_present_routes_to_component_write(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """Component present → its direct write; the reply is mapped; the legacy
    submit never runs and find_server_config_entry never runs, so NO options
    flow is opened (the fast path is not gated behind the flow-open it
    bypasses). The frame goes out on the pooled socket keyed by the client's
    own ``verify_ssl``, so a ``verify_ssl=False`` client never shares a
    default-verification connection."""
    entry = server_entry(hass, pip_spec="")

    with _flow_calls(client) as (opened, submitted), sent_commands(socket) as sent:
        result = await update_source(client, PIP_SPEC)

    data = result["data"]
    assert data["scheduled"] is True
    assert data["entry_id"] == entry.entry_id
    assert data["applying"] == {"pip_spec": PIP_SPEC}
    assert data["previous"] == {"pip_spec": ""}
    assert data["note"] == _RESTART_NOTE
    # Preserves the #1929 target field on the component path.
    assert data["target"] == EMBEDDED_TARGET
    opened.assert_not_awaited()
    submitted.assert_not_awaited()
    assert hass.config_entries.options.async_progress() == []
    # The frame carried EXACTLY the pip_spec delta.
    assert frames(sent, tools_dev.WS_SERVER_ENTRY_UPDATE) == [{"pip_spec": PIP_SPEC}]
    await applied(entry, PIP_SPEC)


async def test_embedded_unchanged_reply_maps_without_submit(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """A no-op component reply (unchanged) maps to scheduled:false/unchanged and
    still bypasses the legacy submit."""
    server_entry(hass, pip_spec="")

    with _flow_calls(client) as (_, submitted):
        result = await update_source(client, "")

    data = result["data"]
    assert data["scheduled"] is False
    assert data["unchanged"] is True
    assert data["previous"] == {"pip_spec": ""}
    assert data["note"] == (
        "No change: the requested pip_spec already matches the "
        "current in-process server source."
    )
    submitted.assert_not_awaited()


async def test_embedded_component_absent_falls_back_to_options_flow(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """No component → the legacy (deferred) options-flow submit, resending the
    preserved server_url override alongside the pip_spec change."""
    assert await async_setup_component(hass, "config", {})
    entry = server_entry(hass, pip_spec="", server_url=SERVER_URL)
    socket = await pooled(core_client)

    with _flow_calls(core_client) as (_, submitted), sent_commands(socket) as sent:
        result = await update_source(core_client, PIP_SPEC)
        assert result["data"]["scheduled"] is True
        submitted.assert_not_awaited()  # deferred, not yet run
        await applied(entry, PIP_SPEC)

    submitted.assert_awaited_once()
    assert submitted.await_args is not None
    assert submitted.await_args.args[1]["server_url"] == SERVER_URL
    assert entry.options["server_url"] == SERVER_URL
    assert frames(sent, tools_dev.WS_SERVER_ENTRY_UPDATE) == []


async def test_embedded_unknown_command_invalidates_caps_and_falls_back(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``server_entry_update`` advertised but answered ``unknown_command`` (Core's
    answer for a command it never registered, as from a component predating
    it) → invalidate the cached caps and fall back to the legacy submit."""
    entry = server_entry(hass, pip_spec="")
    monkeypatch.setattr(
        tools_dev, "WS_SERVER_ENTRY_UPDATE", "ha_mcp_tools/server_entry_update_retired"
    )
    invalidated = MagicMock(wraps=tools_dev.invalidate_caps)
    monkeypatch.setattr(tools_dev, "invalidate_caps", invalidated)

    with _flow_calls(client) as (_, submitted):
        await update_source(client, PIP_SPEC)
        await applied(entry, PIP_SPEC)

    submitted.assert_awaited_once()
    invalidated.assert_called_once_with(client)


async def test_embedded_connection_drop_falls_back(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection dropping on the ``server_entry_update`` frame falls back
    to the legacy submit (the write is idempotent, so a re-apply is safe) and
    keeps the capability: a lost answer is not a downgrade."""
    entry = server_entry(hass, pip_spec="")
    drop_connection_on(hass, tools_dev.WS_SERVER_ENTRY_UPDATE, socket)
    invalidated = MagicMock(wraps=tools_dev.invalidate_caps)
    monkeypatch.setattr(tools_dev, "invalidate_caps", invalidated)

    with _flow_calls(client) as (_, submitted):
        await update_source(client, PIP_SPEC)
        await applied(entry, PIP_SPEC)

    submitted.assert_awaited_once()
    invalidated.assert_not_called()


async def test_non_embedded_never_routes_to_component_write(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NON-embedded mode submits the options flow SYNCHRONOUSLY and never sends
    the server_entry_update frame, though the component offers it."""
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    entry = server_entry(hass, pip_spec="", server_url=SERVER_URL)

    with _flow_calls(client) as (_, submitted), sent_commands(socket) as sent:
        result = await update_source(client, PIP_SPEC)

    # "applied" reports only what the CALLER asked to change. The preserved
    # overrides are resent to avoid wiping them, not requested, and the
    # component's options form carries oauth_client_secret among them, so
    # echoing the whole payload handed a credential back to the client
    # (Codex review, #2256).
    assert result["data"]["applied"] == {"pip_spec": PIP_SPEC}
    submitted.assert_awaited_once()
    # ...while the SUBMISSION still carries the preserved override, or the
    # update would clear it.
    assert entry.options["pip_spec"] == PIP_SPEC
    assert entry.options["server_url"] == SERVER_URL
    assert frames(sent, tools_dev.WS_SERVER_ENTRY_UPDATE) == []
