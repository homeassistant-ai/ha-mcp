"""ha_remove_helpers_integrations with an entity_id and no helper_type.

The entity registry identifies the helper, so an agent need not know which
helper type created an entity, and an entity of a non-helper integration is
refused instead of deleting that integration's config entry.
"""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import auto_backup
from ha_mcp.tools.tools_integrations import IntegrationTools

# What Core's GET /api/config/config_entries/flow_handlers?type=helper returns:
# its helper flows plus custom integrations of integration_type "helper".
_HELPER_FLOW_DOMAINS = ["group", "otp", "template", "utility_meter"]


def _client(
    registry_row: dict[str, Any] | None, helper_flow_domains: Any = None
) -> MagicMock:
    client = MagicMock()
    client.get_entity_state = AsyncMock(return_value=None)
    client.delete_config_entry = AsyncMock(return_value={"require_restart": False})
    client._request = AsyncMock(
        return_value=_HELPER_FLOW_DOMAINS
        if helper_flow_domains is None
        else helper_flow_domains
    )
    client.ws_deletes = []

    async def send(message: dict[str, Any]) -> dict[str, Any]:
        if message["type"] == "config/entity_registry/get":
            if registry_row is None:
                return {
                    "success": False,
                    "error": "Entity not found",
                    "error_code": "not_found",
                }
            return {"success": True, "result": registry_row}
        if message["type"] == "config/entity_registry/list":
            return {"success": True, "result": [registry_row]}
        if message["type"].endswith("/delete"):
            client.ws_deletes.append(message)
        return {"success": True, "result": {}}

    client.send_websocket_message = AsyncMock(side_effect=send)
    return client


async def _remove(
    client: MagicMock, entity_id: str, *, confirm: bool = True
) -> dict[str, Any]:
    result: dict[str, Any] = await IntegrationTools(
        client
    ).ha_remove_helpers_integrations(target=entity_id, confirm=confirm, wait=False)
    return result


async def _refused(client: MagicMock, entity_id: str, **kwargs: Any) -> str:
    with pytest.raises(ToolError) as exc_info:
        await _remove(client, entity_id, **kwargs)
    client.delete_config_entry.assert_not_awaited()
    assert client.ws_deletes == []
    code: str = json.loads(str(exc_info.value))["error"]["code"]
    return code


