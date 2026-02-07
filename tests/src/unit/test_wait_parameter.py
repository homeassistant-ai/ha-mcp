"""
Unit tests for the `wait` parameter on config and service tools (issue #381).

Tests that the wait parameter is accepted, defaults to True, and
controls whether tools poll for completion.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
