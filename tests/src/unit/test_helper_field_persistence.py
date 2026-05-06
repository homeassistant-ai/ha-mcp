"""Unit tests for helper field persistence — tracking issue #1150.

Closes the test gap identified in TEST_GAP_ANALYSIS: existing tests assert that
the right *message type* is sent (`input_number/update`), but echo-reflect the
payload back as the result, so a bug that silently drops fields from the
payload still produces ``success: True`` and passes.

These tests instead assert the **contents** of the outgoing WebSocket payload,
and that inapplicable typed params are rejected rather than silently dropped.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError


@pytest.fixture
def mock_client():
    """Mock client that records every WS message sent."""
    client = MagicMock()

    def make_ws_responses(helper_type: str, unique_id: str = "abc123",
                         existing_config: dict[str, Any] | None = None):
        """Build a ws_handler. ``existing_config`` is what {type}/list returns."""
        existing = existing_config or {
            "id": unique_id,
            "name": "Existing Helper",
        }

        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")

            if msg_type == "config/entity_registry/get":
                return {
                    "success": True,
                    "result": {
                        "entity_id": msg["entity_id"],
                        "unique_id": unique_id,
                        "platform": helper_type,
                    },
                }

            if msg_type.endswith("/list"):
                return {"success": True, "result": [existing]}

            if msg_type.endswith("/update") or msg_type.endswith("/create"):
                # Echo back so the tool's success path runs to completion.
                return {
                    "success": True,
                    "result": {
                        "id": unique_id,
                        **{k: v for k, v in msg.items() if k != "type"},
                    },
                }

            if msg_type == "config/entity_registry/update":
                return {
                    "success": True,
                    "result": {"entity_entry": {"entity_id": msg["entity_id"]}},
                }

            return {"success": True, "result": {}}

        return ws_handler

    client._make_ws_responses = make_ws_responses
    return client


@pytest.fixture
def register_tools(mock_client):
    """Register helper config tools and return the captured tool functions."""
    from ha_mcp.tools.tools_config_helpers import register_config_helper_tools

    registered: dict[str, Any] = {}

    def capture_tool(**kwargs):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = capture_tool
    register_config_helper_tools(mock_mcp, mock_client)
    return registered


def _find_msg(client: Any, msg_type: str) -> dict | None:
    """Find the first WS message of ``msg_type`` from the recorded calls."""
    for call in client.send_websocket_message.call_args_list:
        msg = call[0][0]
        if msg.get("type") == msg_type:
            return msg
    return None


# ---------------------------------------------------------------------------
# Bug 1: input_number CREATE silently drops `initial`
# ---------------------------------------------------------------------------


class TestInputNumberInitialPersistence:
    """input_number create+update must include `initial` in the WS payload (Bug 1, 1b)."""

    async def test_create_includes_initial_in_payload(self, register_tools, mock_client):
        """Bug 1: input_number CREATE message must contain `initial` if caller passed it."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_number")
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Thermostat",
                min_value=60,
                max_value=85,
                initial=72,
            )

        create_msg = _find_msg(mock_client, "input_number/create")
        assert create_msg is not None, "input_number/create message not sent"
        assert create_msg.get("initial") == 72, (
            f"Bug 1: `initial` was not in the create message payload. "
            f"Sent: {create_msg!r}"
        )

    async def test_update_includes_initial_in_payload(self, register_tools, mock_client):
        """Bug 1b: input_number UPDATE message must contain `initial` if caller passed it."""
        existing = {
            "id": "abc123",
            "name": "Thermostat",
            "min": 60,
            "max": 85,
            "step": 1,
            "mode": "slider",
            "initial": 72,
        }
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_number", existing_config=existing)
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                helper_id="abc123",
                initial=80,
            )

        update_msg = _find_msg(mock_client, "input_number/update")
        assert update_msg is not None, "input_number/update message not sent"
        assert update_msg.get("initial") == 80, (
            f"Bug 1b: `initial` was not in the update message payload. "
            f"Sent: {update_msg!r}"
        )


