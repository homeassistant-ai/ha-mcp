"""Unit tests for RenamedToolAliasMiddleware.

Covers the window after a tool rename in which a client still calls the old
name from a catalog it has not re-listed: the call has to reach the current
tool, and the old name has to stay out of the catalog.
"""

import json
import logging

import pytest

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import NotFoundError, ToolError
from ha_mcp.renamed_tools import RENAMED_PARAMETERS
from ha_mcp.tools.renamed_tool_middleware import (
    RENAMED_TOOLS,
    RenamedToolAliasMiddleware,
)
from ha_mcp.tools.validation_middleware import ValidationErrorMiddleware


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

    @mcp.tool()
    async def ha_manage_blueprints(
        action: str,
        domain: str = "automation",
        path: str | None = None,
        url: str | None = None,
    ) -> dict:
        return {
            "tool": "ha_manage_blueprints",
            "action": action,
            "domain": domain,
            "path": path,
            "url": url,
        }

    @mcp.tool()
    async def ha_config_get_automation(entity_id: str) -> dict:
        return {"tool": "ha_config_get_automation", "entity_id": entity_id}

    @mcp.tool()
    async def ha_config_remove_automation(entity_id: str) -> dict:
        return {"entity_id": entity_id, "warnings": ["Backup skipped."]}

    return mcp


# One valid call per retired name, in the retired signature.
_RETIRED_CALLS = {
    "ha_get_addon": {"source": "available"},
    "ha_manage_addon": {"slug": "core_ssh"},
    "ha_get_blueprint": {"path": "user/motion.yaml"},
    "ha_import_blueprint": {"url": "https://example.com/bp.yaml"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("old", "current"), sorted(RENAMED_TOOLS.items()))
async def test_a_call_on_the_old_name_reaches_the_current_tool(old, current):
    """The rewrite happens before resolution, so the old name still dispatches."""
    result = await _make_mcp().call_tool(old, _RETIRED_CALLS[old])

    assert result.structured_content is not None
    assert result.structured_content["tool"] == current


@pytest.mark.asyncio
async def test_the_arguments_survive_the_rewrite():
    """Only the name is replaced — a dropped argument would be silent."""
    result = await _make_mcp().call_tool("ha_get_addon", {"source": "available"})

    assert result.structured_content["tool"] == "ha_get_app"
    assert result.structured_content["source"] == "available"


@pytest.mark.asyncio
async def test_the_old_names_stay_out_of_the_catalog():
    """The alias costs no catalog tokens: it is not a registered tool.

    A second, disabled tool would not serve as the alias either — FastMCP
    filters disabled tools out of resolution as well as out of the listing —
    so the aliases must not be registered at all.
    """
    mcp = _make_mcp()

    listed = {tool.name for tool in (await mcp.list_tools())}

    assert listed == {
        "ha_get_app",
        "ha_manage_app",
        "ha_manage_blueprints",
        "ha_config_get_automation",
        "ha_config_remove_automation",
    }
    assert not listed & set(RENAMED_TOOLS)


@pytest.mark.asyncio
async def test_the_first_call_on_a_retired_name_is_audible(caplog):
    """An operator has to be able to see that a retired name is still in use.

    That observation is the only thing that could ever justify retiring the
    alias, and a per-call INFO line would be noise for a tool called in a
    loop — so the first call on each name announces itself and the rest drop
    to DEBUG.
    """
    mcp = _make_mcp()

    with caplog.at_level(logging.DEBUG, logger="ha_mcp.tools.renamed_tool_middleware"):
        await mcp.call_tool("ha_get_addon", {"source": "available"})
        await mcp.call_tool("ha_get_addon", {"source": "available"})
        await mcp.call_tool("ha_manage_addon", {"slug": "core_ssh"})

    levels = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "ha_mcp.tools.renamed_tool_middleware"
    ]

    assert [level for level, _ in levels] == [
        logging.INFO,
        logging.DEBUG,
        logging.INFO,
    ]
    assert "ha_get_addon" in levels[0][1] and "ha_get_app" in levels[0][1]
    assert "ha_manage_addon" in levels[2][1]


@pytest.mark.asyncio
async def test_an_unrelated_unknown_name_is_left_alone():
    """Only the mapped names are rewritten; everything else resolves as before."""
    with pytest.raises(NotFoundError):
        await _make_mcp().call_tool("ha_get_addon_typo", {})


