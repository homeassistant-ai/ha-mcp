"""
Unit tests for src/ha_mcp/tools/tools_integrations.py module-level helpers.

Phase 2 of #1007: Adds _get_entry_id_for_flow_helper, the lookup helper that
resolves a flow-helper entity_id to its config_entry_id via the
config/entity_registry/get WebSocket API.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_integrations import (
    IntegrationTools,
    _get_entry_id_for_flow_helper,
)


def _make_client(
    ws_response: Any = None, raises: Exception | None = None
) -> MagicMock:
    """Build a mock client whose send_websocket_message returns / raises."""
    client = MagicMock()
    if raises is not None:
        client.send_websocket_message = AsyncMock(side_effect=raises)
    else:
        client.send_websocket_message = AsyncMock(return_value=ws_response)
    return client


class TestGetEntryIdForFlowHelper:
    """Unit tests for the flow-helper entry_id lookup."""

    async def test_returns_entry_id_for_full_entity_id(self) -> None:
        client = _make_client(
            {"success": True, "result": {"config_entry_id": "abc123"}}
        )
        result = await _get_entry_id_for_flow_helper(
            client, "utility_meter", "sensor.peak"
        )
        assert result == "abc123"

    async def test_completes_bare_id_with_helper_type(self) -> None:
        client = _make_client(
            {"success": True, "result": {"config_entry_id": "def456"}}
        )
        result = await _get_entry_id_for_flow_helper(
            client, "template", "my_sensor"
        )
        assert result == "def456"
        sent = client.send_websocket_message.await_args.args[0]
        assert sent["entity_id"] == "template.my_sensor"

    async def test_returns_none_for_unknown_helper_type(self) -> None:
        client = _make_client()
        result = await _get_entry_id_for_flow_helper(
            client, "input_button", "my_button"  # SIMPLE, not FLOW
        )
        assert result is None
        client.send_websocket_message.assert_not_awaited()

    async def test_returns_none_when_entity_not_in_registry(self) -> None:
        client = _make_client({"success": False, "error": "not_found"})
        result = await _get_entry_id_for_flow_helper(
            client, "template", "template.ghost"
        )
        assert result is None

    async def test_returns_none_when_entity_has_no_config_entry_id(self) -> None:
        # YAML-defined helper: entity exists but no config_entry_id
        client = _make_client(
            {"success": True, "result": {"entity_id": "template.x"}}
        )
        result = await _get_entry_id_for_flow_helper(
            client, "template", "template.x"
        )
        assert result is None

    async def test_websocket_exception_appends_to_warnings(self) -> None:
        client = _make_client(raises=ConnectionError("ws drop"))
        warnings: list[str] = []
        result = await _get_entry_id_for_flow_helper(
            client, "utility_meter", "sensor.x", warnings=warnings
        )
        assert result is None
        assert len(warnings) == 1
        assert "entity_registry/get failed" in warnings[0]
        assert "sensor.x" in warnings[0]

    async def test_websocket_exception_without_warnings_is_silent(self) -> None:
        client = _make_client(raises=ConnectionError("ws drop"))
        result = await _get_entry_id_for_flow_helper(
            client, "utility_meter", "sensor.x", warnings=None
        )
        assert result is None

    async def test_unexpected_result_shape_returns_none(self) -> None:
        # success but result is not a dict
        client = _make_client({"success": True, "result": "garbage"})
        result = await _get_entry_id_for_flow_helper(
            client, "template", "template.x"
        )
        assert result is None


class TestDeleteHelpersIntegrations:
    """Unit tests for ha_delete_helpers_integrations.

    Covers all three routing paths (SIMPLE / FLOW / DIRECT) plus the
    confirm gate and wait flag.
    """

    @pytest.fixture
    def mock_client(self):
        """Mock Home Assistant client with all methods used by the tool."""
        client = MagicMock()
        client.get_entity_state = AsyncMock(return_value={"state": "on"})
        client.send_websocket_message = AsyncMock()
        client.delete_config_entry = AsyncMock(
            return_value={"require_restart": False}
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        return IntegrationTools(mock_client)

    # === Confirm gate ===

    async def test_confirm_false_raises_validation_error(self, tools):
        """confirm=False (default) blocks all three paths."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_delete_helpers_integrations(
                target="entry_xyz",
                # helper_type defaults to None → DIRECT path
                # confirm defaults to False
            )
        err = json.loads(str(exc_info.value))
        assert err["success"] is False
        assert err["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "confirm" in err["error"]["message"].lower()

    # === Path 3: DIRECT ===

    async def test_direct_path_happy(self, tools, mock_client):
        """helper_type=None + entry_id → direct delete."""
        mock_client.delete_config_entry.return_value = {
            "require_restart": False
        }
        result = await tools.ha_delete_helpers_integrations(
            target="01HXYZ_entry_id",
            confirm=True,
        )
        assert result["success"] is True
        assert result["method"] == "config_entry_delete"
        assert result["helper_type"] == "config_entry"
        assert result["entry_id"] == "01HXYZ_entry_id"
        assert result["entity_ids"] == []
        assert result["require_restart"] is False
        mock_client.delete_config_entry.assert_awaited_once_with(
            "01HXYZ_entry_id"
        )

    async def test_direct_path_require_restart(self, tools, mock_client):
        """require_restart=True is propagated."""
        mock_client.delete_config_entry.return_value = {
            "require_restart": True
        }
        result = await tools.ha_delete_helpers_integrations(
            target="01HXYZ_entry_id",
            confirm=True,
        )
        assert result["require_restart"] is True
        assert "restart required" in result["message"].lower()

    async def test_direct_path_entry_not_found(self, tools, mock_client):
        """404 from delete_config_entry → RESOURCE_NOT_FOUND."""
        mock_client.delete_config_entry.side_effect = Exception(
            "404 Config entry not found"
        )
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_delete_helpers_integrations(
                target="ghost_entry",
                confirm=True,
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "ghost_entry" in err["error"]["message"]

    # === Path 1: SIMPLE ===

    async def test_simple_path_standard_via_unique_id(
        self, tools, mock_client
    ):
        """Registry returns unique_id → standard delete path."""
        # First call: registry/get → success, unique_id present
        # Second call: <type>/delete → success
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"unique_id": "uid-123"}},
            {"success": True},
        ]
        # State check returns truthy → no retry needed
        mock_client.get_entity_state.return_value = {"state": "off"}

        result = await tools.ha_delete_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,  # skip wait_for_entity_removed
        )
        assert result["success"] is True
        assert result["method"] == "websocket_delete"
        assert result["unique_id"] == "uid-123"
        assert result["entity_ids"] == ["input_button.my_button"]
        # Verify the delete WS message used unique_id
        delete_call = mock_client.send_websocket_message.call_args_list[1]
        assert delete_call[0][0]["input_button_id"] == "uid-123"

    async def test_simple_path_fallback_direct_id(self, tools, mock_client):
        """Registry has no unique_id → direct_id fallback succeeds."""
        # 3 retries all return "no unique_id", then direct delete succeeds
        mock_client.send_websocket_message.side_effect = (
            [{"success": True, "result": {}}] * 3  # registry returns no uid
            + [{"success": True}]  # direct delete succeeds
        )
        result = await tools.ha_delete_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )
        assert result["success"] is True
        assert result["fallback_used"] == "direct_id"
        # Direct-id delete used helper_id (bare), not unique_id
        delete_call = mock_client.send_websocket_message.call_args_list[-1]
        assert delete_call[0][0]["input_button_id"] == "my_button"

    async def test_simple_path_fallback_already_deleted(
        self, tools, mock_client
    ):
        """Registry empty + direct delete fails + state=None → already_deleted."""
        # 3x registry no unique_id, 1x direct delete fails, then state=None
        mock_client.send_websocket_message.side_effect = (
            [{"success": True, "result": {}}] * 3
            + [{"success": False, "error": "not found"}]
        )
        # State check at the end returns None → entity already gone
        mock_client.get_entity_state.side_effect = (
            [{"state": "off"}] * 3  # during retries
            + [None]  # final check after direct-delete fail
        )

        result = await tools.ha_delete_helpers_integrations(
            target="my_button",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )
        assert result["success"] is True
        assert result["fallback_used"] == "already_deleted"

    async def test_simple_path_all_fallbacks_exhausted(
        self, tools, mock_client
    ):
        """Registry empty + direct fails + state still present → ENTITY_NOT_FOUND."""
        mock_client.send_websocket_message.side_effect = (
            [{"success": False, "error": "no entity"}] * 3
            + [{"success": False, "error": "still no"}]
        )
        # State check ALWAYS returns a state → no fallback path catches it
        mock_client.get_entity_state.return_value = {"state": "off"}

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_delete_helpers_integrations(
                target="ghost_button",
                helper_type="input_button",
                confirm=True,
                wait=False,
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "ENTITY_NOT_FOUND"

    async def test_simple_path_ws_delete_fails(self, tools, mock_client):
        """unique_id found, but {type}/delete returns success=False
        → SERVICE_CALL_FAILED."""
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"unique_id": "uid-fail"}},
            {"success": False, "error": "in use by automation"},
        ]
        mock_client.get_entity_state.return_value = {"state": "off"}

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_delete_helpers_integrations(
                target="locked_button",
                helper_type="input_button",
                confirm=True,
                wait=False,
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "in use by automation" in err["error"]["message"]

    # === Path 2: FLOW ===

    async def test_flow_path_happy_single_subentity(
        self, tools, mock_client
    ):
        """FLOW helper resolves entity_id → entry_id → delete + wait."""
        # Sequence of WS calls in order:
        # 1. _get_entry_id_for_flow_helper → registry/get → has config_entry_id
        # 2. _get_entities_for_config_entry → registry/list → 1 entity
        # 3. delete_config_entry (not WS, separate mock)
        # Then wait_for_entity_removed → state poll, returns None (gone)
        mock_client.send_websocket_message.side_effect = [
            # registry/get (lookup)
            {
                "success": True,
                "result": {"config_entry_id": "entry_abc"},
            },
            # registry/list (sub-entities)
            {
                "success": True,
                "result": [
                    {
                        "entity_id": "sensor.my_template",
                        "config_entry_id": "entry_abc",
                    },
                    # noise: another entity not in this entry
                    {
                        "entity_id": "sensor.other",
                        "config_entry_id": "entry_other",
                    },
                ],
            },
        ]
        mock_client.delete_config_entry.return_value = {
            "require_restart": False
        }

        result = await tools.ha_delete_helpers_integrations(
            target="sensor.my_template",
            helper_type="template",
            confirm=True,
            wait=False,  # skip wait phase, focus on delete logic
        )
        assert result["success"] is True
        assert result["method"] == "config_flow_delete"
        assert result["entry_id"] == "entry_abc"
        assert result["entity_ids"] == ["sensor.my_template"]
        mock_client.delete_config_entry.assert_awaited_once_with("entry_abc")

    async def test_flow_path_multi_subentity_utility_meter(
        self, tools, mock_client
    ):
        """utility_meter pattern: multiple sub-entities share one entry_id."""
        mock_client.send_websocket_message.side_effect = [
            # lookup for sensor.energy_peak
            {
                "success": True,
                "result": {"config_entry_id": "um_entry"},
            },
            # registry/list — three sub-entities for um_entry
            {
                "success": True,
                "result": [
                    {
                        "entity_id": "sensor.energy_peak",
                        "config_entry_id": "um_entry",
                    },
                    {
                        "entity_id": "sensor.energy_offpeak",
                        "config_entry_id": "um_entry",
                    },
                    {
                        "entity_id": "select.energy_tariff",
                        "config_entry_id": "um_entry",
                    },
                ],
            },
        ]
        result = await tools.ha_delete_helpers_integrations(
            target="sensor.energy_peak",
            helper_type="utility_meter",
            confirm=True,
            wait=False,
        )
        assert result["success"] is True
        assert set(result["entity_ids"]) == {
            "sensor.energy_peak",
            "sensor.energy_offpeak",
            "select.energy_tariff",
        }
        assert result["entry_id"] == "um_entry"

    async def test_flow_path_entity_not_in_registry(
        self, tools, mock_client
    ):
        """FLOW: entity_id not in registry → ENTITY_NOT_FOUND."""
        # First lookup returns success=False → entry_id resolves to None
        # Disambiguation re-query also returns success=False → ENTITY_NOT_FOUND
        mock_client.send_websocket_message.side_effect = [
            {"success": False, "error": "not found"},  # initial lookup
            {"success": False, "error": "not found"},  # disambiguation
        ]
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_delete_helpers_integrations(
                target="template.ghost",
                helper_type="template",
                confirm=True,
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "ENTITY_NOT_FOUND"
        assert "template.ghost" in err["error"]["message"]

    async def test_flow_path_yaml_helper_no_config_entry(
        self, tools, mock_client
    ):
        """FLOW: entity exists but config_entry_id is None (YAML) →
        RESOURCE_NOT_FOUND."""
        mock_client.send_websocket_message.side_effect = [
            # initial lookup: success but no config_entry_id
            {
                "success": True,
                "result": {"config_entry_id": None},
            },
            # disambiguation: confirms entity is in registry
            {
                "success": True,
                "result": {"config_entry_id": None},
            },
        ]
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_delete_helpers_integrations(
                target="template.yaml_template",
                helper_type="template",
                confirm=True,
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "storage-based" in err["error"]["message"]

    async def test_flow_path_entry_not_found_at_delete(
        self, tools, mock_client
    ):
        """FLOW: lookup succeeds but delete_config_entry returns 404
        → RESOURCE_NOT_FOUND."""
        mock_client.send_websocket_message.side_effect = [
            {
                "success": True,
                "result": {"config_entry_id": "stale_entry"},
            },
            {"success": True, "result": []},  # empty registry/list
        ]
        mock_client.delete_config_entry.side_effect = Exception(
            "404 entry not found"
        )
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_delete_helpers_integrations(
                target="sensor.stale",
                helper_type="template",
                confirm=True,
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "RESOURCE_NOT_FOUND"

    async def test_flow_path_require_restart_propagated(
        self, tools, mock_client
    ):
        """FLOW: delete_config_entry response require_restart=True is
        propagated in the tool response."""
        mock_client.send_websocket_message.side_effect = [
            {
                "success": True,
                "result": {"config_entry_id": "entry_restart"},
            },
            {
                "success": True,
                "result": [
                    {
                        "entity_id": "sensor.needs_restart",
                        "config_entry_id": "entry_restart",
                    },
                ],
            },
        ]
        mock_client.delete_config_entry.return_value = {
            "require_restart": True
        }
        result = await tools.ha_delete_helpers_integrations(
            target="sensor.needs_restart",
            helper_type="template",
            confirm=True,
            wait=False,
        )
        assert result["require_restart"] is True
