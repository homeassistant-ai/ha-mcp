"""The component's "Download diagnostics" against a real Home Assistant (#2584).

The unit tests call the handler directly, with the installed version stubbed
and the manager built without a running server. This checks what only Home
Assistant can: that it discovers the component's diagnostics platform and
serves a download for each of its loaded entries, on whichever entries the lane
sets up, and, where the session's MCP server is the in-process one, the real
server values.
"""

import json
from typing import Any

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient

DOMAIN = "ha_mcp_tools"
# Every seed of the server entry uses the config flow's title.
SERVER_ENTRY_TITLE = "HA-MCP Server"
# Lanes whose session MCP server is the in-process one, so it must be running.
IN_PROCESS_BACKENDS = ("embedded", "haos_embedded")


@pytest.mark.requires_component_surface
@pytest.mark.asyncio
async def test_every_loaded_entry_downloads_the_component_summary(
    ha_client: HomeAssistantClient,
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    entries = [
        e
        for e in await ha_client._request("GET", "/config/config_entries/entry")
        if e["domain"] == DOMAIN
    ]
    loaded = [e for e in entries if e["state"] == "loaded"]
    assert loaded, f"no loaded {DOMAIN} entry: {entries}"
    # The title comes from Home Assistant, the file's entry type from entry.data.
    has_server = any(e["title"] == SERVER_ENTRY_TITLE for e in entries)
    in_process = ha_container_with_fresh_config["backend"] in IN_PROCESS_BACKENDS

    for entry in loaded:
        download = await ha_client._request(
            "GET", f"/diagnostics/config_entry/{entry['entry_id']}"
        )
        assert ha_client.token not in json.dumps(download)
        data = download["data"]
        assert sorted(row["state"] for row in data["entries"]) == sorted(
            e["state"] for e in entries
        )
        server_rows = [row for row in data["entries"] if row["entry_type"] == "server"]
        assert bool(server_rows) == has_server
        assert (data["server"] is not None) == has_server
        if in_process:
            server = data["server"]
            assert server["running"] is True, server
            assert server["installed_server_version"], server
            assert server["startup_error"] is None, server
