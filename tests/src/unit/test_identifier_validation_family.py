"""Unit tests for tool-side identifier validation policy.

Two layers of coverage live here:

1. **Helper-level** — direct unit tests for
   ``ha_mcp.tools.helpers.validate_identifier_not_empty``: every reject
   case (``None``, ``""``, ``"   "``, tab/newline-only, carriage return,
   vertical tab, non-breaking space, ideographic space) raises
   ``VALIDATION_INVALID_PARAMETER`` with the parameter name in
   ``context``; every accept case (``"abc"``, ``" abc "``) is a no-op.

2. **Call-site-level** — one rejection test per affected entry point in
   ``tools_labels.py``, ``tools_categories.py``, ``tools_areas.py``, and
   ``tools_config_helpers.py``, asserting:

   - empty / whitespace identifier surfaces ``VALIDATION_INVALID_PARAMETER``
     (no WS message sent), and
   - the ``None`` "list-all" or "create-new" sentinel still works (the
     guard does not regress the documented routing).

The destructive class these tests pin down:

  ``action = "update" if label_id else "create"`` would route an
  empty-string ``label_id`` silently to ``create``. The guard surfaces a
  structured validation error naming ``label_id`` instead.
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

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "",
            " ",
            "   ",
            "\t",
            "\n",
            " \t\n ",
            "\r",
            "\v",
            "\xa0",  # non-breaking space (U+00A0)
            "　",  # ideographic space (U+3000)
        ],
    )
    def test_rejects_empty_or_whitespace(self, bad):
        with pytest.raises(ToolError) as excinfo:
            validate_identifier_not_empty(bad, "test_param")
        msg = str(excinfo.value)
        assert "VALIDATION_INVALID_PARAMETER" in msg
        assert "test_param" in msg

    @pytest.mark.parametrize("good", ["abc", " abc ", "x", "scene.movie_night", "0"])
    def test_accepts_valid_identifier(self, good):
        # Helper returns the validated value untouched so call sites can
        # rebind to narrow ``str | None`` → ``str`` for mypy.
        assert validate_identifier_not_empty(good, "test_param") == good

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

    def test_message_override_replaces_default_text(self):
        # The ``message`` override is used by call sites that want a tighter
        # context-specific phrase (e.g. "name is required when creating a
        # new area") while still routing through the shared helper.
        with pytest.raises(ToolError) as excinfo:
            validate_identifier_not_empty(
                "",
                "name",
                message="name is required when creating a new area",
            )
        msg = str(excinfo.value)
        assert "name is required when creating a new area" in msg
        # Default phrasing must not also appear when an override is supplied.
        assert "must be a non-empty, non-whitespace string" not in msg

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
        # Was guarded for ``id == ""`` but ``"   "`` slipped through the
        # truthy ``if id:`` branch and routed silently to update with an
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
        # ``if not name`` let ``"   "`` through into the create branch
        # because ``bool(" ") is True``.
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

    async def test_set_with_none_id_routes_to_create_for_area(self, tools):
        # Control symmetry with the labels/categories twins: None remains the
        # documented "create-new" sentinel and routes to area_registry/create.
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": {"area_id": "x", "name": "X"},
        }
        result = await tools.ha_set_area_or_floor(kind="area", name="X")
        assert result["success"] is True
        sent = tools._client.send_websocket_message.call_args[0][0]
        assert sent["type"] == "config/area_registry/create"

    async def test_set_with_none_id_routes_to_create_for_floor(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": {"floor_id": "x", "name": "X"},
        }
        result = await tools.ha_set_area_or_floor(kind="floor", name="X")
        assert result["success"] is True
        sent = tools._client.send_websocket_message.call_args[0][0]
        assert sent["type"] == "config/floor_registry/create"


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

    @pytest.mark.parametrize("helper_type", ["input_boolean", "utility_meter"])
    async def test_implicit_action_with_empty_helper_id_rejects(
        self, register_tools, mock_ws_client, helper_type
    ):
        # Implicit-discriminator path: ``action`` omitted, ``helper_id=""``.
        # Without the up-front guard, ``bool("")`` would be False so the
        # discriminator below silently routes to ``create`` instead of
        # ``update`` — destructive intent-loss class.
        # Parametrized so both the simple-helper guard (``input_boolean``)
        # and the flow-helper twin guard inside ``_handle_flow_helper``
        # (``utility_meter``) are exercised.
        set_helper = register_tools["ha_config_set_helper"]
        for bad in ("", "   "):
            mock_ws_client.send_websocket_message.reset_mock()
            with pytest.raises(ToolError) as excinfo:
                await set_helper(
                    helper_type=helper_type, helper_id=bad, name="X"
                )
            _assert_invalid_param(excinfo)
            mock_ws_client.send_websocket_message.assert_not_called()

    async def test_flow_helper_create_rejects_whitespace_name(
        self, register_tools, mock_ws_client
    ):
        # Flow-helper create gate parity with the simple-helper twin: the
        # top-level ``name`` arg must be non-whitespace, otherwise the
        # downstream config-flow build proceeds with a name HA cannot use.
        set_helper = register_tools["ha_config_set_helper"]
        with pytest.raises(ToolError) as excinfo:
            await set_helper(
                helper_type="utility_meter", action="create", name="   "
            )
        _assert_invalid_param(excinfo)
        # The pre-flow gate runs before any flow start — no WS round-trip.
        mock_ws_client.send_websocket_message.assert_not_called()

    async def test_flow_helper_create_rejects_whitespace_config_name(
        self, register_tools, mock_ws_client
    ):
        # Coverage for the other half of the name-required gate: when the
        # top-level ``name`` is None/empty, ``config_dict["name"]`` must also
        # be non-whitespace.
        set_helper = register_tools["ha_config_set_helper"]
        with pytest.raises(ToolError) as excinfo:
            await set_helper(
                helper_type="utility_meter",
                action="create",
                config={"name": "   "},
            )
        _assert_invalid_param(excinfo)
        mock_ws_client.send_websocket_message.assert_not_called()


class TestCheckNameCollisionWhitespaceSkip:
    """Direct test for the ``_check_name_collision`` dedupe-skip.

    The downstream name-required gate at the simple-helper create branch
    will reject whitespace-only names, but the collision check runs first
    and would otherwise burn a WebSocket round-trip on a name HA is about
    to reject. Locks the early-return on whitespace-only ``name`` so the
    optimisation is not regressed by a refactor.
    """

    @pytest.mark.parametrize("bad_name", [None, "", " ", "   ", "\t", "\n"])
    async def test_skips_ws_call_on_empty_or_whitespace_name(
        self, mock_ws_client, bad_name
    ):
        from ha_mcp.tools.tools_config_helpers import _check_name_collision

        # The early-return runs before any WS message is constructed.
        result = await _check_name_collision(
            mock_ws_client, "input_boolean", bad_name
        )
        assert result is None
        mock_ws_client.send_websocket_message.assert_not_called()


# --- tools_resources.py (Round-2 sibling sweep) --------------------------


class TestResourcesIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_resources import ResourceTools

        return ResourceTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_rejects_empty_resource_id(self, tools, bad):
        # ``_upsert_resource`` previously routed ``resource_id=""`` to the
        # create branch via the truthy ``if resource_id:`` check, producing
        # a phantom dashboard resource instead of the intended update.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_set_dashboard_resource(
                url="/local/test.js", resource_type="module", resource_id=bad
            )
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()

    async def test_set_with_none_resource_id_routes_to_create(self, tools):
        # Control: None remains the documented "create-new" sentinel.
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": {"resource_id": "x"},
        }
        result = await tools.ha_config_set_dashboard_resource(
            url="/local/test.js", resource_type="module"
        )
        assert result["success"] is True
        sent = tools._client.send_websocket_message.call_args[0][0]
        assert sent["type"] == "lovelace/resources/create"

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_delete_rejects_empty_resource_id(self, tools, bad):
        # Empty/whitespace would surface as a misleading HA delete-failure.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_delete_dashboard_resource(resource_id=bad)
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()


# --- tools_zones.py (Round-2 sibling sweep) ------------------------------


class TestZonesIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_zones import ZoneTools

        return ZoneTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_rejects_empty_zone_id(self, tools, bad):
        # Without the guard, ``zone_id=""`` falls into the create branch and
        # surfaces "name, latitude, longitude required" — misleading UX
        # masking the real cause (unusable ``zone_id``).
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_zone(zone_id=bad, name="X")
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()

    async def test_set_with_none_zone_id_routes_to_create(self, tools):
        # Control: None remains the documented "create-new" sentinel.
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": {"zone_id": "x"},
        }
        result = await tools.ha_set_zone(
            name="Office", latitude=40.7128, longitude=-74.0060, radius=150
        )
        assert result["success"] is True
        sent = tools._client.send_websocket_message.call_args[0][0]
        assert sent["type"] == "zone/create"
