"""The ``info`` fields ``ha_report_issue`` reads, from the real component.

``ha_report_issue`` runs on the server's own client against a real ``hass``
that answers the component's commands (``component_core_client``), so a name
or shape drift between the component's ``info`` and the report's component
version and server-entry line (#2584) fails here. The server entry is a real
config entry; its webhook registration is the component's own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceRegistry
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools import mcp_webhook
from custom_components.ha_mcp_tools.const import (
    COMPONENT_VERSION,
)
from custom_components.ha_mcp_tools.websocket_api import system
from custom_components.ha_mcp_tools.websocket_api.constants import WS_INFO
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.tools.component_api import get_component_caps
from ha_mcp.tools.tools_bug_report import BugReportTools

from ._component_core import ws_result
from ._core_client import free_port
from ._core_setup import set_up_tools_entry
from ._server_entry import add_server_entry, replace_server, set_up


def _server(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    """The loaded server entry, with ``options``."""
    entry = add_server_entry(hass, options=options)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


@pytest.fixture
async def reporting(hass: HomeAssistant) -> None:
    """``config`` answers the report's ``config_entries/get``."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
async def webhook(hass: HomeAssistant) -> AsyncIterator[None]:
    """Remove the webhook registration a test makes with ``_server``."""
    assert await async_setup_component(hass, "webhook", {})
    try:
        yield
    finally:
        await mcp_webhook.async_unregister_webhook(hass)


async def _registered(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Register the server's webhook the way bring-up does once it has started."""
    await mcp_webhook.async_register_webhook(
        hass,
        entry,
        port=free_port(),
        secret_path="/private_x",
        auth_mode="none",
        register_endpoint=False,
    )


async def _report(client: HomeAssistantClient) -> Any:
    result = await BugReportTools(client).ha_report_issue(report_type="runtime_bug")
    return result["diagnostic_info"]


def _call_service_spy(client: HomeAssistantClient) -> Any:
    return patch.object(client, "call_service", wraps=client.call_service)


@pytest.mark.usefixtures("reporting")
async def test_an_updated_component_version_reaches_the_next_report(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component update keeps a long-lived server's client, and with it the
    caps snapshot; a later report must show the version now running."""
    _server(hass)
    monkeypatch.setattr(system, "COMPONENT_VERSION", "2.1.0")
    first = await _report(component_core_client)
    monkeypatch.setattr(system, "COMPONENT_VERSION", "2.2.0")

    second = await _report(component_core_client)

    assert (first["component_version"], second["component_version"]) == (
        "2.1.0",
        "2.2.0",
    )


@pytest.mark.usefixtures("reporting", "webhook")
async def test_a_failed_fresh_read_keeps_the_snapshot_version_and_the_line(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot still shows the component is installed, and the entry
    status came from ``config_entries/get``; a failing ``info`` takes neither
    out of the report."""
    await _registered(hass, _server(hass, webhook_auth="ha_auth"))
    monkeypatch.setattr(system, "COMPONENT_VERSION", "2.1.0")
    await get_component_caps(component_core_client)
    monkeypatch.setattr(system, "COMPONENT_VERSION", "2.2.0")

    with (
        patch.object(
            ServiceRegistry, "has_service", side_effect=RuntimeError("core drift")
        ),
        _call_service_spy(component_core_client) as call_service,
    ):
        info = await _report(component_core_client)

    assert info["component_version"] == "2.1.0"
    assert info["server_entry_status"] == "added (loaded)"
    call_service.assert_not_called()


@pytest.mark.usefixtures("reporting")
async def test_a_downgraded_component_reports_its_running_version(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component downgraded below ``info`` answers ``unknown_command``: the
    stale snapshot is dropped and the version comes from the bootstrap
    service, not from the component that was running before."""
    _server(hass)
    await set_up_tools_entry(hass)
    monkeypatch.setattr(system, "COMPONENT_VERSION", "2.1.0")
    await get_component_caps(component_core_client)
    del hass.data["websocket_api"][WS_INFO]

    info = await _report(component_core_client)

    assert info["component_version"] == COMPONENT_VERSION != "2.1.0"


@pytest.mark.usefixtures("reporting")
async def test_a_component_reporting_no_version_reads_as_not_detected(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _server(hass)
    monkeypatch.setattr(system, "COMPONENT_VERSION", "")

    with _call_service_spy(component_core_client) as call_service:
        info = await _report(component_core_client)

    assert info["component_version"] is None
    call_service.assert_not_called()


@pytest.mark.usefixtures("reporting", "webhook")
@pytest.mark.parametrize(
    ("options", "line"),
    [
        ({"webhook_auth": "ha_auth"}, "added (loaded), webhook auth: ha_auth"),
        # Bring-up's default when the option was never set.
        ({}, "added (loaded), webhook auth: none"),
        # Local-only mode keeps the auth option, but no webhook exists; "none"
        # would read as an unauthenticated public webhook.
        (
            {"webhook_auth": "none", "enable_webhook": False},
            "added (loaded), webhook auth: disabled",
        ),
    ],
    ids=["configured", "unset", "disabled"],
)
async def test_the_webhook_mode_reaches_the_report(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    options: dict[str, Any],
    line: str,
) -> None:
    await set_up_tools_entry(hass)
    await _registered(hass, _server(hass, **options))

    info = await _report(component_core_client)

    assert info["server_entry_status"] == line


@pytest.mark.usefixtures("reporting", "webhook")
async def test_a_changed_mode_reaches_the_next_report(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The mode is an entry option the user can change while a long-lived
    server keeps its client; a later report must not repeat the first answer."""
    server = _server(hass, webhook_auth="ha_auth")
    await _registered(hass, server)
    await _report(component_core_client)
    hass.config_entries.async_update_entry(server, options={"webhook_auth": "legacy"})

    info = await _report(component_core_client)

    assert info["server_entry_status"] == "added (loaded), webhook auth: legacy"


@pytest.mark.usefixtures("reporting")
async def test_an_install_without_the_component_is_asked_for_info_once(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The caps probe found no ``info``; the report does not send it again."""
    _server(hass)
    ws = await get_websocket_client(
        url=core_client.base_url, token=core_client.token, verify_ssl=False
    )

    with patch.object(ws, "send_command", wraps=ws.send_command) as send:
        info = await _report(core_client)

    sent = [call.args[0] for call in send.call_args_list if call.args]
    assert sent.count(WS_INFO) == 1
    assert info["server_entry_status"] == "added (loaded)"


@pytest.mark.usefixtures("webhook")
async def test_a_registered_webhook_reports_its_mode(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _registered(hass, _server(hass, webhook_auth="none"))

    assert (await ws_result(component_ws, WS_INFO))["webhook_auth"] == "none"


async def test_without_a_webhook_registration_no_mode_is_reported(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bring-up registers the webhook after the server has started, and a
    failed bring-up leaves the entry loaded; the configured ``none`` would then
    read as an open webhook."""
    replace_server(monkeypatch)
    entry = add_server_entry(hass, options={"webhook_auth": "none"})
    assert await set_up(hass, entry)

    assert (await ws_result(component_ws, WS_INFO))["webhook_auth"] is None


async def test_without_a_server_entry_no_mode_is_reported(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Only the tools entry is set up (an external server): there is no
    in-process webhook."""
    await set_up_tools_entry(hass)

    assert (await ws_result(component_ws, WS_INFO))["webhook_auth"] is None
