"""Inaddon E2E for the self-update notice — the add-on (Supervisor-store) path.

The HA add-on (stable AND dev) is built from source and updated via the
Supervisor add-on store, so its update notice is sourced from
``GET /addons/self/info`` (``version`` / ``version_latest`` / ``update_available``
on the add-on's own counter), NOT PyPI. This test exercises that real path: it
calls the status tools over the running dev add-on's HTTP MCP endpoint and
asserts each carries a well-formed ``ha_mcp_update`` object.

It asserts presence + shape, not a forced ``update_available`` value (that
reflects the real add-on store; after the lane updates the add-on to the PR
version it is typically up to date). The value-mapping logic is unit-tested in
``test_update_check.py`` (TestAddonSupervisorSource); the complementary
``test_self_update_notice.py`` covers the PyPI path on the external/container
lanes.
"""

from __future__ import annotations

import pytest

from ...utilities.assertions import MCPAssertions

pytestmark = [pytest.mark.inaddon_only, pytest.mark.system]


@pytest.mark.parametrize(
    "tool", ["ha_get_overview", "ha_get_system_health", "ha_manage_updates"]
)
async def test_dev_addon_surfaces_update_field_from_supervisor(
    mcp_client, tool: str
) -> None:
    async with MCPAssertions(mcp_client) as mcp:
        result = await mcp.call_tool_success(tool, {})

    # Anti-vacuous: the tool really ran and returned its normal payload.
    assert isinstance(result, dict) and result, f"{tool} returned no payload"
    assert "ha_mcp_update" in result, (
        f"{tool} did not surface ha_mcp_update on the dev add-on "
        f"(it should come from Supervisor /addons/self/info): {sorted(result)}"
    )
    update = result["ha_mcp_update"]
    assert isinstance(update.get("current"), str) and update["current"]
    assert isinstance(update.get("latest"), str) and update["latest"]
    assert isinstance(update.get("update_available"), bool)