async def test_entity_of_a_non_helper_integration_is_refused() -> None:
    """A Hue light named without helper_type must not take Hue with it."""
    client = _client(
        {
            "entity_id": "light.hue_lamp",
            "platform": "hue",
            "config_entry_id": "hue_entry",
        }
    )
    with pytest.raises(ToolError) as exc_info:
        await _remove(client, "light.hue_lamp")
    err = json.loads(str(exc_info.value))
    assert err["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    client.delete_config_entry.assert_not_awaited()
    assert client.ws_deletes == []


async def test_flow_helper_is_removed_without_naming_its_type() -> None:
    client = _client(
        {
            "entity_id": "sensor.energy_peak",
            "platform": "utility_meter",
            "config_entry_id": "um_entry",
        }
    )
    result = await _remove(client, "sensor.energy_peak")
    assert result["helper_type"] == "utility_meter"
    client.delete_config_entry.assert_awaited_once_with("um_entry")


async def test_helper_that_only_core_lists_is_removed_by_its_entity() -> None:
    """otp (and custom-integration helpers) are helpers in Core's list but not
    flow types ha-mcp can create, so the entity alone must still remove them."""
    client = _client(
        {
            "entity_id": "sensor.my_otp",
            "platform": "otp",
            "config_entry_id": "otp_entry",
        }
    )
    result = await _remove(client, "sensor.my_otp")
    assert result["resolved_from"] == "sensor.my_otp"
    client.delete_config_entry.assert_awaited_once_with("otp_entry")


async def test_storage_helper_is_removed_without_naming_its_type() -> None:
    client = _client(
        {
            "entity_id": "input_boolean.guest_mode",
            "platform": "input_boolean",
            "unique_id": "guest_mode",
            "config_entry_id": None,
        }
    )
    result = await _remove(client, "input_boolean.guest_mode")
    assert result["success"] is True
    assert client.ws_deletes == [
        {"type": "input_boolean/delete", "input_boolean_id": "guest_mode"}
    ]
    client.delete_config_entry.assert_not_awaited()


async def test_unconfirmed_entity_only_call_deletes_nothing() -> None:
    client = _client(
        {
            "entity_id": "sensor.energy_peak",
            "platform": "utility_meter",
            "config_entry_id": "um_entry",
        }
    )
    code = await _refused(client, "sensor.energy_peak", confirm=False)
    assert code == "VALIDATION_INVALID_PARAMETER"


async def test_unreadable_helper_list_is_a_connection_error_not_a_refusal() -> None:
    """An unparseable flow_handlers reply must not read as 'not a helper'."""
    client = _client(
        {"entity_id": "sensor.my_otp", "platform": "otp", "config_entry_id": "e"},
        helper_flow_domains={},
    )
    assert await _refused(client, "sensor.my_otp") == "CONNECTION_FAILED"


async def test_entity_missing_from_the_registry_is_not_found() -> None:
    client = _client(None)
    assert await _refused(client, "sensor.typo") == "ENTITY_NOT_FOUND"


async def test_yaml_helper_that_only_core_lists_is_not_found() -> None:
    """A Core-listed helper platform without a config entry is YAML-configured,
    not a foreign integration."""
    client = _client(
        {"entity_id": "sensor.my_otp", "platform": "otp", "config_entry_id": None}
    )
    assert await _refused(client, "sensor.my_otp") == "RESOURCE_NOT_FOUND"


async def test_registry_transport_failure_names_its_cause() -> None:
    client = _client(None)
    client.send_websocket_message = AsyncMock(side_effect=ConnectionError("ws drop"))
    with pytest.raises(ToolError) as exc_info:
        await _remove(client, "sensor.energy_peak")
    err = json.loads(str(exc_info.value))["error"]
    assert err["code"] == "WEBSOCKET_DISCONNECTED"
    assert "ws drop" in err["message"]


@pytest.mark.parametrize(
    "registry_row",
    [
        {
            "entity_id": "sensor.energy_peak",
            "platform": "utility_meter",
            "config_entry_id": "um_entry",
        },
        {"entity_id": "sensor.my_otp", "platform": "otp", "config_entry_id": "e"},
        {
            "entity_id": "input_boolean.guest_mode",
            "platform": "input_boolean",
            "unique_id": "guest_mode",
            "config_entry_id": None,
        },
    ],
    ids=["flow", "core_listed", "storage"],
)
async def test_entity_route_logs_one_tool_call(
    registry_row: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolved removal must not log a second, synthetic tool call."""
    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "ha_mcp.tools.helpers.log_tool_call", lambda **kw: logged.append(kw)
    )
    await _remove(_client(registry_row), registry_row["entity_id"])
    assert [(c["tool_name"], c["parameters"]["target"]) for c in logged] == [
        ("ha_remove_helpers_integrations", registry_row["entity_id"])
    ]


@pytest.mark.parametrize(
    ("registry_row", "captured"),
    [
        (
            {"entity_id": "sensor.my_otp", "platform": "otp", "config_entry_id": "e"},
            ("e", None),
        ),
        (
            {
                "entity_id": "input_boolean.guest_mode",
                "platform": "input_boolean",
                "unique_id": "guest_mode",
                "config_entry_id": None,
            },
            ("input_boolean.guest_mode", "input_boolean"),
        ),
    ],
    ids=["core_listed", "storage"],
)
async def test_entity_route_captures_the_resolved_helper(
    registry_row: dict[str, Any],
    captured: tuple[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Helpers the flow path does not back up are captured by the resolved call."""
    targets: list[tuple[str, str | None]] = []

    async def record(_func: Any, _args: Any, kwargs: dict[str, Any], **_: Any) -> None:
        targets.append((kwargs["target"], kwargs["helper_type"]))

    monkeypatch.setattr(
        auto_backup,
        "get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=True),
    )
    monkeypatch.setattr(auto_backup, "_capture_pre_write_snapshot", record)
    await _remove(_client(registry_row), registry_row["entity_id"])
    assert targets == [captured]
