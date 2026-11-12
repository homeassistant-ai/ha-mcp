"""ha_get_system_health against a real Home Assistant: Core's own replies."""

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client import HomeAssistantClient

from ...utilities.assertions import MCPAssertions
from ...utilities.wait_helpers import wait_for_tool_result


@pytest.mark.system
async def test_thread_network_lists_cores_datasets(
    mcp_client: Client, ha_client: HomeAssistantClient
) -> None:
    """With the Thread integration set up, thread_network carries Core's
    thread/list_datasets reply."""
    async with MCPAssertions(mcp_client) as mcp:
        entries = await ha_client.list_config_entries()
        if not any(entry["domain"] == "thread" for entry in entries):
            await mcp.call_tool_success("ha_set_integration", {"domain": "thread"})

    data = await wait_for_tool_result(
        mcp_client,
        tool_name="ha_get_system_health",
        arguments={"include": "thread_network"},
        predicate=lambda d: "datasets_error" not in d.get("thread_network", {}),
        description="Thread datasets are readable",
    )
    core = await ha_client.send_websocket_message({"type": "thread/list_datasets"})
    assert "datasets_error" not in data["thread_network"]
    assert data["thread_network"]["datasets"] == core["result"]["datasets"]