# ---------------------------------------------------------------------------
# Bug 8: destructive UPDATE — fields not re-passed must be merged from existing
# ---------------------------------------------------------------------------


class TestUpdateMergePreservesExistingFields:
    """UPDATE must preserve fields the caller didn't re-pass (Bug 8)."""

    async def test_input_number_update_name_only_preserves_initial_unit_step(
        self, register_tools, mock_client
    ):
        """Renaming an input_number must not wipe initial/unit_of_measurement/step/mode."""
        existing = {
            "id": "abc123",
            "name": "OldName",
            "min": 60,
            "max": 85,
            "step": 0.5,
            "mode": "box",
            "initial": 72,
            "unit_of_measurement": "°F",
            "icon": "mdi:thermometer",
        }
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_number", existing_config=existing)
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                helper_id="abc123",
                name="NewName",
            )

        msg = _find_msg(mock_client, "input_number/update")
        assert msg is not None
        # Every type-specific field must be present in the outgoing update message
        # (HA's input_number/update is full-replace, so missing = wiped).
        assert msg.get("name") == "NewName"
        assert msg.get("min") == 60, f"Bug 8: min wiped on rename. msg={msg!r}"
        assert msg.get("max") == 85, f"Bug 8: max wiped on rename. msg={msg!r}"
        assert msg.get("step") == 0.5, f"Bug 8: step wiped on rename. msg={msg!r}"
        assert msg.get("mode") == "box", f"Bug 8: mode wiped on rename. msg={msg!r}"
        assert msg.get("initial") == 72, f"Bug 8: initial wiped on rename. msg={msg!r}"
        assert msg.get("unit_of_measurement") == "°F", f"Bug 8: unit wiped. msg={msg!r}"
        assert msg.get("icon") == "mdi:thermometer", f"Bug 8: icon wiped. msg={msg!r}"

    async def test_input_text_update_name_only_preserves_min_max_mode_initial(
        self, register_tools, mock_client
    ):
        """Renaming an input_text must not wipe min/max/mode/initial/icon."""
        existing = {
            "id": "abc123",
            "name": "OldName",
            "min": 5,
            "max": 50,
            "mode": "password",
            "initial": "starter",
            "icon": "mdi:note-text",
        }
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_text", existing_config=existing)
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_text",
                helper_id="abc123",
                name="NewName",
            )

        msg = _find_msg(mock_client, "input_text/update")
        assert msg is not None
        assert msg.get("min") == 5, f"Bug 8: min wiped. msg={msg!r}"
        assert msg.get("max") == 50, f"Bug 8: max wiped. msg={msg!r}"
        assert msg.get("mode") == "password", f"Bug 8: mode wiped. msg={msg!r}"
        assert msg.get("initial") == "starter", f"Bug 8: initial wiped. msg={msg!r}"
        assert msg.get("icon") == "mdi:note-text", f"Bug 8: icon wiped. msg={msg!r}"

    async def test_counter_update_name_only_preserves_all_fields(
        self, register_tools, mock_client
    ):
        """Renaming a counter must not wipe initial/min/max/step/restore/icon."""
        existing = {
            "id": "abc123",
            "name": "OldName",
            "initial": 5,
            "minimum": 0,
            "maximum": 100,
            "step": 2,
            "restore": True,
            "icon": "mdi:counter",
        }
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("counter", existing_config=existing)
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="counter",
                helper_id="abc123",
                name="NewName",
            )

        msg = _find_msg(mock_client, "counter/update")
        assert msg is not None
        assert msg.get("initial") == 5, f"Bug 8: initial wiped. msg={msg!r}"
        assert msg.get("minimum") == 0, f"Bug 8: minimum wiped. msg={msg!r}"
        assert msg.get("maximum") == 100, f"Bug 8: maximum wiped. msg={msg!r}"
        assert msg.get("step") == 2, f"Bug 8: step wiped. msg={msg!r}"
        assert msg.get("restore") is True, f"Bug 8: restore wiped. msg={msg!r}"
        assert msg.get("icon") == "mdi:counter", f"Bug 8: icon wiped. msg={msg!r}"

    async def test_timer_update_name_only_preserves_duration_restore_icon(
        self, register_tools, mock_client
    ):
        """Renaming a timer must not wipe duration/restore/icon."""
        existing = {
            "id": "abc123",
            "name": "OldName",
            "duration": "0:30:00",
            "restore": True,
            "icon": "mdi:timer",
        }
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("timer", existing_config=existing)
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="timer",
                helper_id="abc123",
                name="NewName",
            )

        msg = _find_msg(mock_client, "timer/update")
        assert msg is not None
        assert msg.get("duration") == "0:30:00", f"Bug 8: duration wiped. msg={msg!r}"
        assert msg.get("restore") is True, f"Bug 8: restore wiped. msg={msg!r}"
        assert msg.get("icon") == "mdi:timer", f"Bug 8: icon wiped. msg={msg!r}"

    async def test_input_boolean_update_name_only_preserves_initial_icon(
        self, register_tools, mock_client
    ):
        """Renaming an input_boolean must not wipe initial/icon."""
        existing = {
            "id": "abc123",
            "name": "OldName",
            "initial": True,
            "icon": "mdi:toggle-switch-on",
        }
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_boolean", existing_config=existing)
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                helper_id="abc123",
                name="NewName",
            )

        msg = _find_msg(mock_client, "input_boolean/update")
        assert msg is not None
        assert msg.get("initial") is True, f"Bug 8: initial wiped. msg={msg!r}"
        assert msg.get("icon") == "mdi:toggle-switch-on", f"Bug 8: icon wiped. msg={msg!r}"

    async def test_input_datetime_update_name_only_preserves_has_date_has_time_initial(
        self, register_tools, mock_client
    ):
        """Renaming an input_datetime must not wipe has_date/has_time/initial/icon."""
        existing = {
            "id": "abc123",
            "name": "OldName",
            "has_date": True,
            "has_time": True,
            "initial": "2026-12-31 23:59:59",
            "icon": "mdi:calendar",
        }
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_datetime", existing_config=existing)
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_datetime",
                helper_id="abc123",
                name="NewName",
            )

        msg = _find_msg(mock_client, "input_datetime/update")
        assert msg is not None
        assert msg.get("has_date") is True, f"Bug 8: has_date wiped. msg={msg!r}"
        assert msg.get("has_time") is True, f"Bug 8: has_time wiped. msg={msg!r}"
        assert msg.get("initial") == "2026-12-31 23:59:59", f"Bug 8: initial wiped. msg={msg!r}"
        assert msg.get("icon") == "mdi:calendar", f"Bug 8: icon wiped. msg={msg!r}"

    async def test_input_select_update_name_only_preserves_options_initial_icon(
        self, register_tools, mock_client
    ):
        """Renaming an input_select must not wipe options/initial/icon."""
        existing = {
            "id": "abc123",
            "name": "OldName",
            "options": ["Alpha", "Beta", "Gamma"],
            "initial": "Beta",
            "icon": "mdi:menu",
        }
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_select", existing_config=existing)
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                helper_id="abc123",
                name="NewName",
            )

        msg = _find_msg(mock_client, "input_select/update")
        assert msg is not None
        assert msg.get("options") == ["Alpha", "Beta", "Gamma"], f"Bug 8: options wiped. msg={msg!r}"
        assert msg.get("initial") == "Beta", f"Bug 8: initial wiped. msg={msg!r}"
        assert msg.get("icon") == "mdi:menu", f"Bug 8: icon wiped. msg={msg!r}"
