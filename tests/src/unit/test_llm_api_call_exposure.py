"""A tool hidden mid-conversation is refused on its next call (issue #2713).

Home Assistant 2026.11 keeps an LLM API instance, and so the tool list the
agent holds, for the whole conversation. Exposure is therefore read again on
every forwarded call rather than taken from that list.
"""

from __future__ import annotations

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import llm_api  # noqa: E402

from ._llm_api_helpers import (  # noqa: E402
    CoreToolResult,
    fake_session,
    make_hass,
    tool_entry,
)

_URL = "http://127.0.0.1:9584/private_x"


@pytest.mark.parametrize("via_call_tool", [False, True])
async def test_tool_hidden_after_the_list_was_built_is_refused(
    monkeypatch: pytest.MonkeyPatch, via_call_tool: bool
) -> None:
    monkeypatch.setattr(llm_api.llm, "ToolResult", CoreToolResult, raising=False)
    session = fake_session(monkeypatch, tools=[tool_entry("ha_search", exposed=False)])
    if via_call_tool:
        tool = llm_api.HaMcpCallTool(_URL, {"ha_search"})
        tool_input = llm_api.llm.ToolInput(
            "ha_call_tool", {"name": "ha_search", "arguments": {}}
        )
    else:
        tool = llm_api.HaMcpTool("ha_search", "Search", {"_converted": {}}, _URL)
        tool_input = llm_api.llm.ToolInput("ha_search", {"query": "kitchen"})

    out = await tool.async_call(make_hass(), tool_input, llm_api.llm.LLMContext())

    session.call_tool.assert_not_awaited()
    assert out.error is True
    assert out.data["error"] == "Unknown tool 'ha_search'."
