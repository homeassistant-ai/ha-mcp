"""
Unit tests for input helper update persistence (issue #880).

Verifies that ha_config_set_helper uses the {type}/update WebSocket API
(not just entity registry) when updating input helpers, so that config
changes like options, min/max, etc. persist across HA restarts.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_client():
    """Create a mock client with WebSocket support for helper updates."""
    client = MagicMock()

    def make_ws_responses(helper_type: str, unique_id: str = "abc123"):
        """Build side_effect for send_websocket_message based on message type."""

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
                return {
                    "success": True,
                    "result": [
                        {
                            "id": unique_id,
                            "name": "Existing Helper",
                            "options": ["old_a", "old_b"],
                            "min": 0,
                            "max": 100,
                        }
                    ],
                }

            if msg_type.endswith("/update"):
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


class TestInputSelectUpdatePersistence:
    """Test that input_select updates use the storage API, not just entity registry."""

    async def test_update_options_calls_storage_api(self, register_tools, mock_client):
        """Updating options should call input_select/update, not entity registry."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_select")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                name="My Dropdown",
                helper_id="my_dropdown",
                options=["Option A", "Option B", "Option C"],
            )

        assert result["success"] is True

        # Find the storage update call
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_select/update"),
            None,
        )
        assert update_call is not None, (
            "Expected input_select/update WebSocket call, got: "
            + str([c[0][0].get("type") for c in ws_calls])
        )
        msg = update_call[0][0]
        assert msg["options"] == ["Option A", "Option B", "Option C"]
        assert "input_select_id" in msg

    async def test_update_initial_value(self, register_tools, mock_client):
        """Updating initial value should be included in storage API call."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_select")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                name="My Dropdown",
                helper_id="my_dropdown",
                options=["A", "B"],
                initial="B",
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            c for c in ws_calls if c[0][0].get("type") == "input_select/update"
        )
        assert update_call[0][0]["initial"] == "B"


class TestInputNumberUpdatePersistence:
    """Test that input_number updates use the storage API."""

    async def test_update_min_max_calls_storage_api(self, register_tools, mock_client):
        """Updating min/max should call input_number/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_number")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="My Number",
                helper_id="my_number",
                min_value=0,
                max_value=200,
                step=5,
                unit_of_measurement="W",
                mode="slider",
            )

        assert result["success"] is True

        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_number/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["min"] == 0
        assert msg["max"] == 200
        assert msg["step"] == 5
        assert msg["unit_of_measurement"] == "W"
        assert msg["mode"] == "slider"


class TestInputTextUpdatePersistence:
    """Test that input_text updates use the storage API."""

    async def test_update_mode_calls_storage_api(self, register_tools, mock_client):
        """Updating mode should call input_text/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_text")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_text",
                name="My Text",
                helper_id="my_text",
                min_value=1,
                max_value=50,
                mode="password",
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_text/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["min"] == 1
        assert msg["max"] == 50
        assert msg["mode"] == "password"


class TestInputBooleanUpdatePersistence:
    """Test that input_boolean updates use the storage API."""

    async def test_update_name_calls_storage_api(self, register_tools, mock_client):
        """Updating name should call input_boolean/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_boolean")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                helper_id="my_toggle",
                name="Updated Toggle",
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_boolean/update"),
            None,
        )
        assert update_call is not None
        assert update_call[0][0]["name"] == "Updated Toggle"


class TestInputDatetimeUpdatePersistence:
    """Test that input_datetime updates use the storage API."""

    async def test_update_has_date_time_calls_storage_api(
        self, register_tools, mock_client
    ):
        """Updating has_date/has_time should call input_datetime/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_datetime")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_datetime",
                name="My Datetime",
                helper_id="my_datetime",
                has_date=True,
                has_time=False,
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_datetime/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["has_date"] is True
        assert msg["has_time"] is False


class TestCounterUpdatePersistence:
    """Test that counter updates use the storage API."""

    async def test_update_step_calls_storage_api(self, register_tools, mock_client):
        """Updating step/min/max should call counter/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("counter")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="counter",
                name="My Counter",
                helper_id="my_counter",
                initial="10",
                min_value=0,
                max_value=100,
                step=2,
                restore=True,
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "counter/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["initial"] == 10
        assert msg["minimum"] == 0
        assert msg["maximum"] == 100
        assert msg["step"] == 2
        assert msg["restore"] is True


