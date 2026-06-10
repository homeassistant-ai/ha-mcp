"""Test Read Only Mode enforcement (read_only.py) without a FastMCP boot.

Mirrors ``tests/src/unit/policy/test_middleware.py``: drive
``ReadOnlyMiddleware.on_call_tool`` directly with a fake call_next, and
``ReadOnlyToolsTransform.list_tools``/``get_tool`` with fake Tool objects.
The live-settings read is patched at the ``ha_mcp.read_only`` import site.
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.read_only import (
    _ADDON_CONFIG_WRITE_PARAMS,
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
            # dry_run=True previews validate/simulate without saving —
            # allowed so a read-only agent can still sanity-check a
            # proposed energy config.
            ({"mode": "set", "config": {}, "dry_run": True}, True),
            ({"mode": "add_device", "dry_run": True}, True),
            ({"mode": "remove_device", "dry_run": True}, True),
            ({"mode": "add_source", "dry_run": True}, True),
            # Strict ``is True``: the middleware sees raw pre-validation
            # arguments, so truthy non-bools (which schema coercion may
            # turn into False before the tool runs) must fail closed.
            ({"mode": "set", "config": {}, "dry_run": "true"}, False),
            ({"mode": "set", "config": {}, "dry_run": 1}, False),
            ({"mode": "set", "config": {}, "dry_run": False}, False),
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


# ---------------------------------------------------------------------------
# Schema-drift guard for the exempt tools' write predicates.
# ---------------------------------------------------------------------------

_SRC_TOOLS_DIR = Path(__file__).resolve().parents[3] / "src" / "ha_mcp" / "tools"

# Module that defines each exempt tool's ``@tool`` / ``@mcp.tool`` method.
_EXEMPT_TOOL_MODULES = {
    "ha_manage_backup": "backup.py",
    "ha_manage_addon": "tools_addons.py",
    "ha_manage_energy_prefs": "tools_energy.py",
    "ha_manage_pipeline": "tools_voice_assistant.py",
    "ha_manage_custom_tool": "tools_code.py",
}

# INDEPENDENT, hardcoded manifests of the argument names each exempt
# tool's write predicate inspects. These are duplicated here ON PURPOSE
# (not derived from the predicates): if a future edit drops a name from a
# predicate — silently reclassifying that write as a read — this manifest
# still lists it and the equality / signature assertions below fail until
# the change is consciously acknowledged here. A parameter rename in the
# tool likewise fails this test, telling the maintainer to re-review the
# read-only predicate.
_EXEMPT_INSPECTED_ARGS = {
    "ha_manage_backup": {"scope", "action"},
    "ha_manage_addon": {
        "action",
        "options",
        "network",
        "boot",
        "auto_update",
        "watchdog",
        "array_patch",
        "websocket",
        "method",
    },
    "ha_manage_energy_prefs": {"mode", "dry_run"},
    "ha_manage_pipeline": {"action"},
    "ha_manage_custom_tool": {"list_saved", "code", "run_saved"},
}

# The subset of the addon manifest that ``_addon_write`` iterates as
# "config-change" parameters. Pinned independently so dropping one from
# the production constant fails here rather than silently passing.
_ADDON_CONFIG_WRITE_PARAMS_MANIFEST = (
    "options",
    "network",
    "boot",
    "auto_update",
    "watchdog",
)


def _decorated_tool_param_names(module_path: Path, tool_name: str) -> set[str]:
    """Return the parameter names of the ``@tool``-decorated function that
    backs ``tool_name`` (both the ``@tool(name=...)`` class-method pattern
    and the ``@mcp.tool(...)`` closure pattern, where the function name IS
    the tool name)."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    def _is_tool_decorated(node: ast.AST) -> bool:
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                return True
            if isinstance(target, ast.Name) and target.id == "tool":
                return True
        return False

    def _names_match(node: ast.AST) -> bool:
        # @tool(name="ha_...") explicit name kwarg.
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if (
                        kw.arg == "name"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value == tool_name
                    ):
                        return True
        # @mcp.tool(...) closure: function name is the tool name.
        return node.name == tool_name and _is_tool_decorated(node)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and _names_match(
            node
        ):
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            return {a.arg for a in args if a.arg not in ("self", "ctx")}
    raise AssertionError(
        f"could not locate the @tool-decorated function for {tool_name} "
        f"in {module_path.name}"
    )


