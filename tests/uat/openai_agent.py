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
import logging
import re
import sys
from pathlib import Path

import openai
from fastmcp import Client as MCPClient
from mcp.types import Tool as MCPTool

# Allow `python tests/uat/openai_agent.py` (subprocess path from run_uat.py)
# to resolve the `uat` namespace package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from uat._logging import configure_cli_logging

DEFAULT_API_KEY = "no-key"
# Per-request timeout. Sized for slow local backends: a full DEFAULT_MAX_TOKENS
# generation on a small quantized model plus prefill on a large tool context can
# run several minutes, so a tight timeout would fail legitimate long turns. A
# single timed-out turn is no longer suite-fatal (see _run_test_prompt_inline's
# APITimeoutError handling), so erring high is cheap.
DEFAULT_TIMEOUT = 600
# Retries are for transient blips; retrying a slow local generation just re-runs
# the same slow work, so one retry is plenty and keeps a timed-out turn's worst
# case at 2x timeout rather than 3x.
DEFAULT_MAX_RETRIES = 1
DEFAULT_MAX_TOKENS = 8192
MAX_TOOL_LOOP_ITERATIONS = 20


_PYDANTIC_URL_LINE = re.compile(
    r"\s*For further information visit https://errors\.pydantic\.dev/\S+"
)


def _strip_pydantic_url(text: str) -> str:
    """Drop Pydantic's documentation URL footer from a stringified exception."""
    return _PYDANTIC_URL_LINE.sub("", text)


# llama-server reports a context overflow as HTTP 400 with type
# ``exceed_context_size_error`` and a message like:
#   request (27220 tokens) exceeds the available context size (24576 tokens)
_CTX_OVERFLOW_RE = re.compile(
    r"request \((\d+) tokens?\) exceeds the available context size \((\d+) tokens?\)"
)


class ContextWindowExceededError(RuntimeError):
    """Raised when the accumulated conversation overflows the backend's context.

    Carries the requested prompt size and the backend context limit (either may
    be ``None`` if the marker matched but the numbers couldn't be parsed). The
    inline path surfaces this as a hard failure so a verification-based PASS
    can't mask an agent that crashed mid-run.
    """

    def __init__(self, requested_tokens: int | None, context_size: int | None) -> None:
        self.requested_tokens = requested_tokens
        self.context_size = context_size
        detail = (
            f"request ({requested_tokens} tokens) exceeds context size "
            f"({context_size} tokens)"
            if requested_tokens is not None and context_size is not None
            else "context window exceeded"
        )
        super().__init__(detail)


def _parse_context_overflow(
    exc: openai.BadRequestError,
) -> tuple[int | None, int | None] | None:
    """Return (requested_tokens, context_size) if ``exc`` is a context overflow.

    Matches llama-server's ``exceed_context_size_error`` type or its
    "exceeds the available context size" message on the stringified exception
    (version-robust vs. structured fields). Returns ``None`` when the error is
    some other 400, or ``(None, None)`` when the marker is present but the token
    counts can't be parsed.
    """
    text = str(getattr(exc, "message", "") or "") + " " + str(exc)
    if (
        "exceed_context_size_error" not in text
        and "exceeds the available context size" not in text
    ):
        return None
    match = _CTX_OVERFLOW_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


logger = logging.getLogger("uat.openai_agent")


def mcp_tool_to_openai(tool: MCPTool) -> dict:
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


async def detect_model(client: openai.AsyncOpenAI) -> str:
    """Query /v1/models and return the first available model ID."""
    models = await client.models.list()
    if not models.data:
        raise RuntimeError("No models available at the API endpoint")
    model_id = models.data[0].id
    logger.info(f"Auto-detected model: {model_id}")
    return model_id


