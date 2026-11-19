"""Unit tests for ha_manage_app's array_patch mode.

The app and Supervisor are HTTP peers; the tool uses its real Core client
and production HTTP proxy. Assertions inspect the requests the app receives
and the compact summary the tool returns.
"""

from typing import Any

import pytest
from aiohttp import web

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ._tool_errors import error_body, error_of
from ._tools_addons_core import INGRESS_TOKEN, SLUG, Route, manage
from ._tools_addons_core import app_route as app_route
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app


@pytest.fixture
def _registered_tool(app_route: Route):
    app_route.supervisor.running()
    return app_route


async def _patch_array(
    route: Route,
    items: list[Any],
    operations: list[dict[str, Any]],
    *,
    id_field: str = "id",
    posted: list[Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Apply operations to an array served over the real app HTTP route."""
    route.target.body = items
    seen = len(route.target.requests)
    try:
        result = await manage(
            route.client,
            slug=SLUG,
            path="/flows",
            array_patch={"id_field": id_field, "operations": operations},
        )
    finally:
        bodies = [
            request.body
            for request in route.target.requests[seen:]
            if request.method == "POST"
        ]
        if posted is not None:
            posted.extend(bodies)
    [body] = bodies
    return body, result["summary"]


# ---------------------------------------------------------------------------
# array_patch operations — happy paths
# ---------------------------------------------------------------------------


class TestApplyArrayOps:
    async def test_repeated_calls_capture_only_their_own_post(self, _registered_tool):
        first, _ = await _patch_array(
            _registered_tool,
            [{"id": "n1", "name": "Original"}],
            [{"op": "patch", "id": "n1", "patches": {"name": "First"}}],
        )
        posted: list[Any] = []
        second, summary = await _patch_array(
            _registered_tool,
            first,
            [{"op": "patch", "id": "n1", "patches": {"name": "Second"}}],
            posted=posted,
        )
        assert first == [{"id": "n1", "name": "First"}]
        assert second == [{"id": "n1", "name": "Second"}]
        assert posted == [second]
        assert summary["patched"] == [{"id": "n1", "fields": ["name"]}]

    def _flows_fixture(self) -> list[dict]:
        return [
            {"id": "tab1", "type": "tab", "label": "Lights"},
            {"id": "tab2", "type": "tab", "label": "Bathroom"},
            {"id": "n1", "type": "inject", "z": "tab1", "name": "Trigger"},
            {"id": "n2", "type": "function", "z": "tab1", "name": "Compute"},
            {"id": "n3", "type": "switch", "z": "tab2", "name": "Sense"},
        ]

    async def test_patch_updates_fields_in_place(self, _registered_tool):
        items = self._flows_fixture()
        new, summary = await _patch_array(
            _registered_tool,
            items,
            [{"op": "patch", "id": "n1", "patches": {"name": "Renamed"}}],
            id_field="id",
        )
        n1 = next(it for it in new if it["id"] == "n1")
        assert n1["name"] == "Renamed"
        assert summary["patched"] == [{"id": "n1", "fields": ["name"]}]

    async def test_patch_preserves_unrelated_fields(self, _registered_tool):
        items = self._flows_fixture()
        new, _ = await _patch_array(
            _registered_tool,
            items,
            [{"op": "patch", "id": "n2", "patches": {"name": "X"}}],
            id_field="id",
        )
        n2 = next(it for it in new if it["id"] == "n2")
        assert n2["type"] == "function"
        assert n2["z"] == "tab1"

    async def test_delete_removes_single_item(self, _registered_tool):
        items = self._flows_fixture()
        new, summary = await _patch_array(
            _registered_tool,
            items,
            [{"op": "delete", "id": "n1"}],
            id_field="id",
        )
        assert "n1" not in {it["id"] for it in new}
        assert summary["deleted"] == [{"id": "n1"}]

    async def test_add_appends_new_item(self, _registered_tool):
        items = self._flows_fixture()
        new, summary = await _patch_array(
            _registered_tool,
            items,
            [{"op": "add", "item": {"id": "n4", "type": "debug", "z": "tab1"}}],
            id_field="id",
        )
        assert {it["id"] for it in new} == {"tab1", "tab2", "n1", "n2", "n3", "n4"}
        assert summary["added"] == [{"id": "n4"}]

    async def test_delete_where_removes_all_matching(self, _registered_tool):
        items = self._flows_fixture()
        new, summary = await _patch_array(
            _registered_tool,
            items,
            [{"op": "delete_where", "field": "z", "value": "tab1"}],
            id_field="id",
        )
        # tab1 itself stays (z field is absent on tabs); only the children go
        assert {it["id"] for it in new} == {"tab1", "tab2", "n3"}
        assert summary["deleted_where"] == [{"field": "z", "value": "tab1", "count": 2}]

    async def test_delete_where_field_absent_on_all_items_emits_warning(
        self, _registered_tool
    ):
        """count=0 with a misspelled or unknown field is the silent-failure
        case — distinguish it from "field exists, value didn't match" by
        attaching a warning to the summary entry. Doesn't raise (preserves
        the deliberate best-effort semantics of delete_where)."""
        items = [{"id": "a"}, {"id": "b"}]
        new, summary = await _patch_array(
            _registered_tool,
            items,
            [{"op": "delete_where", "field": "zzz_misspelled", "value": "tab1"}],
            id_field="id",
        )
        assert new == items
        entry = summary["deleted_where"][0]
        assert entry["count"] == 0
        assert entry.get("warnings"), f"Expected warnings list, got: {entry}"
        assert any("zzz_misspelled" in w for w in entry["warnings"])

    async def test_delete_where_field_present_but_no_match_no_warning(
        self, _registered_tool
    ):
        """count=0 when the field exists on items but no value matched is
        not the silent-failure case — no warning, just count=0."""
        items = [{"id": "a", "z": "tab9"}, {"id": "b", "z": "tab9"}]
        new, summary = await _patch_array(
            _registered_tool,
            items,
            [{"op": "delete_where", "field": "z", "value": "tab1"}],
            id_field="id",
        )
        assert new == items
        entry = summary["deleted_where"][0]
        assert entry["count"] == 0
        assert not entry.get("warnings"), (
            f"Unexpected warnings: {entry.get('warnings')}"
        )

    async def test_delete_where_against_empty_array_no_warning(self, _registered_tool):
        """Empty input array is not a typo signal — there are no items to
        check. Without this guard, `any(... for [])` is False and the typo
        warning would fire unconditionally."""
        new, summary = await _patch_array(
            _registered_tool,
            [],
            [{"op": "delete_where", "field": "z", "value": "tab1"}],
            id_field="id",
        )
        assert new == []
        entry = summary["deleted_where"][0]
        assert entry["count"] == 0
        assert not entry.get("warnings"), (
            f"Unexpected warnings: {entry.get('warnings')}"
        )

    async def test_delete_where_against_non_dict_items_no_warning(
        self, _registered_tool
    ):
        """Arrays of scalars (or other non-dict items) are not a typo signal
        either — `field in it` is meaningless for non-dicts, so suggesting
        a typo would be misleading."""
        new, summary = await _patch_array(
            _registered_tool,
            [1, 2, "x"],
            [{"op": "delete_where", "field": "z", "value": "tab1"}],
            id_field="id",
        )
        assert new == [1, 2, "x"]
        entry = summary["deleted_where"][0]
        assert entry["count"] == 0
        assert not entry.get("warnings"), (
            f"Unexpected warnings: {entry.get('warnings')}"
        )

    async def test_delete_where_zero_matches_is_not_an_error(self, _registered_tool):
        items = self._flows_fixture()
        new, summary = await _patch_array(
            _registered_tool,
            items,
            [{"op": "delete_where", "field": "z", "value": "ghost-tab"}],
            id_field="id",
        )
        assert len(new) == len(items)
        assert summary["deleted_where"][0]["count"] == 0

    async def test_operations_apply_in_order(self, _registered_tool):
        items = self._flows_fixture()
        new, summary = await _patch_array(
            _registered_tool,
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

    async def test_custom_id_field(self, _registered_tool):
        items = [
            {"slug": "thermostat-living", "value": 21},
            {"slug": "thermostat-bedroom", "value": 19},
        ]
        new, _ = await _patch_array(
            _registered_tool,
            items,
            [{"op": "patch", "id": "thermostat-bedroom", "patches": {"value": 20}}],
            id_field="slug",
        )
        bed = next(it for it in new if it["slug"] == "thermostat-bedroom")
        assert bed["value"] == 20


# ---------------------------------------------------------------------------
# array_patch operations — validation failures
# ---------------------------------------------------------------------------


class TestApplyArrayOpsValidation:
    async def test_unknown_op_raises(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool, [], [{"op": "yeet", "id": "x"}], id_field="id"
            )
        err = error_body(exc)
        assert err["error"]["code"] == "VALIDATION_FAILED"

    async def test_patch_missing_id_raises(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool,
                [{"id": "a"}],
                [{"op": "patch", "patches": {"name": "X"}}],
                id_field="id",
            )
        assert error_of(exc)["code"] == "VALIDATION_FAILED"

    async def test_patch_unknown_id_raises(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool,
                [{"id": "a"}],
                [{"op": "patch", "id": "ghost", "patches": {"name": "X"}}],
                id_field="id",
            )
        assert error_of(exc)["code"] == "RESOURCE_NOT_FOUND"

    async def test_patch_non_dict_patches_raises(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool,
                [{"id": "a"}],
                [{"op": "patch", "id": "a", "patches": "not a dict"}],
                id_field="id",
            )
        assert error_of(exc)["code"] == "VALIDATION_FAILED"

    async def test_delete_unknown_id_raises(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool,
                [{"id": "a"}],
                [{"op": "delete", "id": "ghost"}],
                id_field="id",
            )
        assert error_of(exc)["code"] == "RESOURCE_NOT_FOUND"

    async def test_add_collision_raises(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool,
                [{"id": "a"}],
                [{"op": "add", "item": {"id": "a", "type": "x"}}],
                id_field="id",
            )
        assert error_of(exc)["code"] == "RESOURCE_ALREADY_EXISTS"

    async def test_add_missing_id_field_raises(self, _registered_tool):
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool,
                [],
                [{"op": "add", "item": {"type": "x"}}],
                id_field="id",
            )
        assert error_of(exc)["code"] == "VALIDATION_FAILED"

    async def test_add_with_empty_string_id_raises(self, _registered_tool):
        """`id_field not in item` only checks key presence, not value validity.
        Empty-string ids enable cross-contamination with items that legitimately
        lack the id field (`dict.get(id_field)` returns `None`/`""`)."""
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool,
                [],
                [{"op": "add", "item": {"id": "", "type": "x"}}],
                id_field="id",
            )
        assert error_of(exc)["code"] == "VALIDATION_FAILED"

    async def test_add_with_none_id_raises(self, _registered_tool):
        """Same rationale as empty-string: `id` cannot be None."""
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool,
                [],
                [{"op": "add", "item": {"id": None, "type": "x"}}],
                id_field="id",
            )
        assert error_of(exc)["code"] == "VALIDATION_FAILED"

    async def test_add_with_integer_zero_id_is_accepted(self, _registered_tool):
        """The check rejects `None` and whitespace-only strings only — non-string
        falsy ids like 0 / 0.0 / False stay valid by design. Pin this down so
        a future refactor to `if not new_id:` doesn't silently regress."""
        new, summary = await _patch_array(
            _registered_tool,
            [],
            [{"op": "add", "item": {"id": 0, "type": "x"}}],
            id_field="id",
        )
        assert summary["added"] == [{"id": 0}]
        assert new == [{"id": 0, "type": "x"}]

    @pytest.mark.parametrize("bad_id", [" ", "   ", "\t", "\n", " \t\n "])
    async def test_add_with_whitespace_only_id_raises(self, _registered_tool, bad_id):
        """Whitespace-only string ids would let later patch/delete ops match
        unrelated items (the same cross-contamination risk as the
        empty-string class). The guard catches the whole class via
        ``str.strip()``."""
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool,
                [],
                [{"op": "add", "item": {"id": bad_id, "type": "x"}}],
                id_field="id",
            )
        assert error_of(exc)["code"] == "VALIDATION_FAILED"

    async def test_patch_with_empty_patches_raises(self, _registered_tool):
        """Empty `patches` is a silent no-op — reject up-front so the caller
        knows their op was meaningless."""
        with pytest.raises(ToolError) as exc:
            await _patch_array(
                _registered_tool,
                [{"id": "a"}],
                [{"op": "patch", "id": "a", "patches": {}}],
                id_field="id",
            )
        assert error_of(exc)["code"] == "VALIDATION_FAILED"

    async def test_delete_where_with_explicit_none_value_is_allowed(
        self, _registered_tool
    ):
        items = [
            {"id": "a", "z": None},
            {"id": "b", "z": "tab1"},
        ]
        new, summary = await _patch_array(
            _registered_tool,
            items,
            [{"op": "delete_where", "field": "z", "value": None}],
            id_field="id",
        )
        assert {it["id"] for it in new} == {"b"}
        assert summary["deleted_where"][0]["count"] == 1

    async def test_delete_where_none_value_does_not_delete_items_missing_field(
        self, _registered_tool
    ):
        # Items that don't have the field at all should NOT be deleted when value=None.
        # dict.get() returns None for missing keys, so without an explicit `field in it`
        # check, these items would be incorrectly matched.
        items = [
            {"id": "a", "z": None},
            {"id": "b"},  # field absent — must survive
            {"id": "c", "z": "tab1"},
        ]
        new, summary = await _patch_array(
            _registered_tool,
            items,
            [{"op": "delete_where", "field": "z", "value": None}],
            id_field="id",
        )
        assert {it["id"] for it in new} == {"b", "c"}
        assert summary["deleted_where"][0]["count"] == 1

    async def test_failure_midway_posts_nothing(self, _registered_tool):
        """An op that fails mid-stream raises before anything is posted, so
        the earlier ops' in-memory edits never reach the app."""
        posted: list[Any] = []
        with pytest.raises(ToolError):
            await _patch_array(
                _registered_tool,
                [{"id": "a"}, {"id": "b"}],
                [
                    {"op": "patch", "id": "a", "patches": {"name": "ok"}},
                    {"op": "patch", "id": "ghost", "patches": {"name": "fail"}},
                ],
                posted=posted,
            )
        assert posted == []


