"""Tests for generic config-entry reconfiguration."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_entry_flow import reconfigure_config_entry
from ha_mcp.tools.reconfigure_security import (
    build_reconfigure_rollback_metadata,
    redact_reconfigure_value,
)
from ha_mcp.tools.tools_integrations import IntegrationTools


@pytest.fixture
def reconfig_entry() -> dict[str, object]:
    """Return a minimal Home Assistant config-entry representation."""
    return {
        "entry_id": "entry-123",
        "domain": "shelly",
        "title": "Living room relay",
        "unique_id": "AA:BB:CC:DD:EE:FF",
        "supports_reconfigure": True,
    }


def test_reconfigure_redaction_covers_nested_camel_case_secrets() -> None:
    """Flow errors must not leak common credential key spellings."""
    value = redact_reconfigure_value(
        {
            "apiKey": "hidden",
            "nested": {"clientSecret": "hidden", "host": "10.0.50.170"},
            "items": [{"refresh-token": "hidden"}],
        }
    )

    assert value == {
        "apiKey": "[REDACTED]",
        "nested": {"clientSecret": "[REDACTED]", "host": "10.0.50.170"},
        "items": [{"refresh-token": "[REDACTED]"}],
    }


def test_reconfigure_rollback_metadata_is_honest_about_redacted_secrets() -> None:
    """Rollback uses the official flow and never promises replay of secrets."""
    metadata = build_reconfigure_rollback_metadata(
        "entry-123",
        "shelly",
        {
            "data": {
                "host": "10.0.50.170",
                "port": 80,
                "password": "do-not-return",
            }
        },
    )

    assert metadata["strategy"] == "official_reconfigure_flow"
    assert metadata["automatic"] is False
    assert metadata["operator_action_required"] is True
    assert metadata["manual_required"] is True
    assert metadata["manual_reason"] == "previous_config_contains_redacted_secrets"
    assert metadata["previous_config"] == {
        "host": "10.0.50.170",
        "port": 80,
        "password": "[REDACTED]",
    }


def test_reconfigure_rollback_metadata_without_secrets_is_replayable() -> None:
    """A non-sensitive previous config can be replayed by an operator."""
    metadata = build_reconfigure_rollback_metadata(
        "entry-123",
        "esphome",
        {"data": {"host": "10.0.50.170", "port": 6053}},
    )

    assert metadata["manual_required"] is False
    assert metadata["manual_reason"] is None
    assert metadata["previous_config"] == {"host": "10.0.50.170", "port": 6053}


@pytest.mark.asyncio
async def test_reconfigure_preserves_entry_and_submits_host_and_port(
    reconfig_entry: dict[str, object],
) -> None:
    """The official reconfigure flow updates the existing entry in place."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-123",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": [
                {"name": "host", "required": True},
                {"name": "port", "required": False, "default": 80},
            ],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client, "entry-123", host="10.0.50.170", port=80
    )

    assert result["success"] is True
    assert result["operation"] == "reconfigured"
    assert result["entry_id"] == "entry-123"
    assert result["domain"] == "shelly"
    assert result["rollback_strategy"] == "official_reconfigure_flow"
    assert result["rollback_automatic"] is False
    assert result["rollback_operator_action_required"] is True
    assert result["rollback_manual_required"] is True
    assert result["rollback_reference"]["manual_reason"] == (
        "previous_config_unavailable"
    )
    assert result["target_config"] == {"host": "10.0.50.170", "port": 80}
    assert result["verification"] == {
        "entry_id_preserved": True,
        "domain_preserved": True,
        "unique_id_preserved": True,
        "unique_id_verification": "preserved",
        "device_id_verification": "unavailable_before_change",
        "entity_verification": "unavailable_before_change",
        "identity_verification": "partial",
        "duplicate_entry_created": False,
        "duplicate_verification": "complete",
    }
    client.start_reconfigure_flow.assert_awaited_once_with("shelly", "entry-123")
    client.submit_config_flow_step.assert_awaited_once_with(
        "flow-123", {"host": "10.0.50.170", "port": 80}
    )


