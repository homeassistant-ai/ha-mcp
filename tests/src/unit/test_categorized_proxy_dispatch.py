"""Categorized tool search via public transform and tool interfaces."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.transforms.categorized_search import (
    CategorizedSearchTransform,
)

from ._categorized_search_helpers import (
    _make_ctx,
    _make_tool,
    proxy_function,
    set_catalog,
)
from ._categorized_search_helpers import read_only_on as read_only_on
from ._search_mode_connection import search_connection as search_connection


class TestCategorizedCallDispatch:
    """Tests for the categorized_call closure — the core dispatch function."""

    @pytest.fixture
    def transform(self):
        t = CategorizedSearchTransform(max_results=5)
        set_catalog(
            t,
            [
                _make_tool("ha_get_state", read_only=True),
                _make_tool("ha_search", read_only=True),
                _make_tool("ha_config_set_automation", destructive=True),
                _make_tool("ha_call_service", destructive=True),
                _make_tool("ha_remove_area_or_floor", destructive=True),
            ],
        )
        return t

    @pytest.mark.anyio
    async def test_read_proxy_happy_path(self, transform):
        """Correct read tool via read proxy succeeds."""
        ctx = _make_ctx(call_tool_return={"state": "on"})
        fn = await proxy_function(transform, "read")
        result = await fn("ha_get_state", {"entity_id": "light.kitchen"}, ctx)
        assert result == {"state": "on"}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_get_state", {"entity_id": "light.kitchen"}
        )

    @pytest.mark.anyio
    async def test_read_proxy_admits_a_mixed_tools_read_action(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """A write-categorised tool's read actions stay reachable on the read
        proxy (#2329). Category membership is unchanged; the call is admitted
        because the read-only predicate approves it."""
        set_catalog(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = await proxy_function(transform, "read")

        result = await fn("ha_manage_blueprints", {"action": "list"}, ctx)

        assert result == {"success": True}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_manage_blueprints", {"action": "list"}
        )

    @pytest.mark.anyio
    async def test_read_proxy_refuses_a_mixed_tools_write_action(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """Admission is per call, not per tool: a state-changing action on the
        same tool is still refused on the read proxy."""
        set_catalog(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")

        with pytest.raises(ToolError) as exc_info:
            await fn(
                "ha_manage_blueprints",
                {"action": "delete", "path": "user/motion.yaml", "confirm": True},
                ctx,
            )
        body = json.loads(str(exc_info.value))
        error = body["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["correct_proxy"] == "ha_call_write_tool"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_read_proxy_refuses_a_manage_tool_outside_the_read_only_table(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """No exemption entry means no read predicate, so the read proxy fails
        closed for every call — even one whose action sounds harmless."""
        set_catalog(
            transform,
            [
                _make_tool("ha_manage_hacs", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")

        with pytest.raises(ToolError) as exc_info:
            await fn("ha_manage_hacs", {"action": "update_information"}, ctx)
        body = json.loads(str(exc_info.value))
        assert body["correct_proxy"] == "ha_call_write_tool"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_read_proxy_reports_an_exempt_tool_missing_from_the_catalog(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The read-only table alone does not admit a call: the tool must be
        in the live catalog, or the caller gets not-found rather than a
        dispatch into FastMCP's unknown-tool error."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")

        with pytest.raises(ToolError) as exc_info:
            await fn("ha_manage_blueprints", {"action": "list"}, ctx)
        body = json.loads(str(exc_info.value))
        error = body["error"]
        assert error["code"] == "RESOURCE_NOT_FOUND"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_write_proxy_runs_every_action_of_a_manage_tool(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The write proxy is the manage tool's home category and admits all
        of it, delete actions included: those are not statically enumerable
        (ha_manage_radio and ha_manage_updates take a free-form action), so
        the proxy does not pretend to carve them out."""
        set_catalog(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = await proxy_function(transform, "write")

        for arguments in (
            {"action": "list"},
            {"action": "import", "url": "https://example.invalid/bp.yaml"},
            {"action": "delete", "path": "user/motion.yaml", "confirm": True},
        ):
            assert await fn("ha_manage_blueprints", arguments, ctx) == {"success": True}
        assert ctx.fastmcp.call_tool.await_count == 3

    @pytest.mark.anyio
    async def test_delete_proxy_runs_every_action_of_a_manage_tool(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The delete proxy is the most gated one, so it runs the whole
        manage tool — reads included — with no per-call check."""
        set_catalog(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_manage_hacs", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = await proxy_function(transform, "delete")

        for name, arguments in (
            ("ha_manage_blueprints", {"action": "list"}),
            ("ha_manage_blueprints", {"action": "import", "url": "https://x.invalid"}),
            (
                "ha_manage_blueprints",
                {"action": "delete", "path": "x", "confirm": True},
            ),
            ("ha_manage_hacs", {"action": "remove", "repository": "a/b"}),
        ):
            assert await fn(name, arguments, ctx) == {"success": True}
        assert ctx.fastmcp.call_tool.await_count == 4

    @pytest.mark.anyio
    async def test_delete_proxy_refuses_a_plain_write_tool(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """Only manage tools cross categories — an ordinary write tool on
        the delete proxy is still the wrong category."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "delete")

        with pytest.raises(ToolError) as exc_info:
            await fn("ha_config_set_automation", {"config": {}}, ctx)
        body = json.loads(str(exc_info.value))
        assert body["correct_proxy"] == "ha_call_write_tool"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_read_proxy_refuses_a_plain_write_tool(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The admission covers mixed tools only — a tool with no read surface
        is refused as before."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")

        with pytest.raises(ToolError):
            await fn("ha_config_set_automation", {"config": {}}, ctx)
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_proxy_adapts_a_retired_blueprint_call(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """A stale catalog calling ha_get_blueprint reaches the consolidated
        tool with the action its old signature never carried."""
        set_catalog(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = await proxy_function(transform, "write")

        await fn("ha_get_blueprint", {"domain": "script"}, ctx)

        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_manage_blueprints", {"action": "list", "domain": "script"}
        )

    @pytest.mark.anyio
    async def test_proxy_dispatches_a_renamed_tool_under_its_current_name(
        self, transform
    ):
        """A stale catalog can name a tool that has since been renamed.

        The alias middleware would rewrite a direct call, but a proxied one is
        checked against the live category set first and would be rejected as
        the wrong category before the re-dispatch ever reaches the middleware.
        """
        set_catalog(
            transform,
            [
                _make_tool("ha_manage_app", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = await proxy_function(transform, "write")

        result = await fn("ha_manage_addon", {"slug": "core_ssh"}, ctx)

        assert result == {"success": True}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_manage_app", {"slug": "core_ssh"}
        )

    @pytest.mark.anyio
    async def test_write_proxy_happy_path(self, transform):
        """Correct write tool via write proxy succeeds."""
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = await proxy_function(transform, "write")
        result = await fn("ha_config_set_automation", {"config": {}}, ctx)
        assert result == {"success": True}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_config_set_automation", {"config": {}}
        )

    @pytest.mark.anyio
    async def test_delete_proxy_happy_path(self, transform):
        """Correct delete tool via delete proxy succeeds."""
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = await proxy_function(transform, "delete")
        result = await fn(
            "ha_remove_area_or_floor", {"kind": "area", "id": "garage"}, ctx
        )
        assert result == {"success": True}

    @pytest.mark.anyio
    async def test_wrong_category_rejected_write_via_read(self, transform):
        """Write tool via read proxy is rejected with correct proxy suggestion."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_config_set_automation", {}, ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "ha_call_write_tool" in error["error"]["message"]
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_wrong_category_rejected_read_via_write(self, transform):
        """Read tool via write proxy is rejected."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "write")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_get_state", {}, ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "ha_call_read_tool" in error["error"]["message"]

    @pytest.mark.anyio
    async def test_wrong_category_rejected_delete_via_read(self, transform):
        """Delete tool via read proxy is rejected."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_remove_area_or_floor", {}, ctx)
        error = json.loads(str(exc_info.value))
        assert "ha_call_delete_tool" in error["error"]["message"]

    @pytest.mark.anyio
    async def test_unknown_tool_returns_not_found(self, transform):
        """Tool not in any category returns RESOURCE_NOT_FOUND."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_nonexistent_tool", {}, ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "ha_nonexistent_tool" in error["error"]["message"]

    @pytest.mark.anyio
    async def test_none_arguments_defaults_to_empty(self, transform):
        """Calling with arguments=None still works."""
        ctx = _make_ctx(call_tool_return="ok")
        fn = await proxy_function(transform, "read")
        result = await fn("ha_get_state", None, ctx)
        assert result == "ok"
        ctx.fastmcp.call_tool.assert_called_once_with("ha_get_state", None)


# ---------------------------------------------------------------------------
# Double-unwrap detection
# ---------------------------------------------------------------------------


class TestDoubleUnwrap:
    """Tests for double-wrapped proxy call detection and unwrapping."""

    @pytest.fixture
    def transform(self):
        t = CategorizedSearchTransform(max_results=5)
        set_catalog(
            t,
            [
                _make_tool("ha_get_state", read_only=True),
                _make_tool("ha_config_set_automation", destructive=True),
                _make_tool("ha_remove_area_or_floor", destructive=True),
            ],
        )
        return t

    @pytest.mark.anyio
    async def test_double_wrapped_read_unwraps_correctly(self, transform):
        """Double-wrapped read tool via read proxy unwraps and succeeds."""
        ctx = _make_ctx(call_tool_return={"state": "on"})
        fn = await proxy_function(transform, "read")
        # LLM accidentally nests: ha_call_read_tool(name="ha_call_read_tool",
        #   arguments={"name": "ha_get_state", "arguments": {"entity_id": "x"}})
        result = await fn(
            "ha_call_read_tool",
            {"name": "ha_get_state", "arguments": {"entity_id": "x"}},
            ctx,
        )
        assert result == {"state": "on"}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_get_state", {"entity_id": "x"}
        )

    @pytest.mark.anyio
    async def test_double_wrapped_renamed_tool_unwraps_under_its_current_name(
        self, transform
    ):
        """The envelope's inner name comes from the same stale catalog.

        The recovery block tests it against the live category sets, so an
        unresolved retired name fails the unwrap and the call dies with a
        wrong-category error naming the proxy — the alias covering one envelope
        shape and not the other.
        """
        set_catalog(
            transform,
            [
                _make_tool("ha_manage_app", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = await proxy_function(transform, "write")

        result = await fn(
            "ha_call_write_tool",
            {"name": "ha_manage_addon", "arguments": {"slug": "core_ssh"}},
            ctx,
        )

        assert result == {"success": True}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_manage_app", {"slug": "core_ssh"}
        )

    @pytest.mark.anyio
    async def test_double_wrapped_consolidated_tool_gets_its_action(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The envelope carries the retired signature too (#2329).

        A nested `ha_get_blueprint` resolves to `ha_manage_blueprints`, which
        dispatches on `action` — so the unwrap has to adapt the inner
        arguments, not just the inner name, or the call arrives actionless.
        """
        set_catalog(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = await proxy_function(transform, "write")

        result = await fn(
            "ha_call_write_tool",
            {"name": "ha_get_blueprint", "arguments": {"domain": "script"}},
            ctx,
        )

        assert result == {"success": True}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_manage_blueprints", {"action": "list", "domain": "script"}
        )

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("nested", "code"),
        [
            # A bare string is first tried as JSON, exactly as the outer
            # payload is, so it classifies as invalid JSON rather than as a
            # non-object.
            ("x", "VALIDATION_INVALID_JSON"),
            ([1, 2], "VALIDATION_INVALID_PARAMETER"),
            (7, "VALIDATION_INVALID_PARAMETER"),
        ],
    )
    async def test_double_wrapped_non_object_arguments_refused(
        self, transform: CategorizedSearchTransform, nested: Any, code: str
    ) -> None:
        """A scalar or list nested payload is refused with the same structured
        error the outer payload gets — never a bare ValueError out of the
        retired-name adapter's dict()."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")

        with pytest.raises(ToolError) as exc:
            await fn(
                "ha_call_read_tool",
                {"name": "ha_get_state", "arguments": nested},
                ctx,
            )

        payload = json.loads(str(exc.value))
        assert payload["error"]["code"] == code
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_double_wrapped_json_string_arguments_are_parsed(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The nested payload gets the same JSON-string tolerance as the outer
        one, so a model that serialized it twice still dispatches."""
        ctx = _make_ctx(call_tool_return={"state": "on"})
        fn = await proxy_function(transform, "read")

        result = await fn(
            "ha_call_read_tool",
            {"name": "ha_get_state", "arguments": '{"entity_id": "light.kitchen"}'},
            ctx,
        )

        assert result == {"state": "on"}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_get_state", {"entity_id": "light.kitchen"}
        )

    @pytest.mark.anyio
    async def test_double_wrapped_wrong_category_still_rejected(self, transform):
        """Double-wrapped write tool via read proxy is rejected after unwrapping."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")
        # LLM wraps write tool in read proxy
        with pytest.raises(ToolError) as exc_info:
            await fn(
                "ha_call_read_tool",
                {"name": "ha_config_set_automation", "arguments": {}},
                ctx,
            )
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "ha_call_write_tool" in error["error"]["message"]
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_no_unwrap_when_name_not_proxy(self, transform):
        """Non-proxy name with name/arguments keys is NOT unwrapped."""
        ctx = _make_ctx(call_tool_return="ok")
        fn = await proxy_function(transform, "read")
        # A real tool called with args that happen to contain "name" and "arguments"
        result = await fn(
            "ha_get_state",
            {"name": "some_value", "arguments": "other"},
            ctx,
        )
        # Should call ha_get_state directly (not unwrap)
        assert result == "ok"
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_get_state", {"name": "some_value", "arguments": "other"}
        )


# ---------------------------------------------------------------------------
# JSON-string arguments fallback
# ---------------------------------------------------------------------------


class TestArgumentsAsString:
    """Tolerate arguments passed as a JSON string instead of a dict.

    Small models sometimes serialize the nested `arguments` param to a JSON
    string before sending it, which FastMCP's schema validator rejects. The
    proxy accepts a string fallback, parses it, and forwards the resulting
    dict — same recovery spirit as the double-unwrap path.
    """

    @pytest.fixture
    def transform(self):
        t = CategorizedSearchTransform(max_results=5)
        set_catalog(
            t,
            [
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        return t

    @pytest.mark.anyio
    async def test_json_string_arguments_parsed_and_forwarded(self, transform):
        """A JSON-object string is parsed to a dict and forwarded."""
        ctx = _make_ctx(call_tool_return={"state": "on"})
        fn = await proxy_function(transform, "read")
        result = await fn("ha_get_state", '{"entity_id": "light.kitchen"}', ctx)
        assert result == {"state": "on"}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_get_state", {"entity_id": "light.kitchen"}
        )

    @pytest.mark.anyio
    async def test_invalid_json_string_rejected(self, transform):
        """Non-JSON string raises with INVALID_JSON and does not dispatch."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_get_state", "not valid json", ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_JSON"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_deeply_nested_json_string_rejected(self, transform):
        """A JSON string nested past the interpreter's recursion limit makes
        ``json.loads`` raise ``RecursionError``, not ``JSONDecodeError``; it
        must still come back as the structured INVALID_JSON refusal rather
        than escape as an internal error (CodeRabbit)."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_get_state", "[" * 20_000, ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_JSON"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_json_string_not_object_rejected(self, transform):
        """JSON that parses to a non-object (e.g. array) raises a clear error."""
        ctx = _make_ctx()
        fn = await proxy_function(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_get_state", "[1, 2, 3]", ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        ctx.fastmcp.call_tool.assert_not_called()


# ---------------------------------------------------------------------------
# _rebuild_category_cache
# ---------------------------------------------------------------------------


class TestRebuildCategoryCache:
    async def test_catalog_changes_update_dispatch_admission(self):
        transform = CategorizedSearchTransform()
        initial = _make_tool("ha_get_state", read_only=True)
        changed = _make_tool("ha_get_state", destructive=True)
        set_catalog(transform, [initial])
        reader = await proxy_function(transform, "read")
        writer = await proxy_function(transform, "write")
        ctx = _make_ctx()
        assert await reader("ha_get_state", {}, ctx) == "tool_result"
        with pytest.raises(ToolError, match="ha_call_read_tool"):
            await writer("ha_get_state", {}, ctx)
        set_catalog(transform, [changed])
        assert await writer("ha_get_state", {}, ctx) == "tool_result"
        with pytest.raises(ToolError, match="ha_call_write_tool"):
            await reader("ha_get_state", {}, ctx)
        # Repeated unchanged catalog requests must still dispatch successfully.
        assert await writer("ha_get_state", {}, ctx) == "tool_result"
