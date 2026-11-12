"""The Thread/OTBR handler behind ``ha_manage_radio`` on real Home Assistant Core.

Core's ``thread`` and ``otbr`` integrations need ``python-otbr-api``, which is
not installed with Core, so they cannot be imported here. Their WebSocket
commands are registered as those integrations register them, with Core's own
``websocket_command`` decorator and the request schemas Core declares, and
answer with the reply shape Core sends. Core validates every request against
those schemas and writes every reply and refusal; config entries come from
Core's config entries API.
"""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_radio import RadioTools

from ._client_spies import sent_messages

EXTENDED_ADDRESS = "f00dcafe"
# ``otbr/info``'s reply: one border router keyed by its extended address.
BORDER_ROUTERS = {
    EXTENDED_ADDRESS: {
        "active_dataset_tlvs": "0e08abcd",
        "border_agent_id": "ba1",
        "channel": 15,
        "extended_address": EXTENDED_ADDRESS,
        "extended_pan_id": "1111222233334444",
        "url": "http://core-silabs-multiprotocol:8081",
    }
}
DATASETS = {
    "datasets": [
        {
            "channel": 15,
            "created": "2026-01-01T00:00:00+00:00",
            "dataset_id": "d1",
            "extended_pan_id": "1111222233334444",
            "network_name": "OpenThread",
            "pan_id": "1234",
            "preferred": True,
            "preferred_border_agent_id": None,
            "preferred_extended_address": None,
            "source": "Google",
        }
    ]
}
# Each command's request fields as Core's integration declares them.
SCHEMAS: dict[str, dict[Any, Any]] = {
    "otbr/info": {},
    "otbr/create_network": {vol.Required("extended_address"): str},
    "otbr/set_network": {
        vol.Required("extended_address"): str,
        vol.Required("dataset_id"): str,
    },
    "otbr/set_channel": {
        vol.Required("extended_address"): str,
        vol.Required("channel"): int,
    },
    "thread/add_dataset_tlv": {
        vol.Required("source"): str,
        vol.Required("tlv"): str,
    },
    "thread/set_preferred_dataset": {vol.Required("dataset_id"): str},
    "thread/delete_dataset": {vol.Required("dataset_id"): str},
    "thread/list_datasets": {},
    "thread/discover_routers": {},
}
REPLIES: dict[str, Any] = {
    "otbr/info": BORDER_ROUTERS,
    "thread/list_datasets": DATASETS,
}