# ---------------------------------------------------------------------------
# ha_manage_app array_patch dispatch — happy + key error paths
# ---------------------------------------------------------------------------


class TestHaManageAddonArrayPatchDispatch:
    async def test_dispatch_fetches_then_posts_mutated_array(self, _registered_tool):
        route = _registered_tool
        # Beyond the normal response truncation boundary: patching the last
        # item proves the internal GET read the whole array.
        route.target.body = [{"id": f"n{i}", "name": "old"} for i in range(2000)]
        result = await manage(
            route.client,
            slug=SLUG,
            path="/flows",
            array_patch={
                "operations": [
                    {"op": "patch", "id": "n1999", "patches": {"name": "new"}},
                ]
            },
        )
        get, post = route.target.requests
        assert [get.method, post.method] == ["GET", "POST"]
        assert len(post.body) == 2000
        assert post.body[-1]["name"] == "new"
        assert post.body[0] == {"id": "n0", "name": "old"}
        assert result["items_before"] == result["items_after"] == 2000
        assert result["summary"]["patched"] == [{"id": "n1999", "fields": ["name"]}]

    async def test_dispatch_rejects_non_array_response(self, _registered_tool):
        route = _registered_tool
        route.target.body = {"not": "an array"}
        with pytest.raises(ToolError) as exc:
            await manage(
                route.client,
                slug=SLUG,
                path="/flows",
                array_patch={"operations": [{"op": "delete", "id": "x"}]},
            )
        assert error_of(exc)["code"] == "VALIDATION_FAILED"
        assert [request.method for request in route.target.requests] == ["GET"]

    @pytest.mark.parametrize("failure", ["get", "post"])
    async def test_dispatch_failure_never_reports_a_successful_write(
        self, _registered_tool, monkeypatch, failure
    ):
        route = _registered_tool
        route.target.body = [{"id": "n1", "name": "old"}]
        handle = route.target.handle

        async def respond(request: web.Request, path: str):
            route.target.status = 502 if request.method.lower() == failure else 200
            return await handle(request, path)

        monkeypatch.setattr(route.target, "handle", respond)
        with pytest.raises(ToolError):
            await manage(
                route.client,
                slug=SLUG,
                path="/flows",
                array_patch={
                    "operations": [
                        {"op": "patch", "id": "n1", "patches": {"name": "new"}},
                    ]
                },
            )
        assert [r.method for r in route.target.requests] == (
            ["GET"] if failure == "get" else ["GET", "POST"]
        )

    @pytest.mark.parametrize(
        "extra",
        [
            {"websocket": True},
            {"body": {"foo": "bar"}},
            {"limit": 10},
            {"array_patch": {"operations": []}},
        ],
    )
    async def test_dispatch_rejects_conflicting_or_empty_modes(
        self, _registered_tool, extra
    ):
        route = _registered_tool
        kwargs = {"array_patch": {"operations": [{"op": "delete", "id": "x"}]}, **extra}
        with pytest.raises(ToolError) as exc:
            await manage(route.client, slug=SLUG, path="/flows", **kwargs)
        assert error_of(exc)["code"] == "VALIDATION_FAILED"
        assert not route.target.requests


