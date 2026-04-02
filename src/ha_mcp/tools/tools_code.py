"""
Sandboxed code execution tool for Home Assistant MCP Server.

Provides an "escape hatch" that lets LLMs write custom one-off Python code
when no existing tool covers the user's request.  Code runs in pydantic-monty
— a Rust-based sandboxed Python interpreter with no filesystem or network
access.  The only I/O channel is ``call_tool(name, args)`` which delegates to
the registered MCP tools.

**Requires** ``ENABLE_CODE_MODE=true`` (disabled by default).

See: https://github.com/homeassistant-ai/ha-mcp/issues/726
"""

import json
import logging
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context

from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error

logger = logging.getLogger(__name__)


def register_code_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register sandboxed code execution tools.

    Skips registration entirely when ``ENABLE_CODE_MODE`` is ``False``
    (the default) so the tool never appears in the tool catalog.
    """
    settings = get_global_settings()
    if not settings.enable_code_mode:
        logger.debug("Code mode disabled — skipping ha_execute_code registration")
        return

    try:
        from pydantic_monty import Monty, ResourceLimits
    except ImportError:
        logger.warning(
            "pydantic-monty is not installed — ha_execute_code will be unavailable. "
            "Install with: pip install pydantic-monty"
        )
        return

    logger.info(
        "Code mode enabled — registering ha_execute_code "
        "(max_duration=%.1fs, max_memory=%d bytes)",
        settings.code_mode_max_duration,
        settings.code_mode_max_memory,
    )

    @mcp.tool(
        tags={"System"},
        annotations={
            "title": "Execute Sandboxed Code",
            "destructiveHint": True,
            "idempotentHint": False,
            "readOnlyHint": False,
        },
    )
    @log_tool_usage
    async def ha_execute_code(
        code: str,
        justification: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Run one-off Python code in a secure sandbox to accomplish tasks that no existing tool can handle.

        ⚠️  **LAST RESORT ONLY** — You MUST first search for and attempt to use
        existing tools before resorting to this.  Purpose-built tools have proper
        error handling, validation, and tested behavior.  Only use this tool when
        you have confirmed that no existing tool can accomplish the task.

        The sandbox (pydantic-monty) is a minimal Python interpreter with:
        - No filesystem or network access
        - No third-party imports (only builtins, sys, re, json, datetime, typing, asyncio)
        - No class definitions or match statements
        - Configurable time and memory limits

        The **only** way to interact with Home Assistant from within the sandbox
        is via the ``call_tool`` function, which delegates to the registered
        MCP tools.

        Example usage inside the sandbox:
        ```python
        # Get all light entities
        result = await call_tool("ha_search_entities", {"query": "light", "domain_filter": "light"})
        entities = result.get("data", {}).get("entities", [])

        # Turn off each one
        for entity in entities:
            await call_tool("ha_call_service", {
                "domain": "light",
                "service": "turn_off",
                "entity_id": entity["entity_id"],
            })

        # Return a summary (the last expression is the tool's output)
        {"turned_off": len(entities)}
        ```

        Args:
            code: Python code to execute in the sandbox.  The result of the last
                  expression becomes the tool's return value.  Use ``await`` when
                  calling ``call_tool`` since it is async.
            justification: A brief explanation of why no existing tool can
                           accomplish this task.  This is logged and may be
                           shown to the user for approval.
        """
        if not code or not code.strip():
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "code parameter must not be empty",
                    suggestions=["Provide Python code to execute in the sandbox"],
                )
            )

        if not justification or not justification.strip():
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "justification parameter must not be empty",
                    suggestions=[
                        "Explain why no existing tool can accomplish this task"
                    ],
                )
            )

        logger.info(
            "ha_execute_code invoked — justification: %s",
            justification[:200],
        )

        # Build the call_tool bridge between sandbox and MCP tools
        async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
            """Bridge: sandbox code → MCP tool execution."""
            try:
                result = await ctx.fastmcp.call_tool(tool_name, arguments)
            except ToolError as te:
                # Return error as a dict so sandbox code can inspect it
                try:
                    return json.loads(str(te))
                except (json.JSONDecodeError, TypeError):
                    return {"success": False, "error": {"message": str(te)}}
            except Exception as exc:
                return {
                    "success": False,
                    "error": {"message": f"Tool call failed: {exc}"},
                }

            # FastMCP call_tool returns a list of content objects.
            # Extract text content and try to parse as JSON for usability.
            if isinstance(result, list):
                texts = []
                for item in result:
                    if hasattr(item, "text"):
                        texts.append(item.text)  # type: ignore[union-attr]
                    elif isinstance(item, str):
                        texts.append(item)
                combined = "\n".join(texts) if texts else str(result)
                try:
                    return json.loads(combined)
                except (json.JSONDecodeError, TypeError):
                    return combined

            # Already a basic type — pass through
            return result

        try:
            m = Monty(code, script_name="ha_execute_code.py")
            result = await m.run_async(
                external_functions={"call_tool": _call_tool},
                limits=ResourceLimits(
                    max_duration_secs=settings.code_mode_max_duration,
                    max_memory=settings.code_mode_max_memory,
                ),
            )
        except ToolError:
            raise
        except Exception as e:
            error_name = type(e).__name__
            # MontyRuntimeError and MontySyntaxError have useful messages
            exception_to_structured_error(
                e,
                context={
                    "sandbox_error_type": error_name,
                    "justification": justification[:200],
                },
                suggestions=[
                    "Check the Python code for syntax errors",
                    "Ensure call_tool calls use valid tool names and arguments",
                    "Remember: no imports, no classes, no match statements",
                    "Use 'await' when calling call_tool",
                ],
            )

        return {
            "success": True,
            "data": result,
            "justification": justification,
        }
