"""Diagnostics tests for empty trace results."""

from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.trace_diagnostics import _gather_diagnostics


class TestGatherDiagnostics:
    """Test _gather_diagnostics function.

    Since #1813 Phase 0 the automation-config probe rides the pooled client's
    ``send_websocket_message`` (issue #5) rather than a dedicated ``ws_client``
    socket, so the config-fetch stub is ``client.send_websocket_message``.
    """

    @pytest.mark.asyncio
    async def test_entity_exists_and_enabled(self):
        """Diagnostics correctly identify existing, enabled automation."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {"config": {"trace": {"stored_traces": 5}}},
        }

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["entity_exists"] is True
        assert result["entity_enabled"] is True
        assert result["last_triggered"] == "2025-11-30T15:00:00Z"
        assert result["trace_storage_enabled"] is True
        # The config probe went through the pooled client, not a dedicated socket.
        client.send_websocket_message.assert_awaited_once_with(
            {"type": "automation/config", "entity_id": "automation.test"}
        )

    @pytest.mark.asyncio
    async def test_automation_without_id_reports_no_trace_key(self):
        """A YAML automation without an id cannot have a stored trace."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {"config": {"trace": {"stored_traces": 5}}},
        }

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["entity_exists"] is True
        assert result["trace_key_available"] is False
        assert "no unique id" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_automation_disabled(self):
        """Diagnostics correctly identify disabled automation."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "off",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": None,
            },
        }
        # Return valid config so the pooled send_websocket_message is awaited
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {"config": {"trace": {"stored_traces": 5}}},
        }

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["entity_exists"] is True
        assert result["entity_enabled"] is False
        assert "disabled" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_automation_never_triggered(self):
        """Diagnostics correctly identify automation that never triggered."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": None,
            },
        }
        # Return valid config so the pooled send_websocket_message is awaited
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {"config": {"trace": {"stored_traces": 5}}},
        }

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["entity_exists"] is True
        assert result["entity_enabled"] is True
        assert result["last_triggered"] is None
        assert "never been triggered" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_automation_not_found(self):
        """Diagnostics handle non-existent automation gracefully."""
        client = AsyncMock()
        client.get_entity_state.side_effect = Exception("Entity not found")

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["entity_exists"] is False
        assert "could not find" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_missing_entity_state_reports_not_found(self):
        """An empty state reply is reported as a missing entity, not a crash."""
        client = AsyncMock()
        client.get_entity_state.return_value = None

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["entity_exists"] is False
        assert result["entity_state"] is None
        assert result["trace_storage_enabled"] is None
        assert "could not find" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_trace_storage_disabled(self):
        """Diagnostics detect when trace storage is disabled."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {"config": {"trace": {"stored_traces": 0}}},
        }

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["trace_storage_enabled"] is False
        assert "trace storage is disabled" in result["suggestion"].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "config_response",
        [
            {"success": True, "result": {"config": {}}},
            {"success": True, "result": {"config": {"trace": {}}}},
        ],
        ids=[
            "no-trace-key",
            "no-stored-traces-key",
        ],
    )
    async def test_trace_storage_defaults_enabled_when_config_lacks_trace(
        self, config_response
    ):
        """An automation without an explicit stored_traces value keeps traces."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        client.send_websocket_message.return_value = config_response

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["trace_storage_enabled"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "config_response",
        [
            {"success": True, "result": {}},
            {
                "success": True,
                "result": {"config": {"trace": {"stored_traces": "abc"}}},
            },
            {"success": True, "result": {"config": {"trace": {"stored_traces": {}}}}},
            {"success": True, "result": {"config": None}},
            {"success": True, "result": "not-a-dict"},
            {"success": False, "error": "unknown command"},
        ],
        ids=[
            "no-config-key",
            "non-numeric-stored-traces",
            "mapping-stored-traces",
            "null-config",
            "malformed-result",
            "failed-command",
        ],
    )
    async def test_trace_storage_reports_unknown_when_config_is_unreadable(
        self, config_response
    ):
        """A config probe that cannot answer leaves the check unknown, not enabled."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        client.send_websocket_message.return_value = config_response

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["trace_storage_enabled"] is None
        assert "could not read whether trace storage is enabled" in (
            result["suggestion"].lower()
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", ["0", 0.0], ids=["string-zero", "float-zero"])
    async def test_trace_storage_coerces_core_accepted_zero_values(self, stored):
        """Core coerces number-like zero values; the diagnostic follows."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {"config": {"trace": {"stored_traces": stored}}},
        }

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["trace_storage_enabled"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", ["5", 5.0], ids=["string-five", "float-five"])
    async def test_trace_storage_keeps_core_accepted_positive_values(self, stored):
        """Core coerces number-like positive values; the diagnostic follows."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {"config": {"trace": {"stored_traces": stored}}},
        }

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["trace_storage_enabled"] is True

    @pytest.mark.asyncio
    async def test_script_domain(self):
        """An idle script is not reported as disabled."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "off",
            "attributes": {
                "last_triggered": None,
            },
        }
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {"config": {"trace": {"stored_traces": 5}}},
        }

        result = await _gather_diagnostics(client, "script.test", "script")

        assert result["entity_exists"] is True
        assert result["entity_enabled"] is True
        # For scripts, we should see "script" in suggestion, not "automation"
        assert "script" in result["suggestion"].lower()
        client.send_websocket_message.assert_awaited_once_with(
            {"type": "script/config", "entity_id": "script.test"}
        )

    @pytest.mark.asyncio
    async def test_script_trace_storage_disabled(self):
        """A script's stored_traces=0 disables its trace storage diagnostic."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "off",
            "attributes": {
                "last_triggered": None,
            },
        }
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {"config": {"trace": {"stored_traces": 0}}},
        }

        result = await _gather_diagnostics(client, "script.test", "script")

        assert result["entity_enabled"] is True
        assert result["trace_storage_enabled"] is False
        assert "trace storage is disabled for this script" in (
            result["suggestion"].lower()
        )

    @pytest.mark.asyncio
    async def test_config_fetch_failure_reports_unknown_storage(self):
        """A failed config read leaves the storage check unknown, not enabled."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        client.send_websocket_message.side_effect = Exception("WebSocket error")

        result = await _gather_diagnostics(client, "automation.test", "automation")

        assert result["entity_exists"] is True
        assert result["entity_enabled"] is True
        assert result["trace_storage_enabled"] is None
        assert "could not read whether trace storage is enabled" in (
            result["suggestion"].lower()
        )
        # The unknown check must not claim the traces expired.
        assert "expired" not in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_unavailable_script_is_not_reported_as_off(self):
        """An unavailable script's state is named instead of being called off."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "unavailable",
            "attributes": {"last_triggered": None},
        }
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {"config": {"trace": {"stored_traces": 5}}},
        }

        result = await _gather_diagnostics(client, "script.test", "script")

        assert result["entity_state"] == "unavailable"
        assert result["entity_enabled"] is False
        assert "unavailable" in result["suggestion"].lower()
        assert "state: off" not in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_traces_cleared_or_expired_suggestion(self):
        """Diagnostics suggest traces may have expired for enabled, triggered automation."""
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        client.send_websocket_message.return_value = {
            "success": True,
            "result": {
                "config": {"trace": {"stored_traces": 5}}
            },  # Trace storage enabled
        }

        result = await _gather_diagnostics(client, "automation.test", "automation")

        # For enabled automation with last_triggered and trace storage enabled,
        # suggestion should mention traces may have been cleared or expired
        assert "cleared or expired" in result["suggestion"].lower()