class TestExemptPredicateSchemaDrift:
    """Each exempt tool's write predicate inspects argument names that must
    exist on the real tool. A parameter rename (or a silent predicate edit)
    fails here so the maintainer re-reviews the read-only classification."""

    @pytest.mark.parametrize("tool_name", sorted(_EXEMPT_INSPECTED_ARGS))
    def test_inspected_args_exist_in_tool_signature(self, tool_name):
        module_path = _SRC_TOOLS_DIR / _EXEMPT_TOOL_MODULES[tool_name]
        real_params = _decorated_tool_param_names(module_path, tool_name)
        for arg in sorted(_EXEMPT_INSPECTED_ARGS[tool_name]):
            assert arg in real_params, (
                f"read-only predicate for {tool_name} inspects {arg!r}, but "
                f"that parameter no longer exists on the tool "
                f"({module_path.name}). Re-review the read-only write "
                f"predicate in read_only.py — a rename may have reclassified "
                f"a write as a read."
            )

    def test_addon_config_write_params_manifest_matches_constant(self):
        """The production ``_ADDON_CONFIG_WRITE_PARAMS`` must equal the
        independent manifest above. Deleting a param from the constant
        (which would let that add-on config write slip through as a read)
        fails this test until the manifest is updated to match."""
        assert (
            tuple(_ADDON_CONFIG_WRITE_PARAMS) == _ADDON_CONFIG_WRITE_PARAMS_MANIFEST
        ), (
            "_ADDON_CONFIG_WRITE_PARAMS drifted from the independent manifest "
            "in this test. If you intentionally changed which add-on params "
            "count as a config write, update the manifest AND re-confirm the "
            "read-only classification is still correct."
        )

    def test_addon_config_write_params_are_real_tool_args(self):
        module_path = _SRC_TOOLS_DIR / _EXEMPT_TOOL_MODULES["ha_manage_addon"]
        real_params = _decorated_tool_param_names(module_path, "ha_manage_addon")
        for arg in _ADDON_CONFIG_WRITE_PARAMS:
            assert arg in real_params, (
                f"_ADDON_CONFIG_WRITE_PARAMS lists {arg!r}, absent from "
                f"ha_manage_addon's signature ({module_path.name})"
            )


@pytest.mark.anyio
class TestLiveFlip:
    """One middleware + one transform, flipping the live flag through a
    mutable holder — exercises the no-restart standalone-mode path."""

    async def test_write_blocked_after_flag_flips_on_and_passes_again_off(
        self, monkeypatch
    ):
        holder = SimpleNamespace(read_only_mode=False)
        monkeypatch.setattr("ha_mcp.read_only.get_global_settings", lambda: holder)
        mw = make_middleware()
        transform = ReadOnlyToolsTransform()

        def write_ctx():
            return make_context("ha_config_set_automation", {"alias": "x"})

        # Flag OFF: write passes, catalog unfiltered.
        call_next = AsyncMock(return_value="ok")
        assert await mw.on_call_tool(write_ctx(), call_next) == "ok"
        assert {t.name for t in await transform.list_tools(CATALOG)} == {
            t.name for t in CATALOG
        }

        # Flip ON: write blocked, catalog filtered.
        holder.read_only_mode = True
        call_next = AsyncMock()
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(write_ctx(), call_next)
        expect_read_only_error(excinfo)
        call_next.assert_not_called()
        filtered = {t.name for t in await transform.list_tools(CATALOG)}
        assert "ha_config_set_automation" not in filtered
        assert "ha_get_state" in filtered

        # Flip OFF again: write passes once more.
        holder.read_only_mode = False
        call_next = AsyncMock(return_value="ok-again")
        assert await mw.on_call_tool(write_ctx(), call_next) == "ok-again"


