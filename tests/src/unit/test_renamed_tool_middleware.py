"""Unit tests for RenamedToolAliasMiddleware.

Covers the window after a tool rename in which a client still calls the old
name from a catalog it has not re-listed: the call has to reach the current
tool, and the old name has to stay out of the catalog.
"""

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError

from ha_mcp.tools.renamed_tool_middleware import (
    RENAMED_TOOLS,
    RenamedToolAliasMiddleware,
)


def _make_mcp() -> FastMCP:
    """A bare server carrying only the alias middleware and the renamed tools."""
    mcp = FastMCP("test")
    mcp.add_middleware(RenamedToolAliasMiddleware())

    @mcp.tool()
    async def ha_get_app(source: str = "installed") -> dict:
        return {"tool": "ha_get_app", "source": source}

    @mcp.tool()
    async def ha_manage_app(slug: str) -> dict:
        return {"tool": "ha_manage_app", "slug": slug}

    return mcp


@pytest.mark.asyncio
@pytest.mark.parametrize(("old", "current"), sorted(RENAMED_TOOLS.items()))
async def test_a_call_on_the_old_name_reaches_the_current_tool(old, current):
    """The rewrite happens before resolution, so the old name still dispatches."""
    arguments = {"slug": "core_ssh"} if "manage" in old else {"source": "available"}

    result = await _make_mcp().call_tool(old, arguments)

    assert result.structured_content is not None
    assert result.structured_content["tool"] == current


@pytest.mark.asyncio
async def test_the_arguments_survive_the_rewrite():
    """Only the name is replaced — a dropped argument would be silent."""
    result = await _make_mcp().call_tool("ha_get_addon", {"source": "available"})

    assert result.structured_content == {
        "tool": "ha_get_app",
        "source": "available",
    }


@pytest.mark.asyncio
async def test_the_old_names_stay_out_of_the_catalog():
    """The alias costs no catalog tokens: it is not a registered tool.

    A second, disabled tool would not serve as the alias either — FastMCP
    filters disabled tools out of resolution as well as out of the listing —
    so the aliases must not be registered at all.
    """
    mcp = _make_mcp()

    listed = {tool.name for tool in (await mcp.list_tools())}

    assert listed == {"ha_get_app", "ha_manage_app"}
    assert not listed & set(RENAMED_TOOLS)


@pytest.mark.asyncio
async def test_an_unrelated_unknown_name_is_left_alone():
    """Only the mapped names are rewritten; everything else resolves as before."""
    with pytest.raises(NotFoundError):
        await _make_mcp().call_tool("ha_get_addon_typo", {})