def _register(
    hass: HomeAssistant, command: str, received: list[dict[str, Any]]
) -> None:
    """Register ``command`` as its integration does; record what Core accepts."""

    @websocket_api.websocket_command(
        {vol.Required("type"): command, **SCHEMAS[command]}
    )
    @callback
    def handler(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        received.append(msg)
        connection.send_result(msg["id"], REPLIES.get(command))

    websocket_api.async_register_command(hass, handler)


async def _thread_and_otbr(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Core with one OTBR config entry and the thread/otbr commands loaded."""
    assert await async_setup_component(hass, "config", {})
    MockConfigEntry(domain="otbr", entry_id="otbr1", title="OTBR").add_to_hass(hass)
    received: list[dict[str, Any]] = []
    for command in SCHEMAS:
        _register(hass, command, received)
    return received


def _commands(received: list[dict[str, Any]], command: str) -> list[dict[str, Any]]:
    return [msg for msg in received if msg["type"] == command]


@pytest.fixture
def radio(core_client: HomeAssistantClient) -> RadioTools:
    return RadioTools(core_client)


async def test_network_status_reports_the_otbr_entry_and_border_routers(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    await _thread_and_otbr(hass)

    out = await radio.ha_manage_radio(radio="thread", action="network_status")

    assert out["success"] is True
    assert out["radio"] == "thread"
    assert out["config_entry_id"] == "otbr1"
    assert out["border_routers"] == BORDER_ROUTERS


async def test_network_status_without_otbr_degrades_to_unavailable(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    assert await async_setup_component(hass, "config", {})

    out = await radio.ha_manage_radio(radio="thread", action="network_status")

    assert out["success"] is True
    assert out["available"] is False
    assert out["warnings"]


async def test_list_datasets_returns_cores_datasets(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    await _thread_and_otbr(hass)

    out = await radio.ha_manage_radio(radio="thread", action="list_datasets")

    assert out["datasets"] == DATASETS


@pytest.mark.parametrize(
    ("action", "params", "missing"),
    [
        ("set_preferred_dataset", {}, "dataset_id"),
        ("delete_dataset", {}, "dataset_id"),
        ("add_dataset", {"source": "Google"}, "tlv"),
        ("set_channel", {}, "channel"),
    ],
)
async def test_missing_required_param_is_refused_before_core_is_asked(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    radio: RadioTools,
    action: str,
    params: dict[str, Any],
    missing: str,
) -> None:
    await _thread_and_otbr(hass)

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="thread", action=action, params=params, confirm=True
        )

    assert f"requires: {missing}" in str(exc.value)
    sent.assert_not_called()


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("delete_dataset", {"dataset_id": "d1"}),
        ("set_network", {"dataset_id": "d1"}),
        ("create_network", {}),
        ("set_channel", {"channel": 20}),
    ],
)
async def test_destructive_action_without_confirm_sends_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    radio: RadioTools,
    action: str,
    params: dict[str, Any],
) -> None:
    await _thread_and_otbr(hass)

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(radio="thread", action=action, params=params)

    assert "confirm=True" in str(exc.value)
    sent.assert_not_called()


@pytest.mark.parametrize("action", ["set_preferred_dataset", "delete_dataset"])
async def test_dataset_write_reaches_core_with_the_dataset_id(
    hass: HomeAssistant, radio: RadioTools, action: str
) -> None:
    """The positive side of the required-param and confirm gates."""
    received = await _thread_and_otbr(hass)

    out = await radio.ha_manage_radio(
        radio="thread", action=action, params={"dataset_id": "d1"}, confirm=True
    )

    assert out["success"] is True
    [sent] = _commands(received, f"thread/{action}")
    assert sent["dataset_id"] == "d1"


async def test_discover_routers_starts_the_scan(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    received = await _thread_and_otbr(hass)

    out = await radio.ha_manage_radio(radio="thread", action="discover_routers")

    assert out["success"] is True
    assert out["long_running"] is True
    assert _commands(received, "thread/discover_routers")


async def test_add_dataset_sends_source_and_tlv(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    received = await _thread_and_otbr(hass)

    out = await radio.ha_manage_radio(
        radio="thread",
        action="add_dataset",
        params={"source": "Google", "tlv": "0e08abcd"},
    )

    assert out["success"] is True
    [sent] = _commands(received, "thread/add_dataset_tlv")
    assert sent["source"] == "Google"
    assert sent["tlv"] == "0e08abcd"


async def test_set_network_targets_the_only_border_router(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    received = await _thread_and_otbr(hass)

    out = await radio.ha_manage_radio(
        radio="thread",
        action="set_network",
        params={"dataset_id": "d1"},
        confirm=True,
    )

    assert out["extended_address"] == EXTENDED_ADDRESS
    [sent] = _commands(received, "otbr/set_network")
    assert sent["extended_address"] == EXTENDED_ADDRESS
    assert sent["dataset_id"] == "d1"


async def test_set_network_without_otbr_raises(
    hass: HomeAssistant, core_client: HomeAssistantClient, radio: RadioTools
) -> None:
    # set_network is a write action: an absent OTBR must raise (not degrade
    # to a no-op success the way the read-only network_status does). Without
    # the integration Core does not know otbr/info.
    assert await async_setup_component(hass, "config", {})

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="thread",
            action="set_network",
            params={"dataset_id": "d1"},
            confirm=True,
        )

    assert "otbr" in str(exc.value).lower()
    assert [call.args[0]["type"] for call in sent.await_args_list] == ["otbr/info"]


async def test_create_network_with_explicit_address_skips_the_lookup(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    received = await _thread_and_otbr(hass)

    out = await radio.ha_manage_radio(
        radio="thread",
        action="create_network",
        params={"extended_address": "abcd1234"},
        confirm=True,
    )

    assert out["extended_address"] == "abcd1234"
    [sent] = _commands(received, "otbr/create_network")
    assert sent["extended_address"] == "abcd1234"
    assert not _commands(received, "otbr/info")


async def test_set_channel_reaches_the_border_router(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    received = await _thread_and_otbr(hass)

    out = await radio.ha_manage_radio(
        radio="thread",
        action="set_channel",
        params={"channel": 20},
        confirm=True,
    )

    assert out["success"] is True
    [sent] = _commands(received, "otbr/set_channel")
    assert sent["channel"] == 20
    assert sent["extended_address"] == EXTENDED_ADDRESS


async def test_unknown_action_lists_supported(
    core_client: HomeAssistantClient, radio: RadioTools
) -> None:
    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(radio="thread", action="nope")

    assert "network_status" in str(exc.value)
    sent.assert_not_called()
