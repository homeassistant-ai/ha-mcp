"""ha_get_integration lists an entry's own WebSocket commands; ha_set_integration sends one.

Every test runs on a real ``hass``: the entry is a ``MockConfigEntry``, the
integration's commands are registered with Core's WebSocket API, and the
component's ``integration_commands`` bridge (``component_core_client``)
describes them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, MockModule, mock_integration

from custom_components.ha_mcp_tools.const import DOMAIN as COMPONENT_DOMAIN
from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.tools import core_contract, integration_ws_commands
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._tool_errors import _error

_DESCRIPTION_WARNING = "Some Core validators cannot be described"


def _light_id(value: Any) -> str:
    """A validator Core's schema serializer cannot describe strictly."""
    return str(value)


async def _add_entry(hass: HomeAssistant, domain: str = "knx") -> MockConfigEntry:
    """An entry of ``domain`` that Core's config-entry API can read.

    KNX's own requirements are not installed with Core; Core loads a stand-in
    integration of that domain when the entry is enabled.
    """
    assert await async_setup_component(hass, "config", {})
    mock_integration(hass, MockModule("knx"))
    entry = MockConfigEntry(domain=domain)
    entry.add_to_hass(hass)
    return entry


def _register_knx_commands(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Register two ``knx/`` commands and one of another domain; the list
    collects every message ``knx/get_entity_config`` receives."""
    received: list[dict[str, Any]] = []

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "knx/get_entity_config",
            vol.Required("entity_id"): _light_id,
        }
    )
    @callback
    def get_entity_config(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        received.append(msg)
        connection.send_result(msg["id"], {"info": {"name": "Demo"}})

    @websocket_api.websocket_command(
        {vol.Required("type"): "knx/update_entity", vol.Required("data"): _light_id}
    )
    @websocket_api.async_response
    async def update_entity(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        raise HomeAssistantError("KNX bus is not connected")

    @websocket_api.websocket_command({vol.Required("type"): "knxx/other"})
    @callback
    def other(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        connection.send_result(msg["id"], None)

    for handler in (get_entity_config, update_entity, other):
        websocket_api.async_register_command(hass, handler)
    return received


def _bridge_spy() -> Any:
    """Spy on the bridge where the listing calls it; it still answers."""
    return patch.object(
        integration_ws_commands,
        "integration_commands",
        wraps=core_contract.integration_commands,
    )


def _sent_types(send: AsyncMock) -> list[str]:
    return [call.args[0]["type"] for call in send.await_args_list]


class TestListing:
    async def test_lists_only_the_entrys_domain_commands_and_lifts_their_warnings(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        entry = await _add_entry(hass)
        _register_knx_commands(hass)

        with _bridge_spy() as bridge:
            result = await IntegrationTools(component_core_client).ha_get_integration(
                entry_id=entry.entry_id, include_ws_commands=True
            )

        bridge.assert_awaited_once_with(component_core_client, "knx")
        assert [row["command"] for row in result["ws_commands"]] == [
            "knx/get_entity_config",
            "knx/update_entity",
        ]
        assert all("warnings" not in row for row in result["ws_commands"])
        # Both commands carry the same warning; the result lists it once.
        assert sum(_DESCRIPTION_WARNING in w for w in result["warnings"]) == 1

    async def test_without_the_component_the_list_is_absent_with_a_warning(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        entry = await _add_entry(hass)
        _register_knx_commands(hass)

        result = await IntegrationTools(core_client).ha_get_integration(
            entry_id=entry.entry_id, include_ws_commands=True
        )

        assert result["entry"]["domain"] == "knx"
        assert "ws_commands" not in result
        (warning,) = [w for w in result["warnings"] if "ws_commands" in w]
        assert warning.startswith("ws_commands not listed: ")
        assert "listing of an integration's own WebSocket commands" in warning
        assert "Core contract" not in warning

    async def test_a_lost_bridge_keeps_the_entry_read_and_logs_one_traceback(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        entry = await _add_entry(hass)
        _register_knx_commands(hass)
        tools = IntegrationTools(component_core_client)

        with (
            patch.object(
                core_contract,
                "get_websocket_client",
                AsyncMock(side_effect=HomeAssistantConnectionError("ws down")),
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = await tools.ha_get_integration(
                entry_id=entry.entry_id, include_ws_commands=True
            )

        assert result["entry"]["entry_id"] == entry.entry_id
        assert "ws_commands" not in result
        assert [w for w in result["warnings"] if "ws_commands" in w] == [
            "ws_commands not listed: Could not obtain the listing "
            "of an integration's own WebSocket commands: ws down"
        ]
        listing_logs = [
            record.exc_info is not None
            for record in caplog.records
            if record.name in (core_contract.__name__, integration_ws_commands.__name__)
        ]
        assert listing_logs == [True, False]

    @pytest.mark.parametrize(
        ("domain", "requested", "warning"),
        [
            ("knx", False, None),
            (COMPONENT_DOMAIN, True, "include_ws_commands was ignored"),
        ],
        ids=["not_requested", "component_entry"],
    )
    async def test_no_bridge_call_and_no_list(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        domain: str,
        requested: bool,
        warning: str | None,
    ) -> None:
        entry = await _add_entry(hass, domain)
        _register_knx_commands(hass)

        with _bridge_spy() as bridge:
            result = await IntegrationTools(component_core_client).ha_get_integration(
                entry_id=entry.entry_id, include_ws_commands=requested
            )

        bridge.assert_not_awaited()
        assert "ws_commands" not in result
        listing_warnings = [w for w in result.get("warnings", []) if "ws_command" in w]
        assert [w.startswith(warning) for w in listing_warnings] == (
            [True] if warning else []
        )

    async def test_list_mode_ignores_the_flag_with_a_warning(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _add_entry(hass)

        with _bridge_spy() as bridge:
            result = await IntegrationTools(core_client).ha_get_integration(
                include_ws_commands=True
            )

        bridge.assert_not_awaited()
        assert any("include_ws_commands" in w for w in result["warnings"])


class TestSend:
    async def test_sends_the_command_as_given_without_a_backup(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Core validates the call itself; the bridge is not asked first. A
        # ws_command changes the integration's own storage, not the entry a
        # snapshot holds, so none is taken; enabling the entry takes one.
        monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
        monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
        reset_global_settings()
        manager = bm.get_backup_manager(core_client, get_global_settings())
        entry = await _add_entry(hass)
        received = _register_knx_commands(hass)
        tools = IntegrationTools(core_client)

        with _bridge_spy() as bridge:
            result = await tools.ha_set_integration(
                entry_id=entry.entry_id,
                ws_command="knx/get_entity_config",
                ws_data={"entity_id": "light.x"},
            )

        bridge.assert_not_awaited()
        assert [{k: v for k, v in msg.items() if k != "id"} for msg in received] == [
            {"type": "knx/get_entity_config", "entity_id": "light.x"}
        ]
        assert result == {
            "success": True,
            "entry_id": entry.entry_id,
            "ws_command": "knx/get_entity_config",
            "result": {"info": {"name": "Demo"}},
        }
        assert manager.list_snapshots() == []

        await tools.ha_set_integration(entry_id=entry.entry_id, enabled=False)
        assert manager.list_snapshots() != []

    @pytest.mark.parametrize("ws_data", [{"type": "call_service"}, {"id": 1}])
    async def test_ws_data_cannot_set_the_message_envelope(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        ws_data: dict[str, Any],
    ) -> None:
        entry = await _add_entry(hass)
        _register_knx_commands(hass)

        with (
            patch.object(
                core_client,
                "send_websocket_message",
                wraps=core_client.send_websocket_message,
            ) as send,
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(core_client).ha_set_integration(
                entry_id=entry.entry_id,
                ws_command="knx/get_entity_config",
                ws_data=ws_data,
            )

        assert _error(exc_info)["code"] == "VALIDATION_FAILED"
        assert "reserved" in _error(exc_info)["message"]
        send.assert_not_awaited()

    @pytest.mark.parametrize(
        ("ws_command", "ws_data", "code", "fragment", "suggests_listing"),
        [
            (
                "knx/get_entity_config",
                {"entity_id": "light.x", "bogus": 1},
                "VALIDATION_INVALID_PARAMETER",
                "bogus",
                True,
            ),
            (
                "knx/update_entity",
                {"data": "x"},
                "SERVICE_CALL_FAILED",
                "KNX bus is not connected",
                False,
            ),
        ],
        ids=["invalid_format", "home_assistant_error"],
    )
    async def test_cores_error_comes_back_with_its_code(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        ws_command: str,
        ws_data: dict[str, Any],
        code: str,
        fragment: str,
        suggests_listing: bool,
    ) -> None:
        entry = await _add_entry(hass)
        received = _register_knx_commands(hass)

        with pytest.raises(ToolError) as exc_info:
            await IntegrationTools(core_client).ha_set_integration(
                entry_id=entry.entry_id, ws_command=ws_command, ws_data=ws_data
            )

        error = _error(exc_info)
        assert error["code"] == code
        assert fragment in error["message"]
        listed = "include_ws_commands" in str(error.get("suggestion"))
        assert listed is suggests_listing
        assert received == []

    @pytest.mark.parametrize(
        "unverified", [True, False], ids=["lost_after_sending", "never_sent"]
    )
    async def test_a_lost_answer_says_the_command_may_have_run(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, unverified: bool
    ) -> None:
        entry = await _add_entry(hass)
        _register_knx_commands(hass)
        if unverified:
            no_answer = patch.object(
                core_client,
                "send_websocket_message",
                side_effect=HomeAssistantConnectionError("closed while waiting"),
            )
        else:
            no_answer = patch.object(
                core_client,
                "send_websocket_message",
                side_effect=HomeAssistantCommandNotSent("WebSocket not authenticated"),
            )

        with no_answer, pytest.raises(ToolError) as exc_info:
            await IntegrationTools(core_client).ha_set_integration(
                entry_id=entry.entry_id,
                ws_command="knx/update_entity",
                ws_data={"data": "x"},
            )

        response = json.loads(str(exc_info.value))
        assert (response.get("status") == "applied_but_unverified") is unverified
        assert ("may have been applied" in response["error"]["message"]) is unverified

    @pytest.mark.parametrize(
        ("domain", "kwargs", "fragment"),
        [
            ("knx", {"ws_command": "config/core/update"}, "'knx/'"),
            # A shared prefix without the slash is another integration.
            ("knx", {"ws_command": "knxx/other"}, "'knx/'"),
            (COMPONENT_DOMAIN, {"ws_command": "ha_mcp_tools/info"}, "ha-mcp's own"),
            ("knx", {"ws_command": "knx/subscribe_telegrams"}, "one-shot"),
            # Snapshot deletion stays with ha_manage_backup.
            (
                "backup",
                {"ws_command": "backup/delete", "ws_data": {"backup_id": "abc"}},
                "ha_manage_backup",
            ),
            ("knx", {"ws_command": "knx/x", "config": {"a": 1}}, "takes no config"),
            ("knx", {"ws_command": "knx/x", "flow_id": "f1"}, "takes no flow_id"),
            ("knx", {"ws_command": "knx/x", "abort": True}, "takes no abort"),
            ("knx", {"ws_data": {"a": 1}}, "needs ws_command"),
            ("knx", {"ws_command": "knx/x", "entry_id": None}, "needs entry_id"),
        ],
    )
    async def test_refused_calls_send_nothing(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        domain: str,
        kwargs: dict[str, Any],
        fragment: str,
    ) -> None:
        entry = await _add_entry(hass, domain)
        _register_knx_commands(hass)

        with (
            patch.object(
                core_client,
                "send_websocket_message",
                wraps=core_client.send_websocket_message,
            ) as send,
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(core_client).ha_set_integration(
                **{"entry_id": entry.entry_id, **kwargs}
            )

        assert fragment in _error(exc_info)["message"]
        # Reading the entry's domain is the only message sent.
        assert set(_sent_types(send)) <= {"config_entries/get_single"}


@pytest.mark.parametrize(
    ("ask", "feature"),
    [
        (
            lambda client: core_contract.core_contract(client, "energy/save_prefs"),
            "Core contract discovery/validation",
        ),
        (
            lambda client: core_contract.integration_commands(client, "knx"),
            "the listing of an integration's own WebSocket commands",
        ),
    ],
    ids=["core_contract", "integration_commands"],
)
async def test_each_bridge_names_its_own_feature_without_the_component(
    core_client: HomeAssistantClient,
    ask: Callable[[HomeAssistantClient], Any],
    feature: str,
) -> None:
    result = await ask(core_client)

    assert result["status"] == "unavailable"
    assert f"does not expose {feature}." in result["reason"]