async def detect_quantization(base_url: str, model_id: str) -> str | None:
    """Best-effort lookup of a model's quantization (e.g. ``IQ2_M``).

    Reads LM Studio's native ``/api/v0/models`` endpoint, which exposes
    ``quantization`` that the OpenAI-compatible ``/v1/models`` does not.
    The same base model at full precision vs a heavy quant behaves very
    differently, so the quant belongs in the result record. Returns
    ``None`` for backends that don't expose it (Ollama, cloud) or on any
    error.
    """
    import httpx

    root = base_url.rsplit("/v1", 1)[0].rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            resp = await http.get(f"{root}/api/v0/models")
            resp.raise_for_status()
            for entry in resp.json().get("data", []):
                if entry.get("id") == model_id:
                    quant = entry.get("quantization")
                    if quant:
                        logger.info(f"Detected quantization: {quant}")
                    return quant
    except Exception as e:  # best-effort enrichment, never fatal
        logger.debug(f"Quantization detection skipped: {type(e).__name__}: {e}")
    return None


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
        help=(
            "Disable reasoning mode: prepends /no_think (original Qwen3) and "
            "sends enable_thinking=false chat-template kwarg (Qwen3.5/3.6, "
            "honored by vLLM/llama-server)"
        ),
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
    client: openai.AsyncOpenAI,
    model: str,
    messages: list[dict],
    tools: list[dict],
    mcp_client: MCPClient,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    no_think: bool = False,
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
    tokens_thoughts = 0
    tokens_first_input: int | None = None
    no_think_warned = False

    for _ in range(MAX_TOOL_LOOP_ITERATIONS):
        kwargs = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
        if no_think:
            # Qwen3.5/3.6 dropped the in-band ``/no_think`` soft switch (still
            # prepended below for older Qwen3) and moved reasoning control to
            # the ``enable_thinking`` chat-template kwarg. Pass it via
            # extra_body for servers that honor it (recent llama-server, vLLM).
            # Servers that don't either silently ignore it (reasoning stays on)
            # or reject the unknown field with HTTP 400; LM Studio's support
            # varies by version. The reasoning-token check below warns when the
            # model kept reasoning despite this request, so a no-op is visible.
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        try:
            response = await client.chat.completions.create(**kwargs)
        except openai.BadRequestError as e:
            overflow = _parse_context_overflow(e)
            if overflow is not None:
                requested, ctx = overflow
                raise ContextWindowExceededError(requested, ctx) from e
            raise
        num_turns += 1

        # Accumulate running token totals; also capture first-turn prompt size as
        # idle context baseline.  If a turn's usage is None (some local servers
        # omit it), tokens_first_input stays None until the first turn that does
        # report usage — so it reflects "first available" rather than "turn 1".
        reasoning_this_turn = 0
        if response.usage:
            prompt_toks = response.usage.prompt_tokens or 0
            tokens_input += prompt_toks
            tokens_output += response.usage.completion_tokens or 0
            # reasoning_tokens (when reported, e.g. LM Studio / o1-style usage) is
            # a subset of completion_tokens; track it so a run can show how much
            # output was reasoning. Absent or null on backends that don't report.
            details = getattr(response.usage, "completion_tokens_details", None)
            raw_reasoning = getattr(details, "reasoning_tokens", None)
            reasoning_this_turn = raw_reasoning if isinstance(raw_reasoning, int) else 0
            tokens_thoughts += reasoning_this_turn
            if tokens_first_input is None:
                tokens_first_input = prompt_toks

        if not response.choices:
            raise RuntimeError(
                f"API returned empty choices (model={model}). "
                "The model may have failed to generate a response."
            )
        choice = response.choices[0]
        message = choice.message

        # If --no-think was requested but the model still reasoned, the backend
        # didn't honor it (e.g. LM Studio can't map enable_thinking to some
        # Qwen3.6 GGUFs). Warn once so the no-op is visible instead of silently
        # paying full reasoning-decode cost. reasoning_tokens is the structured
        # signal; reasoning_content and an inline <think> block in content cover
        # servers that emit reasoning without a separate token detail.
        if no_think and not no_think_warned:
            still_reasoning = (
                reasoning_this_turn
                or getattr(message, "reasoning_content", None)
                or "<think>" in (message.content or "").lower()
            )
            if still_reasoning:
                detail = (
                    f"{reasoning_this_turn} reasoning tokens"
                    if reasoning_this_turn
                    else "reasoning in output, token count unavailable"
                )
                logger.warning(
                    "--no-think requested but model %s still produced reasoning "
                    "(%s); backend may not honor enable_thinking",
                    model,
                    detail,
                )
                no_think_warned = True

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
                "tokens_thoughts": tokens_thoughts,
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
                logger.info(malformed_line)
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
            logger.info(call_line)
            if tool_trace_sink is not None:
                tool_trace_sink.append(call_line.strip())

            try:
                result = await mcp_client.call_tool(tool_name, tool_args)
                result_text = extract_tool_result_text(result)
                total_success += 1
            except Exception as e:
                err_text = _strip_pydantic_url(str(e))
                result_text = f"Error: {err_text}"
                total_fail += 1
                # Server-side WARNING log already shows the failure details;
                # only record to the trace sink for test artifacts.
                if tool_trace_sink is not None:
                    tool_trace_sink.append(f"[tool] {tool_name} failed: {err_text}")

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
        "tokens_thoughts": tokens_thoughts,
        "cost_usd": 0,
    }


