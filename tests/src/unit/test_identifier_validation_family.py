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
        # Parametrized across both helper-type families to lock that the
        # dispatch-level guard at ``ha_config_set_helper`` fires uniformly
        # regardless of which helper family the call would have routed to.
        # The defence-in-depth guard inside ``_handle_flow_helper`` is
        # exercised separately by ``TestFlowHelperDirectGuard``.
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

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_flow_explicit_update_rejects_empty_helper_id(
        self, register_tools, mock_ws_client, bad
    ):
        # Explicit-action update on a FLOW helper with empty/whitespace
        # helper_id. The L2378 ``helper_id is None`` guard does not catch
        # this (value is a non-None empty string), and the simple-path
        # whitespace twin inside ``elif action == "update":`` fires AFTER
        # the FLOW dispatch returns. Without the new guard between L2401
        # and the implicit-action branch, the value would reach
        # ``update_flow_helper`` and HA returns a misleading "entry not
        # found".
        set_helper = register_tools["ha_config_set_helper"]
        with pytest.raises(ToolError) as excinfo:
            await set_helper(
                helper_type="utility_meter", action="update", helper_id=bad
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

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_rejects_empty_zone_id(self, tools, bad):
        # Symmetric to ha_config_delete_dashboard_resource: empty/whitespace
        # would propagate to ``zone/delete`` and surface as a misleading HA
        # delete-failure instead of naming the unusable zone_id.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_zone(zone_id=bad)
        _assert_invalid_param(excinfo)
        tools._client.send_websocket_message.assert_not_called()


# --- tools_config_automations.py / tools_config_scripts.py / tools_groups.py
# (Round-3 sibling sweep — remove-symmetry across destructive write tools) --


class TestAutomationsIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_config_automations import AutomationConfigTools

        return AutomationConfigTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_rejects_empty_identifier(self, tools, bad):
        # Empty/whitespace identifier would propagate to delete_automation_config
        # and surface as a misleading HA delete-failure.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_remove_automation(identifier=bad)
        _assert_invalid_param(excinfo)
        tools._client.delete_automation_config.assert_not_called()


class TestScriptsIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

        return ConfigScriptTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_rejects_empty_script_id(self, tools, bad):
        # Empty/whitespace script_id would propagate to delete_script_config
        # and surface as a misleading HA delete-failure.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_remove_script(script_id=bad)
        _assert_invalid_param(excinfo)
        tools._client.delete_script_config.assert_not_called()


class TestGroupsIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_groups import GroupTools

        return GroupTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_rejects_empty_object_id(self, tools, bad):
        # Empty/whitespace object_id would propagate to the group.remove
        # service call and surface as a misleading HA service-call failure.
        # The pre-flight runs before the pre-existing "." format check so the
        # error names the empty/whitespace problem first. The
        # ``"parameter": "object_id"`` substring discriminates the new
        # validator's structured error from the ``.`` format check's error
        # (which uses ``context={"object_id": ...}`` without a ``parameter``
        # key) — a regression swapping the two guards' order would lose the
        # ``parameter`` field and break this assertion.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_remove_group(object_id=bad)
        _assert_invalid_param(excinfo)
        assert '"parameter": "object_id"' in str(excinfo.value), str(excinfo.value)
        tools._client.call_service.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_rejects_empty_object_id(self, tools, bad):
        # ``_validate_group_params`` only catches ``"." in object_id`` and
        # mutex/empty-list issues; empty/whitespace ``object_id`` would slip
        # through to ``call_service("group", "set", ...)`` and surface as a
        # misleading HA service-call failure. Symmetric with the
        # ``ha_config_remove_group`` pre-flight added in this PR.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_set_group(
                object_id=bad, entities=["light.example"]
            )
        _assert_invalid_param(excinfo)
        assert '"parameter": "object_id"' in str(excinfo.value), str(excinfo.value)
        tools._client.call_service.assert_not_called()


# --- _handle_flow_helper direct guard test --------------------------------
#
# The parametrize on TestSetHelperWhitespaceUpgrade.test_implicit_action_with_
# empty_helper_id_rejects locks behavioural parity at the public-tool dispatch
# level (both helper_types hit the dispatch-level guard inside
# ha_config_set_helper). The direct test below bypasses the dispatch entry and
# exercises the defence-in-depth guard inside ``_handle_flow_helper`` itself,
# so a future refactor that drops the dispatch-level guard would still leave
# this twin as the locked safety net.


