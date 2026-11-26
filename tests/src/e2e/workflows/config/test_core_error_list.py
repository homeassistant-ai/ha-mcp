"""Core 2026.12 lists each error of a refused automation or script config with
its path; ha_config_set_automation and ha_config_set_script pass the list
through as ``errors``."""

import pytest

from ...utilities.assertions import MCPAssertions
from ...utilities.core_version import core_at_least
from ...utilities.topology import component_surface_available


@pytest.mark.asyncio
async def test_a_refused_script_lists_the_errors_core_found(
    mcp_client, ha_client
) -> None:
    async with MCPAssertions(mcp_client) as mcp:
        refused = await mcp.call_tool_failure(
            "ha_config_set_script",
            {
                "script_id": "e2e_core_error_list",
                "config": {"alias": "Bad delay", "sequence": [{"delay": "nope"}]},
            },
        )
    if await core_at_least(ha_client, (2026, 12)):
        assert [e["path"][0] for e in refused["errors"]] == ["sequence"], refused
    else:
        assert "errors" not in refused, refused


@pytest.mark.asyncio
@pytest.mark.component_topology
async def test_a_refused_automation_lists_the_errors_core_found(
    mcp_client, ha_client
) -> None:
    """Without the component Core's config view refuses the write; the
    component's validator refuses it first and gives no list."""
    async with MCPAssertions(mcp_client) as mcp:
        refused = await mcp.call_tool_failure(
            "ha_config_set_automation",
            {
                "config": {
                    "alias": "E2E core error list",
                    "triggers": [{"trigger": "battery.low"}],
                    "actions": [{"delay": {"seconds": 1}}],
                }
            },
        )
    assert refused["error"]["code"] == "VALIDATION_FAILED", refused
    if not component_surface_available() and await core_at_least(ha_client, (2026, 12)):
        assert refused["errors"][0]["path"][:2] == ["triggers", 0], refused
    else:
        assert "errors" not in refused, refused
