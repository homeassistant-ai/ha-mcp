"""Unit tests for helper tool-side input validation (Bugs 9/13/17, issue #1150).

Closes pre-validation gaps in ``ha_config_set_helper``:

- **Bug 9** — ``tag/create`` requires ``tag_id``; omitting it triggers a
  cryptic "Unknown error" 400. The tool now auto-generates a uuid4 hex when
  the caller doesn't supply one (matches the documented behaviour).

- **Bug 13** — numeric range validation expanded beyond the single
  ``min > max`` check. Now also rejects ``min == max``, non-positive
  ``step``, and ``step > range`` (which HA *doesn't* reject — it produces a
  broken slider). For ``input_text``, length must be in [0, 255].

- **Bug 17** — schema-level constraints HA enforces with confusing messages:
  ``input_select`` duplicate options, ``schedule`` per-day overlapping ranges,
  and ``schedule`` ranges missing ``from``/``to`` keys.

Each new validation has a rejection test (asserts ToolError with
``VALIDATION_INVALID_PARAMETER``) and a control test (valid input goes
through to a WS message). The control tests double as regression coverage:
if a future refactor accidentally rejects a legitimate call, they catch it.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

# ---------------------------------------------------------------------------
# Fixtures (mirror the local-fixture pattern from
# test_helper_field_persistence.py and test_helper_param_rejection.py — kept
# inside this file to avoid cross-file fixture coupling).
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


def _wire_default_ws(mock_client, helper_type: str) -> None:
    mock_client.send_websocket_message = AsyncMock(
        side_effect=mock_client._make_ws_responses(helper_type)
    )


def _assert_invalid_param(excinfo) -> None:
    msg = str(excinfo.value)
    assert "VALIDATION_INVALID_PARAMETER" in msg, (
        f"expected VALIDATION_INVALID_PARAMETER in error, got: {msg!r}"
    )


def _find_msg(client: Any, msg_type: str) -> dict | None:
    for call in client.send_websocket_message.call_args_list:
        msg = call[0][0]
        if msg.get("type") == msg_type:
            return msg
    return None


# ---------------------------------------------------------------------------
# Bug 9 — tag auto-generates tag_id
# ---------------------------------------------------------------------------


class TestTagAutoGeneratesTagId:
    """tag/create requires tag_id; tool fills it in when caller omits."""

    async def test_create_without_tag_id_auto_generates(
        self, register_tools, mock_client
    ):
        _wire_default_ws(mock_client, "tag")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="tag",
                name="My Tag",
            )
        msg = _find_msg(mock_client, "tag/create")
        assert msg is not None, "tag/create message must be sent"
        assert "tag_id" in msg, "auto-generated tag_id must be in the payload"
        assert isinstance(msg["tag_id"], str) and len(msg["tag_id"]) == 32, (
            f"expected uuid4 hex (32 chars), got {msg['tag_id']!r}"
        )

    async def test_create_with_explicit_tag_id_preserved(
        self, register_tools, mock_client
    ):
        _wire_default_ws(mock_client, "tag")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="tag",
                name="My Tag",
                tag_id="custom-tag-id",
            )
        msg = _find_msg(mock_client, "tag/create")
        assert msg is not None
        assert msg["tag_id"] == "custom-tag-id"


# ---------------------------------------------------------------------------
# Bug 13 — input_number range/step validation
# ---------------------------------------------------------------------------


class TestInputNumberRangeValidation:
    async def test_rejects_min_equal_max(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_number")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Volume",
                min_value=5,
                max_value=5,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_step_zero(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_number")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Volume",
                min_value=0,
                max_value=100,
                step=0,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_step_negative(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_number")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Volume",
                min_value=0,
                max_value=100,
                step=-1,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_step_larger_than_range(self, register_tools, mock_client):
        # HA itself does NOT reject this, but the slider becomes broken — the
        # tool must catch it before the WS round-trip.
        _wire_default_ws(mock_client, "input_number")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Volume",
                min_value=0,
                max_value=10,
                step=15,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_min_greater_than_max(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_number")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Volume",
                min_value=100,
                max_value=0,
            )
        _assert_invalid_param(excinfo)

    async def test_valid_range_with_equal_step(self, register_tools, mock_client):
        # Control: step exactly equal to range is allowed (slider has 2 stops).
        _wire_default_ws(mock_client, "input_number")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Volume",
                min_value=0,
                max_value=10,
                step=10,
            )
        msg = _find_msg(mock_client, "input_number/create")
        assert msg is not None
        assert msg["step"] == 10

    async def test_valid_range_passes(self, register_tools, mock_client):
        # Control: a normal range goes through unchanged.
        _wire_default_ws(mock_client, "input_number")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                name="Volume",
                min_value=0,
                max_value=100,
                step=1,
            )
        msg = _find_msg(mock_client, "input_number/create")
        assert msg is not None
        assert msg["min"] == 0 and msg["max"] == 100 and msg["step"] == 1


# ---------------------------------------------------------------------------
# Bug 13 — counter range validation
# ---------------------------------------------------------------------------


class TestCounterRangeValidation:
    async def test_rejects_min_equal_max(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "counter")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="counter",
                name="C",
                min_value=3,
                max_value=3,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_step_zero(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "counter")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="counter",
                name="C",
                min_value=0,
                max_value=10,
                step=0,
            )
        _assert_invalid_param(excinfo)


# ---------------------------------------------------------------------------
# Bug 13 — input_text length validation
# ---------------------------------------------------------------------------


class TestInputTextLengthValidation:
    async def test_rejects_min_negative(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_text")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_text",
                name="Note",
                min_value=-1,
                max_value=100,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_max_above_255(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_text")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_text",
                name="Note",
                min_value=0,
                max_value=300,
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_min_equal_max(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_text")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_text",
                name="Note",
                min_value=10,
                max_value=10,
            )
        _assert_invalid_param(excinfo)

    async def test_valid_lengths_pass(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_text")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_text",
                name="Note",
                min_value=0,
                max_value=255,
            )
        msg = _find_msg(mock_client, "input_text/create")
        assert msg is not None
        assert msg["min"] == 0 and msg["max"] == 255


# ---------------------------------------------------------------------------
# Bug 17 — input_select duplicate options
# ---------------------------------------------------------------------------


class TestInputSelectDuplicateOptions:
    async def test_rejects_duplicate_options(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_select")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                name="Mode",
                options=["A", "B", "A"],
            )
        _assert_invalid_param(excinfo)
        assert "unique" in str(excinfo.value).lower() or "duplicate" in str(
            excinfo.value
        ).lower()

    async def test_unique_options_pass(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_select")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                name="Mode",
                options=["A", "B", "C"],
            )
        msg = _find_msg(mock_client, "input_select/create")
        assert msg is not None
        assert msg["options"] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Bug 17 — schedule overlap and missing-key validation
# ---------------------------------------------------------------------------


class TestScheduleValidation:
    async def test_rejects_overlapping_ranges(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "schedule")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="schedule",
                name="Wakeup",
                monday=[
                    {"from": "07:00", "to": "12:00"},
                    {"from": "11:00", "to": "14:00"},
                ],
            )
        _assert_invalid_param(excinfo)
        assert "monday" in str(excinfo.value).lower() or "overlap" in str(
            excinfo.value
        ).lower()

    async def test_rejects_missing_to_key(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "schedule")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="schedule",
                name="Wakeup",
                tuesday=[{"from": "07:00"}],
            )
        _assert_invalid_param(excinfo)

    async def test_rejects_missing_from_key(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "schedule")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="schedule",
                name="Wakeup",
                wednesday=[{"to": "07:00"}],
            )
        _assert_invalid_param(excinfo)

    async def test_non_overlapping_ranges_pass(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "schedule")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="schedule",
                name="Wakeup",
                monday=[
                    {"from": "07:00", "to": "12:00"},
                    {"from": "13:00", "to": "17:00"},
                ],
            )
        msg = _find_msg(mock_client, "schedule/create")
        assert msg is not None
        assert "monday" in msg
        assert len(msg["monday"]) == 2

    async def test_touching_ranges_pass(self, register_tools, mock_client):
        # 07:00-12:00 and 12:00-14:00 do NOT overlap (boundary equal).
        _wire_default_ws(mock_client, "schedule")
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="schedule",
                name="Wakeup",
                monday=[
                    {"from": "07:00", "to": "12:00"},
                    {"from": "12:00", "to": "14:00"},
                ],
            )
        msg = _find_msg(mock_client, "schedule/create")
        assert msg is not None


# ---------------------------------------------------------------------------
# Bug 13 — validation also fires on UPDATE path
# ---------------------------------------------------------------------------


class TestUpdateRangeValidation:
    async def test_update_rejects_invalid_range(self, register_tools, mock_client):
        # Same _validate_numeric_range hook applies to both branches; ensure the
        # update path is wired to it.
        _wire_default_ws(mock_client, "input_number")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                helper_id="vol",
                min_value=10,
                max_value=10,
            )
        _assert_invalid_param(excinfo)

    async def test_update_rejects_duplicate_options(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_select")
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                helper_id="mode",
                options=["A", "A"],
            )
        _assert_invalid_param(excinfo)
