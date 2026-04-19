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
DEFAULT_MAX_TOKENS = 8192
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
    parser.add_argument(
        "--no-think",
        action="store_true",
        help="Prepend /no_think to disable reasoning mode (qwen3 and compatible models)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"Max output tokens per completion (default: {DEFAULT_MAX_TOKENS})",
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
    max_tokens: int = DEFAULT_MAX_TOKENS,
    tool_trace_sink: list[str] | None = None,
) -> dict:
    """Run the LLM tool-call loop until a final text response or iteration limit.

    If ``tool_trace_sink`` is provided, every tool invocation (including
    malformed-arguments and call failures) is appended as a stripped copy
    of the corresponding ``[tool]`` stderr line, so callers on the inline
    (non-subprocess) path can collect the trace without parsing stderr.
    """
    num_turns = 0
    total_calls = 0
    total_success = 0
    total_fail = 0
    tokens_input = 0
    tokens_output = 0
    tokens_first_input: int | None = None

    for _ in range(MAX_TOOL_LOOP_ITERATIONS):
        kwargs = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools

        response = client.chat.completions.create(**kwargs)
        num_turns += 1

        # Accumulate running token totals; also capture first-turn prompt size as
        # idle context baseline.  If a turn's usage is None (some local servers
        # omit it), tokens_first_input stays None until the first turn that does
        # report usage — so it reflects "first available" rather than "turn 1".
        if response.usage:
            prompt_toks = response.usage.prompt_tokens or 0
            tokens_input += prompt_toks
            tokens_output += response.usage.completion_tokens or 0
            if tokens_first_input is None:
                tokens_first_input = prompt_toks

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
                "tokens_first_input": tokens_first_input,
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
                malformed_line = (
                    f"  [tool] {tool_name}: malformed arguments: "
                    f"{tc.function.arguments!r}"
                )
                log(malformed_line)
                if tool_trace_sink is not None:
                    tool_trace_sink.append(malformed_line.strip())
                total_fail += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Error: Invalid JSON in tool arguments: {e}",
                    }
                )
                continue

            call_line = f"  [tool] {tool_name}({tool_args})"
            log(call_line)
            if tool_trace_sink is not None:
                tool_trace_sink.append(call_line.strip())

            try:
                result = await mcp_client.call_tool(tool_name, tool_args)
                result_text = extract_tool_result_text(result)
                total_success += 1
            except Exception as e:
                result_text = f"Error: {str(e)}"
                total_fail += 1
                fail_line = f"  [tool] {tool_name} failed: {e}"
                log(fail_line)
                if tool_trace_sink is not None:
                    tool_trace_sink.append(fail_line.strip())

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                }
            )

    # Max iterations reached without a final message. Flag it so callers can
    # surface it as a test failure — otherwise a model stuck in a tool-call
    # loop looks identical to a clean run.
    return {
        "result": "Max tool-call iterations reached",
        "hit_iteration_limit": True,
        "num_turns": num_turns,
        "tool_stats": {
            "totalCalls": total_calls,
            "totalSuccess": total_success,
            "totalFail": total_fail,
        },
        "tokens_input": tokens_input,
        "tokens_first_input": tokens_first_input,
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
        return await run_scenario_inline(
            client,
            mcp_client,
            model,
            args.prompt,
            max_tokens=args.max_tokens,
            no_think=args.no_think,
            max_tools=args.max_tools,
        )


async def fetch_openai_tools(mcp_client, max_tools: int | None = None) -> list[dict]:
    """Fetch the MCP tool catalog and convert it to OpenAI function-calling format.

    Safe to call once per MCP client and reuse the result across multiple
    scenarios — the catalog doesn't change mid-session.
    """
    mcp_tools = await mcp_client.list_tools()
    if max_tools is not None:
        mcp_tools = mcp_tools[:max_tools]
    return [mcp_tool_to_openai(t) for t in mcp_tools]


async def run_scenario_inline(
    openai_client: openai.OpenAI,
    mcp_client,
    model: str,
    prompt: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    no_think: bool = False,
    max_tools: int | None = None,
    tool_trace_sink: list[str] | None = None,
    openai_tools: list[dict] | None = None,
) -> dict:
    """Run one scenario against an already-connected MCP client.

    Returns the dict from ``tool_call_loop`` with a ``model`` key added.
    If ``openai_tools`` is supplied, ``max_tools`` is ignored — the caller
    is responsible for any truncation.
    """
    if openai_tools is None:
        openai_tools = await fetch_openai_tools(mcp_client, max_tools=max_tools)
        log(f"Loaded {len(openai_tools)} MCP tools")

    agent_prompt = ("/no_think\n\n" + prompt) if no_think else prompt
    messages = [{"role": "user", "content": agent_prompt}]
    result = await tool_call_loop(
        openai_client,
        model,
        messages,
        openai_tools,
        mcp_client,
        max_tokens=max_tokens,
        tool_trace_sink=tool_trace_sink,
    )
    result["model"] = model
    return result


def create_and_warm_openai_client(
    base_url: str,
    api_key: str = DEFAULT_API_KEY,
    timeout: int = DEFAULT_TIMEOUT,
    model: str | None = None,
) -> tuple[openai.OpenAI, str]:
    """Construct an OpenAI client, resolve the model, and warm it up once.

    Returns ``(client, model)``. Issues a 1-token completion to force
    backends like LM Studio and Ollama to load the model into VRAM
    before the first real request (otherwise that request can stall
    30-120s while the model is copied in). Raises on failure.
    """
    client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
    resolved_model = model or detect_model(client)
    log(f"Using model: {resolved_model}")
    log("Warming up model (may take a minute if not loaded)...")
    client.chat.completions.create(
        model=resolved_model,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=1,
    )
    log("Model ready")
    return client, resolved_model


def main() -> None:
    args = parse_args()

    try:
        client, model = create_and_warm_openai_client(
            base_url=args.base_url,
            api_key=args.api_key,
            timeout=args.timeout,
            model=args.model,
        )
    except openai.BadRequestError as e:
        log(f"ERROR: Model warmup failed (BadRequestError): {e}")
        sys.exit(1)
    except Exception as e:
        log(f"ERROR ({type(e).__name__}): {e}")
        sys.exit(1)

    log(f"MCP config: {args.mcp_config}")

    try:
        result = asyncio.run(run_agent(client, model, args))
    except Exception as e:
        log(f"ERROR ({type(e).__name__}): {e}")
        sys.exit(1)

    json.dump(result, sys.stdout, indent=2)
    print()
    if result.get("hit_iteration_limit"):
        log("ERROR: hit max tool-call iterations without a final response")
        sys.exit(1)


if __name__ == "__main__":
    main()