@pytest.mark.asyncio
async def test_reconfigure_drives_multiple_form_steps(
    reconfig_entry: dict[str, object],
) -> None:
    """The generic walker can finish a reconfigure flow with multiple forms."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-multi",
            "type": "form",
            "step_id": "host",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        side_effect=[
            {
                "flow_id": "flow-multi",
                "type": "form",
                "step_id": "port",
                "data_schema": [{"name": "port", "required": True}],
            },
            {"type": "abort", "reason": "reconfigure_successful"},
        ]
    )

    result = await reconfigure_config_entry(
        client, "entry-123", host="10.0.50.173", port=8080
    )

    assert result["success"] is True
    assert client.submit_config_flow_step.await_args_list[0].args == (
        "flow-multi",
        {"host": "10.0.50.173"},
    )
    assert client.submit_config_flow_step.await_args_list[1].args == (
        "flow-multi",
        {"port": 8080},
    )


@pytest.mark.asyncio
async def test_reconfigure_fails_when_success_abort_ignores_requested_values(
    reconfig_entry: dict[str, object],
) -> None:
    """A successful HA abort must not hide values that the flow never consumed."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-ignored",
            "type": "form",
            "step_id": "address",
            "data_schema": [{"name": "address", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "10.0.50.180"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "consum" in payload["error"]["message"].lower()
    client.abort_config_flow.assert_awaited_once_with("flow-ignored")


@pytest.mark.asyncio
async def test_reconfigure_accepts_generic_config_and_menu_selection(
    reconfig_entry: dict[str, object],
) -> None:
    """Generic integrations can receive arbitrary fields and menu choices."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-generic",
            "type": "menu",
            "step_id": "choose_transport",
            "menu_options": ["network", "cloud"],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        side_effect=[
            {
                "flow_id": "flow-generic",
                "type": "form",
                "step_id": "network",
                "data_schema": [{"name": "address", "required": True}],
            },
            {"type": "abort", "reason": "reconfigure_successful"},
        ]
    )

    result = await reconfigure_config_entry(
        client,
        "entry-123",
        config={"next_step_id": "network", "address": "10.0.50.181"},
    )

    assert result["success"] is True
    assert client.submit_config_flow_step.await_args_list[0].args == (
        "flow-generic",
        {"next_step_id": "network"},
    )
    assert client.submit_config_flow_step.await_args_list[1].args == (
        "flow-generic",
        {"address": "10.0.50.181"},
    )


@pytest.mark.asyncio
async def test_reconfigure_allows_offline_entry_with_registry_identity() -> None:
    """A setup_retry entry can be changed without contacting the physical device."""
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    after = {**before, "state": "loaded", "unique_id": "84FCE6387220"}
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[before, after])
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.a1_luces_techo",
                "config_entry_id": "offline-entry",
                "device_id": "device-a1",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-a1",
                "identifiers": [["shelly", "84:FC:E6:38:72:20"]],
            }
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "offline-flow",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "offline-entry",
        host="10.0.50.170",
        expected_device_id="device-a1",
        expected_mac="84-FC-E6-38-72-20",
        expected_entity_ids=["switch.a1_luces_techo"],
    )

    assert result["success"] is True
    assert result["verification"]["identity_verification"] == "complete"
    assert result["verification"]["device_id_verification"] == "preserved"
    assert result["verification"]["entity_verification"] == "preserved"


@pytest.mark.asyncio
async def test_reconfigure_reports_applied_but_unverified_after_commit(
    reconfig_entry: dict[str, object],
) -> None:
    """A post-commit HA read failure is not presented as a clean apply failure."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(
        side_effect=[reconfig_entry, RuntimeError("HA read timeout")]
    )
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-unknown",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(client, "entry-123", host="10.0.50.182")

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "applied_but_unverified"


@pytest.mark.asyncio
async def test_reconfigure_verification_failure_includes_rollback_reference(
    reconfig_entry: dict[str, object],
) -> None:
    """Post-commit identity failures retain the operator rollback path."""
    after = {**reconfig_entry, "unique_id": "DIFFERENT-AFTER-APPLY"}
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, after])
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-identity-mismatch",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(client, "entry-123", host="10.0.50.183")

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "applied_but_unverified"
    assert payload["rollback"]["strategy"] == "official_reconfigure_flow"
    assert payload["rollback"]["operator_action_required"] is True
    assert payload["rollback"]["backup_restore_supported"] is False


