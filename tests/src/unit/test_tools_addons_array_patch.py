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


@pytest.fixture
def _registered_tool():
    """Resolve the raw ha_manage_addon function from the @tool decorator stack.

    Shared by every TestHaManageAddon* class — they all need the same
    pre-registration unwrap to call the tool function directly.
    """
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

    def test_delete_where_field_absent_on_all_items_emits_warning(self):
        """count=0 with a misspelled or unknown field is the silent-failure
        case — distinguish it from "field exists, value didn't match" by
        attaching a warning to the summary entry. Doesn't raise (preserves
        the deliberate best-effort semantics of delete_where)."""
        items = [{"id": "a"}, {"id": "b"}]
        new, summary = _apply_array_ops(
            items,
            [{"op": "delete_where", "field": "zzz_misspelled", "value": "tab1"}],
            id_field="id",
        )
        assert new == items
        entry = summary["deleted_where"][0]
        assert entry["count"] == 0
        assert "warning" in entry
        assert "zzz_misspelled" in entry["warning"]

    def test_delete_where_field_present_but_no_match_no_warning(self):
        """count=0 when the field exists on items but no value matched is
        not the silent-failure case — no warning, just count=0."""
        items = [{"id": "a", "z": "tab9"}, {"id": "b", "z": "tab9"}]
        new, summary = _apply_array_ops(
            items,
            [{"op": "delete_where", "field": "z", "value": "tab1"}],
            id_field="id",
        )
        assert new == items
        entry = summary["deleted_where"][0]
        assert entry["count"] == 0
        assert "warning" not in entry

    def test_delete_where_against_empty_array_no_warning(self):
        """Empty input array is not a typo signal — there are no items to
        check. Without this guard, `any(... for [])` is False and the typo
        warning would fire unconditionally."""
        new, summary = _apply_array_ops(
            [],
            [{"op": "delete_where", "field": "z", "value": "tab1"}],
            id_field="id",
        )
        assert new == []
        entry = summary["deleted_where"][0]
        assert entry["count"] == 0
        assert "warning" not in entry

    def test_delete_where_against_non_dict_items_no_warning(self):
        """Arrays of scalars (or other non-dict items) are not a typo signal
        either — `field in it` is meaningless for non-dicts, so suggesting
        a typo would be misleading."""
        new, summary = _apply_array_ops(
            [1, 2, "x"],
            [{"op": "delete_where", "field": "z", "value": "tab1"}],
            id_field="id",
        )
        assert new == [1, 2, "x"]
        entry = summary["deleted_where"][0]
        assert entry["count"] == 0
        assert "warning" not in entry

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

    def test_add_with_empty_string_id_raises(self):
        """`id_field not in item` only checks key presence, not value validity.
        Empty-string ids enable cross-contamination with items that legitimately
        lack the id field (`dict.get(id_field)` returns `None`/`""`)."""
        with pytest.raises(ToolError) as exc:
            _apply_array_ops(
                [], [{"op": "add", "item": {"id": "", "type": "x"}}], id_field="id"
            )
        assert _parse_tool_error(exc)["error"]["code"] == "VALIDATION_FAILED"

    def test_add_with_none_id_raises(self):
        """Same rationale as empty-string: `id` cannot be None."""
        with pytest.raises(ToolError) as exc:
            _apply_array_ops(
                [], [{"op": "add", "item": {"id": None, "type": "x"}}], id_field="id"
            )
        assert _parse_tool_error(exc)["error"]["code"] == "VALIDATION_FAILED"

    def test_add_with_integer_zero_id_is_accepted(self):
        """The check uses `is None or == ""` (not `not new_id`) so falsy
        but valid ids like 0 / 0.0 / False are accepted. Pin this down so
        a future refactor to `if not new_id:` doesn't silently regress."""
        new, summary = _apply_array_ops(
            [], [{"op": "add", "item": {"id": 0, "type": "x"}}], id_field="id"
        )
        assert summary["added"] == [{"id": 0}]
        assert new == [{"id": 0, "type": "x"}]

    def test_patch_with_empty_patches_raises(self):
        """Empty `patches` is a silent no-op — reject up-front so the caller
        knows their op was meaningless."""
        with pytest.raises(ToolError) as exc:
            _apply_array_ops(
                [{"id": "a"}],
                [{"op": "patch", "id": "a", "patches": {}}],
                id_field="id",
            )
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

    We verify: GET happens with raw=True (full array needed for mutation);
    POST happens without raw (response is small, default truncation guards
    apply); mutated array is sent; response shape is the compact summary.
    """

    @pytest.mark.asyncio
    async def test_dispatch_fetches_then_posts_mutated_array(self, _registered_tool):
        fetched_array = [
            {"id": "n1", "name": "old"},
            {"id": "n2", "name": "keep"},
        ]
        call_log: list[tuple[str, dict]] = []

        async def fake_call(**kwargs):
            method = kwargs.get("method", "GET")
            # GET must use raw=True so the full array isn't truncated.
            # POST does not need raw — its response is small (deploy revision)
            # and we want the size-based truncation guard in effect.
            if method == "GET":
                assert kwargs.get("raw") is True, "GET should use raw=True"
            else:
                assert not kwargs.get("raw"), "POST should not use raw"
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

        with (
            patch(
                "ha_mcp.tools.tools_addons._call_addon_api",
                new=AsyncMock(side_effect=fake_call),
            ),
            pytest.raises(ToolError) as exc,
        ):
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

        with (
            patch(
                "ha_mcp.tools.tools_addons._call_addon_api",
                new=AsyncMock(side_effect=fake_call),
            ),
            pytest.raises(ToolError),
        ):
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
    async def test_dispatch_raises_when_fetch_fails(self, _registered_tool):
        """A failed GET must surface as ToolError and skip the POST entirely.

        Guards against a regression where a swallowed fetch failure would let
        the dispatcher synthesise success on stale/empty state.
        """
        post_called = False

        async def fake_call(**kwargs):
            nonlocal post_called
            if kwargs.get("method", "GET") == "GET":
                return {
                    "success": False,
                    "status_code": 502,
                    "error": "Add-on API returned HTTP 502",
                    "addon_name": "Node-RED",
                    "slug": kwargs["slug"],
                }
            post_called = True
            return {"success": True, "response": "ok", "status_code": 200}

        with (
            patch(
                "ha_mcp.tools.tools_addons._call_addon_api",
                new=AsyncMock(side_effect=fake_call),
            ),
            pytest.raises(ToolError),
        ):
            await _registered_tool(
                slug="a0d7b954_nodered",
                path="/flows",
                array_patch={
                    "operations": [{"op": "patch", "id": "n1", "patches": {"x": 1}}],
                },
            )

        assert post_called is False

    @pytest.mark.asyncio
    async def test_dispatch_raises_when_post_fails(self, _registered_tool):
        """A failed POST must surface as ToolError, not be reported as success.

        Without this guard a failed write would still return `success: True`
        with the in-memory `items_after` even though the addon rejected it.
        """
        fetched_array = [{"id": "n1", "name": "old"}]
        get_called = False
        post_called = False

        async def fake_call(**kwargs):
            nonlocal get_called, post_called
            if kwargs.get("method", "GET") == "GET":
                get_called = True
                return {
                    "success": True,
                    "response": fetched_array,
                    "addon_name": "Node-RED",
                    "slug": kwargs["slug"],
                    "status_code": 200,
                }
            post_called = True
            return {
                "success": False,
                "status_code": 400,
                "error": "Add-on API returned HTTP 400",
                "response": "deploy rejected",
                "addon_name": "Node-RED",
                "slug": kwargs["slug"],
            }

        with (
            patch(
                "ha_mcp.tools.tools_addons._call_addon_api",
                new=AsyncMock(side_effect=fake_call),
            ),
            pytest.raises(ToolError),
        ):
            await _registered_tool(
                slug="a0d7b954_nodered",
                path="/flows",
                array_patch={
                    "operations": [
                        {"op": "patch", "id": "n1", "patches": {"name": "new"}},
                    ],
                },
            )

        assert get_called is True
        assert post_called is True

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


class TestHaManageAddonRequestHeaders:
    """Verify the new top-level request_headers parameter wires through to
    _call_addon_api in proxy mode and array_patch mode (both GET and POST).
    """

    @pytest.mark.asyncio
    async def test_proxy_mode_forwards_request_headers(self, _registered_tool):
        captured_headers: list[dict | None] = []

        async def fake_call(**kwargs):
            captured_headers.append(kwargs.get("extra_headers"))
            return {
                "success": True,
                "response": {"ok": True},
                "addon_name": "X",
                "slug": kwargs["slug"],
                "status_code": 200,
            }

        with patch(
            "ha_mcp.tools.tools_addons._call_addon_api",
            new=AsyncMock(side_effect=fake_call),
        ):
            await _registered_tool(
                slug="a0d7b954_nodered",
                path="/api/state",
                request_headers={"Accept": "text/plain"},
            )

        assert captured_headers == [{"Accept": "text/plain"}]

    @pytest.mark.asyncio
    async def test_array_patch_forwards_request_headers_to_get_and_post(
        self, _registered_tool
    ):
        captured_headers: list[dict | None] = []

        async def fake_call(**kwargs):
            captured_headers.append(kwargs.get("extra_headers"))
            if kwargs.get("method", "GET") == "GET":
                return {
                    "success": True,
                    "response": [{"id": "n1", "name": "old"}],
                    "addon_name": "Node-RED",
                    "slug": kwargs["slug"],
                    "status_code": 200,
                }
            return {"success": True, "response": "rev-1", "status_code": 200}

        with patch(
            "ha_mcp.tools.tools_addons._call_addon_api",
            new=AsyncMock(side_effect=fake_call),
        ):
            await _registered_tool(
                slug="a0d7b954_nodered",
                path="/flows",
                array_patch={
                    "operations": [
                        {"op": "patch", "id": "n1", "patches": {"name": "new"}},
                    ],
                },
                request_headers={"Node-RED-Deployment-Type": "full"},
            )

        # Both GET and POST must receive the same caller-supplied headers
        assert len(captured_headers) == 2
        assert captured_headers[0] == {"Node-RED-Deployment-Type": "full"}
        assert captured_headers[1] == {"Node-RED-Deployment-Type": "full"}

    @pytest.mark.asyncio
    async def test_request_headers_rejected_with_websocket(self, _registered_tool):
        """`_call_addon_ws` doesn't accept caller headers. Without an explicit
        rejection, request_headers would be silently dropped in WebSocket mode
        — inconsistent with the existing fail-loud-on-misroute pattern (e.g.
        message_limit rejection on HTTP)."""
        with pytest.raises(ToolError) as exc:
            await _registered_tool(
                slug="a0d7b954_nodered",
                path="/logs",
                websocket=True,
                request_headers={"X-Custom": "v"},
            )
        err = _parse_tool_error(exc)
        assert err["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.asyncio
    async def test_request_headers_rejected_in_config_mode(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _registered_tool(
                slug="a0d7b954_nodered",
                options={"log_level": "debug"},
                request_headers={"X-Custom": "value"},
            )
        err = _parse_tool_error(exc)
        assert err["error"]["code"] == "VALIDATION_FAILED"
        assert "request_headers" in err["error"]["message"]


class TestCallAddonApiHeaderMerge:
    """Direct test of _call_addon_api's header-merge contract: caller headers
    go in first, internal framing layered on top so it always wins on collision.
    """

    @pytest.mark.asyncio
    async def test_internal_framing_overrides_caller_headers(self):
        """If a caller tries to override X-Ingress-Path / X-Hass-Source /
        Content-Type, the proxy's internal values must still win."""
        from unittest.mock import MagicMock

        from ha_mcp.tools.tools_addons import _call_addon_api

        addon_info = {
            "success": True,
            "addon": {
                "name": "Test",
                "slug": "test",
                "ingress": True,
                "ingress_entry": "/api/hassio_ingress/REAL",
                "ingress_port": 5000,
                "ip_address": "172.30.33.99",
                "state": "started",
            },
        }

        # Capture the headers passed to httpx.request — same mocking pattern
        # the existing _call_addon_api tests use.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "ok"
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.json.return_value = {}

        mock_http_client = AsyncMock()
        mock_http_client.request.return_value = mock_response

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=addon_info,
            ),
            patch(
                "ha_mcp.tools.tools_addons.is_running_in_addon",
                return_value=True,
            ),
            patch(
                "ha_mcp.tools.tools_addons.httpx.AsyncClient",
            ) as mock_httpx,
        ):
            mock_httpx.return_value.__aenter__ = AsyncMock(
                return_value=mock_http_client
            )
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            await _call_addon_api(
                client=AsyncMock(),
                slug="test",
                path="/api/state",
                method="POST",
                body={"x": 1},
                extra_headers={
                    "X-Ingress-Path": "/api/hassio_ingress/EVIL",
                    "X-Hass-Source": "spoofed",
                    "Content-Type": "application/x-attack",
                    "X-Custom-Allowed": "kept",
                },
            )

        # Headers actually sent to the addon container:
        sent = mock_http_client.request.call_args.kwargs["headers"]
        # Internal framing wins
        assert sent["X-Ingress-Path"] == "/api/hassio_ingress/REAL"
        assert sent["X-Hass-Source"] == "core.ingress"
        assert sent["Content-Type"] == "application/json"
        # Non-conflicting caller headers pass through
        assert sent["X-Custom-Allowed"] == "kept"