class TestFlowHelperDirectGuard:
    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_handle_flow_helper_implicit_action_rejects_empty_helper_id(
        self, mock_ws_client, bad
    ):
        from ha_mcp.tools.tools_config_helpers import _handle_flow_helper

        with pytest.raises(ToolError) as excinfo:
            await _handle_flow_helper(
                client=mock_ws_client,
                helper_type="utility_meter",
                name="X",
                helper_id=bad,
                config=None,
                area_id=None,
                labels=None,
                category=None,
                wait=False,
                action=None,  # implicit-discriminator path
            )
        _assert_invalid_param(excinfo)
        mock_ws_client.send_websocket_message.assert_not_called()

    async def test_handle_flow_helper_with_none_helper_id_does_not_raise_guard(
        self, mock_ws_client, monkeypatch
    ):
        # Control: ``helper_id=None`` is the documented "create-new" sentinel
        # and must NOT trip the implicit-action guard. The previous
        # ``"helper_id" not in msg`` assertion would silently pass on any
        # guard-message rewording; tighten to positive proof — mock the
        # validator and assert it was not invoked on the None path, which
        # is independent of any downstream behaviour.
        from ha_mcp.tools import tools_config_helpers

        validator_mock = MagicMock()
        monkeypatch.setattr(
            tools_config_helpers,
            "validate_identifier_not_empty",
            validator_mock,
        )

        try:
            await tools_config_helpers._handle_flow_helper(
                client=mock_ws_client,
                helper_type="utility_meter",
                name="My Meter",
                helper_id=None,
                config=None,
                area_id=None,
                labels=None,
                category=None,
                wait=False,
                action=None,
            )
        except Exception:
            # Downstream may raise (mocked client returns nothing useful);
            # the guard-not-invoked assertion below is independent of that.
            pass
        validator_mock.assert_not_called()


# --- tools_integrations.py (Round-4 sibling sweep) -----------------------
#
# Two destructive-class siblings the round-3 audit missed:
#   1. ``ha_delete_helpers_integrations`` — empty/whitespace ``target``
#      would reach the destructive backend call on every routing path
#      (simple-helper WS delete, flow-helper entry-resolution, direct
#      config-entry delete). Single up-front guard closes all three.
#   2. ``ha_set_integration_enabled`` — empty/whitespace ``entry_id`` would
#      reach the ``config_entries/disable`` WS message and surface as a
#      misleading HA "config entry not found".


class TestIntegrationsIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_integrations import IntegrationTools

        return IntegrationTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    @pytest.mark.parametrize(
        "helper_type",
        [None, "input_boolean", "utility_meter"],
        ids=["direct_entry", "simple_helper", "flow_helper"],
    )
    async def test_delete_helpers_integrations_rejects_empty_target(
        self, tools, bad, helper_type
    ):
        # Parametrized across all three routing paths (None→direct entry,
        # SIMPLE→ws delete, FLOW→entry-resolution) so the single up-front
        # guard is locked against a regression that moves it inside any
        # one path.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_delete_helpers_integrations(
                target=bad, helper_type=helper_type, confirm=True
            )
        _assert_invalid_param(excinfo)
        assert '"parameter": "target"' in str(excinfo.value), str(excinfo.value)
        # No backend call should fire — guard precedes every dispatch arm.
        tools._client.send_websocket_message.assert_not_called()
        tools._client.delete_config_entry.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_integration_enabled_rejects_empty_entry_id(
        self, tools, bad
    ):
        # ``entry_id`` is passed straight into ``config_entries/disable``;
        # without the guard, ``entry_id=""`` would surface as a misleading
        # HA "config entry not found".
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_integration_enabled(entry_id=bad, enabled=False)
        _assert_invalid_param(excinfo)
        assert '"parameter": "entry_id"' in str(excinfo.value), str(excinfo.value)
        tools._client.send_websocket_message.assert_not_called()


# --- tools_calendar.py (Round-4 sibling sweep) ---------------------------


class TestCalendarIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_calendar import CalendarTools

        return CalendarTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_event_rejects_empty_uid(self, tools, bad):
        # The entity_id format-check at the top of the body does not cover
        # ``uid``; without the new guard, ``uid=""`` would flow through to
        # ``calendar.delete_event`` and surface as a misleading HA
        # "event not found".
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_config_remove_calendar_event(
                entity_id="calendar.family", uid=bad
            )
        _assert_invalid_param(excinfo)
        assert '"parameter": "uid"' in str(excinfo.value), str(excinfo.value)
        tools._client.call_service.assert_not_called()


# --- tools_todo.py (Round-4 sibling sweep) -------------------------------


class TestTodoIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_todo import TodoTools

        return TodoTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_item_rejects_empty_item(self, tools, bad):
        # ``item`` is passed straight into ``todo.remove_item``; without the
        # new guard, ``item=""`` would surface as a misleading HA
        # "item not found".
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(
                entity_id="todo.shopping_list", item=bad
            )
        _assert_invalid_param(excinfo)
        assert '"parameter": "item"' in str(excinfo.value), str(excinfo.value)
        tools._client.call_service.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_item_rejects_empty_item_on_implicit_update(
        self, tools, bad
    ):
        # ``item`` is the implicit create/update discriminator: ``None``
        # routes to create, non-None to update. Without the new guard,
        # ``item=""`` would route to update (``"" is None`` is False) and
        # call ``todo.update_item`` with an empty item — destructive
        # silent-routing class identical to the helper implicit-discriminator
        # gate this PR closes. ``status="completed"`` is supplied so the
        # update-mode "at least one update field" gate doesn't fire first.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(
                entity_id="todo.shopping_list",
                item=bad,
                status="completed",
            )
        _assert_invalid_param(excinfo)
        assert '"parameter": "item"' in str(excinfo.value), str(excinfo.value)
        tools._client.call_service.assert_not_called()


# --- tools_entities.py (Round-4 sibling sweep) ---------------------------
#
# ``ha_remove_entity`` is registered via the module-level
# ``register_entity_tools`` function rather than a Tools class. The
# ``_register_and_capture`` helper mirrors the pattern in
# ``test_device_enrichment.py``.