class TestTimerUpdatePersistence:
    """Test that timer updates use the storage API."""

    async def test_update_duration_calls_storage_api(self, register_tools, mock_client):
        """Updating duration should call timer/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("timer")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="timer",
                name="My Timer",
                helper_id="my_timer",
                duration="00:30:00",
                restore=False,
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "timer/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["duration"] == "00:30:00"
        assert msg["restore"] is False


class TestInputButtonUpdatePersistence:
    """Test that input_button updates use the storage API."""

    async def test_update_name_calls_storage_api(self, register_tools, mock_client):
        """Updating name should call input_button/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_button")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_button",
                helper_id="my_button",
                name="Updated Button",
                icon="mdi:gesture-tap",
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_button/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["name"] == "Updated Button"
        assert msg["icon"] == "mdi:gesture-tap"


class TestEntityRegistryFallback:
    """Verify the entity-registry-only fallback still works for unknown types."""

    async def test_unknown_type_uses_entity_registry(self, register_tools, mock_client):
        """Unknown helper types should fall back to entity registry update."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"entity_entry": {"entity_id": "unknown_type.test"}},
            }
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="unknown_type",
                helper_id="test",
                name="Test",
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        # Should use entity registry, not {type}/update
        update_call = ws_calls[0][0][0]
        assert update_call["type"] == "config/entity_registry/update"


class TestFlowHelperRouting:
    """Verify that ha_config_set_helper routes flow-based helper types (#967)
    to the Config Entry Flow API, not to the WebSocket {type}/create path.

    Covers the unified-tool routing added in #967: types in FLOW_HELPER_TYPES
    (template, group, utility_meter, ...) are delegated to create_flow_helper /
    update_flow_helper; entity resolution and registry updates then run against
    all entities of the resulting config entry.
    """

    async def test_flow_helper_create_routes_via_config_flow(
        self, register_tools, mock_client
    ):
        """Creating a min_max helper uses start_config_flow, not WebSocket create."""
        mock_client.start_config_flow = AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "flow-1",
                "result": {
                    "entry_id": "entry-1",
                    "title": "avg_temp",
                    "domain": "min_max",
                },
            }
        )
        # entity_registry/list returns one entity for our entry.
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "entity_id": "sensor.avg_temp",
                        "config_entry_id": "entry-1",
                    }
                ],
            }
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            name="avg_temp",
            config={"entity_ids": ["sensor.a", "sensor.b"], "type": "mean"},
            wait=False,
        )

        assert result["success"] is True
        assert result["action"] == "create"
        assert result["method"] == "config_flow"
        assert result["entry_id"] == "entry-1"
        assert result["entity_ids"] == ["sensor.avg_temp"]
        mock_client.start_config_flow.assert_awaited_once_with("min_max")

    async def test_flow_helper_update_routes_via_options_flow(
        self, register_tools, mock_client
    ):
        """Updating a flow helper (helper_id set) uses start_options_flow."""
        mock_client.get_config_entry = AsyncMock(
            return_value={"domain": "min_max", "entry_id": "entry-1"}
        )
        mock_client.start_options_flow = AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "flow-2",
                "result": {"entry_id": "entry-1", "title": "avg_temp"},
            }
        )
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            name="avg_temp",
            config={"entity_ids": ["sensor.c"], "type": "max"},
            helper_id="entry-1",
            wait=False,
        )

        assert result["success"] is True
        assert result["action"] == "update"
        assert result["updated"] is True
        assert result["method"] == "config_flow"
        mock_client.start_options_flow.assert_awaited_once_with("entry-1")

    async def test_flow_helper_multi_entity_registry_updates_apply_to_all(
        self, register_tools, mock_client
    ):
        """utility_meter with 2 tariffs creates 3 entities; area_id/labels
        must be applied to every one of them (#967 scope by kp13)."""
        mock_client.start_config_flow = AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "flow-um",
                "result": {
                    "entry_id": "entry-um",
                    "title": "daily_kwh",
                    "domain": "utility_meter",
                },
            }
        )

        # Simulate HA: first call lists 3 entities (select + 2 tariff sensors);
        # subsequent calls are entity_registry/update, all succeed.
        responses = [
            {
                "success": True,
                "result": [
                    {"entity_id": "select.daily_kwh", "config_entry_id": "entry-um"},
                    {
                        "entity_id": "sensor.daily_kwh_peak",
                        "config_entry_id": "entry-um",
                    },
                    {
                        "entity_id": "sensor.daily_kwh_offpeak",
                        "config_entry_id": "entry-um",
                    },
                ],
            },
        ]
        responses.extend([{"success": True}] * 10)  # plenty of headroom
        mock_client.send_websocket_message = AsyncMock(side_effect=responses)

        result = await register_tools["ha_config_set_helper"](
            helper_type="utility_meter",
            name="daily_kwh",
            config={"source": "sensor.energy", "cycle": "daily", "tariffs": ["peak", "offpeak"]},
            area_id="kitchen",
            labels=["metered"],
            wait=False,
        )

        assert result["success"] is True
        assert sorted(result["entity_ids"]) == sorted([
            "select.daily_kwh",
            "sensor.daily_kwh_peak",
            "sensor.daily_kwh_offpeak",
        ])
        assert result["area_id"] == "kitchen"
        assert result["labels"] == ["metered"]
        assert len(result["applied"]) == 3

        # Verify every entity received a config/entity_registry/update call
        update_calls = [
            call.args[0]
            for call in mock_client.send_websocket_message.call_args_list
            if isinstance(call.args[0], dict)
            and call.args[0].get("type") == "config/entity_registry/update"
        ]
        updated_entities = {c["entity_id"] for c in update_calls}
        assert updated_entities == {
            "select.daily_kwh",
            "sensor.daily_kwh_peak",
            "sensor.daily_kwh_offpeak",
        }

    async def test_flow_helper_registry_update_failure_collects_warning(
        self, register_tools, mock_client
    ):
        """Partial registry-update failure surfaces as per-entity warning, not hard error."""
        mock_client.start_config_flow = AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "flow-g",
                "result": {"entry_id": "entry-g", "title": "grp", "domain": "group"},
            }
        )
        # One entity, registry/update fails
        responses = [
            {
                "success": True,
                "result": [
                    {"entity_id": "light.grp", "config_entry_id": "entry-g"},
                ],
            },
            {"success": False, "error": {"message": "registry unavailable"}},
        ]
        mock_client.send_websocket_message = AsyncMock(side_effect=responses)

        result = await register_tools["ha_config_set_helper"](
            helper_type="group",
            name="grp",
            config={"group_type": "light", "entities": [], "hide_members": False},
            area_id="hallway",
            wait=False,
        )

        assert result["success"] is True
        assert "warnings" in result
        assert any("light.grp" in w for w in result["warnings"])

    async def test_flow_helper_create_requires_name(
        self, register_tools, mock_client
    ):
        """Flow helper create without name (neither top-level nor in config) errors."""
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            await register_tools["ha_config_set_helper"](
                helper_type="min_max",
                name="",  # explicit empty
                config={"entity_ids": ["sensor.x"], "type": "mean"},
                wait=False,
            )

    async def test_flow_helper_name_param_folded_into_config(
        self, register_tools, mock_client
    ):
        """Top-level name param is injected into config dict when not already present."""
        captured_config: dict = {}

        async def capture_start(helper_type):
            # called by create_flow_helper; return minimal success
            return {
                "type": "create_entry",
                "flow_id": "flow-c",
                "result": {"entry_id": "entry-c", "title": "t", "domain": helper_type},
            }

        async def capture_submit(flow_id, data):
            captured_config.update(data)
            return {
                "type": "create_entry",
                "result": {"entry_id": "entry-c", "title": "t", "domain": "min_max"},
            }

        mock_client.start_config_flow = AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "flow-c",
                "step_id": "user",
            }
        )
        mock_client.submit_config_flow_step = AsyncMock(side_effect=capture_submit)
        mock_client.abort_config_flow = AsyncMock(return_value={})
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            name="my_helper_name",
            config={"entity_ids": ["sensor.x"], "type": "mean"},
            wait=False,
        )

        assert captured_config.get("name") == "my_helper_name"

    async def test_simple_type_rejects_config_param(
        self, register_tools, mock_client
    ):
        """Passing config for a simple helper type raises VALIDATION_INVALID_PARAMETER.

        Silent-ignore would mislead agents into thinking the payload took effect.
        Empty dict and empty string are tolerated (explicit 'nothing').
        """
        from fastmcp.exceptions import ToolError

        # Non-empty config on simple type → reject
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="probe",
                config={"some_key": "some_value"},
            )
        err_text = str(excinfo.value)
        assert "VALIDATION_INVALID_PARAMETER" in err_text
        assert "flow-based" in err_text.lower()

        # Empty dict → tolerated (would proceed to simple path). We only check
        # that no ToolError with VALIDATION_INVALID_PARAMETER for the config
        # reason is raised; the call itself may fail downstream due to mocks.
        try:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="probe_empty",
                config={},
            )
        except ToolError as e:
            assert "flow-based" not in str(e).lower(), (
                f"empty config should not trigger the flow-based-rejection message: {e}"
            )

    async def test_flow_type_accepts_empty_string_as_no_config(
        self, register_tools, mock_client
    ):
        """Empty string config is tolerated for flow helpers, mirroring simple path.

        ha_config_set_helper L785 treats config in (None, {}, "") as "nothing passed"
        for simple types. The flow path must behave the same — passing config=""
        (common when agents stringify a None) should not surface as
        'Invalid JSON' from parse_json_param, which is confusing and inconsistent
        with the simple-type branch.
        """
        mock_client.start_config_flow = AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "flow-empty",
                "result": {
                    "entry_id": "entry-empty",
                    "title": "probe",
                    "domain": "min_max",
                },
            }
        )
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        # Should proceed like config=None / config={}: name folds into config_dict
        # on create, flow is started, no 'Invalid JSON' error surfaces.
        result = await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            name="probe",
            config="",
            wait=False,
        )

        assert result["success"] is True
        assert result["action"] == "create"
        assert result["method"] == "config_flow"
        assert result["entry_id"] == "entry-empty"
        mock_client.start_config_flow.assert_awaited_once_with("min_max")

    async def test_flow_helper_clears_area_on_empty_string(
        self, register_tools, mock_client
    ):
        """area_id='' must clear the area assignment (HA-WS 'area_id: null').

        Mirrors ha_set_entity semantics (see test_set_area_clear and
        test_entity_management.py::test_set_entity_clear_area): the consolidated
        update convention treats None as 'not provided', empty string as 'explicit clear'.
        The outer guard must use `is not None` so the empty-string case reaches the
        WebSocket call, and the payload must carry area_id: None for HA to clear it.
        """
        ws_calls: list[dict] = []

        async def ws_handler(msg: dict) -> dict:
            ws_calls.append(msg)
            msg_type = msg.get("type", "")
            if msg_type == "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "sensor.probe",
                            "config_entry_id": "entry-clear-area",
                        }
                    ],
                }
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)
        mock_client.start_config_flow = AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "flow-clear-area",
                "result": {
                    "entry_id": "entry-clear-area",
                    "title": "probe",
                    "domain": "min_max",
                },
            }
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            name="probe",
            config={"entity_ids": ["sensor.a"], "type": "mean"},
            area_id="",
            wait=False,
        )

        assert result["success"] is True
        # Find the entity_registry/update call
        updates = [c for c in ws_calls if c.get("type") == "config/entity_registry/update"]
        assert len(updates) == 1, f"expected 1 registry update, got {len(updates)}: {ws_calls}"
        assert "area_id" in updates[0], (
            f"area_id must be present in payload (even when clearing): {updates[0]}"
        )
        assert updates[0]["area_id"] is None, (
            f"area_id='' must translate to area_id: None for HA clear semantics, got {updates[0]['area_id']!r}"
        )

    async def test_flow_helper_clears_labels_on_empty_list(
        self, register_tools, mock_client
    ):
        """labels=[] must clear all labels (HA-WS 'labels: []').

        Mirrors ha_set_entity::test_set_labels_empty_list_clears and
        test_label_crud.py usage of 'labels: []' as the clear payload.
        """
        ws_calls: list[dict] = []

        async def ws_handler(msg: dict) -> dict:
            ws_calls.append(msg)
            msg_type = msg.get("type", "")
            if msg_type == "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "sensor.probe",
                            "config_entry_id": "entry-clear-labels",
                        }
                    ],
                }
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)
        mock_client.start_config_flow = AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "flow-clear-labels",
                "result": {
                    "entry_id": "entry-clear-labels",
                    "title": "probe",
                    "domain": "min_max",
                },
            }
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            name="probe",
            config={"entity_ids": ["sensor.a"], "type": "mean"},
            labels=[],
            wait=False,
        )

        assert result["success"] is True
        updates = [c for c in ws_calls if c.get("type") == "config/entity_registry/update"]
        assert len(updates) == 1, f"expected 1 registry update, got {len(updates)}: {ws_calls}"
        assert "labels" in updates[0], (
            f"labels must be present in payload (even when clearing): {updates[0]}"
        )
        assert updates[0]["labels"] == [], (
            f"labels=[] must pass through as [] for HA clear semantics, got {updates[0]['labels']!r}"
        )
