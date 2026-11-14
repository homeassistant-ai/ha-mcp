"""``find_server_config_entry`` without the component, or when it fails.

The component's ``server_entry`` names the server entry in one read
(``test_component_server_entry_contract.py``). Without it, or when that read
fails, the server probes every ``ha_mcp_tools`` entry's options flow and closes
the ones that are not the server's. Core holds a services entry and a server
entry; their options flows are the component's own, opened and closed through
Core's REST API.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import anyio
import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.const import DOMAIN
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_dev
from ha_mcp.tools.component_api import get_component_caps, invalidate_caps
from ha_mcp.tools.tools_dev import find_server_config_entry

from ._command_faults import dropped_on, pooled_socket, received, unanswered
from ._server_entry import add_server_entry

SERVER_ENTRY = "ha_mcp_tools/server_entry"
ENTRIES_GET = "config_entries/get"


@pytest.fixture
async def entries(hass: HomeAssistant) -> tuple[str, str]:
    """(services entry id, server entry id), in the order Core lists them."""
    assert await async_setup_component(hass, "config", {})
    services = MockConfigEntry(domain=DOMAIN, data={}, options={})
    services.add_to_hass(hass)
    server = add_server_entry(hass)
    return services.entry_id, server.entry_id


def _open_flows(hass: HomeAssistant, entry_id: str) -> list[str]:
    return [
        flow["flow_id"]
        for flow in hass.config_entries.options.async_progress_by_handler(entry_id)
    ]


def _spy_invalidate() -> Any:
    return patch.object(tools_dev, "invalidate_caps", wraps=invalidate_caps)


async def _found_after_probing(
    hass: HomeAssistant, client: HomeAssistantClient, server_id: str
) -> None:
    """The legacy probe listed the entries once and found the server entry."""
    with received(hass, ENTRIES_GET) as seen:
        found = await find_server_config_entry(client)

    assert found is not None
    assert found[0] == server_id
    assert len(seen[ENTRIES_GET]) == 1
    hass.config_entries.options.async_abort(found[1]["flow_id"])


async def test_without_the_component_every_entry_is_probed_and_the_wrong_one_closed(
    hass: HomeAssistant, core_client: HomeAssistantClient, entries: tuple[str, str]
) -> None:
    services_id, server_id = entries

    with (
        patch.object(
            core_client, "start_options_flow", wraps=core_client.start_options_flow
        ) as opened,
        patch.object(
            core_client, "abort_options_flow", wraps=core_client.abort_options_flow
        ) as aborted,
    ):
        found = await find_server_config_entry(core_client)

    assert found is not None
    entry_id, flow, current = found
    assert entry_id == server_id
    assert "pip_spec" in current
    assert [call.args[0] for call in opened.await_args_list] == [services_id, server_id]
    aborted.assert_awaited_once()
    assert _open_flows(hass, services_id) == []
    assert _open_flows(hass, server_id) == [flow["flow_id"]]
    hass.config_entries.options.async_abort(flow["flow_id"])


async def test_a_downgraded_component_is_dropped_and_the_entries_probed(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entries: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await get_component_caps(component_core_client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], SERVER_ENTRY)

    with _spy_invalidate() as invalidate:
        await _found_after_probing(hass, component_core_client, entries[1])

    invalidate.assert_called_once_with(component_core_client)


async def test_a_read_core_never_answers_falls_back_and_keeps_the_component(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entries: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _spy_invalidate() as invalidate:
        async with unanswered(hass, SERVER_ENTRY, monkeypatch):
            await _found_after_probing(hass, component_core_client, entries[1])

    invalidate.assert_not_called()


async def test_a_dropped_socket_falls_back_and_keeps_the_component(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entries: tuple[str, str],
) -> None:
    """The probe rides a reconnected socket, so it does not die with this one."""
    assert await get_component_caps(component_core_client) is not None
    socket = await pooled_socket(component_core_client)

    with _spy_invalidate() as invalidate, dropped_on(hass, SERVER_ENTRY, socket):
        await _found_after_probing(hass, component_core_client, entries[1])

    invalidate.assert_not_called()


async def test_an_identified_entry_whose_flow_will_not_open_is_not_found(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The component names the entry but Core refuses its flow, then and on the
    probe's retry: no server entry, not an error."""
    assert await async_setup_component(hass, "config", {})
    server_id = add_server_entry(hass).entry_id

    with (
        patch.object(
            config_entries.OptionsFlowManager,
            "async_create_flow",
            side_effect=data_entry_flow.UnknownHandler,
        ),
        patch.object(
            component_core_client,
            "start_options_flow",
            wraps=component_core_client.start_options_flow,
        ) as opened,
    ):
        found = await find_server_config_entry(component_core_client)

    assert found is None
    assert [call.args[0] for call in opened.await_args_list] == [server_id, server_id]


@pytest.fixture
async def silent_url(socket_enabled: None) -> AsyncIterator[str]:
    """A loopback listener that takes every request and never answers it."""

    async def never_answer(reader: asyncio.StreamReader, writer: Any) -> None:
        await reader.read()
        writer.close()

    server = await asyncio.start_server(never_answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def test_a_probe_abort_that_never_answers_does_not_hold_the_caller(
    silent_url: str,
) -> None:
    client = HomeAssistantClient(base_url=silent_url, token="token", verify_ssl=False)
    try:
        # Cleanup gives Core five seconds to abort a flow; the caller is free
        # once they ran out, well inside this bound.
        await asyncio.wait_for(
            tools_dev.abort_options_flow_quietly(client, {"flow_id": "f1"}),
            timeout=15,
        )
    finally:
        await client.close()


async def test_a_cancelled_caller_still_closes_the_probe_flow(
    hass: HomeAssistant, core_client: HomeAssistantClient, entries: tuple[str, str]
) -> None:
    services_id, _ = entries
    flow = await core_client.start_options_flow(services_id)
    assert _open_flows(hass, services_id) == [flow["flow_id"]]

    with anyio.CancelScope() as scope:
        scope.cancel()
        await tools_dev.abort_options_flow_quietly(core_client, flow)

    assert _open_flows(hass, services_id) == []
