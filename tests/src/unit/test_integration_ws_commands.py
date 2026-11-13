"""ha_get_integration lists an entry's own WebSocket commands; ha_set_integration sends one.

Every test runs on a real ``hass`` with Core's real ``knx`` integration, set up
from its config entry as Core's own KNX tests set it up (``_knx_core``): only
the KNX bus below xknx is replaced, so every ``knx/*`` command, its schema and
its reply are Core's. The component's ``integration_commands`` bridge
(``component_core_client``) describes them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

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

from ._knx_core import set_up_knx
from ._tool_errors import error_of

_DESCRIPTION_WARNING = "Some Core validators cannot be described"
# A KNX switch as the KNX panel creates one: its name and the group address it
# writes to.
_SWITCH = {"entity": {"name": "Hall"}, "knx": {"ga_switch": {"write": "1/2/3"}}}


async def _add_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, domain: str = "knx"
) -> MockConfigEntry:
    """An entry of ``domain`` that Core's config-entry API can read.

    A ``knx`` entry is Core's KNX integration, set up; another domain's entry
    is only added.
    """
    assert await async_setup_component(hass, "config", {})
    if domain == "knx":
        return await set_up_knx(hass, monkeypatch)
    entry = MockConfigEntry(domain=domain)
    entry.add_to_hass(hass)
    return entry


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
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = await _add_entry(hass, monkeypatch)

        with _bridge_spy() as bridge:
            result = await IntegrationTools(component_core_client).ha_get_integration(
                entry_id=entry.entry_id, include_ws_commands=True
            )

        bridge.assert_awaited_once_with(component_core_client, "knx")
        rows = result["ws_commands"]
        commands = [row["command"] for row in rows]
        # Core's config, config_entries and other domains' commands are
        # registered too; only KNX's own are listed.
        assert {"knx/get_entity_config", "knx/update_entity"} <= set(commands)
        assert all(command.startswith("knx/") for command in commands)
        assert all("warnings" not in row for row in rows)
        # A KNX command Core's serializer cannot fully describe
        # (knx/query_telegrams) lifts its warning to the result.
        assert not all(row["description_complete"] for row in rows)
        assert sum(_DESCRIPTION_WARNING in w for w in result["warnings"]) == 1

    async def test_a_warning_several_commands_share_is_listed_once(
        self,
        recorder_mock: Recorder,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core's recorder registers several commands its serializer cannot
        fully describe; the warning they share is listed once."""
        entry = await _add_entry(hass, monkeypatch, domain="recorder")

        result = await IntegrationTools(component_core_client).ha_get_integration(
            entry_id=entry.entry_id, include_ws_commands=True
        )

        rows = result["ws_commands"]
        assert sum(not row["description_complete"] for row in rows) > 1
        assert sum(_DESCRIPTION_WARNING in w for w in result["warnings"]) == 1

    async def test_without_the_component_the_list_is_absent_with_a_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = await _add_entry(hass, monkeypatch)

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
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = await _add_entry(hass, monkeypatch)
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
        monkeypatch: pytest.MonkeyPatch,
        domain: str,
        requested: bool,
        warning: str | None,
    ) -> None:
        entry = await _add_entry(hass, monkeypatch, domain)

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
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _add_entry(hass, monkeypatch)

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
        entity_registry: er.EntityRegistry,
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
        entry = await _add_entry(hass, monkeypatch)
        tools = IntegrationTools(core_client)

        with _bridge_spy() as bridge:
            result = await tools.ha_set_integration(
                entry_id=entry.entry_id,
                ws_command="knx/create_entity",
                ws_data={"platform": "switch", "data": _SWITCH},
            )

        bridge.assert_not_awaited()
        assert result["success"] is True
        assert result["ws_command"] == "knx/create_entity"
        # KNX created the switch the data describes and answered with its id.
        created = entity_registry.async_get(result["result"]["entity_id"])
        assert created is not None
        assert (created.config_entry_id, created.domain) == (entry.entry_id, "switch")
        assert hass.states.get(created.entity_id) is not None
        assert manager.list_snapshots() == []

        await tools.ha_set_integration(entry_id=entry.entry_id, enabled=False)
        assert manager.list_snapshots() != []

    @pytest.mark.parametrize("ws_data", [{"type": "call_service"}, {"id": 1}])
    async def test_ws_data_cannot_set_the_message_envelope(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        ws_data: dict[str, Any],
    ) -> None:
        entry = await _add_entry(hass, monkeypatch)

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

        assert error_of(exc_info)["code"] == "VALIDATION_FAILED"
        assert "reserved" in error_of(exc_info)["message"]
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
            # KNX's handler refuses a switch it does not hold.
            (
                "knx/update_entity",
                {"platform": "switch", "entity_id": "switch.hall", "data": _SWITCH},
                "SERVICE_CALL_FAILED",
                "Entity not found: switch.hall",
                False,
            ),
        ],
        ids=["invalid_format", "home_assistant_error"],
    )
    async def test_cores_error_comes_back_with_its_code(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        ws_command: str,
        ws_data: dict[str, Any],
        code: str,
        fragment: str,
        suggests_listing: bool,
    ) -> None:
        entry = await _add_entry(hass, monkeypatch)

        with pytest.raises(ToolError) as exc_info:
            await IntegrationTools(core_client).ha_set_integration(
                entry_id=entry.entry_id, ws_command=ws_command, ws_data=ws_data
            )

        error = error_of(exc_info)
        assert error["code"] == code
        assert fragment in error["message"]
        listed = "include_ws_commands" in str(error.get("suggestion"))
        assert listed is suggests_listing

    @pytest.mark.parametrize(
        "unverified", [True, False], ids=["lost_after_sending", "never_sent"]
    )
    async def test_a_lost_answer_says_the_command_may_have_run(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        unverified: bool,
    ) -> None:
        entry = await _add_entry(hass, monkeypatch)
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
                ws_command="knx/create_entity",
                ws_data={"platform": "switch", "data": _SWITCH},
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
            (
                "knx",
                {"ws_command": "knx/get_base_data", "config": {"a": 1}},
                "takes no config",
            ),
            (
                "knx",
                {"ws_command": "knx/get_base_data", "flow_id": "f1"},
                "takes no flow_id",
            ),
            (
                "knx",
                {"ws_command": "knx/get_base_data", "abort": True},
                "takes no abort",
            ),
            ("knx", {"ws_data": {"a": 1}}, "needs ws_command"),
            (
                "knx",
                {"ws_command": "knx/get_base_data", "entry_id": None},
                "needs entry_id",
            ),
        ],
    )
    async def test_refused_calls_send_nothing(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        domain: str,
        kwargs: dict[str, Any],
        fragment: str,
    ) -> None:
        entry = await _add_entry(hass, monkeypatch, domain)

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

        assert fragment in error_of(exc_info)["message"]
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
