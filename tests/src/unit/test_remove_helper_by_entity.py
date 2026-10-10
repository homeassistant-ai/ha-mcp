"""ha_remove_helpers_integrations with an entity_id and no helper_type.

The entity registry identifies the helper, so an agent need not know which
helper type created an entity, and an entity of a non-helper integration is
refused instead of deleting that integration's config entry.
"""

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAuthError
from ha_mcp.errors import DEFAULT_SUGGESTIONS
from ha_mcp.tools import auto_backup, helper_flows
from ha_mcp.tools.helpers import exception_to_structured_error
from ha_mcp.tools.tools_integrations import IntegrationTools

# The real Core read, kept before conftest's autouse fixture replaces it.
_REAL_FETCH = helper_flows._fetch_helper_flow_types

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


async def test_otp_is_removed_by_its_entity_like_any_helper_flow() -> None:
    """otp and custom helper integrations are helper flows in Core's list, so
    the entity alone removes them through the flow path."""
    client = _client(
        {
            "entity_id": "sensor.my_otp",
            "platform": "otp",
            "config_entry_id": "otp_entry",
        }
    )
    result = await _remove(client, "sensor.my_otp")
    assert result["helper_type"] == "otp"
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


async def test_unreadable_helper_list_is_a_connection_error_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable flow_handlers reply must not read as 'not a helper'."""
    monkeypatch.setattr(helper_flows, "_fetch_helper_flow_types", _REAL_FETCH)
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
    ("registry_row", "snapshot"),
    [
        (
            {"entity_id": "sensor.my_otp", "platform": "otp", "config_entry_id": "e"},
            ("helper_otp", "e"),
        ),
        (
            {
                "entity_id": "input_boolean.guest_mode",
                "platform": "input_boolean",
                "unique_id": "guest_mode",
                "config_entry_id": None,
            },
            ("helper_input_boolean", "input_boolean.guest_mode"),
        ),
    ],
    ids=["flow", "storage"],
)
async def test_entity_route_backs_up_the_resolved_helper_before_deleting(
    registry_row: dict[str, Any],
    snapshot: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """A helper removed by its entity_id gets one snapshot attempt for the
    resolved helper, made before the delete."""
    client = _client(registry_row)
    client.get_config_entry = AsyncMock(return_value={"domain": "otp"})
    taken: list[tuple[str, str, int]] = []

    async def record(_mgr: Any, domain: str, entity_id: str, **_: Any) -> None:
        deletes = client.delete_config_entry.await_count + len(client.ws_deletes)
        taken.append((domain, entity_id, deletes))

    monkeypatch.setattr(bm.BackupManager, "maybe_snapshot", record)
    monkeypatch.setattr(
        auto_backup,
        "get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=True, auto_backup_dir=str(tmp_path)),
    )
    await _remove(client, registry_row["entity_id"])
    assert taken == [(*snapshot, 0)]


@pytest.mark.parametrize(
    ("platform", "record", "reason"),
    [
        ("otp", {"options": {}}, "has no stored options to back up or restore"),
        (
            "my_custom_helper",
            {"options": None, "options_withheld": "custom_integration"},
            "whose options the component withholds",
        ),
    ],
    ids=["otp", "custom_only"],
)
async def test_a_helper_whose_options_are_not_backed_up_is_removed_with_the_reason(
    platform: str,
    record: dict[str, Any],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Through the real capture: the by-design skip lets the delete run, tells
    the caller why no backup was taken, and is not logged as a fetch failure."""
    monkeypatch.setattr(
        helper_flows,
        "_fetch_helper_flow_types",
        AsyncMock(return_value=frozenset({*_HELPER_FLOW_DOMAINS, "my_custom_helper"})),
    )
    row = {"entity_id": "sensor.h", "platform": platform, "config_entry_id": "e"}
    client = _client(row)

    async def fake_ws_send(_client: Any, message: dict[str, Any]) -> Any:
        if message["type"] == "config/entity_registry/list":
            return [row]
        assert message["type"] == "ha_mcp_tools/helpers_list"
        return {
            "covered_types": [platform],
            "helpers": [
                {"kind": "flow", "helper_type": platform, "entry_id": "e"} | record
            ],
        }

    monkeypatch.setattr(bm, "_ws_send", fake_ws_send)
    monkeypatch.setattr(
        auto_backup,
        "get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=True, auto_backup_dir=str(tmp_path)),
    )
    with caplog.at_level(logging.INFO, logger=bm.logger.name):
        result = await _remove(client, "sensor.h")
    client.delete_config_entry.assert_awaited_once_with("e")
    [warning] = result["warnings"]
    assert warning.startswith(f"No pre-write backup of helper_{platform}:e was taken")
    assert reason in warning
    backup_logs = [r for r in caplog.records if r.name == bm.logger.name]
    assert [r.levelno for r in backup_logs] == [logging.INFO]
    assert reason in backup_logs[0].getMessage()


