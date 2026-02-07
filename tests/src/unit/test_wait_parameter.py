"""
Unit tests for the `wait` parameter on config and service tools (issue #381).

Tests that the wait parameter is accepted, defaults to True, and
controls whether tools poll for completion.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError


class TestAutomationWaitParameter:
    """Test wait parameter on automation config tools."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.upsert_automation_config = AsyncMock(
            return_value={
                "unique_id": "12345",
                "entity_id": "automation.test",
                "result": "ok",
                "operation": "created",
            }
        )
        client.delete_automation_config = AsyncMock(
            return_value={
                "identifier": "automation.test",
                "unique_id": "12345",
                "result": "ok",
                "operation": "deleted",
            }
        )
        client.get_entity_state = AsyncMock(
            return_value={"state": "on", "entity_id": "automation.test"}
        )
        client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "automation.test",
                    "state": "on",
                    "attributes": {"id": "12345", "friendly_name": "Test"},
                }
            ]
        )
        return client

    @pytest.fixture
    def register_tools(self, mock_client):
        from ha_mcp.tools.tools_config_automations import register_config_automation_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool
        register_config_automation_tools(mock_mcp, mock_client)
        return registered_tools

    async def test_set_automation_wait_default_true(self, register_tools, mock_client):
        """wait defaults to True and polls for entity registration."""
        with patch("ha_mcp.tools.tools_config_automations.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = True
            result = await register_tools["ha_config_set_automation"](
                config={
                    "alias": "Test",
                    "trigger": [{"platform": "time", "at": "07:00:00"}],
                    "action": [{"service": "light.turn_on"}],
                },
            )
            assert result["success"] is True
            mock_wait.assert_called_once()

    async def test_set_automation_wait_false_skips_polling(self, register_tools, mock_client):
        """wait=False skips polling."""
        with patch("ha_mcp.tools.tools_config_automations.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_config_set_automation"](
                config={
                    "alias": "Test",
                    "trigger": [{"platform": "time", "at": "07:00:00"}],
                    "action": [{"service": "light.turn_on"}],
                },
                wait=False,
            )
            assert result["success"] is True
            mock_wait.assert_not_called()

    async def test_set_automation_wait_timeout_adds_warning(self, register_tools, mock_client):
        """When wait times out, a warning is added to the response."""
        with patch("ha_mcp.tools.tools_config_automations.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = False
            result = await register_tools["ha_config_set_automation"](
                config={
                    "alias": "Test",
                    "trigger": [{"platform": "time", "at": "07:00:00"}],
                    "action": [{"service": "light.turn_on"}],
                },
            )
            assert result["success"] is True
            assert "warning" in result

    async def test_remove_automation_wait_default_true(self, register_tools, mock_client):
        """wait defaults to True for removal and polls for entity removal."""
        with patch("ha_mcp.tools.tools_config_automations.wait_for_entity_removed", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = True
            result = await register_tools["ha_config_remove_automation"](
                identifier="automation.test",
            )
            assert result["success"] is True
            mock_wait.assert_called_once()

    async def test_remove_automation_wait_false_skips_polling(self, register_tools, mock_client):
        """wait=False skips removal polling."""
        with patch("ha_mcp.tools.tools_config_automations.wait_for_entity_removed", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_config_remove_automation"](
                identifier="automation.test",
                wait=False,
            )
            assert result["success"] is True
            mock_wait.assert_not_called()

    async def test_remove_automation_by_unique_id_still_waits(self, register_tools, mock_client):
        """wait=True works even when identifier is a unique_id, not an entity_id."""
        with patch("ha_mcp.tools.tools_config_automations.wait_for_entity_removed", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = True
            result = await register_tools["ha_config_remove_automation"](
                identifier="12345",  # unique_id, not entity_id
            )
            assert result["success"] is True
            # Should resolve unique_id to entity_id via get_states and still wait
            mock_wait.assert_called_once()
            # Verify it resolved to the correct entity_id
            call_args = mock_wait.call_args
            assert call_args[0][1] == "automation.test"

    async def test_remove_automation_get_states_failure_skips_wait(self, register_tools, mock_client):
        """When get_states fails, wait is skipped but deletion still succeeds."""
        mock_client.get_states.side_effect = Exception("connection error")
        with patch("ha_mcp.tools.tools_config_automations.wait_for_entity_removed", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_config_remove_automation"](
                identifier="12345",  # unique_id, can't resolve without get_states
            )
            assert result["success"] is True
            # wait should be skipped because entity_id_for_wait is None
            mock_wait.assert_not_called()

    async def test_set_automation_wait_exception_still_succeeds(self, register_tools, mock_client):
        """Wait exception doesn't collapse the successful create operation."""
        with patch("ha_mcp.tools.tools_config_automations.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            mock_wait.side_effect = HomeAssistantConnectionError("network down")
            result = await register_tools["ha_config_set_automation"](
                config={
                    "alias": "Test",
                    "trigger": [{"platform": "time", "at": "07:00:00"}],
                    "action": [{"service": "light.turn_on"}],
                },
            )
            assert result["success"] is True
            assert "warning" in result


class TestScriptWaitParameter:
    """Test wait parameter on script config tools."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.upsert_script_config = AsyncMock(
            return_value={"success": True, "script_id": "test_script"}
        )
        client.delete_script_config = AsyncMock(
            return_value={"success": True, "script_id": "test_script"}
        )
        client.get_entity_state = AsyncMock(
            return_value={"state": "off", "entity_id": "script.test_script"}
        )
        return client

    @pytest.fixture
    def register_tools(self, mock_client):
        from ha_mcp.tools.tools_config_scripts import register_config_script_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool
        register_config_script_tools(mock_mcp, mock_client)
        return registered_tools

    async def test_set_script_wait_default_true(self, register_tools, mock_client):
        """wait defaults to True and polls for entity registration."""
        with patch("ha_mcp.tools.tools_config_scripts.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = True
            result = await register_tools["ha_config_set_script"](
                script_id="test_script",
                config={"alias": "Test", "sequence": [{"delay": {"seconds": 1}}]},
            )
            assert result["success"] is True
            mock_wait.assert_called_once()

    async def test_set_script_wait_false_skips_polling(self, register_tools, mock_client):
        """wait=False skips polling."""
        with patch("ha_mcp.tools.tools_config_scripts.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_config_set_script"](
                script_id="test_script",
                config={"alias": "Test", "sequence": [{"delay": {"seconds": 1}}]},
                wait=False,
            )
            assert result["success"] is True
            mock_wait.assert_not_called()

    async def test_remove_script_wait_default_true(self, register_tools, mock_client):
        """wait defaults to True for removal."""
        with patch("ha_mcp.tools.tools_config_scripts.wait_for_entity_removed", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = True
            result = await register_tools["ha_config_remove_script"](
                script_id="test_script",
            )
            assert result["success"] is True
            mock_wait.assert_called_once()

    async def test_remove_script_wait_false_skips_polling(self, register_tools, mock_client):
        """wait=False skips removal polling."""
        with patch("ha_mcp.tools.tools_config_scripts.wait_for_entity_removed", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_config_remove_script"](
                script_id="test_script",
                wait=False,
            )
            assert result["success"] is True
            mock_wait.assert_not_called()


class TestHelperWaitParameter:
    """Test wait parameter on helper config tools."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"id": "abc123", "entity_id": "input_boolean.test"},
            }
        )
        client.get_entity_state = AsyncMock(
            return_value={"state": "off", "entity_id": "input_boolean.test"}
        )
        return client

    @pytest.fixture
    def register_tools(self, mock_client):
        from ha_mcp.tools.tools_config_helpers import register_config_helper_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool
        register_config_helper_tools(mock_mcp, mock_client)
        return registered_tools

    async def test_set_helper_wait_default_true(self, register_tools, mock_client):
        """wait defaults to True and polls for entity registration."""
        with patch("ha_mcp.tools.tools_config_helpers.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = True
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
            )
            assert result["success"] is True
            mock_wait.assert_called_once()

    async def test_set_helper_wait_false_skips_polling(self, register_tools, mock_client):
        """wait=False skips polling."""
        with patch("ha_mcp.tools.tools_config_helpers.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
                wait=False,
            )
            assert result["success"] is True
            mock_wait.assert_not_called()

    async def test_set_helper_wait_string_true(self, register_tools, mock_client):
        """wait='true' (string) is coerced to True."""
        with patch("ha_mcp.tools.tools_config_helpers.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = True
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
                wait="true",
            )
            assert result["success"] is True
            mock_wait.assert_called_once()

    async def test_set_helper_wait_string_false(self, register_tools, mock_client):
        """wait='false' (string) is coerced to False."""
        with patch("ha_mcp.tools.tools_config_helpers.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
                wait="false",
            )
            assert result["success"] is True
            mock_wait.assert_not_called()

    async def test_update_helper_wait_default_true(self, register_tools, mock_client):
        """UPDATE path: wait defaults to True and polls for entity registration."""
        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": {"entity_entry": {"entity_id": "input_boolean.test"}},
        }
        with patch("ha_mcp.tools.tools_config_helpers.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = True
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
                helper_id="test",  # triggers UPDATE path
            )
            assert result["success"] is True
            assert result["action"] == "update"
            mock_wait.assert_called_once()

    async def test_update_helper_wait_false_skips_polling(self, register_tools, mock_client):
        """UPDATE path: wait=False skips polling."""
        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": {"entity_entry": {"entity_id": "input_boolean.test"}},
        }
        with patch("ha_mcp.tools.tools_config_helpers.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
                helper_id="test",
                wait=False,
            )
            assert result["success"] is True
            assert result["action"] == "update"
            mock_wait.assert_not_called()

    async def test_remove_helper_wait_default_true(self, register_tools, mock_client):
        """wait defaults to True for helper removal."""
        # Setup: entity registry returns unique_id
        mock_client.send_websocket_message.side_effect = [
            # Registry get
            {"success": True, "result": {"unique_id": "abc123"}},
            # Delete
            {"success": True},
        ]
        with patch("ha_mcp.tools.tools_config_helpers.wait_for_entity_removed", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = True
            result = await register_tools["ha_config_remove_helper"](
                helper_type="input_boolean",
                helper_id="test",
            )
            assert result["success"] is True
            mock_wait.assert_called_once()

    async def test_remove_helper_wait_false_skips_polling(self, register_tools, mock_client):
        """wait=False skips removal polling."""
        mock_client.send_websocket_message.side_effect = [
            # Registry get
            {"success": True, "result": {"unique_id": "abc123"}},
            # Delete
            {"success": True},
        ]
        with patch("ha_mcp.tools.tools_config_helpers.wait_for_entity_removed", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_config_remove_helper"](
                helper_type="input_boolean",
                helper_id="test",
                wait=False,
            )
            assert result["success"] is True
            mock_wait.assert_not_called()

    async def test_set_helper_wait_exception_still_succeeds(self, register_tools, mock_client):
        """Wait exception doesn't collapse the successful create operation."""
        with patch("ha_mcp.tools.tools_config_helpers.wait_for_entity_registered", new_callable=AsyncMock) as mock_wait:
            mock_wait.side_effect = HomeAssistantConnectionError("network down")
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
            )
            assert result["success"] is True
            assert "warning" in result.get("helper_data", {})


class TestServiceCallWaitParameter:
    """Test wait parameter on ha_call_service."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.call_service = AsyncMock(return_value=[])
        client.get_entity_state = AsyncMock(
            return_value={"state": "on", "entity_id": "light.test"}
        )
        return client

    @pytest.fixture
    def mock_device_tools(self):
        return MagicMock()

    @pytest.fixture
    def register_tools(self, mock_client, mock_device_tools):
        from ha_mcp.tools.tools_service import register_service_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool
        register_service_tools(mock_mcp, mock_client, device_tools=mock_device_tools)
        return registered_tools

    async def test_call_service_wait_default_for_state_changing(self, register_tools, mock_client):
        """wait defaults to True and verifies state for state-changing services."""
        with patch("ha_mcp.tools.tools_service.wait_for_state_change", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = {"state": "on", "entity_id": "light.test"}
            result = await register_tools["ha_call_service"](
                domain="light",
                service="turn_on",
                entity_id="light.test",
            )
            assert result["success"] is True
            assert result.get("verified_state") == "on"
            mock_wait.assert_called_once()

    async def test_call_service_wait_false_skips_verification(self, register_tools, mock_client):
        """wait=False skips state verification."""
        with patch("ha_mcp.tools.tools_service.wait_for_state_change", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_call_service"](
                domain="light",
                service="turn_on",
                entity_id="light.test",
                wait=False,
            )
            assert result["success"] is True
            assert "verified_state" not in result
            mock_wait.assert_not_called()

    async def test_call_service_no_wait_for_trigger(self, register_tools, mock_client):
        """Non-state-changing services like trigger don't wait even with wait=True."""
        with patch("ha_mcp.tools.tools_service.wait_for_state_change", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_call_service"](
                domain="automation",
                service="trigger",
                entity_id="automation.test",
            )
            assert result["success"] is True
            mock_wait.assert_not_called()

    async def test_call_service_no_wait_without_entity(self, register_tools, mock_client):
        """Services without entity_id don't wait."""
        with patch("ha_mcp.tools.tools_service.wait_for_state_change", new_callable=AsyncMock) as mock_wait:
            result = await register_tools["ha_call_service"](
                domain="light",
                service="turn_on",
            )
            assert result["success"] is True
            mock_wait.assert_not_called()

    async def test_call_service_wait_timeout_adds_warning(self, register_tools, mock_client):
        """When state verification times out, a warning is added."""
        with patch("ha_mcp.tools.tools_service.wait_for_state_change", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = None  # timeout
            result = await register_tools["ha_call_service"](
                domain="light",
                service="turn_on",
                entity_id="light.test",
            )
            assert result["success"] is True
            assert "warning" in result

    async def test_call_service_toggle_waits(self, register_tools, mock_client):
        """toggle is a state-changing service and triggers wait."""
        with patch("ha_mcp.tools.tools_service.wait_for_state_change", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = {"state": "on", "entity_id": "light.test"}
            result = await register_tools["ha_call_service"](
                domain="light",
                service="toggle",
                entity_id="light.test",
            )
            assert result["success"] is True
            mock_wait.assert_called_once()
            # toggle has no mapping in _SERVICE_TO_STATE, so expected_state should be None
            call_kwargs = mock_wait.call_args
            assert call_kwargs[1].get("expected_state") is None or call_kwargs[0][2] is None

    async def test_call_service_wait_exception_still_succeeds(self, register_tools, mock_client):
        """Wait exception doesn't collapse the successful service call."""
        with patch("ha_mcp.tools.tools_service.wait_for_state_change", new_callable=AsyncMock) as mock_wait:
            mock_wait.side_effect = HomeAssistantConnectionError("network down")
            result = await register_tools["ha_call_service"](
                domain="light",
                service="turn_on",
                entity_id="light.test",
            )
            assert result["success"] is True
            assert "warning" in result
