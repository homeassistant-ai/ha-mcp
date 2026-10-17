"""The component's "Download diagnostics" against a real Home Assistant (#2584).

The unit tests call the handler directly. This checks what only Home Assistant
can: that it discovers the component's diagnostics platform and serves a
download for each of its loaded entries, on whichever entries the lane sets up.
"""

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient

from ...utilities.topology import component_surface_available

DOMAIN = "ha_mcp_tools"
# Every seed of the server entry uses the config flow's title.
SERVER_ENTRY_TITLE = "HA-MCP Server"


@pytest.mark.asyncio
async def test_every_loaded_entry_downloads_the_component_summary(
    ha_client: HomeAssistantClient,
) -> None:
    entries = [
        e
        for e in await ha_client._request("GET", "/config/config_entries/entry")
        if e["domain"] == DOMAIN
    ]
    loaded = [e for e in entries if e["state"] == "loaded"]
    # Only one way round: on a lane without the component's surface, a HAOS
    # test may still enable the baked server entry earlier in the session.
    if component_surface_available():
        assert loaded, f"no loaded {DOMAIN} entry: {entries}"
    if not loaded:
        pytest.skip(f"no loaded {DOMAIN} entry on this lane")
    # The title comes from Home Assistant, the file's entry type from entry.data.
    has_server = any(e["title"] == SERVER_ENTRY_TITLE for e in entries)

    for entry in loaded:
        download = await ha_client._request(
            "GET", f"/diagnostics/config_entry/{entry['entry_id']}"
        )
        data = download["data"]
        assert sorted(row["state"] for row in data["entries"]) == sorted(
            e["state"] for e in entries
        )
        server_rows = [row for row in data["entries"] if row["entry_type"] == "server"]
        assert bool(server_rows) == has_server
        assert (data["server"] is not None) == has_server
