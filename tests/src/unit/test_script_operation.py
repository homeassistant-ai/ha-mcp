"""Unit tests for script operation reporting ('created' vs 'updated').

Verifies that ha_config_set_script accurately reports:
- 'created' when creating a new script
- 'updated' when updating an existing script (full config, with/without hash,
  python_transform, or blueprint take-control)
And that rest_client.upsert_script_config does not inject a bogus 'created'
heuristic.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient
from ha_mcp.tools.blueprint_substitute import TakenControl
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools


@pytest.fixture
def mock_client():
    client = MagicMock(spec=HomeAssistantClient)
    client.upsert_script_config = AsyncMock(
        side_effect=lambda config, sid, resolved_id=None: {
            "success": True,
            "script_id": resolved_id or sid,
            "result": "ok",
        }
    )
    client.get_services = AsyncMock(return_value=[])
    client.get_states = AsyncMock(return_value=[])
    client.get_entity_state = AsyncMock(
        return_value={"state": "off", "entity_id": "script.test_script"}
    )
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": []}
    )
    return client


@pytest.fixture
def tools(mock_client):
    return ConfigScriptTools(mock_client)


class TestScriptOperationReporting:
    """Test ha_config_set_script 'operation' return field."""

    async def test_create_new_script_reports_created(self, tools, mock_client):
        """Creating a new script (404 on get) returns operation='created'."""
        mock_client.get_script_config = AsyncMock(
            side_effect=HomeAssistantAPIError("Script not found", status_code=404)
        )

        res = await tools.ha_config_set_script(
            script_id="new_script",
            config={"alias": "New Script", "sequence": [{"delay": 1}]},
            wait=False,
        )

        assert res["success"] is True
        assert res["operation"] == "created"
        assert res["script_id"] == "new_script"

    async def test_update_existing_script_without_hash_reports_updated(
        self, tools, mock_client
    ):
        """Updating an existing script without config_hash returns operation='updated'."""
        mock_client.get_script_config = AsyncMock(
            return_value={
                "success": True,
                "script_id": "my_script",
                "config": {"alias": "My Script", "sequence": [{"delay": 1}]},
            }
        )

        res = await tools.ha_config_set_script(
            script_id="my_script",
            config={"alias": "Updated Script", "sequence": [{"delay": 2}]},
            wait=False,
        )

        assert res["success"] is True
        assert res["operation"] == "updated"

    async def test_update_existing_script_with_hash_reports_updated(
        self, tools, mock_client
    ):
        """Updating an existing script with config_hash returns operation='updated'."""
        tools._fetch_and_verify_hash = AsyncMock(
            return_value=({"sequence": [{"delay": 1}]}, "resolved_storage_key")
        )

        res = await tools.ha_config_set_script(
            script_id="my_script",
            config={"alias": "Updated Script", "sequence": [{"delay": 2}]},
            config_hash="matching_hash",
            wait=False,
        )

        assert res["success"] is True
        assert res["operation"] == "updated"
        tools._fetch_and_verify_hash.assert_awaited_once_with(
            "my_script", "matching_hash", "set"
        )

    async def test_update_via_python_transform_reports_updated(
        self, tools, mock_client
    ):
        """Transforming a script via python_transform returns operation='updated'."""
        tools._prepare_script_transform = AsyncMock(
            return_value=({"sequence": [{"delay": 5}]}, "storage_key")
        )
        tools._get_script_config_internal = AsyncMock(
            return_value=({}, "fresh_hash", "storage_key")
        )

        res = await tools.ha_config_set_script(
            script_id="my_script",
            python_transform="config['sequence'][0]['delay'] = 5",
            config_hash="prior_hash",
            wait=False,
        )

        assert res["success"] is True
        assert res["action"] == "python_transform"
        assert res["operation"] == "updated"
        assert "updated via Python transform" in res["message"]

    async def test_update_via_take_control_reports_updated(self, tools, mock_client):
        """Converting a blueprint script via take_control returns operation='updated'."""
        tools._take_control_config = AsyncMock(
            return_value=TakenControl(
                config={"alias": "Converted", "sequence": [{"delay": 1}]},
                blueprint_path="test_bp.yaml",
                config_hash="bp_hash",
            )
        )
        tools._fetch_and_verify_hash = AsyncMock(
            return_value=({"sequence": [{"delay": 1}]}, "resolved_key")
        )

        res = await tools.ha_config_set_script(
            script_id="bp_script",
            take_control_of_blueprint=True,
            wait=False,
        )

        assert res["success"] is True
        assert res["operation"] == "updated"
        assert res["took_control_of_blueprint"] == "test_bp.yaml"

    async def test_get_script_config_non_404_error_propagates(self, tools, mock_client):
        """Non-404 API error during existence check wraps into structured ToolError."""
        mock_client.get_script_config = AsyncMock(
            side_effect=HomeAssistantAPIError("Internal Server Error", status_code=500)
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="test_script",
                config={"sequence": [{"delay": 1}]},
                wait=False,
            )

        assert "Internal Server Error" in str(exc_info.value)


class TestRestClientUpsertScriptConfigOperation:
    """Test rest_client.upsert_script_config returns clean result without bogus operation."""

    async def test_upsert_script_does_not_return_misleading_operation(self):
        with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
            client = HomeAssistantClient()
            client.base_url = "http://test.local:8123"
            client.token = "test-token"
            client.timeout = 30
            client.httpx_client = MagicMock()
            client.send_websocket_message = AsyncMock(
                side_effect=Exception("WebSocket not available in tests")
            )
            client._request = AsyncMock(return_value={"result": "ok"})

            res = await client.upsert_script_config(
                {"sequence": [{"delay": 1}]},
                "test_script",
            )

            assert res["success"] is True
            assert res["script_id"] == "test_script"
            assert res["result"] == "ok"
            assert "operation" not in res
