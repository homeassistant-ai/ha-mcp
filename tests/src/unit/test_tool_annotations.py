"""Tests for tool annotations compliance with MCP Directory Policy.

Every tool MUST declare its safety behavior with one of:
- readOnlyHint: true - For tools that only read data
- destructiveHint: true - For tools that modify data or have side effects
- destructiveHint: false - For non-read-only tools whose side effects are
  additive only (for example, creating a record without changing existing data)

Additionally, every tool SHOULD have a title for UI display.
"""

import ast
import re
import textwrap
from pathlib import Path


def get_tools_dir() -> Path:
    """Get the path to the tools directory."""
    return Path(__file__).parent.parent.parent.parent / "src" / "ha_mcp" / "tools"



def get_styleguide_path() -> Path:
    """Get the path to the canonical code-review style guide."""
    return Path(__file__).parent.parent.parent.parent / ".gemini" / "styleguide.md"

def _is_mcp_tool(func: ast.expr) -> bool:
    """``mcp.tool`` as written on a decorator, called or bare."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "tool"
        and isinstance(func.value, ast.Name)
        and func.value.id == "mcp"
    )


def _annotations_dict(decorator: ast.Call) -> dict[str, ast.expr]:
    """The decorator's ``annotations={...}`` mapping, keyed by literal name."""
    for keyword in decorator.keywords:
        if keyword.arg == "annotations" and isinstance(keyword.value, ast.Dict):
            return {
                key.value: value
                for key, value in zip(
                    keyword.value.keys, keyword.value.values, strict=True
                )
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    return {}


def _tool_info(decorator: ast.expr, func_name: str, file_name: str) -> dict:
    """Tool info for one tool decorator, read off the parsed nodes.

    Every flag comes from the ``annotations`` value itself. The helper this
    replaced classified text instead, looking for ``True`` in the 20
    characters after the key — a window a neighbouring value reaches into:
    ``'destructiveHint': False, 'i': 'True'`` puts one four characters inside
    it and marks a tool destructive and explicitly non-destructive at once
    (measured, and pinned by ``test_scan_reads_flags_off_the_annotations_dict``
    for both registration forms).
    """
    keywords = decorator.keywords if isinstance(decorator, ast.Call) else []
    annotations = (
        _annotations_dict(decorator) if isinstance(decorator, ast.Call) else {}
    )

    def literal(key: str) -> object:
        node = annotations.get(key)
        return node.value if isinstance(node, ast.Constant) else None

    return {
        "file": file_name,
        "function": func_name,
        "has_read_only_hint": literal("readOnlyHint") is True,
        "has_destructive_hint": literal("destructiveHint") is True,
        "has_explicit_non_destructive_hint": literal("destructiveHint") is False,
        "has_title": "title" in annotations,
        "has_tags": any(keyword.arg == "tags" for keyword in keywords),
        "has_open_world_hint": isinstance(literal("openWorldHint"), bool),
        "decorator_args": ", ".join(ast.unparse(keyword) for keyword in keywords),
    }


def _is_class_tool(func: ast.expr) -> bool:
    """``@tool(name="ha_...")`` — the class-method registration form."""
    return isinstance(func, ast.Name) and func.id == "tool"


def _tools_in_source(content: str, file_name: str) -> list[dict]:
    """Every tool in a module, both registration forms, read from the AST.

    ``@mcp.tool`` closures and ``@tool(name="ha_...")`` class methods are the
    same shape to the parser — a decorator on an async function — so one walk
    serves both, and no arm of this scan classifies text any more.

    Read structurally rather than by regex because the decorator arguments are
    prose: a ``)`` inside an annotation string — ``"title": "Get Apps
    (add-ons)"``, ``tags={"Apps (add-ons)"}`` — closes a ``[^)]*`` arm early
    and drops the tool from the scan, and from every annotation check with it,
    while a non-greedy ``(.*?)`` arm over-matches into the next tool instead.

    A ``@mcp.tool(...)`` written inside a docstring is not a tool here because
    a string constant never reaches a ``decorator_list`` — that immunity is
    the parser's, not the ``async def`` filter's. The filter carries a rule of
    its own: a ``@mcp.tool`` on a sync ``def`` is dropped without a word, and
    ``test_tool_scan_finds_every_annotated_tool`` is what turns that into a
    failure. The bare (uncalled) form reaches the same builder as the called
    one, so it arrives with every key the assertions read rather than a dict
    missing ``has_tags``; no tool is written that way today.
    """
    tools = []
    for node in ast.walk(ast.parse(content)):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            func = decorator.func if isinstance(decorator, ast.Call) else decorator
            if _is_mcp_tool(func) or _is_class_tool(func):
                tools.append(_tool_info(decorator, node.name, file_name))
    return tools


def extract_tool_decorators(file_path: Path) -> list[dict]:
    """Extract @mcp.tool and @tool decorator information from a Python file."""
    return _tools_in_source(file_path.read_text(encoding="utf-8"), file_path.name)


def get_all_tools() -> list[dict]:
    """Get all tools from all tool files."""
    tools_dir = get_tools_dir()
    all_tools = []

    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        tools = extract_tool_decorators(py_file)
        all_tools.extend(tools)

    return all_tools


class TestToolAnnotations:
    """Test suite for MCP tool annotation compliance."""


    def test_styleguide_natural_name_exceptions_match_registered_tools(self):
        """The naming exceptions must describe every unconventional tool name."""
        styleguide = get_styleguide_path().read_text(encoding="utf-8")
        section = re.search(
            r"Accepted natural-name exceptions are:\n\n"
            r"(?P<items>(?:- .*\n)+)",
            styleguide,
        )
        assert section, "style guide is missing its natural-name exception list"
        documented = set(re.findall(r"`(ha_[a-z0-9_]+)`", section["items"]))

        conventional = re.compile(
            r"^ha_(?:(?:config|dev)_)?"
            r"(?:get|list|search|set|delete|remove|call|manage)_"
        )
        registered = {tool["function"] for tool in get_all_tools()}
        actual = {name for name in registered if not conventional.match(name)}

        assert documented == actual, (
            "style-guide naming exceptions differ from registered "
            f"unconventional tool names. Missing: {sorted(actual - documented)}; "
            f"stale: {sorted(documented - actual)}"
        )
    def test_all_tools_have_required_hint(self):
        """Every tool must explicitly describe read-only/destructive behavior."""
        tools = get_all_tools()

        missing_hints = []
        both_hints = []

        for tool in tools:
            has_read = tool["has_read_only_hint"]
            has_destructive = tool["has_destructive_hint"]
            has_non_destructive = tool["has_explicit_non_destructive_hint"]

            if not has_read and not has_destructive and not has_non_destructive:
                missing_hints.append(f"{tool['file']}:{tool['function']}")
            elif has_read and has_destructive:
                both_hints.append(f"{tool['file']}:{tool['function']}")

        error_msg = []
        if missing_hints:
            error_msg.append(
                f"Tools missing readOnlyHint or destructiveHint ({len(missing_hints)}):\n  "
                + "\n  ".join(missing_hints)
            )
        if both_hints:
            error_msg.append(
                f"Tools with BOTH hints (should have exactly one) ({len(both_hints)}):\n  "
                + "\n  ".join(both_hints)
            )

        assert not error_msg, "\n\n".join(error_msg)

    def test_all_tools_have_title(self):
        """Every tool should have a title for UI display."""
        tools = get_all_tools()

        missing_titles = [
            f"{tool['file']}:{tool['function']}"
            for tool in tools
            if not tool["has_title"]
        ]

        assert not missing_titles, (
            f"Tools missing title annotation ({len(missing_titles)}):\n  "
            + "\n  ".join(missing_titles)
        )

    def test_all_tools_have_open_world_hint(self):
        """Every tool must set openWorldHint explicitly (true or false).

        The MCP default is true, so an omitted value silently marks a local
        tool as open-world. This guards the explicit value; the true/false
        choice itself is a behaviour judgement left to review.
        """
        tools = get_all_tools()

        missing_open_world = [
            f"{tool['file']}:{tool['function']}"
            for tool in tools
            if not tool["has_open_world_hint"]
        ]

        assert not missing_open_world, (
            f"Tools missing openWorldHint annotation ({len(missing_open_world)}):\n  "
            + "\n  ".join(missing_open_world)
            + "\n\nAdd openWorldHint (true if the tool reaches external systems, "
            "false if it only touches local Home Assistant state) to each "
            "@tool() decorator; the MCP default is true."
        )

    def test_tool_scan_finds_every_annotated_tool(self):
        """The scan must not silently drop a tool from the checks above.

        A tool that get_all_tools() does not return escapes every annotation
        assertion above and inherits the MCP default openWorldHint=true
        unnoticed. That happened while the scan matched decorator arguments
        with ``[^)]*``: a ``)`` inside an annotation string (a title like "Get
        Logs (verbose)") closed the match early. Both registration forms are
        read off the AST now, so no decorator text is matched at all.

        The expectation is the set of names every tool declares as
        ``async def ha_*``, read with no decorator parsing at all. A count over
        an annotation key cannot carry this: one dropped tool and one prose
        mention of the counted key cancel out, and a count names no tool when
        it does fail.
        """
        scanned = {tool["function"] for tool in get_all_tools()}
        declared = {
            name
            for py_file in sorted(get_tools_dir().glob("*.py"))
            if not py_file.name.startswith("_")
            for name in re.findall(
                r"^\s*async def (ha_\w+)\(",
                py_file.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        }

        assert scanned == declared, (
            f"Tool scan and tool sources disagree. Declared but not scanned: "
            f"{sorted(declared - scanned)}. Scanned but not declared: "
            f"{sorted(scanned - declared)}. A tool extract_tool_decorators() "
            f"cannot see is dropped from get_all_tools() and skips every "
            f"annotation check above. Fix the extraction rather than this "
            f"expectation: the scan reads decorators on async functions, so a "
            f"tool on a sync def, or one registered through a decorator this "
            f"scan does not recognise, are the candidates."
        )

    def test_closure_scan_reads_called_and_bare_decorators(self):
        """Both ``@mcp.tool`` forms parse, and prose never becomes a tool.

        The bare form registers no tool in src/ha_mcp/tools today, so nothing
        else exercises that branch — and the branch it replaced built its dict
        by hand without ``has_tags``, which test_all_tools_have_tags reads, so
        the first bare decorator to land would have raised KeyError instead of
        asserting. This pins the called form, the bare form, and the immunity
        the scan claims for decorators mentioned in a docstring.
        """
        source = textwrap.dedent(
            '''
            """Module docstring: @mcp.tool(annotations={"title": "Nope"}) here.

            async def ha_docstring_tool(): ...
            """


            @mcp.tool(
                tags={"Apps (add-ons)"},
                annotations={
                    "title": "Get Apps (add-ons)",
                    "readOnlyHint": True,
                    "openWorldHint": False,
                },
            )
            async def ha_called_tool() -> None:
                """A ``)`` in the title above must not end the decorator."""


            @mcp.tool
            async def ha_bare_tool() -> None:
                """No annotations at all, but the same keys."""
            '''
        )

        tools = {tool["function"]: tool for tool in _tools_in_source(source, "x.py")}

        assert set(tools) == {"ha_called_tool", "ha_bare_tool"}, tools.keys()
        called = tools["ha_called_tool"]
        assert called["has_title"] and called["has_tags"]
        assert called["has_read_only_hint"] and called["has_open_world_hint"]
        assert not called["has_destructive_hint"]
        bare = tools["ha_bare_tool"]
        assert bare["has_tags"] is False and bare["has_title"] is False
        assert bare["has_open_world_hint"] is False

    def test_scan_reads_flags_off_the_annotations_dict(self):
        """A neighbouring value must not decide another key's flag.

        The retired helper classified by looking for ``True`` in the 20
        characters after the key, so a short neighbouring key whose value
        carries the word reached into the window and marked a tool destructive
        and explicitly non-destructive at once — with real annotation keys
        clearing that window by a character or two, which was the whole of the
        margin. Both forms read the ``annotations`` dict now, so the distance
        stops mattering; the class form is asserted here because it is the one
        that used to go through the window, and it carries roughly 80 of the
        tools.
        """
        source = textwrap.dedent(
            '''
            @mcp.tool(annotations={"destructiveHint": False, "i": "True"})
            async def ha_closure_window_tool() -> None:
                """Nothing here is destructive."""


            class Tools:
                @tool(
                    name="ha_class_window_tool",
                    annotations={"destructiveHint": False, "i": "True"},
                )
                @log_tool_usage
                async def ha_class_window_tool(self) -> None:
                    """Nothing here is destructive either."""
            '''
        )

        tools = {t["function"]: t for t in _tools_in_source(source, "x.py")}

        assert set(tools) == {"ha_closure_window_tool", "ha_class_window_tool"}
        for tool in tools.values():
            assert tool["has_explicit_non_destructive_hint"] is True
            assert tool["has_destructive_hint"] is False

    def test_server_registered_tools_have_open_world_hint(self):
        """Tools registered directly on the server must also set openWorldHint.

        get_all_tools() only scans src/ha_mcp/tools, so ha_get_skill_guide --
        registered in server.py via self.mcp.tool(...) -- would otherwise keep
        the implicit open-world default. Parse server.py's AST and assert every
        self.mcp.tool(...) registration carries openWorldHint.
        """
        server_py = get_tools_dir().parent / "server.py"
        tree = ast.parse(server_py.read_text(encoding="utf-8"), filename=str(server_py))

        registrations = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "tool"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "mcp"
        ]
        assert registrations, (
            "no self.mcp.tool(...) registrations found in server.py -- "
            "the scanner's shape assumption drifted"
        )

        # ha_get_skill_guide registers as name=SKILL_TOOL_NAME, not a literal, so
        # resolve module-level string constants to keep failures naming the tool
        # rather than "<unknown>".
        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for target in node.targets
            if isinstance(target, ast.Name)
        }

        def tool_name(call: ast.Call) -> str:
            value = next((kw.value for kw in call.keywords if kw.arg == "name"), None)
            if isinstance(value, ast.Constant):
                return str(value.value)
            if isinstance(value, ast.Name):
                return constants.get(value.id, value.id)
            return "<unknown>" if value is None else ast.unparse(value)

        missing = []
        for call in registrations:
            name = tool_name(call)
            annotations = next(
                (kw.value for kw in call.keywords if kw.arg == "annotations"), None
            )
            keys = (
                {k.value for k in annotations.keys if isinstance(k, ast.Constant)}
                if isinstance(annotations, ast.Dict)
                else set()
            )
            if "openWorldHint" not in keys:
                missing.append(name)

        assert not missing, (
            f"server-registered tools missing openWorldHint: {missing}. Add it to "
            "the annotations dict in the self.mcp.tool(...) call in server.py."
        )

    def test_search_proxy_tools_have_open_world_hint(self):
        """Runtime tool-search proxies must also set openWorldHint.

        When ENABLE_TOOL_SEARCH=true, CategorizedSearchTransform builds
        ha_search_tools and the ha_call_* proxies at runtime via
        ToolAnnotations(...), which the src/ha_mcp/tools scan never sees. Assert
        every ToolAnnotations construction in categorized_search.py sets
        openWorldHint so those proxies don't inherit the implicit default.
        """
        transform_py = get_tools_dir().parent / "transforms" / "categorized_search.py"
        tree = ast.parse(
            transform_py.read_text(encoding="utf-8"), filename=str(transform_py)
        )
        constructions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ToolAnnotations"
        ]
        assert constructions, (
            "no ToolAnnotations(...) constructions found in categorized_search.py "
            "-- the scanner's shape assumption drifted"
        )

        missing = sum(
            1
            for c in constructions
            if not any(kw.arg == "openWorldHint" for kw in c.keywords)
        )
        assert missing == 0, (
            f"{missing} of {len(constructions)} ToolAnnotations(...) in "
            "categorized_search.py omit openWorldHint; the runtime search proxies "
            "would inherit the MCP default. Set it explicitly on each proxy."
        )

    def test_all_tools_have_tags(self):
        """Every tool must have a tags= parameter for categorization."""
        tools = get_all_tools()

        missing_tags = [
            f"{tool['file']}:{tool['function']}"
            for tool in tools
            if not tool["has_tags"]
        ]

        assert not missing_tags, (
            f"Tools missing tags= parameter ({len(missing_tags)}):\n  "
            + "\n  ".join(missing_tags)
            + "\n\nAdd tags={'Category Name'} to each @mcp.tool() decorator."
        )

    def test_ha_check_config_removed_and_folded(self):
        """ha_check_config was removed and folded into ha_get_system_health
        (include='config_check'). Lock the removal at the source registration so
        a bad merge that re-adds the @tool block fails CI."""
        system_src = (get_tools_dir() / "tools_system.py").read_text(encoding="utf-8")
        assert 'name="ha_check_config"' not in system_src, (
            "ha_check_config was removed in favor of "
            "ha_get_system_health(include='config_check'); it must not be "
            "re-registered."
        )
        assert 'name="ha_get_system_health"' in system_src

    def test_tool_count_sanity_check(self):
        """Sanity check that we're finding a reasonable number of tools."""
        tools = get_all_tools()

        # We should have at least 50 tools (currently ~92)
        assert len(tools) >= 50, f"Only found {len(tools)} tools, expected at least 50"

        # We should have a mix of read-only and destructive tools
        read_only_count = sum(1 for t in tools if t["has_read_only_hint"])
        destructive_count = sum(1 for t in tools if t["has_destructive_hint"])

        assert read_only_count >= 20, (
            f"Only {read_only_count} read-only tools, expected at least 20"
        )
        assert destructive_count >= 19, (
            f"Only {destructive_count} destructive tools, expected at least 19"
        )

    def test_total_tool_count_limit(self):
        """Ensure total tool count doesn't exceed reasonable limits.

        This test counts all @mcp.tool decorators in the codebase. The actual
        registered tool count may be lower due to feature flags.

        Current state (as of PR #423):
        - Decorated tools in code: 105
        - Registered tools at runtime: 100 (5 behind feature flags)
        - Antigravity limit: 100 tools maximum

        The limit is set to 105 to match the current codebase. If you need to add
        more tools, you MUST first consolidate existing ones or move tools behind
        feature flags to keep the runtime count at or below 100.
        """
        tools = get_all_tools()
        tool_count = len(tools)

        # Limit matches current decorated tool count
        # Runtime count is lower (100) due to feature flags
        MAX_TOOLS = 105

        assert tool_count <= MAX_TOOLS, (
            f"Tool count ({tool_count}) exceeds limit ({MAX_TOOLS})!\n"
            f"Tools found: {tool_count}\n"
            f"Limit: {MAX_TOOLS}\n"
            f"Over by: {tool_count - MAX_TOOLS}\n\n"
            f"Note: Antigravity has a 100 tool limit at runtime.\n"
            f"Current registered tools: ~100 (some behind feature flags)\n\n"
            f"To fix this, you MUST:\n"
            f"1. Consolidate duplicate or similar tools (e.g., get/list patterns)\n"
            f"2. Move specialized tools behind feature flags\n"
            f"3. Remove rarely-used tools\n"
            f"\nSee issue #420 for context."
        )

    def test_read_only_tools_are_actually_read_only(self):
        """Tools with readOnlyHint should have read-only names (get, list, search, etc)."""
        tools = get_all_tools()

        read_only_tools = [t for t in tools if t["has_read_only_hint"]]

        suspicious = []
        for tool in read_only_tools:
            func = tool["function"].lower()
            # If it starts with a modifying verb, it's suspicious
            # Note: "list_updates" is OK (listing updates), "update_zone" is suspicious
            modifying_prefixes = [
                "create_",
                "set_",
                "delete_",
                "update_",
                "add_",
                "remove_",
                "assign_",
                "restart",
                "reload",
            ]
            if any(
                func.startswith(f"ha_{prefix}") or func.startswith(prefix)
                for prefix in modifying_prefixes
            ):
                suspicious.append(f"{tool['file']}:{tool['function']}")

        assert not suspicious, (
            f"Tools marked readOnlyHint but have modifying names ({len(suspicious)}):\n  "
            + "\n  ".join(suspicious)
        )

    def test_destructive_tools_are_actually_destructive(self):
        """Tools with destructiveHint should have modifying names."""
        tools = get_all_tools()

        destructive_tools = [t for t in tools if t["has_destructive_hint"]]

        # These patterns indicate destructive/modifying operations
        destructive_patterns = [
            "create",
            "set",
            "delete",
            "update",
            "add",
            "remove",
            "assign",
            "restart",
            "reload",
            "restore",
            "import",
            "rename",
            "call",
            "bulk",
            "config_set",
            "config_delete",
        ]

        suspicious = []
        for tool in destructive_tools:
            func = tool["function"].lower()
            # If it has only get/list/search patterns and destructiveHint, that's suspicious
            if not any(pattern in func for pattern in destructive_patterns):
                # Exception: ha_call_service and ha_bulk_control are correctly destructive
                if func not in ["ha_call_service", "ha_bulk_control"]:
                    suspicious.append(f"{tool['file']}:{tool['function']}")

        # This is a warning, not a hard failure - some tools might legitimately be destructive
        # even without obvious naming
        if suspicious:
            print(
                "\nNote: These destructive tools don't have typical modifying names:\n  "
                + "\n  ".join(suspicious)
            )
