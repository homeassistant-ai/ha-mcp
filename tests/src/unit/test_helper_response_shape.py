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

from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)


@pytest.fixture
def mock_client():
    client = MagicMock()
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

        Post-#1355: writer now appends directly to ``result_dict["warnings"]``
        instead of setting singular ``category_warning``; the leak-check
        assertions still hold (neither key should appear in ``data``).
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
            result_dict.setdefault("warnings", []).append(
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
        assert "warnings" not in result["data"], (
            "warnings leaked into data payload — must stay top-level"
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
            result_dict.setdefault("warnings", []).append(
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
        assert "warnings" not in result["data"], (
            "warnings leaked into data payload — must stay top-level"
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


def _assert_warnings_list_shape_core(result: dict[str, Any]) -> None:
    """Shared warnings-list assertions for both shape helpers below.

    Enforces the warnings-list contract independent of any ``success`` /
    status-marker convention:

    - ``warnings`` is a non-empty ``list[str]`` at the top level
    - No singular ``warning`` string at the top level (legacy shape)
    - ``warnings`` does not leak into any nested dict value (no
      ``"data": {"warnings": [...]}`` or ``"result": {"warnings": [...]}``
      nesting pattern that pre-#1332 callers had to chase)
    """
    assert "warning" not in result, (
        "singular 'warning' key must not appear at top level"
    )
    assert isinstance(result.get("warnings"), list), (
        f"warnings missing or not a list: {result.get('warnings')!r}"
    )
    assert all(isinstance(w, str) for w in result["warnings"]), (
        f"non-string warning entries: {result['warnings']!r}"
    )
    assert result["warnings"], "warnings list present but empty"
    # Defence against re-introduction of nested warning bags: scan every
    # dict-typed top-level value for ``warnings`` / ``warning`` keys.
    # ``validation`` is the intentional reference-validator metadata bag
    # (``merge_validation_meta`` — separate concern from #1332's lifecycle
    # warnings) and is whitelisted from the leak check.
    nested_warnings_whitelist = {"validation"}
    for key, value in result.items():
        if key == "warnings" or key in nested_warnings_whitelist:
            continue
        if isinstance(value, dict):
            assert "warnings" not in value, (
                f"warnings leaked into nested '{key}': must stay top-level"
            )
            assert "warning" not in value, (
                f"singular 'warning' leaked into nested '{key}'"
            )


def _assert_warnings_list_shape(result: dict[str, Any]) -> None:
    """Cross-cutting warnings-list contract for lifecycle-write tools (#1332).

    Tighter than ``_assert_uniform_shape`` for the warnings half: applies to
    any successful tool response, regardless of payload-wrapper key.

    - ``success`` is True
    - ``warnings`` is a non-empty ``list[str]`` at the top level
    - No singular ``warning`` string at the top level (legacy shape)
    - ``warnings`` does not leak into any nested dict value (no
      ``"data": {"warnings": [...]}`` or ``"result": {"warnings": [...]}``
      nesting pattern that pre-#1332 callers had to chase)
    """
    assert result["success"] is True
    _assert_warnings_list_shape_core(result)


def _assert_warnings_list_shape_no_success(result: dict[str, Any]) -> None:
    """Relaxed warnings-list shape contract for tools without a top-level
    ``success`` key by design.

    Mirrors ``_assert_warnings_list_shape`` minus the ``success: True``
    assertion. Used for tools whose response uses a status-marker
    (e.g. ``status: "pending_restart"``) or a payload-bag (e.g. ``mode:
    "dry_run"``, list-mode integrations payload, deep_search fallback)
    instead of the ``success`` boolean — those shapes are pre-existing
    design choices, not regressions of #1332.

    The warnings-list contract itself remains identical to the strict
    helper.
    """
    _assert_warnings_list_shape_core(result)


class TestLifecycleWriteWarningsShape:
    """Cross-cutting shape regression for the 4 lifecycle-write families
    migrated under #1332. Asserts the warnings-list contract holds on
    every emission path that landed in #1337-#1340 (now consolidated into #1340).

    Per the narrow exception tuple decision (#1340 thread with kingpanther13):
    only HomeAssistantConnectionError and HomeAssistantAuthError propagate
    from wait_for_entity_registered / wait_for_entity_removed to the call
    sites — TimeoutError returns False (handled separately), HomeAssistantAPIError
    is fully swallowed by the helpers (util_helpers.py:495-499 + :537-543).
    Tests only the two exception types that actually reach the call sites.
    """

    # ------------------------------------------------------------------
    # Per-family tool factories.
    # Each returns a tools instance plus a mock client wired so the
    # underlying write succeeds and only the wait-verification step
    # raises (the codepath under test).
    # ------------------------------------------------------------------

    @pytest.fixture
    def groups_tools(self):
        from ha_mcp.tools.tools_groups import GroupTools

        client = MagicMock()
        client.call_service = AsyncMock(return_value=None)
        client.get_entity_state = AsyncMock(return_value={"state": "on"})
        client.get_states = AsyncMock(return_value=[])
        return GroupTools(client)

    @pytest.fixture
    def scripts_tools(self):
        from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

        client = MagicMock()
        client.upsert_script_config = AsyncMock(
            return_value={"script_id": "test_script"}
        )
        client.delete_script_config = AsyncMock(
            return_value={"script_id": "test_script"}
        )
        client.get_entity_state = AsyncMock(
            return_value={"state": "off", "entity_id": "script.test_script"}
        )
        client.get_services = AsyncMock(return_value=[])
        client.get_states = AsyncMock(return_value=[])
        return ConfigScriptTools(client)

    @pytest.fixture
    def automations_tools(self):
        from ha_mcp.tools.tools_config_automations import AutomationConfigTools

        client = MagicMock()
        client.upsert_automation_config = AsyncMock(
            return_value={"entity_id": "automation.test_auto"}
        )
        client.delete_automation_config = AsyncMock(
            return_value={"identifier": "automation.test_auto"}
        )
        client.get_entity_state = AsyncMock(
            return_value={"state": "on", "entity_id": "automation.test_auto"}
        )
        client.get_services = AsyncMock(return_value=[])
        # _resolve_automation_entity_id reads states; for entity_id input
        # the short-circuit triggers and this is never consulted.
        client.get_states = AsyncMock(return_value=[])
        return AutomationConfigTools(client)

    @pytest.fixture
    def scenes_tools(self, monkeypatch):
        from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

        # Issue #1168 R3 blocker 1 sleep — zero it so registry-miss
        # retry doesn't stretch the unit-test wall clock.
        monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)

        client = MagicMock()
        client.upsert_scene_config = AsyncMock(return_value={"scene_id": "test_scene"})
        client.delete_scene_config = AsyncMock(return_value={"scene_id": "test_scene"})
        client.resolve_scene_id = AsyncMock(
            side_effect=lambda sid: sid.removeprefix("scene.")
        )
        client.get_entity_state = AsyncMock(
            return_value={
                "state": "2026-05-18T00:00:00+00:00",
                "entity_id": "scene.test_scene",
            }
        )
        client.get_services = AsyncMock(return_value=[])
        client.get_states = AsyncMock(return_value=[])
        # _resolve_scene_entity_id: empty registry → falls back to
        # f"scene.{scene_id}" which matches what wait_for_entity_registered
        # is patched to receive.
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        return ConfigSceneTools(client)

    # ------------------------------------------------------------------
    # Groups: set + remove × 2 exception types
    # ------------------------------------------------------------------

    async def test_groups_set_connection_error_yields_top_level_warnings_list(
        self, groups_tools
    ):
        with patch(
            "ha_mcp.tools.tools_groups.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("forced for test"),
        ):
            result = await groups_tools.ha_config_set_group(
                object_id="test_group",
                entities=["light.kitchen"],
            )
        _assert_warnings_list_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_groups_set_auth_error_yields_top_level_warnings_list(
        self, groups_tools
    ):
        with patch(
            "ha_mcp.tools.tools_groups.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantAuthError("forced for test"),
        ):
            result = await groups_tools.ha_config_set_group(
                object_id="test_group",
                entities=["light.kitchen"],
            )
        _assert_warnings_list_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_groups_remove_connection_error_yields_top_level_warnings_list(
        self, groups_tools
    ):
        with patch(
            "ha_mcp.tools.tools_groups.wait_for_entity_removed",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("forced for test"),
        ):
            result = await groups_tools.ha_config_remove_group(
                object_id="test_group",
            )
        _assert_warnings_list_shape(result)
        assert any("removal verification failed" in w for w in result["warnings"])

    async def test_groups_remove_auth_error_yields_top_level_warnings_list(
        self, groups_tools
    ):
        with patch(
            "ha_mcp.tools.tools_groups.wait_for_entity_removed",
            new_callable=AsyncMock,
            side_effect=HomeAssistantAuthError("forced for test"),
        ):
            result = await groups_tools.ha_config_remove_group(
                object_id="test_group",
            )
        _assert_warnings_list_shape(result)
        assert any("removal verification failed" in w for w in result["warnings"])

    async def test_groups_update_path_uses_updated_action_word(self, groups_tools):
        # Rename-only call (name set, entities/add/remove all None) — the
        # is_create branch in tools_groups.py:306 evaluates to False, so
        # action_word must be "updated". Pins the create/update branching
        # against a regression hardcoding "created" on the update path.
        with patch(
            "ha_mcp.tools.tools_groups.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("forced for test"),
        ):
            result = await groups_tools.ha_config_set_group(
                object_id="test_group",
                name="Renamed Test Group",
            )
        _assert_warnings_list_shape(result)
        assert any("updated but" in w for w in result["warnings"])
        assert not any("created but" in w for w in result["warnings"])

    # ------------------------------------------------------------------
    # Scripts: set + remove × 2 exception types
    # ------------------------------------------------------------------

    async def test_scripts_set_connection_error_yields_top_level_warnings_list(
        self, scripts_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_scripts.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("forced for test"),
        ):
            result = await scripts_tools.ha_config_set_script(
                script_id="test_script",
                config={"sequence": [{"delay": {"seconds": 1}}]},
            )
        _assert_warnings_list_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_scripts_set_auth_error_yields_top_level_warnings_list(
        self, scripts_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_scripts.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantAuthError("forced for test"),
        ):
            result = await scripts_tools.ha_config_set_script(
                script_id="test_script",
                config={"sequence": [{"delay": {"seconds": 1}}]},
            )
        _assert_warnings_list_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_scripts_remove_connection_error_yields_top_level_warnings_list(
        self, scripts_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_scripts.wait_for_entity_removed",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("forced for test"),
        ):
            result = await scripts_tools.ha_config_remove_script(
                script_id="test_script",
            )
        _assert_warnings_list_shape(result)
        assert any("removal verification failed" in w for w in result["warnings"])

    async def test_scripts_remove_auth_error_yields_top_level_warnings_list(
        self, scripts_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_scripts.wait_for_entity_removed",
            new_callable=AsyncMock,
            side_effect=HomeAssistantAuthError("forced for test"),
        ):
            result = await scripts_tools.ha_config_remove_script(
                script_id="test_script",
            )
        _assert_warnings_list_shape(result)
        assert any("removal verification failed" in w for w in result["warnings"])

    # ------------------------------------------------------------------
    # Automations: set + remove × 2 exception types
    # ------------------------------------------------------------------

    async def test_automations_set_connection_error_yields_top_level_warnings_list(
        self, automations_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_automations.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("forced for test"),
        ):
            result = await automations_tools.ha_config_set_automation(
                config={
                    "alias": "Test Auto",
                    "trigger": [{"platform": "time", "at": "07:00:00"}],
                    "action": [{"service": "light.turn_on"}],
                },
            )
        _assert_warnings_list_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_automations_set_auth_error_yields_top_level_warnings_list(
        self, automations_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_automations.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantAuthError("forced for test"),
        ):
            result = await automations_tools.ha_config_set_automation(
                config={
                    "alias": "Test Auto",
                    "trigger": [{"platform": "time", "at": "07:00:00"}],
                    "action": [{"service": "light.turn_on"}],
                },
            )
        _assert_warnings_list_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_automations_remove_connection_error_yields_top_level_warnings_list(
        self, automations_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_automations.wait_for_entity_removed",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("forced for test"),
        ):
            result = await automations_tools.ha_config_remove_automation(
                identifier="automation.test_auto",
            )
        _assert_warnings_list_shape(result)
        assert any("removal verification failed" in w for w in result["warnings"])

    async def test_automations_remove_auth_error_yields_top_level_warnings_list(
        self, automations_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_automations.wait_for_entity_removed",
            new_callable=AsyncMock,
            side_effect=HomeAssistantAuthError("forced for test"),
        ):
            result = await automations_tools.ha_config_remove_automation(
                identifier="automation.test_auto",
            )
        _assert_warnings_list_shape(result)
        assert any("removal verification failed" in w for w in result["warnings"])

    async def test_automations_update_path_uses_updated_action_word(
        self, automations_tools
    ):
        # identifier supplied — tools_config_automations.py:740 selects
        # action_word = "updated". Pins the create/update branching against
        # a regression hardcoding "created" on the update path.
        with patch(
            "ha_mcp.tools.tools_config_automations.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("forced for test"),
        ):
            result = await automations_tools.ha_config_set_automation(
                identifier="automation.test_auto",
                config={
                    "alias": "Test Auto",
                    "trigger": [{"platform": "time", "at": "07:00:00"}],
                    "action": [{"service": "light.turn_on"}],
                },
            )
        _assert_warnings_list_shape(result)
        assert any("updated but" in w for w in result["warnings"])
        assert not any("created but" in w for w in result["warnings"])

    # ------------------------------------------------------------------
    # Scenes: set + remove × 2 exception types
    # ------------------------------------------------------------------

    async def test_scenes_set_connection_error_yields_top_level_warnings_list(
        self, scenes_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_scenes.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("forced for test"),
        ):
            result = await scenes_tools.ha_config_set_scene(
                scene_id="test_scene",
                config={
                    "name": "Test Scene",
                    "entities": {"light.kitchen": {"state": "on"}},
                },
            )
        _assert_warnings_list_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_scenes_set_auth_error_yields_top_level_warnings_list(
        self, scenes_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_scenes.wait_for_entity_registered",
            new_callable=AsyncMock,
            side_effect=HomeAssistantAuthError("forced for test"),
        ):
            result = await scenes_tools.ha_config_set_scene(
                scene_id="test_scene",
                config={
                    "name": "Test Scene",
                    "entities": {"light.kitchen": {"state": "on"}},
                },
            )
        _assert_warnings_list_shape(result)
        assert any("verification failed" in w for w in result["warnings"])

    async def test_scenes_remove_connection_error_yields_top_level_warnings_list(
        self, scenes_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_scenes.wait_for_entity_removed",
            new_callable=AsyncMock,
            side_effect=HomeAssistantConnectionError("forced for test"),
        ):
            result = await scenes_tools.ha_config_remove_scene(
                scene_id="test_scene",
            )
        _assert_warnings_list_shape(result)
        assert any("removal verification failed" in w for w in result["warnings"])

    async def test_scenes_remove_auth_error_yields_top_level_warnings_list(
        self, scenes_tools
    ):
        with patch(
            "ha_mcp.tools.tools_config_scenes.wait_for_entity_removed",
            new_callable=AsyncMock,
            side_effect=HomeAssistantAuthError("forced for test"),
        ):
            result = await scenes_tools.ha_config_remove_scene(
                scene_id="test_scene",
            )
        _assert_warnings_list_shape(result)
        assert any("removal verification failed" in w for w in result["warnings"])


class TestSweepWarningsShape:
    """Cross-cutting warnings-list shape coverage for tools migrated under
    #1341 (the non-lifecycle-write half of #1332).

    Mirrors the TestLifecycleWriteWarningsShape pattern from #1340, using
    ``_assert_warnings_list_shape`` which enforces:
      - ``success: True`` at top level
      - ``warnings`` is a non-empty ``list[str]`` at the top level
      - No singular ``warning`` legacy key
      - ``warnings`` doesn't leak into nested dicts

    Coverage 6/8 of the PR-touched src files cross-cuttingly pinned,
    split by response-shape contract:

    **Strict helper (top-level ``success: True`` + warnings) — 4 of 8:**
    - ``backup.py`` (restore_backup)
    - ``tools_service.py`` (ha_call_event timeout)
    - ``tools_system.py`` (ha_restart success)
    - ``tools_energy.py`` (ha_manage_energy_prefs dry_run with validate
      failure — response carries ``success: True`` from
      ``len(shape_errors) == 0``)

    **Relaxed helper (no top-level ``success`` — status-marker / payload-bag
    shape) — 2 of 8:**
    - ``tools_addons.py``: ``ha_manage_addon`` config mode with
      ``options`` returns ``status: "pending_restart"`` + top-level
      warnings (closure tool, registered via ``register_addon_tools``)
    - ``tools_integrations.py``: ``ha_get_integration`` single-entry
      mode with ``include_diagnostics=False`` + ``device_id`` provided

    **Not folded in (2 of 8) — multi-step WebSocket call chains where a
    reliable cross-cutting mock needs to branch on the WS message
    ``type`` to pass early-success steps and fail only at the target
    emit-site. Content-level coverage already lives in the per-tool
    unit/e2e test files cited:**

    - ``tools_search.py``: ``ha_deep_search`` fallback path. The tool
      calls ``await client.<state-source>`` before the fuzzy/exact
      branch; trivial ``MagicMock()`` clients trip the
      ``"MagicMock can't be used in 'await' expression"`` error before
      the warning emit-site is reached. Per-tool tests in
      ``test_tools_search.py`` exercise the fallback content already.
    - ``tools_entities.py``: ``ha_set_entity`` with ``new_device_name``
      flows entity-registry-lookup → entity-registry-update →
      device-registry-lookup → device-registry-update → optional
      expose-update. The nested ``device_rename`` warnings live deep
      in that chain. Per-tool tests in ``test_entity_rename.py`` and
      ``test_rename_consolidation.py`` exercise the nested-result
      shape directly.
    """

    @pytest.mark.asyncio
    async def test_backup_restore_warnings_shape(self):
        from ha_mcp.tools.backup import restore_backup

        ws = AsyncMock()
        ws.send_command.side_effect = [
            {"success": True, "result": {"backups": [{"backup_id": "abc"}]}},
            {
                "success": True,
                "result": {"agents": [{"agent_id": "backup.local", "name": "local"}]},
            },
            {
                "success": True,
                "result": {"config": {"create_backup": {"password": "pw"}}},
            },
            {"success": True, "result": {"backup_job_id": "job"}},
            {
                "success": True,
                "result": {
                    "backups": [{"name": "Pre_Restore_Safety", "backup_id": "sxyz"}]
                },
            },
            {"success": True},
        ]
        client = MagicMock()
        client.base_url = "http://test"
        client.token = "t"
        client.verify_ssl = False
        with patch(
            "ha_mcp.tools.backup.get_connected_ws_client",
            new=AsyncMock(return_value=(ws, None)),
        ):
            result = await restore_backup(client, "abc")
        _assert_warnings_list_shape(result)

    @pytest.mark.asyncio
    async def test_service_call_event_timeout_warnings_shape(self):
        import httpx

        from ha_mcp.tools.tools_service import ServiceTools

        conn_error = HomeAssistantConnectionError("timeout")
        conn_error.__cause__ = httpx.TimeoutException("read timed out")
        client = MagicMock()
        client.fire_event = AsyncMock(side_effect=conn_error)
        tools = ServiceTools.__new__(ServiceTools)
        tools._client = client
        tools._device_tools = MagicMock()

        result = await tools.ha_call_event("evt_x")
        _assert_warnings_list_shape(result)

    @pytest.mark.asyncio
    async def test_system_restart_warnings_shape(self):
        from ha_mcp.tools.tools_system import SystemTools

        client = AsyncMock()
        client.check_config.return_value = {"result": "valid"}
        client.call_service.return_value = None
        tools = SystemTools(client)

        result = await tools.ha_restart(confirm=True)
        _assert_warnings_list_shape(result)

    @pytest.mark.asyncio
    async def test_integrations_get_integration_device_id_ignored_warnings_shape(self):
        """Shape-pins the device_id-ignored warning emit in
        ``ha_get_integration`` (entry_id mode, include_diagnostics=False,
        device_id provided). Response shape is the single-entry payload
        (no top-level ``success``) — uses the relaxed helper."""
        from ha_mcp.tools.tools_integrations import IntegrationTools

        tools = IntegrationTools.__new__(IntegrationTools)
        tools._client = MagicMock()
        tools._get_single_entry = AsyncMock(
            return_value={
                "entry_id": "abc",
                "domain": "test_integration",
                "state": "loaded",
            }
        )

        result = await tools.ha_get_integration(
            entry_id="abc",
            device_id="dev_123",
            include_diagnostics=False,
        )
        _assert_warnings_list_shape_no_success(result)

    @pytest.mark.asyncio
    async def test_addons_manage_addon_options_ignored_fields_warnings_shape(self):
        """Shape-pins the ignored_fields warning emit in ``ha_manage_addon``
        when options include a field not in the add-on schema. Config mode
        with ``options`` returns ``status: "pending_restart"`` (no top-level
        ``success``) — uses the relaxed helper. Mirrors the mock pattern in
        ``test_tools_addons.py::test_config_mode_options_unknown_fields_warned``.
        """
        from ha_mcp.tools.tools_addons import register_addon_tools

        registered: dict[str, Any] = {}
        mock_mcp = MagicMock()

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                registered[func.__name__] = func
                return func

            return wrapper

        mock_mcp.tool = tool_decorator
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.token = "test-token"
        register_addon_tools(mock_mcp, client)
        manage_addon = registered["ha_manage_addon"]

        async def mock_supervisor_api(client_arg, endpoint, **kwargs):
            if endpoint == "/addons/test_addon/info":
                return {
                    "success": True,
                    "result": {
                        "options": {"log_level": "info"},
                        "schema": [
                            {"name": "log_level", "required": False, "type": "str"}
                        ],
                    },
                }
            return {"success": True, "result": {}}

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            side_effect=mock_supervisor_api,
        ):
            result = await manage_addon(
                slug="test_addon",
                options={"log_level": "debug", "zombie_field": "ghost"},
            )

        _assert_warnings_list_shape_no_success(result)

    @pytest.mark.asyncio
    async def test_energy_manage_energy_prefs_dry_run_validate_failure_warnings_shape(
        self,
    ):
        """Shape-pins the validate_warning emit in
        ``_dry_run`` when HA's ``energy/validate`` fails. Response carries
        ``success: True`` at top level (set from ``len(shape_errors) == 0``)
        — uses the STRICT helper. Listed in the no-success bucket of the
        Item 5 analysis but actually fits the strict contract.
        """
        from ha_mcp.tools.tools_energy import EnergyTools

        tools = EnergyTools.__new__(EnergyTools)
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "energy not configured"}
        )
        tools._client = client

        # Minimal valid prefs shape passing _shape_check() — empty source/device
        # lists are accepted, only the top-level keys are required.
        result = await tools._dry_run(
            {
                "energy_sources": [],
                "device_consumption": [],
            }
        )

        _assert_warnings_list_shape(result)
