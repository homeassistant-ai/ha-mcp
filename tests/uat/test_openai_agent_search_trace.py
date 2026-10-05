"""Tests for the search-result lines in the OpenAI UAT agent's tool trace."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp import Client as MCPClient
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.transforms import CategorizedSearchTransform

AGENT_SCRIPT = Path(__file__).resolve().parent / "openai_agent.py"

spec = importlib.util.spec_from_file_location("openai_agent", str(AGENT_SCRIPT))
openai_agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(openai_agent)


def _llm_calling_search(query: str) -> MagicMock:
    """An LLM that calls ``ha_search_tools`` once, then answers."""
    tool_call = MagicMock()
    tool_call.id = "call_1"
    tool_call.function.name = "ha_search_tools"
    tool_call.function.arguments = json.dumps({"query": query})

    resp1 = MagicMock()
    resp1.choices = [MagicMock()]
    resp1.choices[0].message.content = None
    resp1.choices[0].message.tool_calls = [tool_call]
    resp1.usage = MagicMock(prompt_tokens=100, completion_tokens=10)

    resp2 = MagicMock()
    resp2.choices = [MagicMock()]
    resp2.choices[0].message.content = "Done."
    resp2.choices[0].message.tool_calls = None
    resp2.usage = MagicMock(prompt_tokens=200, completion_tokens=10)

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=[resp1, resp2])
    return client


class TestSearchResultsInTrace:
    """A search call's trace line shows only the query. Without the returned
    tool names, a BAT run cannot tell a search that missed the tool from a
    model that picked the wrong one."""

    @pytest.fixture
    def search_server(self) -> FastMCP:
        """Two tools behind the real search transform, no Home Assistant."""
        mcp = FastMCP("search-trace")

        @mcp.tool(annotations={"readOnlyHint": True})
        def ha_get_history(entity_id: str) -> str:
            """Get the history of an entity."""
            return ""

        @mcp.tool(annotations={"readOnlyHint": True})
        def ha_config_get_label(label_id: str) -> str:
            """Get a label."""
            return ""

        mcp.add_transform(CategorizedSearchTransform(max_results=5))
        return mcp

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("query", "returned"),
        [
            ("history", "['ha_get_history']"),
            # A search that finds nothing is the miss the trace exists to show.
            ("thermostat", "[]"),
        ],
    )
    async def test_trace_lists_the_tools_a_search_returned(
        self, search_server: FastMCP, caplog, query: str, returned: str
    ):
        """The names reach both the trace sink (results file) and the log
        (console), so either one is enough to compute a search hit rate."""
        trace: list[str] = []
        with caplog.at_level("INFO"):
            async with MCPClient(search_server) as mcp_client:
                await openai_agent.tool_call_loop(
                    client=_llm_calling_search(query),
                    model="test-model",
                    messages=[{"role": "user", "content": "test"}],
                    tools=[],
                    mcp_client=mcp_client,
                    tool_trace_sink=trace,
                )

        returned_line = f"[tool] ha_search_tools returned: {returned}"
        assert trace == [
            f"[tool] ha_search_tools({{'query': '{query}'}})",
            returned_line,
        ]
        assert any(returned_line in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_trace_flags_a_search_result_it_cannot_read(self):
        """If the search result stops being a list of tool entries, the trace
        must say so. A silent gap reads as "no results logged" and hides that
        the hit rate is being computed from nothing."""
        mock_mcp = AsyncMock()
        mock_mcp.call_tool.return_value = MagicMock(
            content=[MagicMock(text="## ha_get_history")],
            data="## ha_get_history",
        )
        trace: list[str] = []

        await openai_agent.tool_call_loop(
            client=_llm_calling_search("history"),
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            tools=[],
            mcp_client=mock_mcp,
            tool_trace_sink=trace,
        )

        assert trace[1] == "[tool] ha_search_tools returned an unrecognised result"