@pytest.mark.asyncio
async def test_a_consolidated_tool_gets_the_action_its_old_signature_lacked():
    """#2329: ha_get_blueprint listed without a path and read with one;
    ha_import_blueprint always imported. The alias supplies the action."""
    mcp = _make_mcp()

    listed = await mcp.call_tool("ha_get_blueprint", {"domain": "script"})
    assert listed.structured_content["action"] == "list"
    assert listed.structured_content["domain"] == "script"

    read = await mcp.call_tool("ha_get_blueprint", {"path": "user/motion.yaml"})
    assert read.structured_content["action"] == "get"
    assert read.structured_content["path"] == "user/motion.yaml"

    imported = await mcp.call_tool(
        "ha_import_blueprint", {"url": "https://example.com/bp.yaml"}
    )
    assert imported.structured_content["action"] == "import"
    assert imported.structured_content["url"] == "https://example.com/bp.yaml"


@pytest.mark.asyncio
async def test_a_call_with_a_retired_parameter_name_reaches_the_tool():
    """9.1.0 renamed `identifier` to `entity_id` on the automation tools. A
    client still holding the older tool list sends the old name and could not
    call them at all (#2968, #2978)."""
    result = await _make_mcp().call_tool(
        "ha_config_get_automation", {"identifier": "automation.morning"}
    )

    assert result.structured_content["tool"] == "ha_config_get_automation"
    assert result.structured_content["entity_id"] == "automation.morning"


@pytest.mark.asyncio
async def test_a_retired_parameter_name_does_not_override_the_current_one():
    """A call that carries both names knows the current signature, so the
    stray old name is rejected like any other unknown argument instead of
    replacing the value the caller sent under the current name."""
    mcp = _make_mcp()
    mcp.add_middleware(ValidationErrorMiddleware())

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(
            "ha_config_get_automation",
            {"entity_id": "automation.morning", "identifier": "automation.night"},
        )

    assert "`identifier`: unknown parameter" in str(exc_info.value)


@pytest.mark.asyncio
async def test_the_first_call_with_a_retired_parameter_name_is_audible(caplog):
    """The alias hides the stale tool list from the client, so the server log
    is the only place that shows a client is still sending the old name."""
    mcp = _make_mcp()

    with caplog.at_level(logging.DEBUG, logger="ha_mcp.tools.renamed_tool_middleware"):
        await mcp.call_tool("ha_config_get_automation", {"identifier": "automation.a"})
        await mcp.call_tool("ha_config_get_automation", {"identifier": "automation.b"})
        await mcp.call_tool("ha_config_get_automation", {"entity_id": "automation.c"})

    records = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "ha_mcp.tools.renamed_tool_middleware"
    ]

    assert [level for level, _ in records] == [logging.INFO, logging.DEBUG]
    assert "identifier" in records[0][1] and "entity_id" in records[0][1]
    assert "ha_config_get_automation" in records[0][1]


def test_every_tool_that_renamed_identifier_keeps_it_callable():
    """The three automation tools took the rename together; a client with the
    old list calls all three."""
    assert {
        tool
        for tool, renames in RENAMED_PARAMETERS.items()
        if renames.get("identifier") == "entity_id"
    } == {
        "ha_config_get_automation",
        "ha_config_set_automation",
        "ha_config_remove_automation",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "old", "new"),
    [
        ("ha_get_addon", {"source": "available"}, "ha_get_addon", "ha_get_app"),
        (
            "ha_config_get_automation",
            {"identifier": "automation.morning"},
            "identifier",
            "entity_id",
        ),
    ],
)
async def test_a_call_served_through_an_old_name_says_the_tool_list_is_stale(
    tool, arguments, old, new
):
    """The alias makes the call work, so the rejection that used to point at
    the stale list never happens; the result has to say it instead, in the
    structured content and in the text a client without it reads."""
    result = await _make_mcp().call_tool(tool, arguments)

    for payload in (result.structured_content, json.loads(result.content[0].text)):
        (warning,) = payload["warnings"]
        assert f"`{old}`" in warning and f"`{new}`" in warning
        assert "tool list" in warning and "new conversation" in warning


@pytest.mark.asyncio
async def test_a_call_on_the_current_names_carries_no_stale_list_warning():
    result = await _make_mcp().call_tool(
        "ha_config_get_automation", {"entity_id": "automation.morning"}
    )

    assert "warnings" not in result.structured_content


@pytest.mark.asyncio
async def test_the_stale_list_warning_keeps_the_tools_own_warnings():
    result = await _make_mcp().call_tool(
        "ha_config_remove_automation", {"identifier": "automation.morning"}
    )

    warnings = result.structured_content["warnings"]
    assert warnings[0] == "Backup skipped."
    assert len(warnings) == 2 and "`identifier`" in warnings[1]
