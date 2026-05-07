"""Unit tests for Bug 16 (issue #1150): registry-ID validation in ha_config_set_helper.

Phantom ``area_id`` / ``labels`` / ``category`` IDs were previously forwarded to
HA's entity_registry/update without any existence check, leaving dangling
references in the registry. These tests assert that:

  - Phantom area_id / label / category are rejected with
    VALIDATION_INVALID_PARAMETER (and the error mentions the unknown ID).
  - Existing values pass through untouched (control case).
  - Empty-string area_id (the documented "clear" sentinel) does NOT trigger a
    registry lookup or rejection.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

# ---------------------------------------------------------------------------
# Fixtures — match the local-fixture style used by the other helper unit tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    """Mock client. Tests configure ``send_websocket_message`` per-scenario."""
    client = MagicMock()
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


def _assert_invalid_param(excinfo) -> None:
    msg = str(excinfo.value)
    assert "VALIDATION_INVALID_PARAMETER" in msg, (
        f"expected VALIDATION_INVALID_PARAMETER in error, got: {msg!r}"
    )


def _make_ws_handler(
    *,
    area_ids: list[str] | None = None,
    label_ids: list[str] | None = None,
    category_ids: list[str] | None = None,
    helper_type: str = "input_boolean",
    unique_id: str = "abc123",
):
    """Build a side_effect handler for ``send_websocket_message``.

    Returns lists for the registry/list calls and echoes payloads back for
    create/update so the tool's success path can run to completion when the
    validation passes.
    """
    areas = [{"area_id": aid, "name": aid.title()} for aid in (area_ids or [])]
    labels = [{"label_id": lid, "name": lid.title()} for lid in (label_ids or [])]
    categories = [
        {"category_id": cid, "name": cid.title(), "scope": "helpers"}
        for cid in (category_ids or [])
    ]

    async def ws_handler(msg: dict) -> dict:
        msg_type = msg.get("type", "")

        # Registry lookups for validation.
        if msg_type == "config/area_registry/list":
            return {"success": True, "result": areas}
        if msg_type == "config/label_registry/list":
            return {"success": True, "result": labels}
        if msg_type == "config/category_registry/list":
            return {"success": True, "result": categories}

        # Standard helper plumbing (only reached when validation passes).
        if msg_type == "config/entity_registry/get":
            return {
                "success": True,
                "result": {
                    "entity_id": msg.get("entity_id"),
                    "unique_id": unique_id,
                    "platform": helper_type,
                },
            }
        if msg_type.endswith("/list"):
            return {
                "success": True,
                "result": [{"id": unique_id, "name": "Existing"}],
            }
        if msg_type.endswith("/create") or msg_type.endswith("/update"):
            return {
                "success": True,
                "result": {
                    "id": unique_id,
                    "entity_id": f"{helper_type}.{unique_id}",
                    **{k: v for k, v in msg.items() if k != "type"},
                },
            }
        if msg_type == "config/entity_registry/update":
            return {
                "success": True,
                "result": {"entity_entry": {"entity_id": msg.get("entity_id")}},
            }
        return {"success": True, "result": {}}

    return ws_handler


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPhantomAreaIdRejected:
    """Bug 16: phantom area_id must be rejected with VALIDATION_INVALID_PARAMETER."""

    async def test_create_phantom_area_id(self, register_tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(area_ids=["kitchen", "living_room"])
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ), pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test",
                area_id="nonexistent_area_id_xyz",
            )
        _assert_invalid_param(excinfo)
        # Ensure the available IDs are surfaced for the caller.
        msg = str(excinfo.value)
        assert "nonexistent_area_id_xyz" in msg
        assert "kitchen" in msg or "living_room" in msg


class TestPhantomLabelRejected:
    """Bug 16: phantom label must be rejected with VALIDATION_INVALID_PARAMETER."""

    async def test_create_phantom_label(self, register_tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(label_ids=["important", "automation"])
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ), pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test",
                labels=["does_not_exist"],
            )
        _assert_invalid_param(excinfo)
        msg = str(excinfo.value)
        assert "does_not_exist" in msg
        assert "important" in msg or "automation" in msg


class TestPhantomCategoryRejected:
    """Bug 16: phantom category must be rejected with VALIDATION_INVALID_PARAMETER."""

    async def test_create_phantom_category(self, register_tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(category_ids=["mood_lighting", "security"])
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ), pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test",
                category="phantom_category",
            )
        _assert_invalid_param(excinfo)
        msg = str(excinfo.value)
        assert "phantom_category" in msg
        assert "mood_lighting" in msg or "security" in msg


class TestExistingIdsPass:
    """Control: when IDs exist, the call must succeed and apply them."""

    async def test_existing_area_label_category_pass(self, register_tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(
                area_ids=["kitchen"],
                label_ids=["automation"],
                category_ids=["lighting"],
            )
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test",
                area_id="kitchen",
                labels=["automation"],
                category="lighting",
            )
        # Tool should succeed (no ToolError raised).
        assert isinstance(result, dict)
        assert result.get("success") is True


class TestEmptyStringAreaIdSkipsValidation:
    """Empty-string area_id is the documented 'clear' sentinel — must NOT validate."""

    async def test_empty_area_id_does_not_query_registry(
        self, register_tools, mock_client
    ):
        # Provide NO areas so any validation lookup would fail an existence
        # check; if the tool errors, then the validation guard is wrongly
        # treating "" as a real ID.
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(area_ids=[])
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test",
                area_id="",
            )
        assert isinstance(result, dict)
        assert result.get("success") is True

        # Defense in depth: ensure the area_registry/list endpoint was NOT
        # consulted for an empty-string clear (it's an unconditional skip).
        sent_types = [
            call[0][0].get("type")
            for call in mock_client.send_websocket_message.call_args_list
        ]
        assert "config/area_registry/list" not in sent_types


class TestPhantomRejectedAgainstEmptyRegistry:
    """Pin the (ok=True, items=[]) contract: phantom IDs must be rejected even
    when the registry is genuinely empty.

    The ``_validate_registry_ids`` helper returns a (ok, items) tuple to
    distinguish a successful-but-empty lookup from a lookup failure. Without
    explicit coverage, a future refactor that fails open on empty results
    would silently regress phantom-ID rejection."""

    async def test_phantom_area_rejected_against_empty_area_registry(
        self, register_tools, mock_client
    ):
        # Empty area registry — but a real phantom area_id must still be rejected.
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(area_ids=[])
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ), pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test",
                area_id="phantom_area",
            )
        _assert_invalid_param(excinfo)
        assert "phantom_area" in str(excinfo.value)

    async def test_phantom_label_rejected_against_empty_label_registry(
        self, register_tools, mock_client
    ):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(label_ids=[])
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ), pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test",
                labels=["phantom_label"],
            )
        _assert_invalid_param(excinfo)
        assert "phantom_label" in str(excinfo.value)

    async def test_phantom_category_rejected_against_empty_category_registry(
        self, register_tools, mock_client
    ):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(category_ids=[])
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ), pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test",
                category="phantom_category",
            )
        _assert_invalid_param(excinfo)
        assert "phantom_category" in str(excinfo.value)