@pytest.mark.asyncio
async def test_reconfigure_rejects_registry_duplicate_without_unique_id() -> None:
    """A second entry sharing the registered device is unsafe even without unique_id."""
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.a1",
                "config_entry_id": "offline-entry",
                "device_id": "device-a1",
            },
            {
                "entity_id": "switch.a1_duplicate",
                "config_entry_id": "duplicate-entry",
                "device_id": "device-a1",
            },
        ]
    )
    client.list_device_registry = AsyncMock(return_value=[])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "offline-entry",
            host="10.0.50.185",
        )

    payload = json.loads(str(exc_info.value))
    assert "duplicate" in payload["error"]["message"].lower()
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_expected_unique_id_mismatch_before_flow(
    reconfig_entry: dict[str, object],
) -> None:
    """A known entry identity mismatch must fail before any mutating flow starts."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "entry-123",
            host="10.0.50.183",
            expected_unique_id="different-device",
        )

    payload = json.loads(str(exc_info.value))
    assert "expected unique_id" in payload["error"]["message"]
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_expected_mac_mismatch_before_flow(
    reconfig_entry: dict[str, object],
) -> None:
    """A known registry MAC mismatch must fail before any mutating flow starts."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.living_room",
                "config_entry_id": "entry-123",
                "device_id": "device-living-room",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-living-room",
                "identifiers": [["shelly", "AA:BB:CC:DD:EE:FF"]],
            }
        ]
    )
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "entry-123",
            host="10.0.50.184",
            expected_mac="11:22:33:44:55:66",
        )

    payload = json.loads(str(exc_info.value))
    assert "expected MAC" in payload["error"]["message"]
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_does_not_treat_temporarily_missing_identity_as_change(
    reconfig_entry: dict[str, object],
) -> None:
    """A post-flow missing identifier is unverified, not a different device."""
    after = {key: value for key, value in reconfig_entry.items() if key != "unique_id"}
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, after])
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-temporary-identity",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "entry-123",
        host="10.0.50.186",
        expected_unique_id="AA:BB:CC:DD:EE:FF",
    )

    assert result["status"] == "applied_but_unverified"
    assert result["verification"]["unique_id_verification"] == (
        "unavailable_after_change"
    )


@pytest.mark.asyncio
async def test_client_starts_official_reconfigure_flow_with_entry_id() -> None:
    """The REST client uses entry_id, which makes HA select source=reconfigure."""
    client = HomeAssistantClient(
        base_url="http://homeassistant.local",
        token="test-token",
        verify_ssl=True,
    )
    client._request = AsyncMock(return_value={"flow_id": "flow-1", "type": "form"})

    result = await client.start_reconfigure_flow("esphome", "entry-123")

    assert result["flow_id"] == "flow-1"
    client._request.assert_awaited_once_with(
        "POST",
        "/config/config_entries/flow",
        json={"handler": "esphome", "entry_id": "entry-123"},
    )
    await client.close()


@pytest.mark.asyncio
async def test_reconfigure_fails_closed_when_original_entry_cannot_be_verified(
    reconfig_entry: dict[str, object],
) -> None:
    """A completed flow is not reported as success if HA returns another entry."""
    changed_entry = dict(reconfig_entry)
    changed_entry["entry_id"] = "different-entry"
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, changed_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-verify",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(client, "entry-123", host="10.0.50.174")

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
    assert payload["entry_id"] == "entry-123"


@pytest.mark.asyncio
async def test_reconfigure_detects_duplicate_identity(
    reconfig_entry: dict[str, object],
) -> None:
    """Post-flight verification rejects a second entry sharing the identity."""
    duplicate_entry = dict(reconfig_entry)
    duplicate_entry["entry_id"] = "entry-duplicate"
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_config_entries = AsyncMock(
        return_value=[reconfig_entry, duplicate_entry]
    )
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-duplicate",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(client, "entry-123", host="10.0.50.175")

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "duplicate" in payload["error"]["message"].lower()


@pytest.mark.asyncio
async def test_reconfigure_rejects_entries_without_official_support(
    reconfig_entry: dict[str, object],
) -> None:
    """Entries without async_step_reconfigure are rejected before any flow starts."""
    reconfig_entry["supports_reconfigure"] = False
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(client, "entry-123", host="10.0.50.170")

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "reconfigure" in payload["error"]["message"].lower()
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_requires_explicit_confirmation(
    reconfig_entry: dict[str, object],
) -> None:
    """The MCP tool performs preflight but never writes without confirm=True."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()
    tools = IntegrationTools(client)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_reconfigure_integration(
            entry_id="entry-123", host="10.0.50.180", port=80
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "confirm" in payload["error"]["message"].lower()
    assert payload["entry_id"] == "entry-123"
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_invalid_port(
    reconfig_entry: dict[str, object],
) -> None:
    """Ports outside TCP's valid range are rejected locally."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", host="10.0.50.180", port=65536
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "port" in payload["error"]["message"].lower()
    client.get_config_entry.assert_not_awaited()
