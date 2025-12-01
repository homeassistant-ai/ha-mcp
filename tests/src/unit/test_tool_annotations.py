"""Tests for tool annotations compliance with MCP Directory Policy.

Every tool MUST have exactly one of:
- readOnlyHint: true - For tools that only read data
- destructiveHint: true - For tools that modify data or have side effects

Additionally, every tool SHOULD have a title for UI display.
"""

import ast
import re
from pathlib import Path

import pytest


def get_tools_dir() -> Path:
    """Get the path to the tools directory."""
    return Path(__file__).parent.parent.parent.parent / "src" / "ha_mcp" / "tools"


def extract_tool_decorators(file_path: Path) -> list[dict]:
    """Extract @mcp.tool decorator information from a Python file."""
    content = file_path.read_text(encoding="utf-8")
    tools = []

    # Find all @mcp.tool decorators with their annotations
    # Pattern matches @mcp.tool(...) followed by async def function_name
    pattern = r'@mcp\.tool\(([^)]*)\)\s*(?:@\w+\s*)*async def (\w+)'

    for match in re.finditer(pattern, content, re.DOTALL):
        decorator_args = match.group(1)
        func_name = match.group(2)

        # Check for annotations
        has_read_only = 'readOnlyHint' in decorator_args and 'True' in decorator_args.split('readOnlyHint')[1][:20]
        has_destructive = 'destructiveHint' in decorator_args and 'True' in decorator_args.split('destructiveHint')[1][:20]
        has_title = 'title' in decorator_args

        tools.append({
            'file': file_path.name,
            'function': func_name,
            'has_read_only_hint': has_read_only,
            'has_destructive_hint': has_destructive,
            'has_title': has_title,
            'decorator_args': decorator_args.strip(),
        })

    # Also find bare @mcp.tool without arguments
    bare_pattern = r'@mcp\.tool\s*\n\s*(?:@\w+\s*)*async def (\w+)'
    for match in re.finditer(bare_pattern, content):
        func_name = match.group(1)
        tools.append({
            'file': file_path.name,
            'function': func_name,
            'has_read_only_hint': False,
            'has_destructive_hint': False,
            'has_title': False,
            'decorator_args': '',
        })

    return tools


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

    def test_all_tools_have_required_hint(self):
        """Every tool must have exactly one of readOnlyHint or destructiveHint."""
        tools = get_all_tools()

        missing_hints = []
        both_hints = []

        for tool in tools:
            has_read = tool['has_read_only_hint']
            has_destructive = tool['has_destructive_hint']

            if not has_read and not has_destructive:
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
            if not tool['has_title']
        ]

        assert not missing_titles, (
            f"Tools missing title annotation ({len(missing_titles)}):\n  "
            + "\n  ".join(missing_titles)
        )

    def test_tool_count_sanity_check(self):
        """Sanity check that we're finding a reasonable number of tools."""
        tools = get_all_tools()

        # We should have at least 50 tools (currently ~82)
        assert len(tools) >= 50, f"Only found {len(tools)} tools, expected at least 50"

        # We should have a mix of read-only and destructive tools
        read_only_count = sum(1 for t in tools if t['has_read_only_hint'])
        destructive_count = sum(1 for t in tools if t['has_destructive_hint'])

        assert read_only_count >= 20, f"Only {read_only_count} read-only tools, expected at least 20"
        assert destructive_count >= 20, f"Only {destructive_count} destructive tools, expected at least 20"

    def test_read_only_tools_are_actually_read_only(self):
        """Tools with readOnlyHint should have read-only names (get, list, search, etc)."""
        tools = get_all_tools()

        read_only_tools = [t for t in tools if t['has_read_only_hint']]

        # These prefixes/patterns indicate read-only operations
        read_only_patterns = ['get', 'list', 'search', 'check', 'eval', 'render']

        suspicious = []
        for tool in read_only_tools:
            func = tool['function'].lower()
            # If it starts with a modifying verb, it's suspicious
            # Note: "list_updates" is OK (listing updates), "update_zone" is suspicious
            modifying_prefixes = ['create_', 'set_', 'delete_', 'update_', 'add_', 'remove_', 'assign_', 'restart', 'reload']
            if any(func.startswith(f'ha_{prefix}') or func.startswith(prefix) for prefix in modifying_prefixes):
                suspicious.append(f"{tool['file']}:{tool['function']}")

        assert not suspicious, (
            f"Tools marked readOnlyHint but have modifying names ({len(suspicious)}):\n  "
            + "\n  ".join(suspicious)
        )

    def test_destructive_tools_are_actually_destructive(self):
        """Tools with destructiveHint should have modifying names."""
        tools = get_all_tools()

        destructive_tools = [t for t in tools if t['has_destructive_hint']]

        # These patterns indicate destructive/modifying operations
        destructive_patterns = ['create', 'set', 'delete', 'update', 'add', 'remove', 'assign', 'restart', 'reload', 'restore', 'import', 'rename', 'call', 'bulk', 'config_set', 'config_delete']

        suspicious = []
        for tool in destructive_tools:
            func = tool['function'].lower()
            # If it has only get/list/search patterns and destructiveHint, that's suspicious
            if not any(pattern in func for pattern in destructive_patterns):
                # Exception: ha_call_service and ha_bulk_control are correctly destructive
                if func not in ['ha_call_service', 'ha_bulk_control']:
                    suspicious.append(f"{tool['file']}:{tool['function']}")

        # This is a warning, not a hard failure - some tools might legitimately be destructive
        # even without obvious naming
        if suspicious:
            print(f"\nNote: These destructive tools don't have typical modifying names:\n  " + "\n  ".join(suspicious))
