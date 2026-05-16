"""Unit tests for issue #1293 — uniform response shape across helper actions.

Lock the contract for ``ha_config_set_helper`` so create / update / flow-helper
branches return the same wrapper key (``data``) and the same warning shape
(``warnings`` — top-level list of strings). The pre-#1293 file exposed four
distinct shapes: nested ``helper_data["warning"]`` (create), top-level
``response["warning"]`` (update), nested ``updated_data["warning"]`` (update
registry path), top-level ``result["warnings"]`` plural list (flow). Callers
doing ``result.get("warning")`` or ``result.get("helper_data")`` uniformly used
to miss data on at least one branch.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError


@pytest.fixture
def mock_client():
    client = MagicMock()
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


def _assert_uniform_shape(result: dict[str, Any], *, expect_warnings: bool) -> None:
    """Common contract for all three branches.

    - ``data`` is always the wrapper key for the post-write payload.
    - ``warnings``, when present, is a list of strings (never a singular string).
    - Legacy keys must not leak.
    """
    assert result["success"] is True
    assert "data" in result, f"missing 'data' key: {result.keys()}"
    assert isinstance(result["data"], dict)
    # Legacy keys must be gone everywhere — pure rename per #1293.
    assert "helper_data" not in result, "legacy 'helper_data' wrapper still present"
    assert "updated_data" not in result, "legacy 'updated_data' wrapper still present"
    # ``data`` itself must not nest a singular ``warning`` string anymore.
    assert "warning" not in result["data"], (
        f"warning leaked into data payload: {result['data']}"
    )
    if expect_warnings:
        assert isinstance(result.get("warnings"), list)
        assert all(isinstance(w, str) for w in result["warnings"])
        assert result["warnings"], "warnings list present but empty"
    else:
        # Either absent or an empty list — both acceptable.
        assert "warnings" not in result or result["warnings"] == []


class TestUniformResponseShape:
    """Issue #1293: create / update / flow paths return the same wrapper shape."""

    async def test_create_simple_helper_uses_data_wrapper(
        self, register_tools, mock_client
    ):
        """Simple WS create returns ``data`` wrapper, no ``helper_data``."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"id": "abc123", "name": "Test Switch"},
            }
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
            )
        _assert_uniform_shape(result, expect_warnings=False)
        assert result["action"] == "create"
        assert result["data"]["id"] == "abc123"

    async def test_update_simple_helper_uses_data_wrapper(
        self, register_tools, mock_client
    ):
        """Simple WS update returns ``data`` wrapper, no ``updated_data``."""

        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/entity_registry/get":
                return {
                    "success": True,
                    "result": {
                        "entity_id": msg["entity_id"],
                        "unique_id": "abc123",
                        "platform": "input_select",
                    },
                }
            if msg_type.endswith("/list"):
                return {
                    "success": True,
                    "result": [
                        {"id": "abc123", "name": "Existing", "options": ["a", "b"]}
                    ],
                }
            if msg_type.endswith("/update"):
                return {
                    "success": True,
                    "result": {"id": "abc123", "options": ["x", "y"]},
                }
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                helper_id="input_select.existing",
                options=["x", "y"],
            )
        _assert_uniform_shape(result, expect_warnings=False)
        assert result["action"] == "update"

    async def test_flow_helper_create_uses_data_wrapper(
        self, register_tools, mock_client
    ):
        """Flow-helper create returns ``data`` wrapper with entry_id/title.

        Flat ``entry_id`` / ``title`` are also preserved as convenience
        accessors — they're the primary identifiers callers reach for.
        """
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
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {"entity_id": "sensor.avg_temp", "config_entry_id": "entry-1"}
                ],
            }
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            name="avg_temp",
            config={"entity_ids": ["sensor.a", "sensor.b"], "type": "mean"},
            wait=False,
        )
        _assert_uniform_shape(result, expect_warnings=False)
        assert result["data"]["entry_id"] == "entry-1"
        assert result["data"]["title"] == "avg_temp"
        # Flat accessors remain (per-action metadata, not wrapper keys).
        assert result["entry_id"] == "entry-1"
        assert result["title"] == "avg_temp"
        assert result["entity_ids"] == ["sensor.avg_temp"]

    async def test_create_wait_failure_surfaces_top_level_warnings_list(
        self, register_tools, mock_client
    ):
        """A wait exception lands in top-level ``warnings`` list, never nested."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"id": "abc123", "name": "Test Switch"},
            }
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("network down"),
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
            )
        _assert_uniform_shape(result, expect_warnings=True)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_update_wait_failure_surfaces_top_level_warnings_list(
        self, register_tools, mock_client
    ):
        """Update wait exception lands in top-level ``warnings`` list, never nested."""

        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/entity_registry/get":
                return {
                    "success": True,
                    "result": {
                        "entity_id": msg["entity_id"],
                        "unique_id": "abc123",
                        "platform": "input_boolean",
                    },
                }
            if msg_type.endswith("/list"):
                return {
                    "success": True,
                    "result": [{"id": "abc123", "name": "Existing"}],
                }
            if msg_type.endswith("/update"):
                return {"success": True, "result": {"id": "abc123"}}
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("net glitch"),
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                helper_id="input_boolean.existing",
                name="Renamed",
            )
        _assert_uniform_shape(result, expect_warnings=True)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_create_failed_registry_update_surfaces_top_level_warning(
        self, register_tools, mock_client
    ):
        """Failed ``config/entity_registry/update`` on create → warning at top level, not nested in data.

        Locks the create-branch registry-write failure path
        (``warnings.append("Helper created but entity registry update failed: ...")``).
        A regression that re-nests this string under ``data``, or drops the
        ``warnings.append`` entirely, would slip past the category test —
        this fills that gap. The ``area.kitchen`` is registered (so the
        upstream ``_validate_registry_ids`` lookup passes) and the failure
        happens at the post-create registry-update step itself.
        """

        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/area_registry/list":
                return {
                    "success": True,
                    "result": [{"area_id": "area.kitchen", "name": "Kitchen"}],
                }
            if msg_type == "config/entity_registry/update":
                return {
                    "success": False,
                    "error": {"message": "registry write rejected"},
                }
            return {
                "success": True,
                "result": {"id": "abc123", "name": "Test Switch"},
            }

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
                area_id="area.kitchen",
            )
        _assert_uniform_shape(result, expect_warnings=True)
        # Warning must surface at top level — never nested under ``data``.
        assert "warning" not in result["data"]
        assert "warnings" not in result["data"]
        assert any("entity registry update failed" in w for w in result["warnings"])
        assert any("registry write rejected" in w for w in result["warnings"])
        # Successful registry-write would have propagated area_id into data;
        # the failure must not silently mark data as if it succeeded.
        assert "area_id" not in result["data"]

    async def test_create_propagates_icon_into_data_after_registry_update(
        self, register_tools, mock_client
    ):
        """Successful create-path registry write echoes ``icon`` into ``data``.

        Locks the icon-propagation symmetry with the update branch
        (``tools_config_helpers.py:3343``). Previously the create-side
        success branch echoed ``area_id`` and ``labels`` into
        ``helper_data`` but skipped ``icon`` — a silent asymmetry now
        closed. The WS create response intentionally omits ``icon`` so
        the assertion fails closed if the propagation line ever gets
        dropped or moved out of the success branch.
        """

        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/area_registry/list":
                return {
                    "success": True,
                    "result": [{"area_id": "area.kitchen", "name": "Kitchen"}],
                }
            if msg_type == "config/entity_registry/update":
                return {"success": True, "result": {}}
            # Helper create — deliberately omit ``icon`` from the result so the
            # only way it lands in ``data`` is via the post-registry-write
            # propagation we're locking here.
            return {
                "success": True,
                "result": {"id": "abc123", "name": "Test Switch"},
            }

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
                icon="mdi:toggle-switch",
                area_id="area.kitchen",
            )
        _assert_uniform_shape(result, expect_warnings=False)
        assert result["action"] == "create"
        assert result["data"]["icon"] == "mdi:toggle-switch"
        assert result["data"]["area_id"] == "area.kitchen"

    async def test_update_failed_registry_update_surfaces_top_level_warning(
        self, register_tools, mock_client
    ):
        """Failed ``config/entity_registry/update`` on update → warning at top level, not nested in data.

        Mirror of the create-side test on the simple-update branch.
        ``logger.warning`` was previously emitted alongside the
        ``warnings.append`` here (create-side never logged); the post-#1303
        contract is ``warnings.append`` only, with the message carried to
        the caller via the response. ``area.kitchen`` is registered so
        upstream validation passes and the failure occurs at the registry
        write itself.
        """

        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/entity_registry/get":
                return {
                    "success": True,
                    "result": {
                        "entity_id": msg["entity_id"],
                        "unique_id": "abc123",
                        "platform": "input_boolean",
                    },
                }
            if msg_type == "config/area_registry/list":
                return {
                    "success": True,
                    "result": [{"area_id": "area.kitchen", "name": "Kitchen"}],
                }
            if msg_type == "config/entity_registry/update":
                return {
                    "success": False,
                    "error": {"message": "registry write rejected"},
                }
            if msg_type.endswith("/list"):
                return {
                    "success": True,
                    "result": [{"id": "abc123", "name": "Existing"}],
                }
            if msg_type.endswith("/update"):
                return {"success": True, "result": {"id": "abc123"}}
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                helper_id="input_boolean.existing",
                area_id="area.kitchen",
            )
        _assert_uniform_shape(result, expect_warnings=True)
        assert "warning" not in result["data"]
        assert "warnings" not in result["data"]
        assert any("entity registry update failed" in w for w in result["warnings"])
        assert any("registry write rejected" in w for w in result["warnings"])
        assert "area_id" not in result["data"]

    async def test_create_failed_category_apply_surfaces_top_level_warning(
        self, register_tools, mock_client
    ):
        """Failed ``apply_entity_category`` on create → warning at top level, not nested in data.

        Issue #1293 close-out: the helper used to leak a ``category_warning`` key
        into ``helper_data`` because ``apply_entity_category`` mutates its
        target dict in-place. The fix routes through a temp dict and lifts the
        warning to the top-level ``warnings`` list (mirrors the precedent in
        ``_handle_flow_helper``'s ``cat_result`` block in
        ``_apply_registry_updates_to_entity``).
        """

        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/category_registry/list":
                return {
                    "success": True,
                    "result": [{"category_id": "cat-123", "name": "Test Cat"}],
                }
            return {
                "success": True,
                "result": {"id": "abc123", "name": "Test Switch"},
            }

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)

        async def fake_apply(
            client, entity_id, category, scope, result_dict, entity_type
        ):
            result_dict["category_warning"] = (
                "Helper saved but failed to set category: forced failure"
            )

        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ha_mcp.tools.tools_config_helpers.apply_entity_category",
                side_effect=fake_apply,
            ),
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
                category="cat-123",
            )
        _assert_uniform_shape(result, expect_warnings=True)
        assert "category_warning" not in result["data"], (
            "category_warning leaked into data payload"
        )
        assert "category" not in result["data"], (
            "category should not be set in data when apply failed"
        )
        assert any("failed to set category" in w for w in result["warnings"])

    async def test_update_failed_category_apply_surfaces_top_level_warning(
        self, register_tools, mock_client
    ):
        """Failed ``apply_entity_category`` on update → warning at top level, not nested in data.

        Mirror of the create-side test on the simple/registry update branch.
        """

        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/entity_registry/get":
                return {
                    "success": True,
                    "result": {
                        "entity_id": msg["entity_id"],
                        "unique_id": "abc123",
                        "platform": "input_boolean",
                    },
                }
            if msg_type == "config/category_registry/list":
                return {
                    "success": True,
                    "result": [{"category_id": "cat-123", "name": "Test Cat"}],
                }
            if msg_type.endswith("/list"):
                return {
                    "success": True,
                    "result": [{"id": "abc123", "name": "Existing"}],
                }
            if msg_type.endswith("/update"):
                return {"success": True, "result": {"id": "abc123"}}
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)

        async def fake_apply(
            client, entity_id, category, scope, result_dict, entity_type
        ):
            result_dict["category_warning"] = (
                "Helper saved but failed to set category: forced failure"
            )

        with (
            patch(
                "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ha_mcp.tools.tools_config_helpers.apply_entity_category",
                side_effect=fake_apply,
            ),
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                helper_id="input_boolean.existing",
                name="Renamed",
                category="cat-123",
            )
        _assert_uniform_shape(result, expect_warnings=True)
        assert "category_warning" not in result["data"], (
            "category_warning leaked into data payload"
        )
        assert "category" not in result["data"], (
            "category should not be set in data when apply failed"
        )
        assert any("failed to set category" in w for w in result["warnings"])

    async def test_no_singular_warning_key_at_top_level_or_nested(
        self, register_tools, mock_client
    ):
        """``warning`` (singular string) must never appear — neither flat nor nested."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"id": "abc123", "name": "Test Switch"},
            }
        )
        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=False,  # not registered → triggers warning code path
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                name="Test Switch",
            )
        # The pre-#1293 contract wrote ``helper_data["warning"]``; the new
        # contract writes ``result["warnings"][i]``.
        assert "warning" not in result, (
            "singular 'warning' key must not appear at top level"
        )
        assert "warning" not in result["data"], (
            "singular 'warning' key must not appear inside data wrapper"
        )
        assert isinstance(result.get("warnings"), list)
        assert any("not yet queryable" in w for w in result["warnings"])
