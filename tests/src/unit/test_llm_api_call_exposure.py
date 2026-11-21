"""A tool hidden mid-conversation is refused on its next call (issue #2713).

Home Assistant 2026.11 keeps an LLM API instance, and so the tool list the
agent holds, for the whole conversation. Exposure is therefore read again on
every forwarded call rather than taken from that list.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import llm_api

from ._llm_api_server import (
    SECRET_PATH,
    ServedTool,
    call,
    mirrored_tool,
    payload,
    serve,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")


@pytest.mark.parametrize("via_call_tool", [False, True])
async def test_tool_hidden_after_the_list_was_built_is_refused(
    hass: HomeAssistant, via_call_tool: bool
) -> None:
    async with serve([ServedTool("ha_search", exposed=False)]) as server:
        tool: llm_api.HaMcpCallTool | llm_api.HaMcpTool
        if via_call_tool:
            url = f"http://127.0.0.1:{server.port}{SECRET_PATH}"
            tool = llm_api.HaMcpCallTool(url, {"ha_search"})
            args = {"name": "ha_search", "arguments": {}}
        else:
            tool = mirrored_tool(server.port)
            args = {"query": "kitchen"}

        out = await call(hass, tool, args)

    assert server.calls == []
    assert out.error is True
    assert payload(out)["error"] == "Unknown tool 'ha_search'."
