"""ha_remove_helpers_integrations reports a failed removal check after a delete
that succeeded as a warning, as the sibling delete tools do."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_integrations import IntegrationTools


class TestRemovalCheckAfterSuccessfulDelete:
    @pytest.fixture(autouse=True)
    def _immediate_registry_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ha_mcp.tools.tools_integrations._REGISTRY_RETRY_BASE_DELAY", 0
        )

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        return MagicMock(
            get_entity_state=AsyncMock(return_value={"state": "on"}),
            send_websocket_message=AsyncMock(),
            delete_config_entry=AsyncMock(return_value={"require_restart": False}),
        )

    @pytest.fixture
    def tools(self, mock_client: MagicMock) -> IntegrationTools:
        return IntegrationTools(mock_client)

    @pytest.mark.parametrize(
        ("failure", "logs_traceback"),
        [(HomeAssistantConnectionError("down"), False), (ValueError("down"), True)],
        ids=["connection", "other"],
    )
    async def test_flow_path_wait_true_reports_a_failed_check_as_a_warning(
        self,
        tools: IntegrationTools,
        mock_client: MagicMock,
        failure: Exception,
        logs_traceback: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """FLOW utility_meter wait=True: the entry is already deleted, so any
        error while checking one sub-entity is a warning on a successful delete
        that names the entity, which is not reported as still present. An
        unexpected error is also logged with its traceback."""
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
        with (
            patch(
                "ha_mcp.tools.tools_integrations.wait_for_entity_removed",
                new_callable=AsyncMock,
                side_effect=[True, failure],
            ) as mock_wait,
            caplog.at_level(logging.WARNING),
        ):
            result = await tools.ha_remove_helpers_integrations(
                target="sensor.energy_peak",
                helper_type="utility_meter",
                confirm=True,
                wait=True,
            )
        assert result["success"] is True
        assert result["warnings"] == [
            "Deletion confirmed but removal verification failed: "
            "sensor.energy_offpeak: down"
        ]
        assert mock_wait.await_count == 2
        assert [r.exc_info[1] for r in caplog.records if r.exc_info] == (
            [failure] if logs_traceback else []
        )

    @pytest.mark.parametrize(
        ("failure", "logs_traceback"),
        [
            (HomeAssistantConnectionError("network down during poll"), False),
            (HomeAssistantAuthError("network down during poll"), False),
            (ValueError("network down during poll"), True),
        ],
        ids=["connection", "auth", "other"],
    )
    async def test_simple_path_wait_true_reports_a_failed_check_as_a_warning(
        self,
        tools: IntegrationTools,
        mock_client: MagicMock,
        failure: Exception,
        logs_traceback: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SIMPLE standard wait=True: the helper is already deleted, so any
        error while checking its removal is a warning on a successful delete,
        as in the sibling delete tools, not an error. An unexpected error is
        also logged with its traceback."""
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"unique_id": "uid-w3"}},
            {"success": True},
        ]
        mock_client.get_entity_state.return_value = {"state": "off"}
        with (
            patch(
                "ha_mcp.tools.ws_waiters.wait_for_entity_removed",
                new_callable=AsyncMock,
                side_effect=failure,
            ),
            caplog.at_level(logging.WARNING),
        ):
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
        assert [r.exc_info[1] for r in caplog.records if r.exc_info] == (
            [failure] if logs_traceback else []
        )

    async def test_simple_path_wait_true_lets_cancellation_propagate(
        self, tools: IntegrationTools, mock_client: MagicMock
    ) -> None:
        """Cancelling the removal check cancels the call; it is not turned
        into a warning."""
        mock_client.send_websocket_message.side_effect = [
            {"success": True, "result": {"unique_id": "uid-w3"}},
            {"success": True},
        ]
        mock_client.get_entity_state.return_value = {"state": "off"}
        with (
            patch(
                "ha_mcp.tools.ws_waiters.wait_for_entity_removed",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await tools.ha_remove_helpers_integrations(
                target="my_button",
                helper_type="input_button",
                confirm=True,
                wait=True,
            )
