"""Cross-type rejection matrix for ha_config_set_helper (Bugs 4b/7c/10/14).

These tests assert that inapplicable typed params for a given helper_type are
rejected with VALIDATION_INVALID_PARAMETER instead of silently dropped.

Per-type allowlist (beyond name/helper_id/icon/area_id/labels/category/wait):
  - input_button:   (none)
  - input_boolean:  initial
  - input_select:   options, initial
  - input_number:   min_value, max_value, step, unit_of_measurement, mode, initial
  - input_text:     min_value, max_value, mode, initial
  - input_datetime: has_date, has_time, initial
  - counter:        initial, min_value, max_value, step, restore
  - timer:          duration, restore
  - schedule:       monday..sunday
  - zone:           latitude, longitude, radius, passive
  - person:         user_id, device_trackers, picture (NO icon)
  - tag:            tag_id, description (NO icon)
  - flow types:     config (only)

Tests are written TDD-red: most should fail with "DID NOT RAISE" until the
companion source fix lands. The control (last) tests should pass already.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

# ---------------------------------------------------------------------------
# Fixtures (mirror test_helper_field_persistence.py — kept local to avoid
# cross-file fixture coupling, per the task spec).
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    """Mock client that records every WS message sent."""
    client = MagicMock()

    def make_ws_responses(
        helper_type: str,
        unique_id: str = "abc123",
        existing_config: dict[str, Any] | None = None,
    ):
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

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mock_mcp = MagicMock()
    mock_mcp.add_tool = capture_add_tool
    register_config_helper_tools(mock_mcp, mock_client)
    return registered


def _wire_default_ws(mock_client, helper_type: str) -> None:
    """Attach a default WS side-effect so the call can complete if not rejected."""
    mock_client.send_websocket_message = AsyncMock(
        side_effect=mock_client._make_ws_responses(helper_type)
    )


def _assert_invalid_param(excinfo) -> None:
    msg = str(excinfo.value)
    assert "VALIDATION_INVALID_PARAMETER" in msg, (
        f"expected VALIDATION_INVALID_PARAMETER in error, got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Bug 4b: per-simple-type wrong-param rejection (matrix)
# ---------------------------------------------------------------------------


class TestInputBooleanRejectsInapplicableParams:
    """input_boolean only accepts `initial` — others must be rejected."""

    async def test_rejects_min_value(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_boolean")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Lamp",
                min_value=0,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_options(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_boolean")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Lamp",
                options=["A", "B"],
            )
        _assert_invalid_param(excinfo)


class TestInputNumberRejectsInapplicableParams:
    async def test_rejects_options(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_number")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Volume",
                min_value=0,
                max_value=100,
                options=["X", "Y"],
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_has_date(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_number")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Volume",
                min_value=0,
                max_value=100,
                has_date=True,
            )
        _assert_invalid_param(excinfo)


class TestInputSelectRejectsInapplicableParams:
    async def test_rejects_min_value(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_select")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                name="Mode",
                options=["A", "B"],
                min_value=0,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_duration(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_select")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                name="Mode",
                options=["A", "B"],
                duration="0:05:00",
            )
        _assert_invalid_param(excinfo)


class TestInputTextRejectsInapplicableParams:
    async def test_rejects_options(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_text")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_text",
                name="Note",
                options=["A", "B"],
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_unit_of_measurement(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_text")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_text",
                name="Note",
                unit_of_measurement="W",
            )
        _assert_invalid_param(excinfo)


class TestInputDatetimeRejectsInapplicableParams:
    async def test_rejects_options(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_datetime")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_datetime",
                name="Wakeup",
                has_time=True,
                options=["A"],
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_min_value(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_datetime")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_datetime",
                name="Wakeup",
                has_date=True,
                min_value=0,
            )
        _assert_invalid_param(excinfo)


class TestCounterRejectsInapplicableParams:
    async def test_rejects_options(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "counter")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="counter",
                name="Count",
                options=["A"],
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_duration(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "counter")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="counter",
                name="Count",
                duration="0:05:00",
            )
        _assert_invalid_param(excinfo)


class TestTimerRejectsInapplicableParams:
    async def test_rejects_min_value(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "timer")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="timer",
                name="Coffee",
                duration="0:05:00",
                min_value=0,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_options(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "timer")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="timer",
                name="Coffee",
                duration="0:05:00",
                options=["A"],
            )
        _assert_invalid_param(excinfo)


class TestScheduleRejectsInapplicableParams:
    async def test_rejects_min_value(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "schedule")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="schedule",
                name="Workdays",
                monday=[{"from": "09:00", "to": "17:00"}],
                min_value=0,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_initial(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "schedule")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="schedule",
                name="Workdays",
                monday=[{"from": "09:00", "to": "17:00"}],
                initial="anything",
            )
        _assert_invalid_param(excinfo)


class TestZoneRejectsInapplicableParams:
    async def test_rejects_options(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "zone")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="zone",
                name="Home",
                latitude=45.0,
                longitude=-122.0,
                options=["A"],
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_initial(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "zone")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="zone",
                name="Home",
                latitude=45.0,
                longitude=-122.0,
                initial="x",
            )
        _assert_invalid_param(excinfo)


class TestPersonRejectsInapplicableParams:
    async def test_rejects_latitude(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "person")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="person",
                name="Alice",
                latitude=45.0,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_options(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "person")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="person",
                name="Alice",
                options=["A"],
            )
        _assert_invalid_param(excinfo)


class TestTagRejectsInapplicableParams:
    async def test_rejects_latitude(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "tag")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="tag",
                name="DoorTag",
                latitude=45.0,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_initial(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "tag")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="tag",
                name="DoorTag",
                initial="x",
            )
        _assert_invalid_param(excinfo)


# ---------------------------------------------------------------------------
# Bug 7c: person/tag must reject `icon`
# ---------------------------------------------------------------------------


class TestIconRejectionForPersonAndTag:
    """Bug 7c: icon is not applicable to person or tag and must be rejected."""

    async def test_person_rejects_icon(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "person")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="person",
                name="Alice",
                icon="mdi:account",
            )
        _assert_invalid_param(excinfo)

    async def test_tag_rejects_icon(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "tag")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="tag",
                name="DoorTag",
                icon="mdi:tag",
            )
        _assert_invalid_param(excinfo)


# ---------------------------------------------------------------------------
# Bug 10: input_button accepts no typed params
# ---------------------------------------------------------------------------


class TestInputButtonRejectsAllTypedParams:
    """Bug 10: input_button has no typed params; any of them must be rejected."""

    async def test_rejects_initial(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_button")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_button",
                name="Doorbell",
                initial="x",
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_min_value(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_button")
        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_button",
                name="Doorbell",
                min_value=0,
            )
        _assert_invalid_param(excinfo)


# ---------------------------------------------------------------------------
# Bug 14: flow types reject simple-helper params (only `config` is allowed)
# ---------------------------------------------------------------------------


class TestFlowTypesRejectSimpleHelperParams:
    """Bug 14: flow types only accept `config`; simple-helper params must be rejected."""

    async def test_template_rejects_min_value(self, register_tools, mock_client):
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="template",
                name="x",
                config={"template_type": "sensor", "state": "{{ 1 }}"},
                min_value=0,
                wait=False,
            )
        _assert_invalid_param(excinfo)

    async def test_template_rejects_options(self, register_tools, mock_client):
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="template",
                name="x",
                config={"template_type": "sensor", "state": "{{ 1 }}"},
                options=["A"],
                wait=False,
            )
        _assert_invalid_param(excinfo)

    async def test_group_rejects_initial(self, register_tools, mock_client):
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="group",
                name="grp",
                config={"group_type": "light", "entities": [], "hide_members": False},
                initial="x",
                wait=False,
            )
        _assert_invalid_param(excinfo)

    async def test_group_rejects_duration(self, register_tools, mock_client):
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="group",
                name="grp",
                config={"group_type": "light", "entities": [], "hide_members": False},
                duration="0:05:00",
                wait=False,
            )
        _assert_invalid_param(excinfo)

    async def test_switch_as_x_rejects_options(self, register_tools, mock_client):
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="switch_as_x",
                name="sx",
                config={"entity_id": "switch.kitchen", "target_domain": "light"},
                options=["A"],
                wait=False,
            )
        _assert_invalid_param(excinfo)

    async def test_switch_as_x_rejects_latitude(self, register_tools, mock_client):
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="switch_as_x",
                name="sx",
                config={"entity_id": "switch.kitchen", "target_domain": "light"},
                latitude=45.0,
                wait=False,
            )
        _assert_invalid_param(excinfo)


# ---------------------------------------------------------------------------
# Control tests: passing only allowed params must NOT raise.
# These should pass already (today) and continue passing after the fix lands.
# ---------------------------------------------------------------------------


class TestAllowedParamsControl:
    """Control: legitimate per-type param sets must succeed without rejection."""

    async def test_input_boolean_with_initial_only(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_boolean")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Lamp",
                initial=True,
            )
        assert result["success"] is True

    async def test_input_number_full_typed_set(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_number")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Volume",
                min_value=0,
                max_value=100,
                step=1,
                unit_of_measurement="%",
                mode="slider",
                initial=50,
            )
        assert result["success"] is True

    async def test_input_select_options_initial(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_select")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                name="Mode",
                options=["A", "B"],
                initial="A",
            )
        assert result["success"] is True

    async def test_input_button_with_only_icon(self, register_tools, mock_client):
        """input_button accepts icon (common) and the universal name/area_id/labels."""
        _wire_default_ws(mock_client, "input_button")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_button",
                name="Doorbell",
                icon="mdi:gesture-tap",
            )
        assert result["success"] is True

    async def test_person_with_typed_set_no_icon(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "person")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="person",
                name="Alice",
                user_id="user-123",
                device_trackers=["device_tracker.alice_phone"],
                picture="/local/alice.jpg",
            )
        assert result["success"] is True

    async def test_tag_with_typed_set_no_icon(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "tag")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="tag",
                name="DoorTag",
                tag_id="abc",
                description="Front door NFC tag",
            )
        assert result["success"] is True

    async def test_flow_type_with_only_config(self, register_tools, mock_client):
        """Flow type with only `config` (and universal params) must not raise."""
        mock_client.start_config_flow = AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "flow-c",
                "result": {"entry_id": "entry-c", "title": "x", "domain": "min_max"},
            }
        )
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            name="x",
            config={"entity_ids": ["sensor.a", "sensor.b"], "type": "mean"},
            wait=False,
        )
        assert result["success"] is True
