"""Unit tests for Bugs 11 and 12 (issue #1150).

Bug 11: ``ha_config_set_helper`` previously inferred create-vs-update from which
of ``name`` / ``helper_id`` was passed. Passing BOTH silently mode-switched to
UPDATE, so a typo in helper_id surfaced as ``ENTITY_NOT_FOUND`` even though the
caller had supplied a valid ``name``. The fix rejects the ambiguous call with
``VALIDATION_INVALID_PARAMETER``.

Bug 12: HA's ``{type}/create`` endpoints auto-suffix duplicate names with
``_2`` / ``_3`` / ..., so a "create" call against an existing name returns
``success: True`` even though the caller actually got a duplicate entity. The
fix queries the helper list first and raises ``VALIDATION_INVALID_PARAMETER``
with the existing helper_id in the suggestion when the slug already exists.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

# ---------------------------------------------------------------------------
# Fixtures — local copy of the pattern used by test_helper_field_persistence.py.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    """Mock client. Tests configure ``send_websocket_message`` per-scenario."""
    return MagicMock()


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


def _assert_invalid_param(excinfo, *, must_contain: str | None = None) -> None:
    msg = str(excinfo.value)
    assert "VALIDATION_INVALID_PARAMETER" in msg, (
        f"expected VALIDATION_INVALID_PARAMETER, got: {msg!r}"
    )
    if must_contain is not None:
        assert must_contain in msg, (
            f"expected {must_contain!r} in error message, got: {msg!r}"
        )


def _make_simple_handler(
    *,
    helper_type: str,
    list_items: list[dict[str, Any]] | None = None,
    unique_id: str = "abc123",
):
    """Build a side_effect for ``send_websocket_message`` for SIMPLE helpers.

    ``list_items`` is what ``{type}/list`` should return (used to seed the
    Bug 12 collision detector).
    """
    items = list_items if list_items is not None else []

    async def ws_handler(msg: dict) -> dict:
        msg_type = msg.get("type", "")

        # Registry validators upstream of our checks must pass.
        if msg_type == "config/area_registry/list":
            return {"success": True, "result": []}
        if msg_type == "config/label_registry/list":
            return {"success": True, "result": []}
        if msg_type == "config/category_registry/list":
            return {"success": True, "result": []}

        # Collision detector + general listing.
        if msg_type == f"{helper_type}/list":
            # person/list returns {"storage": [...], "config": [...]}.
            if helper_type == "person":
                return {"success": True, "result": {"storage": items, "config": []}}
            return {"success": True, "result": items}

        if msg_type == "config/entity_registry/get":
            return {
                "success": True,
                "result": {
                    "entity_id": msg.get("entity_id"),
                    "unique_id": unique_id,
                    "platform": helper_type,
                },
            }

        # Generic catch-all so the success path runs to completion.
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


def _make_flow_handler(
    *,
    helper_type: str,
    config_entries: list[dict[str, Any]] | None = None,
):
    """Build a side_effect for flow-helper paths.

    ``config_entries`` is what ``config_entries/get`` should return (used to
    seed the Bug 12 collision detector for flow helpers).
    """
    entries = config_entries if config_entries is not None else []

    async def ws_handler(msg: dict) -> dict:
        msg_type = msg.get("type", "")

        if msg_type == "config/area_registry/list":
            return {"success": True, "result": []}
        if msg_type == "config/label_registry/list":
            return {"success": True, "result": []}
        if msg_type == "config/category_registry/list":
            return {"success": True, "result": []}

        if msg_type == "config_entries/get":
            return {"success": True, "result": entries}

        # The flow path itself is mocked at a higher level (create_flow_helper);
        # any leaked WS calls here just succeed-as-noop.
        return {"success": True, "result": {}}

    return ws_handler


# ---------------------------------------------------------------------------
# Bug 11 — both `name` and `helper_id` is ambiguous.
# ---------------------------------------------------------------------------


class TestBug11RenameOnUpdateIsAllowed:
    """Bug 11 (issue #1150) — `name` AND `helper_id` together is the LEGITIMATE
    rename pattern. The fix is NOT to reject this combination (which would break
    the rename use case); the underlying problem of confusing ENTITY_NOT_FOUND
    on a misspelled helper_id is mitigated by the existing per-type-update error
    branch that already includes helpful context. These tests confirm rename
    still works.
    """

    async def test_only_name_works_create_path(self, register_tools, mock_client):
        """Control: passing only ``name`` proceeds (no ambiguity error)."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(helper_type="input_boolean")
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Brand New Helper",
            )
        assert result["success"] is True
        assert result["action"] == "create"

    async def test_only_helper_id_works_update_path(self, register_tools, mock_client):
        """Control: passing only ``helper_id`` proceeds (no ambiguity error)."""
        # Existing entry the update will load.
        existing = [{"id": "abc123", "name": "OldName", "icon": "mdi:bell"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(
                helper_type="input_boolean", list_items=existing
            )
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                helper_id="abc123",
                icon="mdi:toggle-switch",
            )
        assert result["success"] is True
        assert result["action"] == "update"

    async def test_rename_via_update_with_both_name_and_helper_id(
        self, register_tools, mock_client
    ):
        """Bug 11 fix: rename pattern (name + helper_id) MUST still work."""
        existing = [{"id": "abc123", "name": "OldName", "icon": "mdi:bell"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(
                helper_type="input_boolean", list_items=existing
            )
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="NewDisplayName",
                helper_id="abc123",
            )
        assert result["success"] is True
        assert result["action"] == "update"


# ---------------------------------------------------------------------------
# Bug 12 — name collision on create silently produced `_2`/`_3` duplicates.
# ---------------------------------------------------------------------------


class TestBug12NameCollisionSimpleHelpers:
    """Creating a simple helper with an existing name must raise."""

    @pytest.mark.parametrize(
        "helper_type",
        ["input_boolean", "counter", "input_number"],
    )
    async def test_name_collision_raises_with_existing_id(
        self, register_tools, mock_client, helper_type
    ):
        existing_id = "kitchen_light"
        existing = [{"id": existing_id, "name": "Kitchen Light"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(
                helper_type=helper_type, list_items=existing
            )
        )

        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type=helper_type,
                name="Kitchen Light",
            )

        _assert_invalid_param(excinfo, must_contain=existing_id)
        # Suggestion text must steer the caller toward an UPDATE.
        assert "helper_id" in str(excinfo.value)

    async def test_collision_is_slug_based_not_exact_string(
        self, register_tools, mock_client
    ):
        """Spaces / casing / punctuation collapse to the same slug."""
        existing = [{"id": "kitchen_light", "name": "Kitchen Light"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(
                helper_type="input_boolean", list_items=existing
            )
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="kitchen light!",  # different chars, same slug
            )
        _assert_invalid_param(excinfo, must_contain="kitchen_light")

    async def test_unique_name_passes_collision_check(
        self, register_tools, mock_client
    ):
        """Control: a truly new name should pass through to create."""
        existing = [{"id": "kitchen_light", "name": "Kitchen Light"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(
                helper_type="input_boolean", list_items=existing
            )
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Bedroom Light",
            )
        assert result["success"] is True
        assert result["action"] == "create"

    async def test_empty_name_falls_through_to_required_check(
        self, register_tools, mock_client
    ):
        """Empty `name` must raise the existing 'name is required' error,
        not a collision error.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(helper_type="input_boolean")
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="",
            )
        msg = str(excinfo.value)
        assert "VALIDATION_INVALID_PARAMETER" in msg
        # Existing required-check message — explicitly NOT a collision.
        assert "already exists" not in msg


class TestBug12NameCollisionFlowHelper:
    """Creating a flow helper with an existing title must raise."""

    async def test_template_collision_via_config_entries_get(
        self, register_tools, mock_client
    ):
        existing_entry_id = "01J99XYZTEMPLATE"
        existing_entries = [
            {"entry_id": existing_entry_id, "title": "Room Temp", "domain": "template"},
        ]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_flow_handler(
                helper_type="template", config_entries=existing_entries
            )
        )

        # The flow create path itself is mocked away — we only care that the
        # collision check fires before HA is called.
        with patch(
            "ha_mcp.tools.tools_config_helpers.create_flow_helper",
            new_callable=AsyncMock,
            return_value={"entry_id": "shouldnotbecreated", "title": "Room Temp",
                          "message": "ok"},
        ), pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="template",
                name="Room Temp",
                config={
                    "next_step_id": "sensor",
                    "state": "{{ states('sensor.x')|float }}",
                },
            )

        _assert_invalid_param(excinfo, must_contain=existing_entry_id)

    async def test_template_unique_name_passes(self, register_tools, mock_client):
        """Control: a unique flow-helper name proceeds to create_flow_helper."""
        existing_entries = [
            {"entry_id": "EXISTING", "title": "Other Sensor", "domain": "template"},
        ]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_flow_handler(
                helper_type="template", config_entries=existing_entries
            )
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.create_flow_helper",
            new_callable=AsyncMock,
            return_value={
                "entry_id": "NEWENTRY",
                "title": "Brand New Sensor",
                "message": "ok",
            },
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="template",
                name="Brand New Sensor",
                config={
                    "next_step_id": "sensor",
                    "state": "{{ states('sensor.x')|float }}",
                },
            )
        assert result["success"] is True
        assert result["action"] == "create"
        assert result["entry_id"] == "NEWENTRY"
