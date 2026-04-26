"""Unit tests for the array_patch helper and ha_manage_addon array_patch dispatch.

The helper (`_apply_array_ops`) is exercised directly with synthetic data —
no addon, no httpx — so the operation semantics are isolated from the
HTTP plumbing. The dispatch layer is exercised by mocking `_call_addon_api`
so we verify that array_patch fetches with raw=True, applies ops, and
posts the mutated array back exactly once.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_addons import _apply_array_ops


def _parse_tool_error(exc: pytest.ExceptionInfo[ToolError]) -> dict:
    return json.loads(str(exc.value))


# ---------------------------------------------------------------------------
# _apply_array_ops — happy paths
# ---------------------------------------------------------------------------


class TestApplyArrayOps:
    def _flows_fixture(self) -> list[dict]:
        return [
            {"id": "tab1", "type": "tab", "label": "Lights"},
            {"id": "tab2", "type": "tab", "label": "Bathroom"},
            {"id": "n1", "type": "inject", "z": "tab1", "name": "Trigger"},
            {"id": "n2", "type": "function", "z": "tab1", "name": "Compute"},
            {"id": "n3", "type": "switch", "z": "tab2", "name": "Sense"},
        ]

    def test_patch_updates_fields_in_place(self):
        items = self._flows_fixture()
        new, summary = _apply_array_ops(
            items,
            [{"op": "patch", "id": "n1", "patches": {"name": "Renamed"}}],
            id_field="id",
        )
        n1 = next(it for it in new if it["id"] == "n1")
        assert n1["name"] == "Renamed"
        assert summary["patched"] == [{"id": "n1", "fields": ["name"]}]

    def test_patch_preserves_unrelated_fields(self):
        items = self._flows_fixture()
        new, _ = _apply_array_ops(
            items,
            [{"op": "patch", "id": "n2", "patches": {"name": "X"}}],
            id_field="id",
        )
        n2 = next(it for it in new if it["id"] == "n2")
        assert n2["type"] == "function"
        assert n2["z"] == "tab1"

    def test_delete_removes_single_item(self):
        items = self._flows_fixture()
        new, summary = _apply_array_ops(
            items,
            [{"op": "delete", "id": "n1"}],
            id_field="id",
        )
        assert "n1" not in {it["id"] for it in new}
        assert summary["deleted"] == [{"id": "n1"}]

    def test_add_appends_new_item(self):
        items = self._flows_fixture()
        new, summary = _apply_array_ops(
            items,
            [{"op": "add", "item": {"id": "n4", "type": "debug", "z": "tab1"}}],
            id_field="id",
        )
        assert {it["id"] for it in new} == {"tab1", "tab2", "n1", "n2", "n3", "n4"}
        assert summary["added"] == [{"id": "n4"}]

    def test_delete_where_removes_all_matching(self):
        items = self._flows_fixture()
        new, summary = _apply_array_ops(
            items,
            [{"op": "delete_where", "field": "z", "value": "tab1"}],
            id_field="id",
        )
        # tab1 itself stays (z field is absent on tabs); only the children go
        assert {it["id"] for it in new} == {"tab1", "tab2", "n3"}
        assert summary["deleted_where"] == [{"field": "z", "value": "tab1", "count": 2}]

    def test_delete_where_zero_matches_is_not_an_error(self):
        items = self._flows_fixture()
        new, summary = _apply_array_ops(
            items,
            [{"op": "delete_where", "field": "z", "value": "ghost-tab"}],
            id_field="id",
        )
        assert len(new) == len(items)
        assert summary["deleted_where"][0]["count"] == 0

    def test_operations_apply_in_order(self):
        items = self._flows_fixture()
        new, summary = _apply_array_ops(
            items,
            [
                {"op": "delete", "id": "n1"},
                {"op": "add", "item": {"id": "n1", "type": "debug", "z": "tab1"}},
                {"op": "patch", "id": "n1", "patches": {"name": "Reborn"}},
            ],
            id_field="id",
        )
        n1 = next(it for it in new if it["id"] == "n1")
        assert n1["type"] == "debug"
        assert n1["name"] == "Reborn"
        assert len(summary["deleted"]) == 1
        assert len(summary["added"]) == 1
        assert len(summary["patched"]) == 1

    def test_custom_id_field(self):
        items = [
            {"slug": "thermostat-living", "value": 21},
            {"slug": "thermostat-bedroom", "value": 19},
        ]
        new, _ = _apply_array_ops(
            items,
            [{"op": "patch", "id": "thermostat-bedroom", "patches": {"value": 20}}],
            id_field="slug",
        )
        bed = next(it for it in new if it["slug"] == "thermostat-bedroom")
        assert bed["value"] == 20


# ---------------------------------------------------------------------------
# _apply_array_ops — validation failures
# ---------------------------------------------------------------------------


class TestApplyArrayOpsValidation:
    def test_unknown_op_raises(self):
        with pytest.raises(ToolError) as exc:
            _apply_array_ops([], [{"op": "yeet", "id": "x"}], id_field="id")
        err = _parse_tool_error(exc)
        assert err["error"]["code"] == "VALIDATION_FAILED"

    def test_patch_missing_id_raises(self):
        with pytest.raises(ToolError) as exc:
            _apply_array_ops(
                [{"id": "a"}],
                [{"op": "patch", "patches": {"name": "X"}}],
                id_field="id",
            )
        assert _parse_tool_error(exc)["error"]["code"] == "VALIDATION_FAILED"

    def test_patch_unknown_id_raises(self):
        with pytest.raises(ToolError) as exc:
            _apply_array_ops(
                [{"id": "a"}],
                [{"op": "patch", "id": "ghost", "patches": {"name": "X"}}],
                id_field="id",
            )
        assert _parse_tool_error(exc)["error"]["code"] == "RESOURCE_NOT_FOUND"

    def test_patch_non_dict_patches_raises(self):
        with pytest.raises(ToolError) as exc:
            _apply_array_ops(
                [{"id": "a"}],
                [{"op": "patch", "id": "a", "patches": "not a dict"}],
                id_field="id",
            )
        assert _parse_tool_error(exc)["error"]["code"] == "VALIDATION_FAILED"

    def test_delete_unknown_id_raises(self):
        with pytest.raises(ToolError) as exc:
            _apply_array_ops(
                [{"id": "a"}], [{"op": "delete", "id": "ghost"}], id_field="id"
            )
        assert _parse_tool_error(exc)["error"]["code"] == "RESOURCE_NOT_FOUND"

    def test_add_collision_raises(self):
        with pytest.raises(ToolError) as exc:
            _apply_array_ops(
                [{"id": "a"}],
                [{"op": "add", "item": {"id": "a", "type": "x"}}],
                id_field="id",
            )
        assert _parse_tool_error(exc)["error"]["code"] == "RESOURCE_ALREADY_EXISTS"

    def test_add_missing_id_field_raises(self):
        with pytest.raises(ToolError) as exc:
            _apply_array_ops([], [{"op": "add", "item": {"type": "x"}}], id_field="id")
        assert _parse_tool_error(exc)["error"]["code"] == "VALIDATION_FAILED"

    def test_delete_where_with_explicit_none_value_is_allowed(self):
        items = [
            {"id": "a", "z": None},
            {"id": "b", "z": "tab1"},
        ]
        new, summary = _apply_array_ops(
            items, [{"op": "delete_where", "field": "z", "value": None}], id_field="id"
        )
        assert {it["id"] for it in new} == {"b"}
        assert summary["deleted_where"][0]["count"] == 1

    def test_failure_midway_does_not_keep_partial_state_visible(self):
        """Op that fails mid-stream raises before caller can POST.
        The working list is mutated in memory but never returned, so the
        server's view of the state is unchanged."""
        items = [{"id": "a"}, {"id": "b"}]
        with pytest.raises(ToolError):
            _apply_array_ops(
                items,
                [
                    {"op": "patch", "id": "a", "patches": {"name": "ok"}},
                    {"op": "patch", "id": "ghost", "patches": {"name": "fail"}},
                ],
                id_field="id",
            )
        # Even though the helper mutated 'a' in its working copy, the test
        # never received a (new_array, summary) tuple — the production
        # dispatcher won't POST anything when a ToolError propagates.


