"""Unit tests for WriteToolNoteTransform (issue #2367).

The note is appended to every ``destructive_hint`` tool and to nothing else,
on both the ``list_tools`` and ``get_tool`` paths, and the server installs it
unconditionally after the lite-docstrings transform so it survives the
description replacement.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.transforms import DESKTOP_APPROVAL_NOTE, WriteToolNoteTransform


def _make_tool(name: str, *, destructive: bool, description: str = "Does X.") -> Tool:
    async def noop() -> str:
        return "ok"

    annotations = (
        ToolAnnotations(destructive_hint=True)
        if destructive
        else ToolAnnotations(read_only_hint=True)
    )
    return Tool.from_function(
        fn=noop, name=name, description=description, annotations=annotations
    )


@pytest.mark.asyncio
async def test_list_tools_appends_note_to_write_tools_only() -> None:
    write = _make_tool("ha_config_set_dashboard", destructive=True)
    read = _make_tool("ha_config_get_dashboard", destructive=False)

    result = await WriteToolNoteTransform().list_tools([write, read])

    by_name = {t.name: t for t in result}
    assert by_name["ha_config_set_dashboard"].description == (
        f"Does X.\n\n{DESKTOP_APPROVAL_NOTE}"
    )
    assert by_name["ha_config_get_dashboard"].description == "Does X."


@pytest.mark.asyncio
async def test_list_tools_handles_missing_description_and_annotations() -> None:
    async def noop() -> str:
        return "ok"

    bare = Tool.from_function(fn=noop, name="bare", description="")
    bare = bare.model_copy(update={"annotations": None, "description": None})
    write = _make_tool("w", destructive=True, description="")

    result = await WriteToolNoteTransform().list_tools([bare, write])

    by_name = {t.name: t for t in result}
    assert by_name["bare"].description is None
    assert by_name["w"].description == DESKTOP_APPROVAL_NOTE


@pytest.mark.asyncio
async def test_get_tool_path_matches_list_path() -> None:
    write = _make_tool("w", destructive=True)
    call_next = AsyncMock(return_value=write)

    got = await WriteToolNoteTransform().get_tool("w", call_next)

    assert got is not None
    assert got.description is not None
    assert got.description.endswith(DESKTOP_APPROVAL_NOTE)
    call_next.assert_awaited_once_with("w", version=None)

    call_next_missing = AsyncMock(return_value=None)
    assert await WriteToolNoteTransform().get_tool("nope", call_next_missing) is None


def test_note_names_the_workarounds() -> None:
    lowered = DESKTOP_APPROVAL_NOTE.lower()
    assert "4-minute" in lowered
    assert "wait" in lowered
    assert "always allow" in lowered


def test_server_installs_transform_after_lite_docstrings() -> None:
    """The note must be appended AFTER lite docstrings replace descriptions."""
    from ha_mcp.server import HomeAssistantSmartMCPServer

    server = MagicMock(spec=HomeAssistantSmartMCPServer)
    server.mcp = MagicMock()
    HomeAssistantSmartMCPServer._apply_write_tool_note(server)

    server.mcp.add_transform.assert_called_once()
    assert isinstance(
        server.mcp.add_transform.call_args.args[0], WriteToolNoteTransform
    )

    body = inspect.getsource(HomeAssistantSmartMCPServer._initialize_server)
    assert body.index("_apply_lite_docstrings()") < body.index(
        "_apply_write_tool_note()"
    )
    assert body.index("_apply_write_tool_note()") < body.index(
        "_apply_search_keyword_enrichment()"
    )
