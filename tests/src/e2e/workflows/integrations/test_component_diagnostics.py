"""The component's "Download diagnostics" against a real Home Assistant (#2584).

The unit tests call the handler directly. This checks what only Home Assistant
can: that it discovers the component's diagnostics platform and serves a
download for each of its loaded entries, on whichever entries the lane sets up.
"""

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient

from ...utilities.topology import component_surface_available

DOMAIN = "ha_mcp_tools"


@pytest.mark.asyncio
async def test_every_loaded_entry_downloads_the_component_summary(
    ha_client: HomeAssistantClient,
):
    entries = [
        e
        for e in await ha_client._request("GET", "/config/config_entries/entry")
        if e["domain"] == DOMAIN
    ]
    loaded = [e for e in entries if e["state"] == "loaded"]
    if component_surface_available():
        assert loaded, f"no loaded {DOMAIN} entry: {entries}"

    for entry in loaded:
        download = await ha_client._request(
            "GET", f"/diagnostics/config_entry/{entry['entry_id']}"
        )
        data = download["data"]
        assert sorted(row["state"] for row in data["entries"]) == sorted(
            e["state"] for e in entries
        )
        has_server = any(row["entry_type"] == "server" for row in data["entries"])
        assert (data["server"] is not None) == has_server
