"""Test Read Only Mode enforcement (read_only.py) without a FastMCP boot.

Mirrors ``tests/src/unit/policy/test_middleware.py``: drive
``ReadOnlyMiddleware.on_call_tool`` directly with a fake call_next, and
``ReadOnlyToolsTransform.list_tools``/``get_tool`` with fake Tool objects.
The live-settings read is patched at the ``ha_mcp.read_only`` import site.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.read_only import (
    READ_ONLY_EXEMPT_TOOLS,
    ReadOnlyMiddleware,
    ReadOnlyToolsTransform,
    is_read_safe,
    read_only_visible,
)


def make_tool(name: str, read_only: bool | None):
    annotations = None if read_only is None else SimpleNamespace(readOnlyHint=read_only)
    return SimpleNamespace(name=name, annotations=annotations)


def make_context(name: str, arguments: dict | None = None):
    msg = MagicMock()
    msg.name = name
    msg.arguments = arguments or {}
    ctx = MagicMock()
    ctx.message = msg
    return ctx


CATALOG = [
    make_tool("ha_get_state", True),
    make_tool("ha_search", True),
    make_tool("ha_config_set_automation", False),
    make_tool("ha_restart", False),
    make_tool("ha_unannotated", None),
    make_tool("ha_manage_backup", False),
    make_tool("ha_manage_addon", False),
]


@pytest.fixture
def read_only_on(monkeypatch):
    monkeypatch.setattr(
        "ha_mcp.read_only.get_global_settings",
        lambda: SimpleNamespace(read_only_mode=True),
    )


@pytest.fixture
def read_only_off(monkeypatch):
    monkeypatch.setattr(
        "ha_mcp.read_only.get_global_settings",
        lambda: SimpleNamespace(read_only_mode=False),
    )


def make_middleware():
    return ReadOnlyMiddleware(list_tools=AsyncMock(return_value=CATALOG))


def expect_read_only_error(excinfo) -> dict:
    body = json.loads(excinfo.value.args[0])
    assert body["error"]["code"] == "READ_ONLY_MODE"
    assert body["read_only_mode"] is True
    return body


class TestHelpers:
    def test_read_only_annotated_tool_is_read_safe(self):
        assert is_read_safe(make_tool("ha_get_state", True))

    def test_write_annotated_tool_is_not_read_safe(self):
        assert not is_read_safe(make_tool("ha_restart", False))

    def test_missing_annotations_fail_closed(self):
        assert not is_read_safe(make_tool("ha_unannotated", None))

    def test_exempt_tool_stays_visible(self):
        assert read_only_visible(make_tool("ha_manage_backup", False))

    def test_write_tool_not_visible(self):
        assert not read_only_visible(make_tool("ha_restart", False))


class TestExemptionRules:
    """Argument-level write detection for the exempt mixed tools."""

    @pytest.mark.parametrize(
        ("args", "allowed"),
        [
            ({"scope": "edits", "action": "list"}, True),
            ({"scope": "edits", "action": "view", "backup_name": "x"}, True),
            ({"scope": "edits", "action": "create"}, False),
            ({"scope": "edits", "action": "restore"}, False),
            ({"scope": "edits", "action": "delete"}, False),
            ({"scope": "snapshot", "action": "create"}, False),
            ({"scope": "snapshot", "action": "restore"}, False),
            ({}, False),
        ],
    )
    def test_manage_backup(self, args, allowed):
        rule = READ_ONLY_EXEMPT_TOOLS["ha_manage_backup"].blocked_write
        assert (rule(args) is None) is allowed

    @pytest.mark.parametrize(
        ("args", "allowed"),
        [
            ({"slug": "a0d7b954_nodered", "path": "/flows"}, True),
            ({"slug": "x", "path": "/api", "method": "GET"}, True),
            ({"slug": "x", "path": "/api", "method": "get"}, True),
            ({"slug": "x", "path": "/api", "method": "POST"}, False),
            ({"slug": "x", "path": "/api", "method": "DELETE"}, False),
            ({"slug": "x", "action": "install"}, False),
            ({"slug": "x", "action": "stop"}, False),
            ({"action": "add_repository", "repository": "url"}, False),
            ({"slug": "x", "options": {"a": 1}}, False),
            ({"slug": "x", "boot": "auto"}, False),
            ({"slug": "x", "auto_update": True}, False),
            # auto_update=False is still a config write, not an absence.
            ({"slug": "x", "auto_update": False}, False),
            ({"slug": "x", "array_patch": {"path": "/flows"}}, False),
            ({"slug": "x", "path": "/ws", "websocket": True}, False),
        ],
    )
    def test_manage_addon(self, args, allowed):
        rule = READ_ONLY_EXEMPT_TOOLS["ha_manage_addon"].blocked_write
        assert (rule(args) is None) is allowed

    @pytest.mark.parametrize(
        ("args", "allowed"),
        [
            ({"mode": "get"}, True),
            ({"mode": "set", "config": {}}, False),
            # dry_run previews mutate nothing but are conservatively
            # blocked — the get-only rule is the documented contract.
            ({"mode": "set", "config": {}, "dry_run": True}, False),
            ({"mode": "add_device", "dry_run": True}, False),
            ({"mode": "remove_device"}, False),
            ({"mode": "add_source"}, False),
            ({}, False),
        ],
    )
    def test_manage_energy_prefs(self, args, allowed):
        rule = READ_ONLY_EXEMPT_TOOLS["ha_manage_energy_prefs"].blocked_write
        assert (rule(args) is None) is allowed

    @pytest.mark.parametrize(
        ("args", "allowed"),
        [
            ({"action": "list"}, True),
            ({"action": "get", "pipeline_id": "p1"}, True),
            ({"action": "create", "name": "x"}, False),
            ({"action": "update", "pipeline_id": "p1"}, False),
            ({"action": "set_preferred", "pipeline_id": "p1"}, False),
            ({}, False),
        ],
    )
    def test_manage_pipeline(self, args, allowed):
        rule = READ_ONLY_EXEMPT_TOOLS["ha_manage_pipeline"].blocked_write
        assert (rule(args) is None) is allowed

    @pytest.mark.parametrize(
        ("args", "allowed"),
        [
            ({"list_saved": True}, True),
            ({"code": "print(1)", "justification": "x"}, False),
            ({"run_saved": "my_tool"}, False),
            ({"code": "print(1)", "save_as": "t", "justification": "x"}, False),
            # Defensive: the tool rejects this combination anyway, but
            # the read-only rule must not treat it as a pure read.
            ({"list_saved": True, "code": "print(1)"}, False),
            ({}, False),
        ],
    )
    def test_manage_custom_tool(self, args, allowed):
        rule = READ_ONLY_EXEMPT_TOOLS["ha_manage_custom_tool"].blocked_write
        assert (rule(args) is None) is allowed


@pytest.mark.anyio
class TestMiddleware:
    async def test_flag_off_passes_everything(self, read_only_off):
        mw = make_middleware()
        call_next = AsyncMock(return_value="ok")
        result = await mw.on_call_tool(
            make_context("ha_config_set_automation", {"alias": "x"}), call_next
        )
        assert result == "ok"

    async def test_read_tool_passes(self, read_only_on):
        mw = make_middleware()
        call_next = AsyncMock(return_value="state")
        result = await mw.on_call_tool(
            make_context("ha_get_state", {"entity_id": "light.x"}), call_next
        )
        assert result == "state"

    async def test_write_tool_blocked(self, read_only_on):
        mw = make_middleware()
        call_next = AsyncMock()
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(
                make_context("ha_config_set_automation", {"alias": "x"}), call_next
            )
        body = expect_read_only_error(excinfo)
        assert body["tool_name"] == "ha_config_set_automation"
        call_next.assert_not_called()

    async def test_unannotated_tool_blocked_fail_closed(self, read_only_on):
        mw = make_middleware()
        call_next = AsyncMock()
        with pytest.raises(ToolError):
            await mw.on_call_tool(make_context("ha_unannotated"), call_next)
        call_next.assert_not_called()

    async def test_unregistered_tool_passes_to_normal_error(self, read_only_on):
        """A name absent from the unfiltered catalog cannot execute
        anything — pass through so FastMCP raises its normal
        unknown-tool error instead of a misleading READ_ONLY_MODE one."""
        mw = make_middleware()
        call_next = AsyncMock(return_value="unknown-tool-error")
        result = await mw.on_call_tool(make_context("ha_not_in_catalog"), call_next)
        assert result == "unknown-tool-error"

    async def test_empty_catalog_fails_closed(self, read_only_on):
        """A broken catalog lookup (empty list) must not fail open."""
        mw = ReadOnlyMiddleware(list_tools=AsyncMock(return_value=[]))
        call_next = AsyncMock()
        with pytest.raises(ToolError):
            await mw.on_call_tool(make_context("ha_anything"), call_next)
        call_next.assert_not_called()

    async def test_proxy_meta_tools_pass_through_without_envelope(self, read_only_on):
        from ha_mcp.policy.middleware import PROXY_META_TOOLS

        mw = make_middleware()
        call_next = AsyncMock(return_value="proxied")
        for name in PROXY_META_TOOLS:
            result = await mw.on_call_tool(make_context(name, {}), call_next)
            assert result == "proxied"

    async def test_proxied_write_tool_blocked_with_inner_name(self, read_only_on):
        """ha_call_write_tool(name=<hidden write tool>) must produce the
        explanatory READ_ONLY_MODE error naming the INNER tool — the
        proxy's category cache no longer contains hidden tools, so
        without unwrapping the user would get a generic not-found."""
        mw = make_middleware()
        call_next = AsyncMock()
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(
                make_context(
                    "ha_call_write_tool",
                    {"name": "ha_config_set_automation", "arguments": {"alias": "x"}},
                ),
                call_next,
            )
        body = expect_read_only_error(excinfo)
        assert body["tool_name"] == "ha_config_set_automation"
        call_next.assert_not_called()

    async def test_proxied_read_tool_passes(self, read_only_on):
        mw = make_middleware()
        call_next = AsyncMock(return_value="proxied-read")
        result = await mw.on_call_tool(
            make_context(
                "ha_call_read_tool",
                {"name": "ha_get_state", "arguments": {"entity_id": "light.x"}},
            ),
            call_next,
        )
        assert result == "proxied-read"

    async def test_proxied_exempt_write_action_blocked(self, read_only_on):
        mw = make_middleware()
        call_next = AsyncMock()
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(
                make_context(
                    "ha_call_write_tool",
                    {
                        "name": "ha_manage_backup",
                        "arguments": {"scope": "snapshot", "action": "create"},
                    },
                ),
                call_next,
            )
        body = expect_read_only_error(excinfo)
        assert body["tool_name"] == "ha_manage_backup"
        call_next.assert_not_called()

    async def test_proxied_exempt_read_action_passes(self, read_only_on):
        mw = make_middleware()
        call_next = AsyncMock(return_value="proxied-backups")
        result = await mw.on_call_tool(
            make_context(
                "ha_call_write_tool",
                {
                    "name": "ha_manage_backup",
                    "arguments": {"scope": "edits", "action": "list"},
                },
            ),
            call_next,
        )
        assert result == "proxied-backups"

    async def test_double_wrapped_proxy_envelope_unwrapped(self, read_only_on):
        """Mirror the proxy's own double-wrap recovery: a proxy call
        whose inner name is another proxy gets unwrapped to the real
        tool before the read-only decision."""
        mw = make_middleware()
        call_next = AsyncMock()
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(
                make_context(
                    "ha_call_write_tool",
                    {
                        "name": "ha_call_write_tool",
                        "arguments": {
                            "name": "ha_config_set_automation",
                            "arguments": {"alias": "x"},
                        },
                    },
                ),
                call_next,
            )
        body = expect_read_only_error(excinfo)
        assert body["tool_name"] == "ha_config_set_automation"
        call_next.assert_not_called()

    async def test_exempt_tool_read_action_passes(self, read_only_on):
        mw = make_middleware()
        call_next = AsyncMock(return_value="backups")
        result = await mw.on_call_tool(
            make_context("ha_manage_backup", {"scope": "edits", "action": "list"}),
            call_next,
        )
        assert result == "backups"

    async def test_exempt_tool_write_action_blocked(self, read_only_on):
        mw = make_middleware()
        call_next = AsyncMock()
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(
                make_context(
                    "ha_manage_backup", {"scope": "snapshot", "action": "restore"}
                ),
                call_next,
            )
        body = expect_read_only_error(excinfo)
        assert body["blocked_operation"]
        # The error must teach the LLM what remains available.
        assert "list" in body["error"]["message"]
        call_next.assert_not_called()

    async def test_exempt_rule_consulted_before_annotations(self, read_only_on):
        """Exempt tools never hit the annotation cache — their (write-
        annotated) catalog entry must not override the per-call rule."""
        list_tools = AsyncMock(return_value=CATALOG)
        mw = ReadOnlyMiddleware(list_tools=list_tools)
        call_next = AsyncMock(return_value="flows")
        result = await mw.on_call_tool(
            make_context("ha_manage_addon", {"slug": "x", "path": "/flows"}),
            call_next,
        )
        assert result == "flows"
        list_tools.assert_not_called()


@pytest.mark.anyio
class TestTransform:
    async def test_flag_off_returns_catalog_unchanged(self, read_only_off):
        transform = ReadOnlyToolsTransform()
        result = await transform.list_tools(CATALOG)
        assert list(result) == CATALOG

    async def test_flag_on_hides_write_tools(self, read_only_on):
        transform = ReadOnlyToolsTransform()
        names = {t.name for t in await transform.list_tools(CATALOG)}
        assert names == {
            "ha_get_state",
            "ha_search",
            "ha_manage_backup",
            "ha_manage_addon",
        }

    async def test_get_tool_hides_write_tool(self, read_only_on):
        transform = ReadOnlyToolsTransform()
        write_tool = make_tool("ha_restart", False)
        call_next = AsyncMock(return_value=write_tool)
        assert await transform.get_tool("ha_restart", call_next) is None

    async def test_get_tool_passes_read_and_exempt(self, read_only_on):
        transform = ReadOnlyToolsTransform()
        for tool in (
            make_tool("ha_get_state", True),
            make_tool("ha_manage_backup", False),
        ):
            call_next = AsyncMock(return_value=tool)
            assert await transform.get_tool(tool.name, call_next) is tool

    async def test_get_tool_passthrough_when_off(self, read_only_off):
        transform = ReadOnlyToolsTransform()
        write_tool = make_tool("ha_restart", False)
        call_next = AsyncMock(return_value=write_tool)
        assert await transform.get_tool("ha_restart", call_next) is write_tool


class TestExemptTableContract:
    def test_exempt_table_pins_expected_tools(self):
        """The exempt set is a reviewed contract — additions/removals
        must be deliberate (each entry means 'this tool stays callable
        in read-only mode')."""
        assert set(READ_ONLY_EXEMPT_TOOLS) == {
            "ha_manage_backup",
            "ha_manage_addon",
            "ha_manage_energy_prefs",
            "ha_manage_pipeline",
            "ha_manage_custom_tool",
        }

    def test_every_exemption_describes_whats_allowed(self):
        for name, exemption in READ_ONLY_EXEMPT_TOOLS.items():
            assert exemption.allowed, name
