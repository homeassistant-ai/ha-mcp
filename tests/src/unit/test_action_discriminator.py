"""Unit tests for the explicit ``action`` discriminator in
``ha_config_set_helper`` (Bug 11 design fix, issue #1150).

History
-------
The original Bug 11 fix (see ``test_action_and_collision.py``) preserved the
implicit discriminator (presence of ``helper_id`` => update) because the
rename pattern legitimately passes BOTH ``name`` (new display name) and
``helper_id`` (which entity to update). That meant a misspelled ``helper_id``
combined with a ``name`` still surfaced as ``ENTITY_NOT_FOUND`` — useful but
opaque, since the caller's intent (create-with-typo vs update-with-typo)
remained ambiguous.

The proper fix adds an explicit ``action: Literal["create","update"] | None``
parameter:

- When ``action`` is omitted, behaviour is unchanged (back-compat: implicit
  discriminator from ``helper_id`` presence). The misspelled-helper_id case
  also gets a clearer ``ENTITY_NOT_FOUND`` message that, when ``name`` was
  also passed, suggests "if you meant to create, omit helper_id".
- When ``action="create"``, passing ``helper_id`` is rejected as contradictory.
- When ``action="update"``, ``helper_id`` is required (and rename via
  ``name`` continues to work).

This file covers all combinations enumerated in the task spec.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

# ---------------------------------------------------------------------------
# Fixtures — mirror test_action_and_collision.py to avoid cross-file fixture
# coupling.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
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


def _assert_entity_not_found(excinfo, *, must_contain: str | None = None) -> None:
    msg = str(excinfo.value)
    assert "ENTITY_NOT_FOUND" in msg, (
        f"expected ENTITY_NOT_FOUND, got: {msg!r}"
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
    registry_get_succeeds: bool = True,
):
    """Build a side_effect for ``send_websocket_message`` for SIMPLE helpers.

    ``registry_get_succeeds=False`` simulates a misspelled helper_id (the
    ``config/entity_registry/get`` call returns success=False, which is what
    the update path treats as ENTITY_NOT_FOUND).
    """
    items = list_items if list_items is not None else []

    async def ws_handler(msg: dict) -> dict:
        msg_type = msg.get("type", "")

        # Registry validators.
        if msg_type == "config/area_registry/list":
            return {"success": True, "result": []}
        if msg_type == "config/label_registry/list":
            return {"success": True, "result": []}
        if msg_type == "config/category_registry/list":
            return {"success": True, "result": []}

        # Collision detector + general listing.
        if msg_type == f"{helper_type}/list":
            if helper_type == "person":
                return {"success": True, "result": {"storage": items, "config": []}}
            return {"success": True, "result": items}

        if msg_type == "config/entity_registry/get":
            if not registry_get_succeeds:
                return {"success": False, "error": {"code": "not_found"}}
            return {
                "success": True,
                "result": {
                    "entity_id": msg.get("entity_id"),
                    "unique_id": unique_id,
                    "platform": helper_type,
                },
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
# Implicit discriminator — back-compat path (no `action` argument).
# These confirm that the legacy behaviour still holds when callers don't
# opt into the explicit form.
# ---------------------------------------------------------------------------


class TestImplicitDiscriminatorBackCompat:
    async def test_only_name_creates(self, register_tools, mock_client):
        """No `action`, no helper_id -> implicit create."""
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
                name="Plain Create",
            )
        assert result["success"] is True
        assert result["action"] == "create"

    async def test_only_helper_id_updates(self, register_tools, mock_client):
        """No `action`, helper_id present -> implicit update."""
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

    async def test_rename_pattern_still_works(self, register_tools, mock_client):
        """Rename: name + helper_id without `action` -> implicit update.

        This is the case that broke the earlier "reject both" attempt at the
        Bug 11 fix; it MUST keep working.
        """
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

    async def test_misspelled_helper_id_with_name_hints_at_create(
        self, register_tools, mock_client
    ):
        """Bug 11 augmented error: ENTITY_NOT_FOUND now suggests omitting
        helper_id (or passing action='create') when `name` was also passed.

        Triggered against the config_store_types update branch (input_boolean)
        which goes through ``config/entity_registry/get`` and treats a failed
        lookup as ENTITY_NOT_FOUND.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(
                helper_type="input_boolean", registry_get_succeeds=False
            )
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Brand New Helper",
                helper_id="nonexistent_typo",
            )
        _assert_entity_not_found(excinfo, must_contain="nonexistent_typo")
        msg = str(excinfo.value)
        # The augmented suggestion should mention `omit helper_id` and the
        # provided name, so the caller can correct course in one step.
        assert "Brand New Helper" in msg
        assert "omit helper_id" in msg

    async def test_misspelled_helper_id_without_name_no_create_hint(
        self, register_tools, mock_client
    ):
        """Without `name`, the augmented error skips the create suggestion
        (there's no hypothesis to suggest)."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(
                helper_type="input_boolean", registry_get_succeeds=False
            )
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                helper_id="nonexistent_typo",
            )
        _assert_entity_not_found(excinfo, must_contain="nonexistent_typo")
        msg = str(excinfo.value)
        # No `name` hypothesis -> no "If you meant to create" line.
        assert "If you meant to create" not in msg


# ---------------------------------------------------------------------------
# Explicit `action='create'` — intent contradictions get caught early.
# ---------------------------------------------------------------------------


class TestExplicitActionCreate:
    async def test_create_only_name_works(self, register_tools, mock_client):
        """`action='create'` with only `name` is the canonical create call."""
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
                name="Explicit Create",
                action="create",
            )
        assert result["success"] is True
        assert result["action"] == "create"

    async def test_create_with_helper_id_is_contradictory(
        self, register_tools, mock_client
    ):
        """`action='create'` + `helper_id` -> validation error (intent clash)."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(helper_type="input_boolean")
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Some Name",
                helper_id="abc123",
                action="create",
            )
        _assert_invalid_param(excinfo, must_contain="contradictory")
        msg = str(excinfo.value)
        # Suggestions should give callers two clear repair paths.
        assert "Omit helper_id" in msg
        assert "action='update'" in msg

    async def test_create_without_name_falls_through_to_required_check(
        self, register_tools, mock_client
    ):
        """`action='create'` without `name` should still hit the existing
        'name is required' validation. The discriminator block must not
        short-circuit that check away.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(helper_type="input_boolean")
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                action="create",
            )
        _assert_invalid_param(excinfo, must_contain="name is required")


# ---------------------------------------------------------------------------
# Explicit `action='update'` — rename pattern + missing-helper_id rejection.
# ---------------------------------------------------------------------------


class TestExplicitActionUpdate:
    async def test_update_requires_helper_id(self, register_tools, mock_client):
        """`action='update'` with no `helper_id` -> validation error.

        This is the symmetric guard to the create-with-helper_id check.
        Without `helper_id` we have no entity to update.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(helper_type="input_boolean")
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Just A Name",
                action="update",
            )
        _assert_invalid_param(excinfo, must_contain="helper_id")
        msg = str(excinfo.value)
        # Suggest the two repair paths.
        assert "action='create'" in msg

    async def test_update_with_only_helper_id_works(
        self, register_tools, mock_client
    ):
        """`action='update'` + `helper_id` (no name) is a plain update."""
        existing = [{"id": "abc123", "name": "OldName"}]
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
                action="update",
            )
        assert result["success"] is True
        assert result["action"] == "update"

    async def test_update_rename_pattern_works(self, register_tools, mock_client):
        """`action='update'` + name + helper_id is the canonical RENAME call.

        This is the legitimate use of "both passed". Adding the explicit
        action makes intent unambiguous.
        """
        existing = [{"id": "abc123", "name": "OldName"}]
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
                action="update",
            )
        assert result["success"] is True
        assert result["action"] == "update"

    async def test_update_with_misspelled_helper_id_still_includes_create_hint(
        self, register_tools, mock_client
    ):
        """Even with explicit `action='update'`, a non-resolving helper_id
        with a `name` arg should still surface the augmented suggestion. The
        error wording cost is low and the caller may have typoed an existing
        helper while genuinely meaning to update — surfacing the create
        alternative gives them a quick recovery path either way.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(
                helper_type="input_boolean", registry_get_succeeds=False
            )
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Maybe New Helper",
                helper_id="nonexistent",
                action="update",
            )
        _assert_entity_not_found(excinfo, must_contain="nonexistent")
        msg = str(excinfo.value)
        assert "Maybe New Helper" in msg


# ---------------------------------------------------------------------------
# Cross-cutting: the existing required-name check still fires regardless of
# how the discriminator was set.
# ---------------------------------------------------------------------------


class TestEmptyNameFallsThroughToRequiredCheck:
    async def test_empty_name_implicit_create(self, register_tools, mock_client):
        """Implicit create + empty `name` -> name-required error (not a
        discriminator error)."""
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
        assert "name is required" in msg

    async def test_empty_name_explicit_create(self, register_tools, mock_client):
        """Explicit `action='create'` + empty `name` -> still required-name."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_simple_handler(helper_type="input_boolean")
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="",
                action="create",
            )
        msg = str(excinfo.value)
        assert "VALIDATION_INVALID_PARAMETER" in msg
        assert "name is required" in msg
