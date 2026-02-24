#!/usr/bin/env python3
"""
OpenAI-compatible UAT agent.

Bridges any OpenAI-compatible LLM (LM Studio, Ollama, vLLM, etc.) with an MCP
server for Bot Acceptance Testing. Invoked as a subprocess by run_uat.py.

Usage:
    python tests/uat/openai_agent.py \\
      --prompt "Search for light entities." \\
      --mcp-config /tmp/mcp_config.json \\
      --base-url http://localhost:1234/v1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import openai

DEFAULT_API_KEY = "no-key"
DEFAULT_TIMEOUT = 120
MAX_TOOL_LOOP_ITERATIONS = 20


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def mcp_tool_to_openai(tool) -> dict:
    """Convert an MCP tool definition to OpenAI function-calling format."""
    parameters = tool.inputSchema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": parameters,
        },
    }


def detect_model(client: openai.OpenAI) -> str:
    """Query /v1/models and return the first available model ID."""
    models = client.models.list()
    if not models.data:
        raise RuntimeError("No models available at the API endpoint")
    model_id = models.data[0].id
    log(f"Auto-detected model: {model_id}")
    return model_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenAI-compatible UAT agent for MCP testing",
    )
    parser.add_argument("--prompt", required=True, help="Prompt to send to the LLM")
    parser.add_argument("--mcp-config", required=True, help="Path to MCP config JSON")
    parser.add_argument(
        "--base-url", required=True, help="OpenAI-compatible API base URL"
    )
    parser.add_argument("--model", help="Model name (auto-detected if omitted)")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key")
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout in seconds"
    )
    parser.add_argument(
        "--max-tools",
        type=int,
        default=None,
        help="Limit MCP tools passed to the model (useful for small context windows)",
    )
    return parser.parse_args()


def extract_tool_result_text(result) -> str:
    """Extract text from an MCP tool result."""
    if hasattr(result, "content") and result.content:
        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(result)


async def tool_call_loop(
    client: openai.OpenAI,
    model: str,
    messages: list[dict],
    tools: list[dict],
    mcp_client,
) -> dict:
    """Run the LLM tool-call loop until a final text response or iteration limit."""
    num_turns = 0
    total_calls = 0
    total_success = 0
    total_fail = 0
    tokens_input = 0
    tokens_output = 0

    for _ in range(MAX_TOOL_LOOP_ITERATIONS):
        kwargs = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools

        response = client.chat.completions.create(**kwargs)
        num_turns += 1

        # Accumulate token usage
        if response.usage:
            tokens_input += response.usage.prompt_tokens or 0
            tokens_output += response.usage.completion_tokens or 0

        if not response.choices:
            raise RuntimeError(
                f"API returned empty choices (model={model}). "
                "The model may have failed to generate a response."
            )
        choice = response.choices[0]
        message = choice.message

        # No tool calls — we have a final response
        if not message.tool_calls:
            return {
                "result": message.content or "",
                "num_turns": num_turns,
                "tool_stats": {
                    "totalCalls": total_calls,
                    "totalSuccess": total_success,
                    "totalFail": total_fail,
                },
                "tokens_input": tokens_input,
                "tokens_output": tokens_output,
                "cost_usd": 0,
            }

        # Append assistant message with tool calls to history
        messages.append(
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ],
            }
        )

        for tc in message.tool_calls:
            total_calls += 1
            tool_name = tc.function.name
            try:
                tool_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError as e:
                log(
                    f"  [tool] {tool_name}: malformed arguments: "
                    f"{tc.function.arguments!r}"
                )
                total_fail += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Error: Invalid JSON in tool arguments: {e}",
                    }
                )
                continue

            log(f"  [tool] {tool_name}({tool_args})")

            try:
                result = await mcp_client.call_tool(tool_name, tool_args)
                result_text = extract_tool_result_text(result)
                total_success += 1
            except Exception as e:
                result_text = f"Error: {str(e)}"
                total_fail += 1
                log(f"  [tool] {tool_name} failed: {e}")

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                }
            )

    # Max iterations reached
    return {
        "result": "Max tool-call iterations reached",
        "num_turns": num_turns,
        "tool_stats": {
            "totalCalls": total_calls,
            "totalSuccess": total_success,
            "totalFail": total_fail,
        },
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "cost_usd": 0,
    }


async def run_agent(
    client: openai.OpenAI, model: str, args: argparse.Namespace
) -> dict:
    """Connect to MCP server and run the tool-call loop."""
    from fastmcp import Client

    # Read MCP config — same format as Claude's --mcp-config
    config = json.loads(Path(args.mcp_config).read_text())  # noqa: ASYNC240

    log("Starting MCP server...")

    # fastmcp.Client accepts a config dict (same format as Claude's --mcp-config)
    async with Client(config) as mcp_client:
        mcp_tools = await mcp_client.list_tools()
        if args.max_tools is not None:
            mcp_tools = mcp_tools[: args.max_tools]
        openai_tools = [mcp_tool_to_openai(t) for t in mcp_tools]
        log(f"Loaded {len(openai_tools)} MCP tools")

        messages = [{"role": "user", "content": args.prompt}]
        result = await tool_call_loop(client, model, messages, openai_tools, mcp_client)
        result["model"] = model
        return result


def main() -> None:
    args = parse_args()

    try:
        client = openai.OpenAI(
            base_url=args.base_url,
            api_key=args.api_key,
            timeout=args.timeout,
        )
        model = args.model or detect_model(client)
    except Exception as e:
        log(f"ERROR: Failed to connect to API at {args.base_url}: {e}")
        sys.exit(1)

    log(f"Using model: {model}")
    log(f"MCP config: {args.mcp_config}")

    try:
        result = asyncio.run(run_agent(client, model, args))
    except Exception as e:
        log(f"ERROR ({type(e).__name__}): {e}")
        sys.exit(1)

    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
