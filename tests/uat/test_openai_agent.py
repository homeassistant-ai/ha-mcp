"""Tests for the OpenAI UAT agent."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

AGENT_SCRIPT = Path(__file__).resolve().parent / "openai_agent.py"

# Import the module directly for unit testing internal functions
spec = importlib.util.spec_from_file_location("openai_agent", str(AGENT_SCRIPT))
openai_agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(openai_agent)


class TestArgParsing:
    """Test CLI argument parsing."""

    def test_missing_required_args_exits(self):
        """Script exits with error when required args are missing."""
        result = subprocess.run(
            [sys.executable, str(AGENT_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0

    def test_help_flag(self):
        """Script shows help text."""
        result = subprocess.run(
            [sys.executable, str(AGENT_SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--prompt" in result.stdout
        assert "--base-url" in result.stdout
        assert "--mcp-config" in result.stdout


class TestToolConversion:
    """Test MCP tool schema to OpenAI function format conversion."""

    def test_basic_tool_conversion(self):
        """MCP tool schema converts to OpenAI function format."""
        mcp_tool = MagicMock()
        mcp_tool.name = "search_entities"
        mcp_tool.description = "Search for entities"
        mcp_tool.inputSchema = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term"},
            },
            "required": ["query"],
        }

        result = openai_agent.mcp_tool_to_openai(mcp_tool)

        assert result == {
            "type": "function",
            "function": {
                "name": "search_entities",
                "description": "Search for entities",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search term"},
                    },
                    "required": ["query"],
                },
            },
        }

    def test_tool_with_no_parameters(self):
        """MCP tool with no inputSchema gets empty parameters."""
        mcp_tool = MagicMock()
        mcp_tool.name = "get_version"
        mcp_tool.description = "Get HA version"
        mcp_tool.inputSchema = None

        result = openai_agent.mcp_tool_to_openai(mcp_tool)

        assert result["function"]["parameters"] == {
            "type": "object",
            "properties": {},
        }


class TestToolCallLoop:
    """Test the agent tool-call loop logic."""

    @pytest.mark.asyncio
    async def test_direct_text_response(self):
        """LLM responds with text immediately (no tool calls)."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Here are the results."
        mock_response.choices[0].message.tool_calls = None
        mock_response.choices[0].finish_reason = "stop"
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
        mock_client.chat.completions.create.return_value = mock_response

        result = await openai_agent.tool_call_loop(
            client=mock_client,
            model="test-model",
            messages=[{"role": "user", "content": "Hello"}],
            tools=[],
            mcp_client=AsyncMock(),
        )

        assert result["result"] == "Here are the results."
        assert result["num_turns"] == 1
        assert result["tool_stats"]["totalCalls"] == 0
        assert result["tokens_input"] == 100
        assert result["tokens_output"] == 50
        assert result["cost_usd"] == 0

    @pytest.mark.asyncio
    async def test_single_tool_call(self):
        """LLM calls one tool, then responds with text."""
        mock_client = MagicMock()

        # First response: tool call
        tool_call = MagicMock()
        tool_call.id = "call_1"
        tool_call.function.name = "search_entities"
        tool_call.function.arguments = '{"query": "light"}'

        resp1 = MagicMock()
        resp1.choices = [MagicMock()]
        resp1.choices[0].message.content = None
        resp1.choices[0].message.tool_calls = [tool_call]
        resp1.choices[0].finish_reason = "tool_calls"
        resp1.usage = MagicMock(prompt_tokens=200, completion_tokens=30)

        # Second response: text
        resp2 = MagicMock()
        resp2.choices = [MagicMock()]
        resp2.choices[0].message.content = "Found 3 lights."
        resp2.choices[0].message.tool_calls = None
        resp2.choices[0].finish_reason = "stop"
        resp2.usage = MagicMock(prompt_tokens=300, completion_tokens=20)

        mock_client.chat.completions.create.side_effect = [resp1, resp2]

        # Mock MCP client
        mock_mcp = AsyncMock()
        mock_mcp.call_tool.return_value = MagicMock(
            content=[MagicMock(text="light.bed_light, light.ceiling, light.kitchen")]
        )

        result = await openai_agent.tool_call_loop(
            client=mock_client,
            model="test-model",
            messages=[{"role": "user", "content": "Find lights"}],
            tools=[{"type": "function", "function": {"name": "search_entities"}}],
            mcp_client=mock_mcp,
        )

        assert result["result"] == "Found 3 lights."
        assert result["num_turns"] == 2
        assert result["tool_stats"]["totalCalls"] == 1
        assert result["tool_stats"]["totalSuccess"] == 1
        assert result["tool_stats"]["totalFail"] == 0
        assert result["tokens_input"] == 500
        assert result["tokens_output"] == 50

    @pytest.mark.asyncio
    async def test_tool_call_failure_counted(self):
        """Failed MCP tool calls are counted in stats."""
        mock_client = MagicMock()

        # First response: tool call
        tool_call = MagicMock()
        tool_call.id = "call_1"
        tool_call.function.name = "bad_tool"
        tool_call.function.arguments = "{}"

        resp1 = MagicMock()
        resp1.choices = [MagicMock()]
        resp1.choices[0].message.content = None
        resp1.choices[0].message.tool_calls = [tool_call]
        resp1.usage = MagicMock(prompt_tokens=100, completion_tokens=10)

        # Second response: text
        resp2 = MagicMock()
        resp2.choices = [MagicMock()]
        resp2.choices[0].message.content = "Tool failed."
        resp2.choices[0].message.tool_calls = None
        resp2.usage = MagicMock(prompt_tokens=200, completion_tokens=10)

        mock_client.chat.completions.create.side_effect = [resp1, resp2]

        mock_mcp = AsyncMock()
        mock_mcp.call_tool.side_effect = RuntimeError("Tool not found")

        result = await openai_agent.tool_call_loop(
            client=mock_client,
            model="test-model",
            messages=[{"role": "user", "content": "Try it"}],
            tools=[],
            mcp_client=mock_mcp,
        )

        assert result["tool_stats"]["totalCalls"] == 1
        assert result["tool_stats"]["totalFail"] == 1
        assert result["tool_stats"]["totalSuccess"] == 0

    @pytest.mark.asyncio
    async def test_malformed_tool_arguments_reported_as_error(self):
        """Malformed JSON arguments are reported as errors, not silently ignored."""
        mock_client = MagicMock()

        tool_call = MagicMock()
        tool_call.id = "call_bad"
        tool_call.function.name = "some_tool"
        tool_call.function.arguments = "not valid json{{"

        resp1 = MagicMock()
        resp1.choices = [MagicMock()]
        resp1.choices[0].message.content = None
        resp1.choices[0].message.tool_calls = [tool_call]
        resp1.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

        resp2 = MagicMock()
        resp2.choices = [MagicMock()]
        resp2.choices[0].message.content = "Done."
        resp2.choices[0].message.tool_calls = None
        resp2.usage = MagicMock(prompt_tokens=20, completion_tokens=5)

        mock_client.chat.completions.create.side_effect = [resp1, resp2]

        mock_mcp = AsyncMock()

        result = await openai_agent.tool_call_loop(
            client=mock_client,
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            tools=[],
            mcp_client=mock_mcp,
        )

        assert result["result"] == "Done."
        assert result["tool_stats"]["totalCalls"] == 1
        assert result["tool_stats"]["totalFail"] == 1
        assert result["tool_stats"]["totalSuccess"] == 0
        # MCP tool should NOT have been called with bad args
        mock_mcp.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_iterations_reached(self):
        """Loop terminates after MAX_TOOL_LOOP_ITERATIONS with tool calls."""
        mock_client = MagicMock()

        tool_call = MagicMock()
        tool_call.id = "call_loop"
        tool_call.function.name = "some_tool"
        tool_call.function.arguments = "{}"

        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = None
        resp.choices[0].message.tool_calls = [tool_call]
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

        mock_client.chat.completions.create.return_value = resp

        mock_mcp = AsyncMock()
        mock_mcp.call_tool.return_value = MagicMock(content=[MagicMock(text="ok")])

        result = await openai_agent.tool_call_loop(
            client=mock_client,
            model="test-model",
            messages=[{"role": "user", "content": "loop forever"}],
            tools=[],
            mcp_client=mock_mcp,
        )

        assert result["result"] == "Max tool-call iterations reached"
        assert result["num_turns"] == openai_agent.MAX_TOOL_LOOP_ITERATIONS
        assert (
            result["tool_stats"]["totalCalls"] == openai_agent.MAX_TOOL_LOOP_ITERATIONS
        )

    @pytest.mark.asyncio
    async def test_null_usage_handled(self):
        """Response with usage=None doesn't crash token counting."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Result."
        mock_response.choices[0].message.tool_calls = None
        mock_response.usage = None
        mock_client.chat.completions.create.return_value = mock_response

        result = await openai_agent.tool_call_loop(
            client=mock_client,
            model="m",
            messages=[{"role": "user", "content": "test"}],
            tools=[],
            mcp_client=AsyncMock(),
        )

        assert result["tokens_input"] == 0
        assert result["tokens_output"] == 0
        assert result["result"] == "Result."

    @pytest.mark.asyncio
    async def test_empty_choices_raises(self):
        """API returning empty choices produces a clear error."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = []
        mock_response.usage = None
        mock_client.chat.completions.create.return_value = mock_response

        with pytest.raises(RuntimeError, match="empty choices"):
            await openai_agent.tool_call_loop(
                client=mock_client,
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                tools=[],
                mcp_client=AsyncMock(),
            )