# ---------------------------------------------------------------------------
# ha_manage_addon array_patch dispatch — happy + key error paths
# ---------------------------------------------------------------------------


class TestHaManageAddonArrayPatchDispatch:
    """Exercise the dispatch in ha_manage_addon by mocking _call_addon_api.

    We verify: GET happens with raw=True; ops are applied; POST sends the
    mutated array; response shape is the compact summary (no full array).
    """

    @pytest.fixture
    def _registered_tool(self):
        from ha_mcp.tools.tools_addons import register_addon_tools

        captured: dict = {}

        class _MockMCP:
            def tool(self, *args, **kwargs):
                def deco(fn):
                    captured.setdefault(fn.__name__, fn)
                    return fn

                return deco

        register_addon_tools(_MockMCP(), client=AsyncMock())
        fn = captured["ha_manage_addon"]
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        return fn

    @pytest.mark.asyncio
    async def test_dispatch_fetches_then_posts_mutated_array(self, _registered_tool):
        fetched_array = [
            {"id": "n1", "name": "old"},
            {"id": "n2", "name": "keep"},
        ]
        call_log: list[tuple[str, dict]] = []

        async def fake_call(**kwargs):
            method = kwargs.get("method", "GET")
            assert kwargs.get("raw") is True
            call_log.append((method, kwargs))
            if method == "GET":
                return {
                    "success": True,
                    "response": fetched_array,
                    "addon_name": "Node-RED",
                    "slug": kwargs["slug"],
                    "status_code": 200,
                }
            return {"success": True, "response": "rev-1", "status_code": 200}

        with patch(
            "ha_mcp.tools.tools_addons._call_addon_api",
            new=AsyncMock(side_effect=fake_call),
        ):
            result = await _registered_tool(
                slug="a0d7b954_nodered",
                path="/flows",
                array_patch={
                    "operations": [
                        {"op": "patch", "id": "n1", "patches": {"name": "new"}},
                    ],
                },
            )

        # Two calls: GET then POST
        assert [m for m, _ in call_log] == ["GET", "POST"]
        # POST body is the mutated array
        post_body = call_log[1][1]["body"]
        assert post_body[0]["name"] == "new"
        assert post_body[1] == {"id": "n2", "name": "keep"}
        # Response shape is the compact summary, not the full array
        assert result["success"] is True
        assert result["items_before"] == 2
        assert result["items_after"] == 2
        assert result["summary"]["patched"] == [{"id": "n1", "fields": ["name"]}]

    @pytest.mark.asyncio
    async def test_dispatch_rejects_non_array_response(self, _registered_tool):
        async def fake_call(**kwargs):
            return {
                "success": True,
                "response": {"not": "an array"},
                "addon_name": "X",
                "slug": kwargs["slug"],
                "status_code": 200,
            }

        with patch(
            "ha_mcp.tools.tools_addons._call_addon_api",
            new=AsyncMock(side_effect=fake_call),
        ):
            with pytest.raises(ToolError) as exc:
                await _registered_tool(
                    slug="a0d7b954_nodered",
                    path="/flows",
                    array_patch={
                        "operations": [{"op": "delete", "id": "x"}],
                    },
                )
        err = _parse_tool_error(exc)
        assert err["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.asyncio
    async def test_dispatch_does_not_post_when_op_validation_fails(
        self, _registered_tool
    ):
        get_called = False
        post_called = False

        async def fake_call(**kwargs):
            nonlocal get_called, post_called
            if kwargs.get("method", "GET") == "GET":
                get_called = True
                return {
                    "success": True,
                    "response": [{"id": "a"}],
                    "addon_name": "X",
                    "slug": kwargs["slug"],
                    "status_code": 200,
                }
            post_called = True
            return {"success": True, "response": "ok", "status_code": 200}

        with patch(
            "ha_mcp.tools.tools_addons._call_addon_api",
            new=AsyncMock(side_effect=fake_call),
        ):
            with pytest.raises(ToolError):
                await _registered_tool(
                    slug="a0d7b954_nodered",
                    path="/flows",
                    array_patch={
                        # 'ghost' isn't in the fetched array — _apply_array_ops raises
                        "operations": [{"op": "delete", "id": "ghost"}],
                    },
                )

        assert get_called is True
        assert post_called is False

    @pytest.mark.asyncio
    async def test_dispatch_rejects_websocket_combo(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _registered_tool(
                slug="a0d7b954_nodered",
                path="/flows",
                websocket=True,
                array_patch={"operations": [{"op": "delete", "id": "x"}]},
            )
        err = _parse_tool_error(exc)
        assert err["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.asyncio
    async def test_dispatch_rejects_explicit_body(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _registered_tool(
                slug="a0d7b954_nodered",
                path="/flows",
                body={"foo": "bar"},
                array_patch={"operations": [{"op": "delete", "id": "x"}]},
            )
        err = _parse_tool_error(exc)
        assert err["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.asyncio
    async def test_dispatch_rejects_offset_limit(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _registered_tool(
                slug="a0d7b954_nodered",
                path="/flows",
                limit=10,
                array_patch={"operations": [{"op": "delete", "id": "x"}]},
            )
        err = _parse_tool_error(exc)
        assert err["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.asyncio
    async def test_dispatch_rejects_empty_operations(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _registered_tool(
                slug="a0d7b954_nodered",
                path="/flows",
                array_patch={"operations": []},
            )
        err = _parse_tool_error(exc)
        assert err["error"]["code"] == "VALIDATION_FAILED"