async def test_a_custom_helper_named_by_type_is_snapshotted_once_before_deleting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """An explicit custom helper_type is a flow type: the outer capture leaves
    it to the flow path, which snapshots the resolved entry once."""
    monkeypatch.setattr(
        helper_flows,
        "_fetch_helper_flow_types",
        AsyncMock(return_value=frozenset({*_HELPER_FLOW_DOMAINS, "my_custom_helper"})),
    )
    client = _client(
        {
            "entity_id": "sensor.h",
            "platform": "my_custom_helper",
            "config_entry_id": "e",
        }
    )
    taken: list[tuple[str, str, int]] = []

    async def record(_mgr: Any, domain: str, entity_id: str, **_: Any) -> None:
        taken.append((domain, entity_id, client.delete_config_entry.await_count))

    monkeypatch.setattr(bm.BackupManager, "maybe_snapshot", record)
    monkeypatch.setattr(
        auto_backup,
        "get_global_settings",
        lambda: SimpleNamespace(enable_auto_backup=True, auto_backup_dir=str(tmp_path)),
    )
    result: dict[str, Any] = await IntegrationTools(
        client
    ).ha_remove_helpers_integrations(
        target="sensor.h", helper_type="my_custom_helper", confirm=True, wait=False
    )
    assert result["success"] is True
    assert taken == [("helper_my_custom_helper", "e", 0)]


async def test_entity_without_a_registry_entry_is_not_reported_missing() -> None:
    """zone.home and a YAML template sensor without unique_id have a state but
    no registry entry; telling the caller they are missing sends them to
    ha_search(), which finds them."""
    client = _client(None)
    client.get_entity_state = AsyncMock(return_value={"state": "zoning"})
    assert await _refused(client, "zone.home") == "RESOURCE_NOT_FOUND"


async def test_failed_registry_read_keeps_its_classified_suggestions() -> None:
    """An auth failure must keep its token guidance; the added route names
    the config entry_id, since it needs no registry or helper-flow read.
    Adding it must not leak into the shared defaults every other tool's
    errors are built from."""
    failure = HomeAssistantAuthError("token expired")
    classified = exception_to_structured_error(
        failure, context={"target": "sensor.my_otp"}, raise_error=False
    )["error"]["suggestions"]
    assert classified
    defaults = {code: list(hints) for code, hints in DEFAULT_SUGGESTIONS.items()}
    client = _client(None)
    client.send_websocket_message = AsyncMock(side_effect=failure)
    with pytest.raises(ToolError) as exc_info:
        await _remove(client, "sensor.my_otp")
    suggestions = json.loads(str(exc_info.value))["error"]["suggestions"]
    assert suggestions == [*classified, suggestions[-1]]
    assert "config entry_id" in suggestions[-1]
    assert defaults == DEFAULT_SUGGESTIONS
