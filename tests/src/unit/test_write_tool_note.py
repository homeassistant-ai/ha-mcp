"""Unit tests for WriteToolNoteTransform (issue #2367).

The note is appended to every ``destructive_hint`` tool and to nothing else,
on both the ``list_tools`` and ``get_tool`` paths, and the server installs it
unconditionally after the lite-docstrings transform so it survives the
description replacement.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.transforms import DESKTOP_APPROVAL_NOTE, WriteToolNoteTransform

from ._server_factory import make_server


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


@pytest.mark.usefixtures("tool_search_default")
@pytest.mark.asyncio
async def test_tool_search_catalog_carries_the_note_on_write_paths() -> None:
    """With tool search on, the note reaches every path a write can take.

    ``CategorizedSearchTransform`` synthesises the call proxies after
    ``WriteToolNoteTransform`` ran, so the proxies embed the note themselves;
    the hidden write tool behind them carries it from the transform. Checks
    the produced catalog, not source ordering.
    """
    from ha_mcp.transforms import CategorizedSearchTransform

    tools = [
        _make_tool("ha_get_state", destructive=False),
        _make_tool("ha_config_set_dashboard", destructive=True),
        _make_tool("ha_remove_zone", destructive=True),
    ]
    noted = await WriteToolNoteTransform().list_tools(tools)
    catalog = await CategorizedSearchTransform(max_results=5).transform_tools(noted)

    by_name = {t.name: t.description or "" for t in catalog}
    assert DESKTOP_APPROVAL_NOTE in by_name["ha_call_write_tool"]
    assert DESKTOP_APPROVAL_NOTE in by_name["ha_call_delete_tool"]
    assert DESKTOP_APPROVAL_NOTE not in by_name["ha_call_read_tool"]
    assert DESKTOP_APPROVAL_NOTE not in by_name["ha_search_tools"]

    hidden = {t.name: t.description or "" for t in noted}
    assert hidden["ha_config_set_dashboard"].endswith(DESKTOP_APPROVAL_NOTE)
    assert DESKTOP_APPROVAL_NOTE not in hidden["ha_get_state"]


def test_note_preserves_manual_approval() -> None:
    lowered = DESKTOP_APPROVAL_NOTE.lower()
    assert "4-minute" in lowered
    assert "wait" in lowered
    assert "always allow" not in lowered


async def test_server_installs_transform_after_lite_docstrings(
    core_client: HomeAssistantClient, monkeypatch
) -> None:
    """The real catalog keeps the note after lite descriptions replace the text."""
    from ha_mcp.server_tool_text import LITE_DOCSTRINGS

    server = make_server(
        core_client,
        monkeypatch,
        enabled_tool_modules="tools_config_dashboards",
        enable_lite_docstrings=True,
    )
    tool = await server.mcp.get_tool("ha_config_set_dashboard")
    assert tool is not None
    assert tool.description.startswith(LITE_DOCSTRINGS["ha_config_set_dashboard"])
    assert DESKTOP_APPROVAL_NOTE in tool.description
