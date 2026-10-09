"""ha_remove_helpers_integrations reports a failed removal check after a delete
that succeeded as a warning, as the sibling delete tools do."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.tools_integrations import IntegrationTools


class TestRemovalCheckAfterSuccessfulDelete:
    @pytest.fixture(autouse=True)
    def _immediate_registry_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ha_mcp.tools.tools_integrations._REGISTRY_RETRY_BASE_DELAY", 0
        )

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        client = MagicMock()
        client.get_entity_state = AsyncMock(return_value={"state": "on"})
        client.send_websocket_message = AsyncMock()
        client.delete_config_entry = AsyncMock(return_value={"require_restart": False})
        return client

    @pytest.fixture
    def tools(self, mock_client: MagicMock) -> IntegrationTools:
        return IntegrationTools(mock_client)

    async def test_flow_path_wait_true_reports_a_failed_check_as_a_warning(
        self, tools, mock_client
    ):
        """FLOW utility_meter wait=True: the entry is already deleted, so a
        connection error while checking one sub-entity is a warning on a
        successful delete, and that entity is not reported as still present."""
        mock_client.send_websocket_message.side_effect = [
            {
                "success": True,
                "result": {"platform": "utility_meter", "config_entry_id": "entry_um"},
            },
            {
                "success": True,
                "result": [
                    {
                        "entity_id": "sensor.energy_peak",
                        "config_entry_id": "entry_um",
                    },
                    {
                        "entity_id": "sensor.energy_offpeak",
                        "config_entry_id": "entry_um",
                    },
                ],
            },
        ]
        mock_client.delete_config_entry.return_value = {"require_restart": False}
        with patch(
            "ha_mcp.tools.tools_integrations.wait_for_entity_removed",
            new_callable=AsyncMock,
        ) as mock_wait:
            mock_wait.side_effect = [True, HomeAssistantConnectionError("down")]
            result = await tools.ha_remove_helpers_integrations(
                target="sensor.energy_peak",
                helper_type="utility_meter",
                confirm=True,
                wait=True,
            )
        assert result["success"] is True
        assert result["warnings"] == [
            "Deletion confirmed but removal verification failed: down"
        ]
        assert mock_wait.await_count == 2

    async def test_simple_path_wait_true_reports_a_failed_check_as_a_warning(
        self, tools, mock_client
    ):
        """SIMPLE standard wait=True: the helper is already deleted, so a
        connection error while checking its removal is a warning on a
        successful delete, as in the sibling delete tools, not an error."""
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"unique_id": "uid-w3"}},
            {"success": True},
        ]
        mock_client.get_entity_state.return_value = {"state": "off"}
        with patch(
            "ha_mcp.tools.ws_waiters.wait_for_entity_removed",
            new_callable=AsyncMock,
        ) as mock_wait:
            mock_wait.side_effect = HomeAssistantConnectionError(
                "network down during poll"
            )
            result = await tools.ha_remove_helpers_integrations(
                target="my_button",
                helper_type="input_button",
                confirm=True,
                wait=True,
            )
        assert result["success"] is True
        assert result["warnings"] == [
            "Deletion confirmed but removal verification failed: "
            "network down during poll"
        ]
