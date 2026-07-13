"""Tests for packaging/mcpb/generate_manifest.py's AST-based tool discovery.

Covers extract_tools_from_file / discover_all_tools and their helpers
(_get_docstring_description, _extract_title_and_decorator_description,
_build_tool_entry) — this module had zero prior test coverage.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "packaging" / "mcpb"))
import generate_manifest  # noqa: E402


class TestExtractToolsFromFile:
    def test_docstring_first_line_used_as_description(self, tmp_path):
        source = '''
async def ha_get_state(entity_id: str):
    """Get the current state of an entity.

    Second line is ignored.
    """
'''
        py_file = tmp_path / "tools_example.py"
        py_file.write_text(source, encoding="utf-8")

        tools = generate_manifest.extract_tools_from_file(py_file)

        assert tools == [
            {"name": "Get State", "description": "Get the current state of an entity."}
        ]

    def test_title_from_decorator_annotations_overrides_formatted_name(self, tmp_path):
        source = '''
from fastmcp.tools import tool

@tool(name="ha_get_state", annotations={"title": "Get Entity State"})
async def ha_get_state(entity_id: str):
    """Get the current state of an entity."""
'''
        py_file = tmp_path / "tools_example.py"
        py_file.write_text(source, encoding="utf-8")

        tools = generate_manifest.extract_tools_from_file(py_file)

        assert tools[0]["name"] == "Get Entity State"

    def test_description_from_decorator_when_no_docstring(self, tmp_path):
        source = """
from fastmcp.tools import tool

@tool(name="ha_get_state", description="Fallback description from decorator")
async def ha_get_state(entity_id: str):
    pass
"""
        py_file = tmp_path / "tools_example.py"
        py_file.write_text(source, encoding="utf-8")

        tools = generate_manifest.extract_tools_from_file(py_file)

        assert tools[0]["description"] == "Fallback description from decorator"

    def test_docstring_wins_over_decorator_description(self, tmp_path):
        source = '''
from fastmcp.tools import tool

@tool(name="ha_get_state", description="Should not be used")
async def ha_get_state(entity_id: str):
    """Docstring description wins."""
'''
        py_file = tmp_path / "tools_example.py"
        py_file.write_text(source, encoding="utf-8")

        tools = generate_manifest.extract_tools_from_file(py_file)

        assert tools[0]["description"] == "Docstring description wins."

    def test_no_title_no_docstring_falls_back_to_formatted_name(self, tmp_path):
        source = """
async def ha_list_services():
    pass
"""
        py_file = tmp_path / "tools_example.py"
        py_file.write_text(source, encoding="utf-8")

        tools = generate_manifest.extract_tools_from_file(py_file)

        assert tools == [{"name": "List Services", "description": "List Services"}]

    def test_description_truncated_at_100_chars(self, tmp_path):
        long_line = "x" * 150
        source = f'''
async def ha_do_something():
    """{long_line}"""
'''
        py_file = tmp_path / "tools_example.py"
        py_file.write_text(source, encoding="utf-8")

        tools = generate_manifest.extract_tools_from_file(py_file)

        assert len(tools[0]["description"]) == 100
        assert tools[0]["description"] == long_line[:100]

    def test_non_ha_prefixed_functions_ignored(self, tmp_path):
        source = '''
async def helper_function():
    """Not a tool."""

async def ha_actual_tool():
    """This one counts."""
'''
        py_file = tmp_path / "tools_example.py"
        py_file.write_text(source, encoding="utf-8")

        tools = generate_manifest.extract_tools_from_file(py_file)

        assert len(tools) == 1
        assert tools[0]["name"] == "Actual Tool"

    def test_sync_functions_ignored(self, tmp_path):
        source = '''
def ha_sync_tool():
    """Sync functions are not MCP tools and must be skipped."""

async def ha_async_tool():
    """Only this one should be picked up."""
'''
        py_file = tmp_path / "tools_example.py"
        py_file.write_text(source, encoding="utf-8")

        tools = generate_manifest.extract_tools_from_file(py_file)

        assert len(tools) == 1
        assert tools[0]["name"] == "Async Tool"


class TestDiscoverAllTools:
    def test_skips_files_starting_with_underscore(self, tmp_path):
        (tmp_path / "tools_visible.py").write_text(
            'async def ha_visible():\n    """Visible tool."""\n', encoding="utf-8"
        )
        (tmp_path / "_private.py").write_text(
            'async def ha_hidden():\n    """Should be skipped."""\n', encoding="utf-8"
        )

        tools = generate_manifest.discover_all_tools(tmp_path)

        assert [t["name"] for t in tools] == ["Visible"]

    def test_sorts_tools_by_name_across_files(self, tmp_path):
        (tmp_path / "tools_a.py").write_text(
            'async def ha_zebra():\n    """Z tool."""\n', encoding="utf-8"
        )
        (tmp_path / "tools_b.py").write_text(
            'async def ha_apple():\n    """A tool."""\n', encoding="utf-8"
        )

        tools = generate_manifest.discover_all_tools(tmp_path)

        assert [t["name"] for t in tools] == ["Apple", "Zebra"]