class TestExtractToolResultText:
    """Test MCP tool result text extraction."""

    def test_text_content_blocks(self):
        """Multiple text blocks are joined with newlines."""
        result = MagicMock(content=[MagicMock(text="line1"), MagicMock(text="line2")])
        assert openai_agent.extract_tool_result_text(result) == "line1\nline2"

    def test_non_text_content_falls_back_to_str(self):
        """Non-text content blocks fall back to str()."""

        class ImageBlock:
            def __str__(self):
                return "raw-block"

        result = MagicMock(content=[ImageBlock()])
        assert openai_agent.extract_tool_result_text(result) == "raw-block"

    def test_no_content_falls_back_to_str(self):
        """Result with no content attribute falls back to str()."""

        class BareResult:
            def __str__(self):
                return "fallback"

        assert openai_agent.extract_tool_result_text(BareResult()) == "fallback"


class TestDetectModel:
    """Test model auto-detection."""

    def test_returns_first_model_id(self):
        """Returns the first model ID from the API."""
        client = MagicMock()
        model = MagicMock()
        model.id = "llama-3.1-8b"
        client.models.list.return_value = MagicMock(data=[model])
        assert openai_agent.detect_model(client) == "llama-3.1-8b"

    def test_raises_when_no_models(self):
        """Raises RuntimeError when no models are available."""
        client = MagicMock()
        client.models.list.return_value = MagicMock(data=[])
        with pytest.raises(RuntimeError, match="No models available"):
            openai_agent.detect_model(client)