async def run_agent(
    client: openai.AsyncOpenAI, model: str, args: argparse.Namespace
) -> dict:
    """Connect to MCP server and run the tool-call loop."""
    # Read MCP config — same format as Claude's --mcp-config
    config = json.loads(Path(args.mcp_config).read_text())  # noqa: ASYNC240

    logger.info("Starting MCP server...")

    # fastmcp.Client accepts a config dict (same format as Claude's --mcp-config)
    async with MCPClient(config) as mcp_client:
        return await run_scenario_inline(
            client,
            mcp_client,
            model,
            args.prompt,
            max_tokens=args.max_tokens,
            no_think=args.no_think,
            max_tools=args.max_tools,
        )


async def fetch_openai_tools(
    mcp_client: MCPClient, max_tools: int | None = None
) -> list[dict]:
    """Fetch the MCP tool catalog and convert it to OpenAI function-calling format.

    Safe to call once per MCP client and reuse the result across multiple
    scenarios — the catalog doesn't change mid-session.
    """
    mcp_tools = await mcp_client.list_tools()
    if max_tools is not None:
        mcp_tools = mcp_tools[:max_tools]
    return [mcp_tool_to_openai(t) for t in mcp_tools]


async def run_scenario_inline(
    openai_client: openai.AsyncOpenAI,
    mcp_client: MCPClient,
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
        logger.info(f"Loaded {len(openai_tools)} MCP tools")

    agent_prompt = ("/no_think\n\n" + prompt) if no_think else prompt
    messages = [{"role": "user", "content": agent_prompt}]
    result = await tool_call_loop(
        openai_client,
        model,
        messages,
        openai_tools,
        mcp_client,
        max_tokens=max_tokens,
        no_think=no_think,
        tool_trace_sink=tool_trace_sink,
    )
    result["model"] = model
    return result


async def create_and_warm_openai_client(
    base_url: str,
    api_key: str = DEFAULT_API_KEY,
    timeout: int = DEFAULT_TIMEOUT,
    model: str | None = None,
) -> tuple[openai.AsyncOpenAI, str, str | None]:
    """Construct an OpenAI client, resolve the model, and warm it up once.

    Returns ``(client, model, quantization)``. Issues a 1-token completion
    to force backends like LM Studio and Ollama to load the model into VRAM
    before the first real request (otherwise that request can stall
    30-120s while the model is copied in). ``quantization`` is best-effort
    (``None`` when the backend doesn't expose it). Raises on failure.
    """
    client = openai.AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=DEFAULT_MAX_RETRIES,
    )
    resolved_model = model or await detect_model(client)
    quantization = await detect_quantization(base_url, resolved_model)
    suffix = f" ({quantization})" if quantization else ""
    logger.info(f"Using model: {resolved_model}{suffix}")
    logger.info("Warming up model (may take a minute if not loaded)...")
    await client.chat.completions.create(
        model=resolved_model,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=1,
    )
    logger.info("Model ready")
    return client, resolved_model, quantization


async def _main_async(args: argparse.Namespace) -> None:
    try:
        client, model, _quant = await create_and_warm_openai_client(
            base_url=args.base_url,
            api_key=args.api_key,
            timeout=args.timeout,
            model=args.model,
        )
    except openai.BadRequestError as e:
        logger.error(f"Model warmup failed (BadRequestError): {e}")
        sys.exit(1)
    except Exception:
        logger.exception("Model warmup failed")
        sys.exit(1)

    logger.info(f"MCP config: {args.mcp_config}")

    try:
        try:
            result = await run_agent(client, model, args)
        finally:
            await client.close()
    except Exception:
        logger.exception("Agent run failed")
        sys.exit(1)

    json.dump(result, sys.stdout, indent=2)
    print()
    if result.get("hit_iteration_limit"):
        logger.error("hit max tool-call iterations without a final response")
        sys.exit(1)


def main() -> None:
    configure_cli_logging()
    args = parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