def _register_entity_tools_and_capture(mock_client):
    from ha_mcp.tools.tools_entities import register_entity_tools

    mock_mcp = MagicMock()
    captured: dict[str, Any] = {}

    def fake_tool(**kwargs):
        def decorator(fn):
            captured[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp.tool = fake_tool
    register_entity_tools(mock_mcp, mock_client)
    return captured


class TestEntitiesIdentifierValidation:
    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_entity_rejects_empty_entity_id(
        self, mock_ws_client, bad
    ):
        # ``entity_id`` is passed straight into the
        # ``config/entity_registry/remove`` WS message; without the new
        # guard, ``entity_id=""`` surfaces as a misleading HA
        # "entity not found".
        captured = _register_entity_tools_and_capture(mock_ws_client)
        ha_remove_entity = captured["ha_remove_entity"]

        with pytest.raises(ToolError) as excinfo:
            await ha_remove_entity(entity_id=bad)
        _assert_invalid_param(excinfo)
        assert '"parameter": "entity_id"' in str(excinfo.value), str(
            excinfo.value
        )
        mock_ws_client.send_websocket_message.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_entity_rejects_empty_entity_id_str(
        self, mock_ws_client, bad
    ):
        # ``ha_set_entity`` accepts ``entity_id: str | list[str]``. The
        # existing list-empty check rejects ``[]`` but lets ``[""]``
        # through; for the string input path, ``""`` was normalised to
        # ``[""]`` and propagated to the entity-registry update WS call.
        # The new per-element guard closes both paths.
        captured = _register_entity_tools_and_capture(mock_ws_client)
        ha_set_entity = captured["ha_set_entity"]

        with pytest.raises(ToolError) as excinfo:
            await ha_set_entity(entity_id=bad, name="New Name")
        _assert_invalid_param(excinfo)
        assert '"parameter": "entity_id"' in str(excinfo.value), str(
            excinfo.value
        )
        mock_ws_client.send_websocket_message.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_set_entity_rejects_empty_entity_id_in_list(
        self, mock_ws_client, bad
    ):
        # List-input path: ``[""]`` and ``["sensor.real", ""]`` must both
        # be rejected per-element, not just rejected when the list itself
        # is empty.
        captured = _register_entity_tools_and_capture(mock_ws_client)
        ha_set_entity = captured["ha_set_entity"]

        with pytest.raises(ToolError) as excinfo:
            await ha_set_entity(
                entity_id=["sensor.real", bad],
                categories={"automation": "cat_id"},
            )
        _assert_invalid_param(excinfo)
        assert '"parameter": "entity_id"' in str(excinfo.value), str(
            excinfo.value
        )
        mock_ws_client.send_websocket_message.assert_not_called()


# --- tools_registry.py (Round-4 sibling sweep) ---------------------------


def _register_registry_tools_and_capture(mock_client):
    from ha_mcp.tools.tools_registry import register_registry_tools

    mock_mcp = MagicMock()
    captured: dict[str, Any] = {}

    def fake_tool(**kwargs):
        def decorator(fn):
            captured[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp.tool = fake_tool
    register_registry_tools(mock_mcp, mock_client)
    return captured


class TestRegistryIdentifierValidation:
    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_remove_device_rejects_empty_device_id(
        self, mock_ws_client, bad
    ):
        # Empty/whitespace ``device_id`` would slip past the local-filter
        # check (``next((d for d in devices if d.get("id") == device_id)...)``)
        # after wasting a ``config/device_registry/list`` round-trip, and
        # surface as a generic "Device not found: " error. Guard fires
        # before the list WS call.
        captured = _register_registry_tools_and_capture(mock_ws_client)
        ha_remove_device = captured["ha_remove_device"]

        with pytest.raises(ToolError) as excinfo:
            await ha_remove_device(device_id=bad)
        _assert_invalid_param(excinfo)
        assert '"parameter": "device_id"' in str(excinfo.value), str(
            excinfo.value
        )
        mock_ws_client.send_websocket_message.assert_not_called()

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_update_device_rejects_empty_device_id(
        self, mock_ws_client, bad
    ):
        # ``device_id`` is passed straight through ``ha_update_device`` to
        # ``_update_device_internal`` which builds a
        # ``config/device_registry/update`` WS message; without the new
        # guard, ``device_id=""`` would surface as a misleading HA
        # "device not found". Same destructive-WS-call class as
        # ``ha_remove_device``.
        captured = _register_registry_tools_and_capture(mock_ws_client)
        ha_update_device = captured["ha_update_device"]

        with pytest.raises(ToolError) as excinfo:
            await ha_update_device(device_id=bad, name="New Name")
        _assert_invalid_param(excinfo)
        assert '"parameter": "device_id"' in str(excinfo.value), str(
            excinfo.value
        )
        mock_ws_client.send_websocket_message.assert_not_called()


# --- tools_addons.py (Iter6 — ha_manage_addon slug) ----------------------


def _register_addon_tools_and_capture(mock_client):
    from ha_mcp.tools.tools_addons import register_addon_tools

    mock_mcp = MagicMock()
    captured: dict[str, Any] = {}

    def fake_tool(**kwargs):
        def decorator(fn):
            captured[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp.tool = fake_tool
    register_addon_tools(mock_mcp, mock_client)
    return captured


class TestAddonsIdentifierValidation:
    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_manage_addon_rejects_empty_slug(self, mock_ws_client, bad):
        # ``ha_manage_addon`` is multi-modal (proxy / config / websocket);
        # ``slug`` is required across all modes and propagates to the
        # Supervisor API on every dispatch arm. Without the guard,
        # ``slug=""`` would surface as a misleading "addon not found" /
        # 404 from the Supervisor; the up-front guard names the offending
        # parameter before any backend call.
        captured = _register_addon_tools_and_capture(mock_ws_client)
        ha_manage_addon = captured["ha_manage_addon"]

        with pytest.raises(ToolError) as excinfo:
            await ha_manage_addon(slug=bad, path="/api/health")
        _assert_invalid_param(excinfo)
        assert '"parameter": "slug"' in str(excinfo.value), str(excinfo.value)
        mock_ws_client.send_websocket_message.assert_not_called()


# --- tools_energy.py (Iter8 — ha_manage_energy_prefs stat_consumption) ---


class TestEnergyPrefsIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_energy import EnergyTools

        # add an AsyncMock for save_prefs (used by _mutate_atomic) so the
        # downstream path is reachable in principle — the guard must fire
        # before we get there.
        mock_ws_client.send_websocket_message.return_value = {
            "success": True,
            "result": {"prefs": {}},
        }
        return EnergyTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    @pytest.mark.parametrize(
        "mode", ["add_device", "remove_device"], ids=["add", "remove"]
    )
    async def test_manage_energy_prefs_rejects_empty_stat_consumption(
        self, tools, bad, mode
    ):
        # Both ``add_device`` and ``remove_device`` modes require
        # ``stat_consumption`` and pass it to the prefs storage. Without
        # the guard, ``add_device`` would write a ``{"stat_consumption": ""}``
        # phantom entry, and ``remove_device`` would search for an empty
        # match (always missing) and surface as a misleading
        # "Device with stat_consumption='' not found".
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_energy_prefs(mode=mode, stat_consumption=bad)
        _assert_invalid_param(excinfo)
        assert '"parameter": "stat_consumption"' in str(excinfo.value), str(
            excinfo.value
        )
        tools._client.send_websocket_message.assert_not_called()


# --- tools_hacs.py (Iter7 — ha_hacs_download repository_id) --------------


class TestHacsIdentifierValidation:
    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_hacs import HacsTools

        return HacsTools(mock_ws_client)

    @pytest.mark.parametrize("bad", ["", "   "])
    async def test_hacs_download_rejects_empty_repository_id(self, tools, bad):
        # Empty/whitespace ``repository_id`` would either fall through
        # ``_resolve_hacs_repo_id`` (no empty-check) into a HACS lookup
        # miss, or — for a numeric-looking candidate — reach
        # ``hacs/repository/download`` with an empty repository field.
        # Same destructive-WS-call class as ``ha_manage_addon``; the
        # guard fires before any backend call (including the HACS
        # availability check) so neither the supervisor nor HACS sees
        # the empty id.
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_hacs_download(repository_id=bad)
        _assert_invalid_param(excinfo)
        assert '"parameter": "repository_id"' in str(excinfo.value), str(
            excinfo.value
        )
        tools._client.send_websocket_message.assert_not_called()
