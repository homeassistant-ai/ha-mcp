"""ha_remove_helpers_integrations reports a failed removal check after a delete
that succeeded as a warning, as the sibling delete tools do.

The tool runs on the server's own client against a real ``hass``; only the
removal check, this repository's own waiter, is replaced.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
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
    async def tools(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> IntegrationTools:
        assert await async_setup_component(hass, "config", {})
        return IntegrationTools(core_client)

    @pytest.fixture
    async def my_button(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> str:
        """A stored input_button; its entity is input_button.my_button."""
        assert await async_setup_component(hass, "input_button", {})
        created = await core_client.send_websocket_message(
            {"type": "input_button/create", "name": "my_button"}
        )
        assert created["success"], created
        await hass.async_block_till_done()
        return "input_button.my_button"

    @pytest.mark.parametrize(
        ("failure", "logs_traceback"),
        [(HomeAssistantConnectionError("down"), False), (ValueError("down"), True)],
        ids=["connection", "other"],
    )
    async def test_flow_path_wait_true_reports_a_failed_check_as_a_warning(
        self,
        hass: HomeAssistant,
        tools: IntegrationTools,
        entity_registry: er.EntityRegistry,
        failure: Exception,
        logs_traceback: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """FLOW utility_meter wait=True: the entry is already deleted, so any
        error while checking one sub-entity is a warning on a successful delete
        that names the entity, which is not reported as still present. An
        unexpected error is also logged with its traceback."""
        entry = MockConfigEntry(domain="utility_meter")
        entry.add_to_hass(hass)
        for tariff in ("peak", "offpeak"):
            entity_registry.async_get_or_create(
                "sensor",
                "utility_meter",
                f"energy_{tariff}",
                config_entry=entry,
                suggested_object_id=f"energy_{tariff}",
            )
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
        my_button: str,
        failure: Exception,
        logs_traceback: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SIMPLE standard wait=True: the helper is already deleted, so any
        error while checking its removal is a warning on a successful delete,
        as in the sibling delete tools, not an error. An unexpected error is
        also logged with its traceback."""
        with (
            patch(
                "ha_mcp.tools.ws_waiters.wait_for_entity_removed",
                new_callable=AsyncMock,
                side_effect=failure,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = await tools.ha_remove_helpers_integrations(
                target=my_button,
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
        self, tools: IntegrationTools, my_button: str
    ) -> None:
        """Cancelling the removal check cancels the call; it is not turned
        into a warning."""
        with (
            patch(
                "ha_mcp.tools.ws_waiters.wait_for_entity_removed",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await tools.ha_remove_helpers_integrations(
                target=my_button,
                helper_type="input_button",
                confirm=True,
                wait=True,
            )
