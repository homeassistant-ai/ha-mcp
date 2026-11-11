"""Cross-seam contract test for the ``server_entry`` capability.

A real ``hass`` holds both ``ha_mcp_tools``-domain entries the component can
register, a "server" entry and a "services" entry. The REAL server-side
``find_server_config_entry`` asks the component's REAL
``ha_mcp_tools/server_entry`` command over Core's WebSocket API, then opens the
server entry's REAL options flow over Core's REST API, so a vocabulary or shape
drift between the component's marker-based entry discrimination and the
server's consumption of it fails here rather than at runtime.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.const import DOMAIN, OPT_PIP_SPEC
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_dev import find_server_config_entry

from ._server_entry import add_server_entry

PIP_SPEC = "ha-mcp==7.0.0"


def _add_services_entry(hass: HomeAssistant) -> MockConfigEntry:
    """The services (tools) entry: no ``entry_type`` marker, as the tools step stores it."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    return entry


def _legacy_lookups(send: Any) -> list[Any]:
    """The legacy ``config_entries/get`` probes among the WebSocket sends."""
    return [
        call
        for call in send.await_args_list
        if call.args and call.args[0].get("type") == "config_entries/get"
    ]


@pytest.mark.asyncio
async def test_real_server_entry_picks_the_server_entry_over_services(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A server entry AND a services entry: the real ``_do_server_entry``
    picks the server one by its ``entry_type`` marker, and the real
    ``find_server_config_entry`` opens exactly its flow."""
    assert await async_setup_component(hass, "config", {})
    services_entry = _add_services_entry(hass)
    server_entry = add_server_entry(hass, options={OPT_PIP_SPEC: PIP_SPEC})
    client = component_core_client

    with (
        patch.object(
            client, "start_options_flow", wraps=client.start_options_flow
        ) as opened,
        patch.object(
            client, "abort_options_flow", wraps=client.abort_options_flow
        ) as aborted,
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ) as send,
    ):
        found = await find_server_config_entry(client)

    assert found is not None
    entry_id, flow, current = found
    assert entry_id == server_entry.entry_id
    options_flows = hass.config_entries.options
    assert [
        open_flow["flow_id"]
        for open_flow in options_flows.async_progress_by_handler(entry_id)
    ] == [flow["flow_id"]]
    assert current[OPT_PIP_SPEC] == PIP_SPEC
    # Exactly one flow was opened, for the entry the component identified —
    # the services entry was never touched (no abort, no probe).
    opened.assert_awaited_once_with(server_entry.entry_id)
    aborted.assert_not_awaited()
    assert options_flows.async_progress_by_handler(services_entry.entry_id) == []
    assert _legacy_lookups(send) == []
    options_flows.async_abort(flow["flow_id"])


@pytest.mark.asyncio
async def test_real_server_entry_no_server_entry_returns_none(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Only a services entry: the real ``_do_server_entry`` reports
    ``entry_id: None`` (authoritative) and ``find_server_config_entry``
    returns ``None`` without opening any options flow."""
    assert await async_setup_component(hass, "config", {})
    _add_services_entry(hass)
    client = component_core_client

    with (
        patch.object(
            client, "start_options_flow", wraps=client.start_options_flow
        ) as opened,
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ) as send,
    ):
        found = await find_server_config_entry(client)

    assert found is None
    opened.assert_not_awaited()
    assert hass.config_entries.options.async_progress() == []
    assert _legacy_lookups(send) == []