class TestHaManageAddonRequestHeaders:
    @pytest.mark.parametrize("array_patch", [False, True])
    async def test_caller_headers_reach_every_app_request(
        self, _registered_tool, array_patch
    ):
        route = _registered_tool
        route.target.body = [{"id": "n1", "name": "old"}]
        extra = (
            {
                "array_patch": {
                    "operations": [
                        {"op": "patch", "id": "n1", "patches": {"name": "new"}},
                    ]
                }
            }
            if array_patch
            else {}
        )
        await manage(
            route.client,
            slug=SLUG,
            path="/flows",
            request_headers={"Node-RED-Deployment-Type": "full"},
            **extra,
        )
        assert len(route.target.requests) == (2 if array_patch else 1)
        assert all(
            r.headers["Node-RED-Deployment-Type"] == "full"
            for r in route.target.requests
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"path": "/logs", "websocket": True},
            {"options": {"log_level": "debug"}},
        ],
    )
    async def test_headers_are_rejected_outside_http_proxy_mode(
        self, _registered_tool, kwargs
    ):
        route = _registered_tool
        with pytest.raises(ToolError) as exc:
            await manage(
                route.client, slug=SLUG, request_headers={"X-Custom": "v"}, **kwargs
            )
        assert error_of(exc)["code"] == "VALIDATION_FAILED"
        assert not route.target.requests


class TestProxyHeaderMerge:
    """The proxy's header-merge contract: caller headers go in first, internal
    framing layered on top so it always wins on collision.
    """

    async def test_internal_framing_overrides_caller_headers(
        self, app_route: Route
    ) -> None:
        """If a caller tries to override X-Ingress-Path / X-Hass-Source /
        Content-Type, the proxy's internal values must still win."""
        app_route.supervisor.running()

        await manage(
            app_route.client,
            slug=SLUG,
            path="/api/state",
            method="POST",
            body={"x": 1},
            request_headers={
                "X-Ingress-Path": "/api/hassio_ingress/EVIL",
                "X-Hass-Source": "spoofed",
                "Content-Type": "application/x-attack",
                "X-Custom-Allowed": "kept",
            },
        )

        # Headers the app received:
        [seen] = app_route.target.requests
        # Internal framing wins
        assert seen.headers["X-Ingress-Path"] == f"/api/hassio_ingress/{INGRESS_TOKEN}"
        assert seen.headers["X-Hass-Source"] == "core.ingress"
        assert seen.headers["Content-Type"] == "application/json"
        assert seen.body == {"x": 1}
        # Non-conflicting caller headers pass through
        assert seen.headers["X-Custom-Allowed"] == "kept"
