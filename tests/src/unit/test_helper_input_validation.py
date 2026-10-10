"""Helper create/update calls that reach Home Assistant (issue #1150).

Home Assistant validates storage-helper fields itself (#2632); these tests pin
what the tool still owns:

- **Bug 9** — ``tag/create`` requires ``tag_id``; the tool generates a uuid4
  hex when the caller omits it, as documented.
- **Bug 13** — a ``step`` larger than an input_number's range, which Home
  Assistant stores although the slider cannot use it, is rejected before the
  WebSocket round-trip.
- Control tests: valid input goes through to the WS message, so a refactor
  that rejects a legitimate call fails here.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError

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


def _wire_default_ws(
    mock_client,
    helper_type: str,
    existing_config: dict[str, Any] | None = None,
) -> None:
    """Seed the WS-response handler. ``existing_config`` overrides the
    default seed the update path merges from, used by the parity-guard tests
    to construct a specific starting state.
    """
    mock_client.send_websocket_message = AsyncMock(
        side_effect=mock_client._make_ws_responses(
            helper_type, existing_config=existing_config
        )
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
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
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
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
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

    async def test_create_skips_entity_registration_wait(
        self, register_tools, mock_client
    ):
        """Tags don't have entity states — the create branch must not call
        ``wait_for_entity_registered``.

        The update branch already documents this (``_execute_update_simple_helper`` in
        ``config_helpers/update.py``): tags live in their own tag registry and
        never appear in ``/api/states/<entity_id>``. The create branch was
        previously calling ``wait_for_entity_registered`` against a
        synthesized ``tag.<id>`` slug, which 404s for the full timeout on
        every single tag-create — burning ~10s per tag-create test on
        every CI run. This test pins the fix.
        """
        _wire_default_ws(mock_client, "tag")
        with patch(
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ) as wait_mock:
            await register_tools["ha_config_set_helper"](
                helper_type="tag",
                name="My Tag",
                tag_id="some-tag",
            )
        wait_mock.assert_not_awaited()

    async def test_create_non_tag_still_waits_for_registration(
        self, register_tools, mock_client
    ):
        """Non-tag helper types still wait for entity registration.

        The skip is scoped specifically to ``helper_type == "tag"`` —
        input_boolean / input_number / etc. continue to use
        ``wait_for_entity_registered`` to gate ``area_id`` /``labels``
        registry updates and to surface a queryability warning to callers.
        """
        _wire_default_ws(mock_client, "input_boolean")
        with patch(
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ) as wait_mock:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="My Bool",
            )
        wait_mock.assert_awaited()


# ---------------------------------------------------------------------------
# Bug 13 — input_number range/step validation
# ---------------------------------------------------------------------------


class TestInputNumberStepGuard:
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

    async def test_valid_range_with_equal_step(self, register_tools, mock_client):
        # Control: step exactly equal to range is allowed (slider has 2 stops).
        _wire_default_ws(mock_client, "input_number")
        with patch(
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
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
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
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


# ---------------------------------------------------------------------------
# Bug 13 — input_text length validation
# ---------------------------------------------------------------------------


class TestInputTextLengthValidation:
    async def test_accepts_exact_length(self, register_tools, mock_client):
        """Core accepts min == max for input_text: an exact-length value."""
        _wire_default_ws(mock_client, "input_text")
        with patch(
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_text", name="Pin", min_value=4, max_value=4
            )
        msg = _find_msg(mock_client, "input_text/create")
        assert msg["min"] == 4 and msg["max"] == 4

    async def test_valid_lengths_pass(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_text")
        with patch(
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
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
    async def test_unique_options_pass(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_select")
        with patch(
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
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
    async def test_non_overlapping_ranges_pass(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "schedule")
        with patch(
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
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
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
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


# ---------------------------------------------------------------------------
# input_select initial-in-options + input_datetime has_date/has_time guards —
# create-side coverage plus the corresponding update-path parity.
# ---------------------------------------------------------------------------


class TestInputSelectInitialInOptions:
    """input_select: ``initial`` must be one of ``options`` on both branches.

    Each scenario must produce the same ``VALIDATION_INVALID_PARAMETER`` error
    on both code paths, so a caller hitting the invariant gets the same
    actionable message regardless of action.
    """

    # --- Create-side coverage ---

    async def test_create_valid_initial_passes(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_select")
        with patch(
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                name="Mode",
                options=["A", "B", "C"],
                initial="B",
            )
        msg = _find_msg(mock_client, "input_select/create")
        assert msg is not None
        assert msg["initial"] == "B"

    # --- Update-side coverage ---

    async def test_update_happy_path_passes(self, register_tools, mock_client):
        """Valid merge — happy path. Guards against false-positive rejections."""
        _wire_default_ws(
            mock_client,
            "input_select",
            {"id": "abc123", "name": "Mode", "options": ["A", "B"], "initial": "A"},
        )
        with patch(
            "ha_mcp.tools.config_helpers.update.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                helper_id="mode",
                options=["A", "B", "C"],
                initial="C",
            )
        msg = _find_msg(mock_client, "input_select/update")
        assert msg is not None
        assert msg["options"] == ["A", "B", "C"]
        assert msg["initial"] == "C"


class TestInputDatetimeHasDateOrTime:
    """input_datetime: at least one of has_date/has_time must be True on both branches.

    The create and update branches each call the shared validator with the
    resolved-after-merge ``(has_date, has_time)`` pair; either explicit
    ``(False, False)`` from the caller or an update that disables the one
    remaining True component is caught before the WS write.
    """

    # --- Create-side coverage ---

    async def test_create_with_only_date_passes(self, register_tools, mock_client):
        _wire_default_ws(mock_client, "input_datetime")
        with patch(
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_datetime",
                name="DateOnly",
                has_date=True,
                has_time=False,
            )
        msg = _find_msg(mock_client, "input_datetime/create")
        assert msg is not None
        assert msg["has_date"] is True
        assert msg["has_time"] is False

    # --- Update-side coverage ---

    async def test_update_happy_path_keeps_both_true(self, register_tools, mock_client):
        """Valid merge passes — guards against false-positive rejection on a
        no-op-ish update."""
        _wire_default_ws(
            mock_client,
            "input_datetime",
            {"id": "abc123", "name": "Schedule", "has_date": True, "has_time": True},
        )
        with patch(
            "ha_mcp.tools.config_helpers.update.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_datetime",
                helper_id="schedule",
                has_date=True,
            )
        msg = _find_msg(mock_client, "input_datetime/update")
        assert msg is not None
        assert msg["has_date"] is True
        assert msg["has_time"] is True

    async def test_update_happy_path_omits_both_against_one_remaining(
        self, register_tools, mock_client
    ):
        """Caller passes neither ``has_date`` nor ``has_time``; existing has
        ``(False, True)``. The merge resolves to the existing state — no fall
        to ``(False, False)``, so the guard must not reject."""
        _wire_default_ws(
            mock_client,
            "input_datetime",
            {"id": "abc123", "name": "TimeOnly", "has_date": False, "has_time": True},
        )
        with patch(
            "ha_mcp.tools.config_helpers.update.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await register_tools["ha_config_set_helper"](
                helper_type="input_datetime",
                helper_id="timeonly",
                name="Renamed",
            )
        msg = _find_msg(mock_client, "input_datetime/update")
        assert msg is not None
        assert msg["has_date"] is False
        assert msg["has_time"] is True


# ---------------------------------------------------------------------------
# Direct validator contract tests — exercise the helpers' own invariants
# rather than the tool's WS plumbing, so the shape-guard semantics are
# pinned independently of any future call-site refactor.
# ---------------------------------------------------------------------------
