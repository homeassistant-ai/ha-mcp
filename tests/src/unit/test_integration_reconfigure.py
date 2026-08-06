"""Tests for generic config-entry reconfiguration."""

import inspect
import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.config_entry_flow import (
    _same_domain_related_entry_ids,
    reconfigure_config_entry,
    set_config_subentry,
)
from ha_mcp.tools.config_entry_flow_walker import _handle_config_subentry_flow_steps
from ha_mcp.tools.tools_integrations import IntegrationTools


def test_reconfigure_internal_contract_accepts_only_generic_config() -> None:
    """The reconfigure helper must not expose a parallel host/port API."""
    parameters = inspect.signature(reconfigure_config_entry).parameters

    assert "config" in parameters
    assert "host" not in parameters
    assert "port" not in parameters


@pytest.mark.asyncio
async def test_reconfigure_result_preserves_caller_config_values(
    reconfig_entry: dict[str, object],
) -> None:
    """Reconfigure results do not transform caller-supplied configuration."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, reconfig_entry])
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-config-values",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": [
                {"name": "host", "required": True},
                {"name": "password", "required": True},
            ],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "entry-123",
        config={"host": "10.0.50.170", "password": "caller-value"},
    )

    assert result["target_config"] == {
        "host": "10.0.50.170",
        "password": "caller-value",
    }


@pytest.fixture
def reconfig_entry() -> dict[str, object]:
    """Return a minimal Home Assistant config-entry representation."""
    return {
        "entry_id": "entry-123",
        "domain": "shelly",
        "title": "Living room relay",
        "state": "loaded",
        "unique_id": "AA:BB:CC:DD:EE:FF",
        "supports_reconfigure": True,
    }


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
        client, "entry-123", config={"host": "10.0.50.170", "port": 80}
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
        "entry_state": "loaded",
        "operational_state_verified": True,
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
        client, "entry-123", config={"host": "10.0.50.173", "port": 8080}
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
    assert payload["status"] == "applied_but_incomplete"
    client.abort_config_flow.assert_not_awaited()


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
                "connections": [["mac", "84:FC:E6:38:72:20"]],
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
        config={"host": "10.0.50.170"},
        expected_device_id="device-a1",
        expected_mac="84-FC-E6-38-72-20",
        expected_entity_ids=["switch.a1_luces_techo"],
    )

    assert result["success"] is True
    assert result["status"] == "applied_and_verified"
    assert result["verification"]["identity_verification"] == "complete"
    assert result["verification"]["device_id_verification"] == "preserved"
    assert result["verification"]["entity_verification"] == "preserved"


@pytest.mark.asyncio
async def test_reconfigure_does_not_verify_setup_retry_as_loaded() -> None:
    """Identity preservation is insufficient when HA leaves the entry degraded."""
    before = {
        "entry_id": "degraded-entry",
        "domain": "shelly",
        "state": "loaded",
        "supports_reconfigure": True,
        "unique_id": "84FCE6387220",
    }
    after = {**before, "state": "setup_retry"}
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[before, after])
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.degraded",
                "config_entry_id": "degraded-entry",
                "device_id": "device-degraded",
            }
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-degraded",
                "connections": [["mac", "84:FC:E6:38:72:20"]],
            }
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "degraded-flow",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "degraded-entry",
        config={"host": "10.0.50.170"},
        expected_device_id="device-degraded",
        expected_mac="84-FC-E6-38-72-20",
        expected_entity_ids=["switch.degraded"],
    )

    assert result["status"] == "applied_but_unverified"
    assert result["verification"]["entry_state"] == "setup_retry"
    assert result["verification"]["operational_state_verified"] is False


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
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "10.0.50.182"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "applied_but_unverified"


@pytest.mark.asyncio
async def test_reconfigure_submit_timeout_is_applied_but_unverified(
    reconfig_entry: dict[str, object],
) -> None:
    """A submit timeout may follow a commit and must retain rollback context."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-submit-timeout",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(side_effect=TimeoutError())
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "10.0.50.183"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "applied_but_unverified"
    assert payload["rollback"]["manual_required"] is True
    assert "timed out" in payload["error"]["message"]
    client.abort_config_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_step_budget_aborts_pending_flow(
    reconfig_entry: dict[str, object],
) -> None:
    """Exhausting the walker budget aborts the still-pending reconfigure flow."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-budget",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={
            "flow_id": "flow-budget",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.abort_config_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "10.0.50.184"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "applied_but_unverified"
    assert payload["rollback"]["manual_required"] is True
    client.abort_config_flow.assert_awaited_once_with("flow-budget")


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
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "10.0.50.183"}
        )

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
    client.list_config_entries = AsyncMock(
        return_value=[before, {"entry_id": "duplicate-entry", "domain": "shelly"}]
    )
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "offline-entry",
            config={"host": "10.0.50.185"},
        )

    payload = json.loads(str(exc_info.value))
    assert "duplicate" in payload["error"]["message"].lower()
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_registry_transport_failure_before_flow() -> None:
    """A dead registry transport must block the mutating flow."""
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_entity_registry = AsyncMock(
        side_effect=HomeAssistantConnectionError("registry unavailable")
    )
    client.list_device_registry = AsyncMock(return_value=[])
    client.list_config_entries = AsyncMock(return_value=[before])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(HomeAssistantConnectionError, match="registry unavailable"):
        await reconfigure_config_entry(
            client,
            "offline-entry",
            config={"host": "10.0.50.185"},
        )

    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_allows_auxiliary_entry_sharing_same_device() -> None:
    """A switch_as_x light is not a duplicate physical Shelly entry."""
    shelly_entry = {
        "entry_id": "shelly-entry",
        "domain": "shelly",
        "title": "Luces pasillo",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    switch_as_x_entry = {
        "entry_id": "switch-as-x-entry",
        "domain": "switch_as_x",
        "title": "luces_pasillo_switch_0",
        "state": "loaded",
        "supports_reconfigure": False,
    }
    entity_rows = [
        {
            "entity_id": "switch.luces_pasillo_switch_0",
            "config_entry_id": "shelly-entry",
            "device_id": "shared-device",
            "platform": "shelly",
        },
        {
            "entity_id": "light.luces_pasillo",
            "config_entry_id": "switch-as-x-entry",
            "device_id": "shared-device",
            "platform": "switch_as_x",
        },
    ]
    client = MagicMock()
    client.get_config_entry = AsyncMock(
        side_effect=[shelly_entry, {**shelly_entry, "state": "loaded"}]
    )
    client.list_entity_registry = AsyncMock(return_value=entity_rows)
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "shared-device",
                "connections": [["mac", "EC:DA:3B:C2:32:1C"]],
                "config_entries": ["shelly-entry", "switch-as-x-entry"],
            }
        ]
    )
    client.list_config_entries = AsyncMock(
        return_value=[shelly_entry, switch_as_x_entry]
    )
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-auxiliary-entry",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "shelly-entry",
        config={"host": "10.0.50.51"},
        expected_device_id="shared-device",
        expected_mac="EC:DA:3B:C2:32:1C",
    )

    assert result["success"] is True
    client.start_reconfigure_flow.assert_awaited_once_with("shelly", "shelly-entry")


@pytest.mark.asyncio
async def test_reconfigure_allows_known_auxiliary_handler_without_domain() -> None:
    """A known helper remains allowed when HA exposes its handler, not domain."""
    shelly_entry = {
        "entry_id": "shelly-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    auxiliary_entry = {
        "entry_id": "switch-as-x-entry",
        "handler": "switch_as_x",
        "state": "loaded",
    }
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=shelly_entry)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.source",
                "config_entry_id": "shelly-entry",
                "device_id": "shared-device",
            },
            {
                "entity_id": "light.derived",
                "config_entry_id": "switch-as-x-entry",
                "device_id": "shared-device",
            },
        ]
    )
    client.list_device_registry = AsyncMock(return_value=[])
    client.list_config_entries = AsyncMock(return_value=[shelly_entry, auxiliary_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-handler-only-auxiliary",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "shelly-entry",
        config={"host": "10.0.50.52"},
        expected_device_id="shared-device",
    )

    assert result["success"] is True
    client.start_reconfigure_flow.assert_awaited_once_with("shelly", "shelly-entry")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auxiliary_domain", ["derivative", "threshold", "utility_meter"]
)
async def test_reconfigure_allows_known_auxiliary_domains(
    auxiliary_domain: str,
) -> None:
    """Known helper/platform entries do not block a primary entry."""
    client = MagicMock()
    client.list_config_entries = AsyncMock(
        return_value=[
            {"entry_id": "primary", "domain": "shelly"},
            {"entry_id": "auxiliary", "domain": auxiliary_domain},
        ]
    )

    blocking = await _same_domain_related_entry_ids(
        client,
        {"related_entry_ids": ["primary", "auxiliary"]},
        entry_id="primary",
        domain="shelly",
    )

    assert blocking == []


@pytest.mark.asyncio
async def test_reconfigure_rejects_unknown_related_domain() -> None:
    """An unexplained cross-domain relationship remains fail-closed."""
    shelly_entry = {
        "entry_id": "shelly-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    unknown_entry = {
        "entry_id": "unknown-entry",
        "domain": "some_other_integration",
        "state": "loaded",
    }
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=shelly_entry)
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "switch.source",
                "config_entry_id": "shelly-entry",
                "device_id": "shared-device",
            },
            {
                "entity_id": "sensor.unexplained",
                "config_entry_id": "unknown-entry",
                "device_id": "shared-device",
            },
        ]
    )
    client.list_device_registry = AsyncMock(return_value=[])
    client.list_config_entries = AsyncMock(return_value=[shelly_entry, unknown_entry])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "shelly-entry",
            config={"host": "10.0.50.53"},
            expected_device_id="shared-device",
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
            config={"host": "10.0.50.183"},
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
                "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
            }
        ]
    )
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "entry-123",
            config={"host": "10.0.50.184"},
            expected_mac="11:22:33:44:55:66",
        )

    payload = json.loads(str(exc_info.value))
    assert "expected MAC" in payload["error"]["message"]
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_allows_entry_without_identity_and_reports_partial_verification() -> (
    None
):
    """An offline entry may proceed, but new identity cannot prove continuity."""
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "state": "setup_retry",
        "supports_reconfigure": True,
    }
    after = {
        **before,
        "state": "loaded",
        "unique_id": "new-device-identity",
    }
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[before, after])
    client.list_entity_registry = AsyncMock(
        side_effect=[
            [],
            [
                {
                    "entity_id": "switch.offline",
                    "config_entry_id": "offline-entry",
                    "device_id": "new-device",
                }
            ],
        ]
    )
    client.list_device_registry = AsyncMock(
        side_effect=[
            [],
            [
                {
                    "id": "new-device",
                    "config_entries": ["offline-entry"],
                }
            ],
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-offline-no-identity",
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
        config={"host": "10.0.50.187"},
    )

    assert result["status"] == "applied_but_unverified"
    assert result["verification"]["identity_verification"] == "partial"
    assert result["verification"]["device_id_verification"] == (
        "available_after_change"
    )
    client.start_reconfigure_flow.assert_awaited_once_with("shelly", "offline-entry")


@pytest.mark.asyncio
async def test_reconfigure_rejects_cleared_unique_id_after_commit(
    reconfig_entry: dict[str, object],
) -> None:
    """A post-flow missing identifier is an identity change, not a soft warning."""
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

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "entry-123",
            config={"host": "10.0.50.186"},
        )

    payload = json.loads(str(exc_info.value))
    assert "changed the original entry unique_id" in payload["error"]["message"]
    assert payload["status"] == "applied_but_unverified"


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
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "10.0.50.174"}
        )

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
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "10.0.50.175"}
        )

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
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "10.0.50.170"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "reconfigure" in payload["error"]["message"].lower()
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_requires_explicit_confirmation(
    reconfig_entry: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP tool performs preflight but never writes without confirm=True."""
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_global_settings",
        lambda: pytest.fail("preflight must not resolve auto-backup settings"),
    )
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()
    tools = IntegrationTools(client)

    preview = await tools.ha_set_integration(
        entry_id="entry-123",
        reconfigure=True,
        config={},
    )

    assert preview["success"] is True
    assert preview["preview"] is True
    assert preview["status"] == "preview"
    assert isinstance(preview["confirm_token"], str)
    assert preview["confirm_token"].startswith("sha256:")
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_missing_confirmation_token(
    reconfig_entry: dict[str, object],
) -> None:
    """A mutating confirmation cannot bypass the preflight token."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            config={"host": "10.0.50.171"},
            confirm=True,
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "confirmation_required"
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_stale_confirmation_token(
    reconfig_entry: dict[str, object],
) -> None:
    """A changed entry invalidates a previously issued preflight token."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(
        side_effect=[reconfig_entry, {**reconfig_entry, "title": "changed"}]
    )
    client.start_reconfigure_flow = AsyncMock()
    tools = IntegrationTools(client)

    preview = await tools.ha_set_integration(
        entry_id="entry-123",
        reconfigure=True,
        config={"host": "10.0.50.171"},
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            config={"host": "10.0.50.171"},
            confirm=True,
            confirm_token=preview["confirm_token"],
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "stale_preflight"
    assert payload["preview"] is True
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("backup_capture_fails", [False, True])
async def test_confirmed_reconfigure_uses_normal_auto_backup_policy(
    reconfig_entry: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    backup_capture_fails: bool,
) -> None:
    """Confirmed reconfigure snapshots before applying without a mandatory gate."""
    from ha_mcp.config import reset_global_settings
    from ha_mcp.utils.data_paths import get_data_dir

    config_dir = tmp_path / "config"
    backup_dir = tmp_path / "backups"
    config_dir.mkdir()
    (config_dir / "backup_settings.json").write_text(
        json.dumps(
            {
                "enable_auto_backup": True,
                "auto_backup_dir": str(backup_dir),
            }
        )
    )
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(backup_dir))
    get_data_dir.cache_clear()
    reset_global_settings()
    from ha_mcp.config import get_global_settings

    settings = get_global_settings()
    assert settings.enable_auto_backup is True
    assert settings.auto_backup_dir == str(backup_dir)

    async def fake_backup_ws(_client: Any, message: dict[str, Any]) -> Any:
        assert message == {"type": "config_entries/get"}
        if backup_capture_fails:
            raise HomeAssistantConnectionError("transient backup capture failure")
        return [
            {
                "entry_id": "entry-123",
                "domain": "shelly",
                "data": {"host": "10.0.50.170", "port": 80},
            }
        ]

    monkeypatch.setattr("ha_mcp.backup_manager._ws_send", fake_backup_ws)
    from ha_mcp.backup_manager import BackupManager

    original_maybe_snapshot = BackupManager.maybe_snapshot
    observed_mandatory: list[Any] = []

    async def checked_maybe_snapshot(self: Any, *args: Any, **kwargs: Any) -> Any:
        observed_mandatory.append(kwargs.get("mandatory", "<missing>"))
        return await original_maybe_snapshot(self, *args, **kwargs)

    monkeypatch.setattr(BackupManager, "maybe_snapshot", checked_maybe_snapshot)

    client = MagicMock()
    client.base_url = "http://homeassistant.local"
    client.token = "test-token"
    client.verify_ssl = True
    client.get_config_entry = AsyncMock(return_value=dict(reconfig_entry))
    client.list_entity_registry = AsyncMock(return_value=[])
    client.list_device_registry = AsyncMock(return_value=[])
    client.list_config_entries = AsyncMock(return_value=[dict(reconfig_entry)])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-123",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    try:
        tools = IntegrationTools(client)
        preview = await tools.ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            config={"host": "10.0.50.171"},
        )
        result = await tools.ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            config={"host": "10.0.50.171"},
            confirm=True,
            confirm_token=preview["confirm_token"],
        )
    finally:
        reset_global_settings()
        get_data_dir.cache_clear()

    snapshots = list(backup_dir.glob("integration.*.yaml"))
    assert len(snapshots) == (0 if backup_capture_fails else 1)
    assert observed_mandatory == [False]
    if not backup_capture_fails:
        assert "entry-123" in snapshots[0].read_text()
    assert result["status"] == "applied_and_verified"
    client.start_reconfigure_flow.assert_awaited_once_with("shelly", "entry-123")
    client.submit_config_flow_step.assert_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_non_object_config(
    reconfig_entry: dict[str, object],
) -> None:
    """The generic flow payload must be an object."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config=cast(Any, ["invalid"])
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "config" in payload["error"]["message"].lower()
    client.get_config_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_stops_when_registry_transport_is_unavailable() -> None:
    """A dead registry transport must not disable duplicate protection."""
    before = {
        "entry_id": "offline-entry",
        "domain": "shelly",
        "supports_reconfigure": True,
        "unique_id": "device-1",
    }
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_entity_registry = AsyncMock(
        side_effect=HomeAssistantConnectionError("registry unavailable")
    )
    client.list_device_registry = AsyncMock(return_value=[])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(HomeAssistantConnectionError):
        await reconfigure_config_entry(
            client, "offline-entry", config={"host": "10.0.50.185"}
        )

    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_malformed_registry_response() -> None:
    """A malformed registry payload cannot be treated as an empty registry."""
    before = {
        "entry_id": "malformed-registry-entry",
        "domain": "shelly",
        "supports_reconfigure": True,
        "unique_id": "device-1",
    }
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=before)
    client.list_entity_registry = AsyncMock(return_value={"error": "degraded"})
    client.list_device_registry = AsyncMock(return_value=[])
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(HomeAssistantAPIError):
        await reconfigure_config_entry(
            client,
            "malformed-registry-entry",
            config={"host": "10.0.50.186"},
        )

    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_rejects_expected_unique_id_cleared_after_flow(
    reconfig_entry: dict[str, object],
) -> None:
    """An explicitly expected unique_id cannot disappear after commit."""
    after = {key: value for key, value in reconfig_entry.items() if key != "unique_id"}
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[reconfig_entry, after])
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-cleared-unique-id",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "entry-123",
            config={"host": "10.0.50.186"},
            expected_unique_id="AA:BB:CC:DD:EE:FF",
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "applied_but_unverified"
    assert "changed the original entry unique_id" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_reconfigure_reads_mac_from_device_connections(
    reconfig_entry: dict[str, object],
) -> None:
    """Expected MAC validation includes device registry connections."""
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
                "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
                "identifiers": [],
            }
        ]
    )
    client.start_reconfigure_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client,
            "entry-123",
            config={"host": "10.0.50.187"},
            expected_mac="11:22:33:44:55:66",
        )

    payload = json.loads(str(exc_info.value))
    assert "expected MAC" in payload["error"]["message"]
    client.start_reconfigure_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconfigure_seeds_device_identity_without_entities() -> None:
    """A device linked directly to an entry remains an identity anchor without entities."""
    before = {
        "entry_id": "device-only-entry",
        "domain": "shelly",
        "supports_reconfigure": True,
        "state": "setup_retry",
    }
    after = {**before, "state": "loaded"}
    client = MagicMock()
    client.get_config_entry = AsyncMock(side_effect=[before, after])
    client.list_entity_registry = AsyncMock(return_value=[])
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "device-only",
                "config_entries": ["device-only-entry"],
                "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
            }
        ]
    )
    client.list_config_entries = AsyncMock(return_value=[after])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-device-only",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "abort", "reason": "reconfigure_successful"}
    )

    result = await reconfigure_config_entry(
        client,
        "device-only-entry",
        config={"host": "10.0.50.188"},
        expected_device_id="device-only",
        expected_mac="AA:BB:CC:DD:EE:FF",
    )

    assert result["success"] is True


@pytest.mark.asyncio
async def test_reconfigure_create_entry_is_applied_but_unverified(
    reconfig_entry: dict[str, object],
) -> None:
    """A reconfigure flow must reject create_entry instead of reporting success."""
    client = MagicMock()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.list_config_entries = AsyncMock(return_value=[reconfig_entry])
    client.start_reconfigure_flow = AsyncMock(
        return_value={
            "flow_id": "flow-created-entry",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_flow_step = AsyncMock(
        return_value={"type": "create_entry", "result": {"entry_id": "new-entry"}}
    )

    with pytest.raises(ToolError) as exc_info:
        await reconfigure_config_entry(
            client, "entry-123", config={"host": "10.0.50.189"}
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "applied_but_unverified"
    assert payload["rollback"]["strategy"] == "official_reconfigure_flow"


@pytest.mark.asyncio
async def test_subentry_reconfigure_rejects_unconsumed_values() -> None:
    """Subentry reconfigure aborts enforce the same consumed-key contract."""
    client = MagicMock()
    with pytest.raises(ToolError) as exc_info:
        await _handle_config_subentry_flow_steps(
            client,
            "flow-subentry",
            {"type": "abort", "reason": "reconfigure_successful"},
            {"unexpected": True},
            is_reconfigure=True,
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_subentry_reconfigure_step_budget_aborts_pending_flow() -> None:
    """Subentry budget exhaustion aborts the pending official flow too."""
    client = MagicMock()
    client.start_config_subentry_flow = AsyncMock(
        return_value={
            "flow_id": "flow-subentry-budget",
            "type": "form",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.submit_config_subentry_flow_step = AsyncMock(
        return_value={
            "flow_id": "flow-subentry-budget",
            "type": "form",
            "step_id": "reconfigure",
            "data_schema": [{"name": "host", "required": True}],
        }
    )
    client.abort_config_subentry_flow = AsyncMock()

    with pytest.raises(ToolError) as exc_info:
        await set_config_subentry(
            client,
            "entry-123",
            "network",
            {"host": "10.0.50.190"},
            subentry_id="subentry-123",
        )

    payload = json.loads(str(exc_info.value))
    assert payload["status"] == "applied_but_unverified"
    client.abort_config_subentry_flow.assert_awaited_once_with("flow-subentry-budget")
