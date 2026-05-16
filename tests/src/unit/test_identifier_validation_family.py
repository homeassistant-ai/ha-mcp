"""Unit tests for tool-side identifier validation policy (issue #1294).

The maintainer signal on #1294 from kingpanther13 endorses a
"belt and suspenders" approach with per-tool nuance: tool-side
empty/whitespace identifier rejection is valuable for the destructive
intent-loss class (``action = "update" if id else "create"`` on the
registry-metadata tools) and for partial guards that miss whitespace-only
strings on the simple-helper writes.

Two layers of coverage live here:

1. **Helper-level** — direct unit tests for
   ``ha_mcp.tools.helpers.validate_identifier_not_empty``: every reject
   case (``None``, ``""``, ``"   "``, tab/newline-only) raises
   ``VALIDATION_INVALID_PARAMETER`` with the parameter name in
   ``context``; every accept case (``"abc"``, ``" abc "``) is a no-op.

2. **Call-site-level** — one rejection test per affected entry point in
   ``tools_labels.py``, ``tools_categories.py``, ``tools_areas.py``, and
   ``tools_config_helpers.py``, asserting:

   - empty / whitespace identifier surfaces ``VALIDATION_INVALID_PARAMETER``
     (no WS message sent), and
   - the ``None`` "list-all" or "create-new" sentinel still works (the
     guard does not regress the documented routing).

The destructive class this PR closes:

  ``action = "update" if label_id else "create"`` previously routed an
  empty-string ``label_id`` to ``create`` silently. After this PR, the
  caller sees a structured validation error naming ``label_id`` instead.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.helpers import validate_identifier_not_empty

# ---------------------------------------------------------------------------
# Layer 1 — helper unit tests (no tool-class plumbing involved).
# ---------------------------------------------------------------------------


class TestValidateIdentifierNotEmptyHelper:
    """Direct tests for the shared validator."""

    @pytest.mark.parametrize("bad", [None, "", " ", "   ", "\t", "\n", " \t\n "])
    def test_rejects_empty_or_whitespace(self, bad):
        with pytest.raises(ToolError) as excinfo:
            validate_identifier_not_empty(bad, "test_param")
        msg = str(excinfo.value)
        assert "VALIDATION_INVALID_PARAMETER" in msg
        assert "test_param" in msg

    @pytest.mark.parametrize("good", ["abc", " abc ", "x", "scene.movie_night", "0"])
    def test_accepts_valid_identifier(self, good):
        # Returns None — must not raise on legitimate values.
        assert validate_identifier_not_empty(good, "test_param") is None

    def test_merges_caller_context_into_error(self):
        with pytest.raises(ToolError) as excinfo:
            validate_identifier_not_empty(
                "  ",
                "label_id",
                suggestions=["Omit label_id to create a new label"],
                context={"action": "set", "name": "Critical"},
            )
        msg = str(excinfo.value)
        assert "label_id" in msg
        assert "Omit label_id" in msg
        assert "action" in msg and "set" in msg
        assert "Critical" in msg

    def test_caller_context_does_not_shadow_parameter_name(self):
        # The helper always records the canonical ``parameter`` and ``value``
        # keys regardless of what the caller passed in ``context``.
        with pytest.raises(ToolError) as excinfo:
            validate_identifier_not_empty(
                "",
                "label_id",
                context={"parameter": "imposter", "value": "imposter"},
            )
        msg = str(excinfo.value)
        # Canonical name wins over caller-supplied "imposter" so downstream
        # diagnostics can trust the recorded parameter.
        assert '"parameter": "label_id"' in msg


# ---------------------------------------------------------------------------
# Layer 2 — call-site rejection tests per affected module.
# ---------------------------------------------------------------------------


def _assert_invalid_param(excinfo: pytest.ExceptionInfo[ToolError]) -> None:
    msg = str(excinfo.value)
    assert "VALIDATION_INVALID_PARAMETER" in msg, msg


@pytest.fixture
def mock_ws_client():
    """Mock client whose ``send_websocket_message`` records every call.

    Tests that expect validation rejection assert the mock was *not* called
    (the guard fires before any WS round-trip); tests that exercise the
    legitimate "list-all"/"create-new" path provide a single success
    response so the body completes.
    """
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return client


# --- tools_labels.py ------------------------------------------------------


class TestLabelsIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_labels import LabelTools

        return LabelTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_rejects_empty_label_id(self, tools, bad):
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_set_label(name="X", label_id=bad)
        _assert_invalid_param(excinfo)
        # Guard fires before WS — never reaches send.
        tools._client.send_websocket_message.assert_not_called()

    async def test_set_with_none_label_id_routes_to_create(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": {"label_id": "x"},
        }
        result = await tools.ha_config_set_label(name="X")
        assert result["success"] is True
        sent = tools._client.send_websocket_message.call_args[0][0]
        assert sent["type"] == "config/label_registry/create"

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_get_rejects_empty_label_id(self, tools, bad):
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_get_label(label_id=bad)
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_rejects_empty_label_id(self, tools, bad):
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_remove_label(label_id=bad)
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()


# --- tools_categories.py --------------------------------------------------


class TestCategoriesIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_categories import CategoryTools

        return CategoryTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_rejects_empty_category_id(self, tools, bad):
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_set_category(
                name="X", scope="automation", category_id=bad
            )
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()

    async def test_set_with_none_routes_to_create(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": {"category_id": "x"},
        }
        result = await tools.ha_config_set_category(name="X", scope="automation")
        assert result["success"] is True
        sent = tools._client.send_websocket_message.call_args[0][0]
        assert sent["type"] == "config/category_registry/create"

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_get_rejects_empty_category_id(self, tools, bad):
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_get_category(scope="automation", category_id=bad)
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_rejects_empty_category_id(self, tools, bad):
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_remove_category(
                scope="automation", category_id=bad
            )
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()


# --- tools_areas.py -------------------------------------------------------


class TestAreasIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_areas import AreaTools

        return AreaTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_rejects_whitespace_id_for_area(self, tools, bad):
        # Pre-#1294: ``id == ""`` was guarded but ``"   "`` slipped through
        # the truthy ``if id:`` branch and routed silently to update with an
        # invalid id. This regression test locks the whitespace upgrade.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_area_or_floor(kind="area", id=bad, name="X")
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_rejects_whitespace_id_for_floor(self, tools, bad):
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_area_or_floor(kind="floor", id=bad, name="X")
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_create_rejects_whitespace_name_for_area(self, tools, bad):
        # Pre-#1294: ``if not name`` let ``"   "`` through into the create
        # branch because ``bool(" ") is True``.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_area_or_floor(kind="area", name=bad)
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_create_rejects_whitespace_name_for_floor(self, tools, bad):
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_area_or_floor(kind="floor", name=bad)
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_rejects_whitespace_id(self, tools, bad):
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_area_or_floor(kind="area", id=bad)
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()


# --- tools_config_helpers.py (partial-guard whitespace upgrade) -----------


class TestSetHelperWhitespaceUpgrade:
    """Locks the .strip()-aware upgrade on the two pre-existing partial
    guards in ``ha_config_set_helper`` (create-name, update-helper_id)."""

    @pytest.fixture
    def register_tools(self, mock_ws_client):
        from ha_mcp.tools.tools_config_helpers import register_config_helper_tools

        registered: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn

            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool
        register_config_helper_tools(mock_mcp, mock_ws_client)
        return registered

    async def test_create_rejects_whitespace_name(self, register_tools, mock_ws_client):
        set_helper = register_tools["ha_config_set_helper"]
        with pytest.raises(ToolError) as excinfo:
            await set_helper(
                helper_type="input_boolean", action="create", name="   "
            )
        _assert_invalid_param(excinfo)
        # Validation fires before the WS round-trip.
        mock_ws_client.send_websocket_message.assert_not_called()

    async def test_update_rejects_whitespace_helper_id(
        self, register_tools, mock_ws_client
    ):
        set_helper = register_tools["ha_config_set_helper"]
        with pytest.raises(ToolError) as excinfo:
            await set_helper(
                helper_type="input_boolean",
                action="update",
                helper_id="   ",
                name="X",
            )
        _assert_invalid_param(excinfo)
        mock_ws_client.send_websocket_message.assert_not_called()