@pytest.mark.anyio
class TestRaisingCatalog:
    async def test_catalog_lookup_raise_blocks_call_fail_closed(self, read_only_on):
        """If the unfiltered-catalog lookup raises, the middleware must
        block with READ_ONLY_MODE rather than let the exception propagate
        opaquely or fail open."""
        mw = ReadOnlyMiddleware(list_tools=AsyncMock(side_effect=RuntimeError("boom")))
        call_next = AsyncMock()
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(make_context("ha_anything"), call_next)
        body = expect_read_only_error(excinfo)
        assert body["tool_name"] == "ha_anything"
        call_next.assert_not_called()


@pytest.mark.anyio
class TestStringEnvelopeProxy:
    """The proxy tolerates ``arguments`` as a JSON string and parses it
    after this middleware runs, so the middleware must coerce it too."""

    async def test_string_envelope_write_blocked_with_inner_name(self, read_only_on):
        mw = make_middleware()
        call_next = AsyncMock()
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(
                make_context(
                    "ha_call_write_tool",
                    {
                        "name": "ha_manage_addon",
                        "arguments": '{"slug": "x", "action": "install"}',
                    },
                ),
                call_next,
            )
        body = expect_read_only_error(excinfo)
        assert body["tool_name"] == "ha_manage_addon"
        call_next.assert_not_called()

    async def test_string_envelope_exempt_read_passes(self, read_only_on):
        mw = make_middleware()
        call_next = AsyncMock(return_value="energy-config")
        result = await mw.on_call_tool(
            make_context(
                "ha_call_read_tool",
                {"name": "ha_manage_energy_prefs", "arguments": '{"mode": "get"}'},
            ),
            call_next,
        )
        assert result == "energy-config"

    async def test_unparseable_string_envelope_passes_through(self, read_only_on):
        """``arguments='not json'`` is a malformed envelope — pass through
        so the proxy raises its own VALIDATION error (nothing dispatched,
        nothing written)."""
        mw = make_middleware()
        call_next = AsyncMock(return_value="proxy-validation-error")
        result = await mw.on_call_tool(
            make_context(
                "ha_call_write_tool",
                {"name": "ha_manage_addon", "arguments": "not json"},
            ),
            call_next,
        )
        assert result == "proxy-validation-error"

    async def test_non_object_json_string_envelope_passes_through(self, read_only_on):
        """A JSON string that parses to a non-object (e.g. a list) is also
        malformed — pass through to the proxy's own validation."""
        mw = make_middleware()
        call_next = AsyncMock(return_value="proxy-validation-error")
        result = await mw.on_call_tool(
            make_context(
                "ha_call_write_tool",
                {"name": "ha_manage_addon", "arguments": "[1, 2]"},
            ),
            call_next,
        )
        assert result == "proxy-validation-error"


@pytest.mark.anyio
class TestLateRegistration:
    async def test_rebuild_on_miss_blocks_late_registered_write_tool(
        self, read_only_on
    ):
        """A write tool registered after the first classification must be
        caught on the cache-miss rebuild (still fail-closed)."""
        new_write = make_tool("ha_late_write", False)
        list_tools = AsyncMock(side_effect=[CATALOG, [*CATALOG, new_write]])
        mw = ReadOnlyMiddleware(list_tools=list_tools)

        # Prime the cache with the original catalog (a known read tool).
        call_next = AsyncMock(return_value="state")
        assert (
            await mw.on_call_tool(
                make_context("ha_get_state", {"entity_id": "light.x"}), call_next
            )
            == "state"
        )

        # The late tool is a cache miss → rebuild picks up the second
        # catalog → classified write → blocked.
        call_next = AsyncMock()
        with pytest.raises(ToolError) as excinfo:
            await mw.on_call_tool(make_context("ha_late_write"), call_next)
        body = expect_read_only_error(excinfo)
        assert body["tool_name"] == "ha_late_write"
        call_next.assert_not_called()
