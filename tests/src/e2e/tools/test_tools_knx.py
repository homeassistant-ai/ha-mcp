"""
E2E smoke test for the KNX project fold into ha_get_integration.

The test container has no KNX integration configured, so there is no KNX
config entry to pass include_knx_project against. This exercises the
not-loaded path the maintainer asked us to keep: requesting
include_knx_project on a non-KNX entry must degrade cleanly (a warning, no
crash, no KNX data attached) over the real WebSocket/REST plumbing. The
parsed-project success path is covered by the unit tests, which would
otherwise require a live HA with the KNX integration and an uploaded ETS
project.
"""

import logging

import pytest

from ..utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_include_knx_project_on_non_knx_entry_degrades_cleanly(mcp_client):
    """include_knx_project against a non-KNX entry returns a warning, not data.

    Picks the first available config entry (none are KNX in the test
    container) and asserts the response succeeds with no knx_project key and a
    warning explaining the flag was ignored.
    """
    listing = await mcp_client.call_tool("ha_get_integration", {})
    raw_list = assert_mcp_success(listing, "list integrations")
    entries = raw_list.get("data", raw_list).get("entries", [])
    if not entries:
        pytest.skip("no config entries available in the test container")
    entry_id = entries[0]["entry_id"]

    result = await mcp_client.call_tool(
        "ha_get_integration",
        {"entry_id": entry_id, "include_knx_project": True},
    )
    raw = assert_mcp_success(result, "get integration with include_knx_project")
    data = raw.get("data", raw)

    assert "knx_project" not in data
    warnings = data.get("warnings", [])
    assert any("KNX" in w for w in warnings), (
        f"expected an include_knx_project warning, got warnings={warnings}"
    )
    logger.info("include_knx_project degraded cleanly on a non-KNX entry")
