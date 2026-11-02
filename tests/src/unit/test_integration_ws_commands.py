"""ha_get_integration lists an entry's own WebSocket commands; ha_set_integration sends one."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import integration_ws_commands as module
from ha_mcp.tools.component_api import ComponentCaps
from ha_mcp.tools.tools_integrations import IntegrationTools

from .test_component_ws_search import _REAL_VOL, FakeHass

_VALID = {"status": "validated", "valid": True, "errors": []}
_NOT_EXPOSED = (
    "The installed component does not expose the listing and validation of an "
    "integration's own WebSocket commands."
)
_COMMAND_LIST = {
    "status": "available",
    "domain": "knx",
    "commands": [
        {
            "command": "knx/get_entity_config",
            "schema": {"type": "object"},
            "description_complete": False,
            "warnings": ["Some Core validators cannot be described."],
        },
        {
            "command": "knx/update_entity",
            "schema": {"type": "object"},
            "description_complete": False,
            "warnings": ["Some Core validators cannot be described."],
        },
    ],
}


def _client(domain: str = "knx") -> MagicMock:
    client = MagicMock(get_config_entry=AsyncMock(return_value={"domain": domain}))
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"info": {"name": "Demo"}}}
    )
    return client


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))["error"]


async def _get(client: MagicMock, domain: str | None = "knx", **kwargs: Any) -> Any:
    single = {"success": True, "entry_id": "e1", "entry": {"domain": domain}}
    with patch.object(
        IntegrationTools, "_get_single_entry", new=AsyncMock(return_value=single)
    ):
        return await IntegrationTools(client).ha_get_integration(**kwargs)


def _bridge(answer: dict[str, Any] | Exception) -> Any:
    """The real bridge, talking to a component that answers ``answer``."""
    from contextlib import ExitStack

    caps = ComponentCaps(1, "test", frozenset({"integration_commands"}), {})
    ws = Mock(
        send_command=AsyncMock(
            side_effect=answer if isinstance(answer, Exception) else None,
            return_value={"result": answer},
        )
    )
    stack = ExitStack()
    for name, value in (("get_component_caps", caps), ("get_websocket_client", ws)):
        stack.enter_context(
            patch(f"ha_mcp.tools.core_contract.{name}", AsyncMock(return_value=value))
        )
    return stack, ws


class TestListing:
    async def test_lists_commands_and_lifts_their_warnings(self) -> None:
        listing = json.loads(json.dumps(_COMMAND_LIST))
        bridge = AsyncMock(return_value=listing)
        with patch.object(module, "integration_commands", bridge):
            result = await _get(_client(), entry_id="e1", include_ws_commands=True)
        assert bridge.await_args.args[1:] == ("knx",)
        assert [row["command"] for row in result["ws_commands"]] == [
            "knx/get_entity_config",
            "knx/update_entity",
        ]
        assert all("warnings" not in row for row in result["ws_commands"])
        assert result["warnings"] == ["Some Core validators cannot be described."]

    async def test_without_the_capability_the_list_is_absent_with_a_warning(
        self,
    ) -> None:
        caps = ComponentCaps(1, "test", frozenset({"core_contract"}), {})
        with patch(
            "ha_mcp.tools.core_contract.get_component_caps",
            AsyncMock(return_value=caps),
        ):
            result = await _get(_client(), entry_id="e1", include_ws_commands=True)
        assert "ws_commands" not in result
        (warning,) = result["warnings"]
        assert warning.startswith(f"ws_commands not listed: {_NOT_EXPOSED}")
        assert "Core contract" not in warning

    async def test_a_failed_listing_keeps_the_entry_read_with_a_warning(
        self,
    ) -> None:
        with patch(
            "ha_mcp.tools.core_contract.get_component_caps",
            AsyncMock(side_effect=RuntimeError("ws down")),
        ):
            result = await _get(_client(), entry_id="e1", include_ws_commands=True)
        assert result["entry"] == {"domain": "knx"}
        assert "ws_commands" not in result
        assert result["warnings"] == [
            "ws_commands not listed: Could not obtain the listing and validation "
            "of an integration's own WebSocket commands: ws down"
        ]

    async def test_a_malformed_listing_names_the_bridge_and_logs_one_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stack, _ws = _bridge({"status": "weird"})
        with stack, caplog.at_level("WARNING"):
            result = await _get(_client(), entry_id="e1", include_ws_commands=True)
        assert result["warnings"] == [
            "ws_commands not listed: Could not obtain the listing and validation "
            "of an integration's own WebSocket commands: malformed "
            "ha_mcp_tools/integration_commands response"
        ]
        assert [r.exc_info is not None for r in caplog.records] == [True, False]

    async def test_not_requested_means_no_bridge_call(self) -> None:
        bridge = AsyncMock()
        with patch.object(module, "integration_commands", bridge):
            result = await _get(_client(), entry_id="e1")
        bridge.assert_not_awaited()
        assert "ws_commands" not in result

    async def test_component_entry_lists_nothing(self) -> None:
        bridge = AsyncMock()
        with patch.object(module, "integration_commands", bridge):
            result = await _get(
                _client(), "ha_mcp_tools", entry_id="e1", include_ws_commands=True
            )
        bridge.assert_not_awaited()
        assert "ws_commands" not in result
        assert "include_ws_commands was ignored" in result["warnings"][0]

    async def test_an_entry_without_a_domain_lists_nothing(self) -> None:
        bridge = AsyncMock()
        with patch.object(module, "integration_commands", bridge):
            result = await _get(
                _client(), None, entry_id="e1", include_ws_commands=True
            )
        bridge.assert_not_awaited()
        assert "ws_commands" not in result
        assert result["warnings"] == [
            "ws_commands not listed: the entry carries no domain"
        ]

    async def test_list_mode_ignores_the_flag_with_a_warning(self) -> None:
        tools = IntegrationTools(_client())
        with patch.object(
            IntegrationTools,
            "_list_entries",
            new=AsyncMock(return_value={"success": True, "entries": []}),
        ):
            result = await tools.ha_get_integration(include_ws_commands=True)
        assert any("include_ws_commands" in w for w in result["warnings"])


class TestSend:
    async def test_sends_a_validated_command_without_a_backup(self) -> None:
        client = _client()
        bridge = AsyncMock(return_value=_VALID)
        with (
            patch.object(module, "integration_commands", bridge),
            patch(
                "ha_mcp.tools.auto_backup.get_global_settings",
                return_value=SimpleNamespace(enable_auto_backup=True),
            ),
            patch(
                "ha_mcp.tools.auto_backup._capture_pre_write_snapshot",
                new=AsyncMock(),
            ) as capture,
        ):
            result = await IntegrationTools(client).ha_set_integration(
                entry_id="e1",
                ws_command="knx/get_entity_config",
                ws_data={"entity_id": "light.x"},
            )
        bridge.assert_awaited_once_with(
            client, "knx", "knx/get_entity_config", {"entity_id": "light.x"}
        )
        client.send_websocket_message.assert_awaited_once_with(
            {"type": "knx/get_entity_config", "entity_id": "light.x"}
        )
        capture.assert_not_awaited()
        assert result["result"] == {"info": {"name": "Demo"}}
        assert "warnings" not in result

    @pytest.mark.parametrize(
        "command", ["config/core/update", "knxx/other", "knx", "auth/x"]
    )
    async def test_only_the_entrys_own_prefix_is_sent(self, command: str) -> None:
        client = _client()
        bridge = AsyncMock(return_value=_VALID)
        with (
            patch.object(module, "integration_commands", bridge),
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(client).ha_set_integration(
                entry_id="e1", ws_command=command
            )
        assert "'knx/'" in _error(exc_info)["message"]
        bridge.assert_not_awaited()
        client.send_websocket_message.assert_not_awaited()

    async def test_a_schema_rejection_sends_nothing(self) -> None:
        client = _client()
        errors = [{"path": ["entity_id"], "message": "required key not provided"}]
        bridge = AsyncMock(
            return_value={"status": "validated", "valid": False, "errors": errors}
        )
        with (
            patch.object(module, "integration_commands", bridge),
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(client).ha_set_integration(
                entry_id="e1", ws_command="knx/delete_entity"
            )
        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "VALIDATION_FAILED"
        assert payload["errors"] == errors
        client.send_websocket_message.assert_not_awaited()

    async def test_component_entries_are_refused(self) -> None:
        client = _client("ha_mcp_tools")
        bridge = AsyncMock(return_value=_VALID)
        with (
            patch.object(module, "integration_commands", bridge),
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(client).ha_set_integration(
                entry_id="e1", ws_command="ha_mcp_tools/info"
            )
        assert "ha-mcp's own" in _error(exc_info)["message"]
        bridge.assert_not_awaited()
        client.send_websocket_message.assert_not_awaited()

    async def test_without_the_capability_core_alone_validates(self) -> None:
        client = _client()
        caps = ComponentCaps(1, "test", frozenset({"core_contract"}), {})
        with patch(
            "ha_mcp.tools.core_contract.get_component_caps",
            AsyncMock(return_value=caps),
        ):
            result = await IntegrationTools(client).ha_set_integration(
                entry_id="e1", ws_command="knx/get_knx_project"
            )
        client.send_websocket_message.assert_awaited_once_with(
            {"type": "knx/get_knx_project"}
        )
        assert result["warnings"] == [
            f"Not validated before sending: {_NOT_EXPOSED} "
            "Core still validates actual requests."
        ]

    async def test_an_answer_that_is_no_validation_sends_with_a_warning(
        self,
    ) -> None:
        client = _client()
        bridge = AsyncMock(return_value={"status": "available", "commands": []})
        with patch.object(module, "integration_commands", bridge):
            result = await IntegrationTools(client).ha_set_integration(
                entry_id="e1", ws_command="knx/get_knx_project"
            )
        client.send_websocket_message.assert_awaited_once()
        assert result["warnings"] == [
            "Not validated before sending: the component answered 'available', "
            "not a validation"
        ]

    async def test_a_validation_without_a_verdict_sends_nothing(self) -> None:
        client = _client()
        bridge = AsyncMock(return_value={"status": "validated"})
        with (
            patch.object(module, "integration_commands", bridge),
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(client).ha_set_integration(
                entry_id="e1", ws_command="knx/delete_entity"
            )
        assert _error(exc_info)["code"] == "VALIDATION_FAILED"
        client.send_websocket_message.assert_not_awaited()

    async def test_a_failed_bridge_sends_nothing(self) -> None:
        client = _client()
        with (
            patch(
                "ha_mcp.tools.core_contract.get_component_caps",
                AsyncMock(side_effect=RuntimeError("ws down")),
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(client).ha_set_integration(
                entry_id="e1", ws_command="knx/delete_entity"
            )
        assert _error(exc_info)["code"] == "SERVICE_CALL_FAILED"
        client.send_websocket_message.assert_not_awaited()

    async def test_a_core_error_is_a_service_failure(self) -> None:
        client = _client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": {"code": "home_assistant_error", "message": "No entity"},
            }
        )
        with (
            patch.object(
                module, "integration_commands", AsyncMock(return_value=_VALID)
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(client).ha_set_integration(
                entry_id="e1", ws_command="knx/delete_entity"
            )
        error = _error(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert error["message"] == "No entity"

    async def test_streaming_commands_are_refused(self) -> None:
        client = _client()
        with pytest.raises(ToolError) as exc_info:
            await IntegrationTools(client).ha_set_integration(
                entry_id="e1", ws_command="knx/subscribe_telegrams"
            )
        assert "one-shot" in _error(exc_info)["message"]
        client.send_websocket_message.assert_not_awaited()

    async def test_snapshot_deletion_stays_with_ha_manage_backup(self) -> None:
        client = _client("backup")
        bridge = AsyncMock(return_value=_VALID)
        with (
            patch.object(module, "integration_commands", bridge),
            pytest.raises(ToolError) as exc_info,
        ):
            await IntegrationTools(client).ha_set_integration(
                entry_id="e1",
                ws_command="backup/delete",
                ws_data={"backup_id": "abc"},
            )
        assert "ha_manage_backup" in _error(exc_info)["message"]
        bridge.assert_not_awaited()
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            ({"ws_command": "knx/x", "config": {"a": 1}}, "takes no config"),
            ({"ws_command": "knx/x", "describe": True}, "takes no describe"),
            ({"ws_data": {"a": 1}}, "needs ws_command"),
            ({"ws_command": "knx/x", "entry_id": None}, "needs entry_id"),
        ],
    )
    async def test_other_modes_are_refused(
        self, kwargs: dict[str, Any], fragment: str
    ) -> None:
        client = _client()
        with pytest.raises(ToolError) as exc_info:
            await IntegrationTools(client).ha_set_integration(
                **{"entry_id": "e1", **kwargs}
            )
        assert fragment in _error(exc_info)["message"]
        client.send_websocket_message.assert_not_awaited()


@pytest.mark.parametrize(
    ("bridge", "feature"),
    [
        ("core_contract", "Core contract discovery/validation"),
        (
            "integration_commands",
            "the listing and validation of an integration's own WebSocket commands",
        ),
    ],
)
async def test_each_bridge_names_its_own_feature_when_absent(
    bridge: str, feature: str
) -> None:
    from ha_mcp.tools import core_contract

    caps = ComponentCaps(1, "test", frozenset(), {})
    with patch.object(
        core_contract, "get_component_caps", AsyncMock(return_value=caps)
    ):
        result = await getattr(core_contract, bridge)(Mock(), "knx")
    assert result == {
        "status": "unavailable",
        "reason": f"The installed component does not expose {feature}. "
        "Core still validates actual requests.",
    }


async def test_server_bridge_reaches_the_component_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import core_contract as producer
    from custom_components.ha_mcp_tools import websocket_api as wsapi
    from ha_mcp.tools import component_api, core_contract

    hass = FakeHass()
    hass.data["websocket_api"] = {
        "knx/delete_entity": (
            Mock(),
            _REAL_VOL.Schema(
                {
                    _REAL_VOL.Required("id"): int,
                    _REAL_VOL.Required("type"): str,
                    _REAL_VOL.Required("entity_id"): str,
                }
            ),
        )
    }
    execute = next(
        run
        for schema, run, _prep in producer.command_specs(_REAL_VOL)
        if schema[_REAL_VOL.Required("type")] == producer.INTEGRATION_COMMAND
    )

    async def send_command(name: str, **params: Any) -> dict[str, Any]:
        if name == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info(hass)}
        assert name == producer.INTEGRATION_COMMAND
        return {"success": True, "result": execute(hass, params)}

    ws = Mock(send_command=AsyncMock(side_effect=send_command))
    monkeypatch.setattr(
        component_api, "get_websocket_client", AsyncMock(return_value=ws)
    )
    monkeypatch.setattr(
        core_contract, "get_websocket_client", AsyncMock(return_value=ws)
    )
    client = Mock(
        base_url="http://test-integration-commands", token="t", verify_ssl=True
    )
    result = await core_contract.integration_commands(
        client, "knx", "knx/delete_entity", {}
    )
    assert result["valid"] is False
    assert result["errors"][0]["path"] == ["entity_id"]
