"""The component-gated config-entry reads, chiefly ``unique_id``.

Home Assistant withholds a config entry's ``unique_id`` from every endpoint it
has, so the component is the only source, and the value it returns is
three-valued, not two. These tests keep "we could not read it" distinct from
"the entry has none": conflating them is what made the anchor dead in the first
place, and what would silently disarm it again.

Every read goes to a real ``hass``: entries are ``MockConfigEntry`` rows and
``component_core_client`` answers the component's ``config_entries`` command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import get_websocket_client, websocket_manager
from ha_mcp.config import get_global_settings
from ha_mcp.tools import component_config_entries
from ha_mcp.tools.component_api import get_component_caps, invalidate_caps
from ha_mcp.tools.component_config_entries import (
    UNKNOWN_UNIQUE_ID,
    WS_CONFIG_ENTRIES,
    EntryUniqueId,
    fetch_config_entries,
    fetch_config_entry_unique_id,
)


def _add_entry(
    hass: HomeAssistant, *, domain: str = "mqtt", unique_id: str | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(domain=domain, unique_id=unique_id)
    entry.add_to_hass(hass)
    return entry


async def _pooled_ws(client: HomeAssistantClient) -> Any:
    """The pooled WebSocket client the component reads share with the probe."""
    return await get_websocket_client(
        url=client.base_url, token=client.token, verify_ssl=client.verify_ssl
    )


def _sent_commands(send: AsyncMock) -> list[str]:
    return [call.args[0] for call in send.await_args_list]


async def test_returns_the_value_when_the_component_reports_one(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    entry = _add_entry(hass, unique_id="abc-123")

    assert await fetch_config_entry_unique_id(
        component_core_client, entry.entry_id
    ) == EntryUniqueId(known=True, value="abc-123")


async def test_known_absent_when_the_entry_genuinely_has_none(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """MQTT-style entries have no unique_id: that is an answer, not a gap."""
    entry = _add_entry(hass, unique_id=None)

    assert await fetch_config_entry_unique_id(
        component_core_client, entry.entry_id
    ) == EntryUniqueId(known=True, value=None)


@pytest.mark.parametrize("component", [True, False], ids=["component", "no_component"])
async def test_unknown_without_the_capability_and_nothing_is_sent(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    component: bool,
) -> None:
    """No component (app / Docker / PyPI): the read is not even sent."""
    if component:
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    entry = _add_entry(hass, unique_id="abc-123")
    ws = await _pooled_ws(core_client)

    with patch.object(ws, "send_command", wraps=ws.send_command) as send:
        read = await fetch_config_entry_unique_id(core_client, entry.entry_id)

    assert (WS_CONFIG_ENTRIES in _sent_commands(send)) is component
    assert (read == UNKNOWN_UNIQUE_ID) is not component


async def test_unknown_for_an_entry_home_assistant_does_not_have(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    _add_entry(hass, unique_id="abc-123")

    assert (
        await fetch_config_entry_unique_id(component_core_client, "no_such_entry")
        == UNKNOWN_UNIQUE_ID
    )


async def test_unknown_command_invalidates_the_cached_caps(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component downgraded since the probe must not be read on stale caps."""
    entry = _add_entry(hass, unique_id="abc-123")
    assert (
        await fetch_config_entry_unique_id(component_core_client, entry.entry_id)
    ).known
    # The downgraded component no longer registers the command; Core answers
    # unknown_command.
    monkeypatch.delitem(hass.data["websocket_api"], WS_CONFIG_ENTRIES)

    with patch.object(
        component_config_entries, "invalidate_caps", wraps=invalidate_caps
    ) as invalidate:
        read = await fetch_config_entry_unique_id(component_core_client, entry.entry_id)

    assert read == UNKNOWN_UNIQUE_ID
    invalidate.assert_called_once_with(component_core_client)


async def test_unknown_when_the_read_times_out(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A slow socket degrades the anchor; it does not fail the reconfigure."""
    entry = _add_entry(hass, unique_id="abc-123")
    assert await get_component_caps(component_core_client) is not None
    ws = await _pooled_ws(component_core_client)

    with (
        patch.object(
            ws, "send_command", side_effect=HomeAssistantCommandTimeout("slow")
        ),
        patch.object(
            component_config_entries, "invalidate_caps", wraps=invalidate_caps
        ) as invalidate,
    ):
        read = await fetch_config_entry_unique_id(component_core_client, entry.entry_id)

    assert read == UNKNOWN_UNIQUE_ID
    invalidate.assert_not_called()


async def test_unknown_when_the_socket_cannot_be_reached(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    entry = _add_entry(hass, unique_id="abc-123")

    with patch.object(
        component_config_entries,
        "get_websocket_client",
        AsyncMock(side_effect=HomeAssistantConnectionError("socket died")),
    ):
        read = await fetch_config_entry_unique_id(component_core_client, entry.entry_id)

    assert read == UNKNOWN_UNIQUE_ID


async def test_the_client_tls_override_reaches_the_pooled_client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """verify_ssl keys the client pool, so the read must use the probe's.

    A read that omits it gets a DIFFERENT pooled client, so a self-signed setup
    passes detection and then fails the read.
    """
    # The client turns verification off; the settings default keeps it on.
    assert component_core_client.verify_ssl is False
    assert get_global_settings().verify_ssl is True
    entry = _add_entry(hass, unique_id="u")

    read = await fetch_config_entry_unique_id(component_core_client, entry.entry_id)

    assert read == EntryUniqueId(known=True, value="u")
    url, token = component_core_client.base_url, component_core_client.token
    assert websocket_manager.pooled_client(url, token, verify_ssl=True) is None
    assert websocket_manager.pooled_client(url, token, verify_ssl=False) is not None


@pytest.mark.parametrize(
    ("secrets", "degraded"),
    [
        pytest.param("token: [unclosed\n", True, id="unreadable_secrets"),
        pytest.param(None, False, id="no_secrets"),
    ],
)
async def test_reader_carries_the_scrub_degraded_flag(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    secrets: str | None,
    degraded: bool,
) -> None:
    """The component flags options it could not scrub of ``!secret`` values."""
    if secrets is not None:
        path = Path(hass.config.path("secrets.yaml"))
        await hass.async_add_executor_job(path.write_text, secrets)
    entry = _add_entry(hass)

    read = await fetch_config_entries(component_core_client, entry_id=entry.entry_id)

    assert read is not None
    assert [row["entry_id"] for row in read.rows] == [entry.entry_id]
    assert read.secret_scrub_degraded is degraded


@pytest.mark.parametrize("filters", [True, False], ids=["capability", "older"])
async def test_reader_sends_the_type_only_to_a_component_that_filters_by_it(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    filters: bool,
) -> None:
    """An older component would ignore the type, so it is not sent there."""
    if not filters:
        monkeypatch.setattr(
            system,
            "CAPABILITIES",
            [cap for cap in system.CAPABILITIES if cap != "config_entries_type_filter"],
        )
    assert await async_setup_component(hass, "config", {})
    _add_entry(hass, domain="sun")
    ws = await _pooled_ws(component_core_client)

    with patch.object(ws, "send_command", wraps=ws.send_command) as send:
        read = await fetch_config_entries(
            component_core_client, domain="sun", integration_type="service"
        )

    (frame,) = [
        call.kwargs
        for call in send.await_args_list
        if call.args[0] == WS_CONFIG_ENTRIES
    ]
    assert ("type_filter" in frame) is filters
    assert frame["domain"] == "sun"
    assert read is not None
    assert read.type_filtered is filters
