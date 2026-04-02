"""
Custom tool creation via sandboxed code execution for Home Assistant MCP Server.

Provides an "escape hatch" that lets LLMs create custom one-off tools by
writing Python code when no existing tool covers the user's request.  Code
runs in pydantic-monty — a Rust-based sandboxed Python interpreter with no
filesystem or network access.  The only I/O channel is ``call_tool(name,
args)`` which delegates to the registered MCP tools.

Saved tools: LLMs can save frequently-used custom tools by name and re-run
them without re-synthesizing the code each time.

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

# In-memory cache for saved custom tools (session-scoped, not persistent)
_saved_tools: dict[str, dict[str, str]] = {}


async def _run_sandboxed_code(
    code: str,
    ctx: Context,
    settings: Any,
    Monty: Any,
    ResourceLimits: Any,
) -> Any:
    """Execute code in the pydantic-monty sandbox with call_tool bridge."""

    async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
        """Bridge: sandbox code → MCP tool execution."""
        try:
            result = await ctx.fastmcp.call_tool(tool_name, arguments)
        except ToolError as te:
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
                    texts.append(item.text)
                elif isinstance(item, str):
                    texts.append(item)
            combined = "\n".join(texts) if texts else str(result)
            try:
                return json.loads(combined)
            except (json.JSONDecodeError, TypeError):
                return combined

        return result

    m = Monty(code, script_name="ha_custom_tool.py")
    run_kwargs: dict[str, Any] = {
        "external_functions": {"call_tool": _call_tool},
        "limits": ResourceLimits(
            max_duration_secs=settings.code_mode_max_duration,
            max_memory=settings.code_mode_max_memory,
            max_recursion_depth=settings.code_mode_max_recursion,
        ),
    }

    # Monty.run_async() is the preferred path but may not be available on
    # all platforms (e.g., ARM wheels).  Fall back to the deprecated
    # module-level run_monty_async, then to sync run() in a thread.
    if hasattr(m, "run_async"):
        return await m.run_async(**run_kwargs)

    try:
        from pydantic_monty import run_monty_async

        return await run_monty_async(m, **run_kwargs)
    except ImportError:
        pass

    # Last resort: sync run() in a thread (async external functions
    # will NOT work — only simple code without call_tool).
    import asyncio

    return await asyncio.to_thread(m.run, **run_kwargs)


def register_code_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register custom tool creation tools.

    Skips registration entirely when ``ENABLE_CODE_MODE`` is ``False``
    (the default) so the tool never appears in the tool catalog.
    """
    settings = get_global_settings()
    if not settings.enable_code_mode:
        logger.debug(
            "Code mode disabled — skipping ha_create_custom_tool registration"
        )
        return

    try:
        from pydantic_monty import Monty, ResourceLimits
    except ImportError:
        logger.warning(
            "pydantic-monty is not installed — ha_create_custom_tool will be "
            "unavailable. Install with: pip install pydantic-monty"
        )
        return

    logger.info(
        "Code mode enabled — registering ha_create_custom_tool "
        "(max_duration=%.1fs, max_memory=%d bytes)",
        settings.code_mode_max_duration,
        settings.code_mode_max_memory,
    )

    # -----------------------------------------------------------------
    # ha_create_custom_tool — create and run a one-off custom tool
    # -----------------------------------------------------------------

    @mcp.tool(
        tags={"System"},
        annotations={
            "title": "Create Custom Tool",
            "destructiveHint": True,
            "idempotentHint": False,
            "readOnlyHint": False,
        },
    )
    @log_tool_usage
    async def ha_create_custom_tool(
        code: str,
        justification: str,
        ctx: Context,
        save_as: str | None = None,
    ) -> dict[str, Any]:
        """Create and run a one-off custom tool when no existing tool can accomplish the task.

        ⚠️  **LAST RESORT ONLY** — You MUST first search for and attempt to use
        existing tools before resorting to this.  Purpose-built tools have proper
        error handling, validation, and tested behavior.  Only use this tool when
        you have confirmed that no existing tool can accomplish the task.

        Write Python code that implements the custom tool logic.  The sandbox
        (pydantic-monty) is a minimal Python interpreter with:
        - No filesystem or network access
        - No third-party imports (only builtins, sys, re, json, datetime, typing, asyncio)
        - No class definitions or match statements
        - Configurable time and memory limits

        The **only** way to interact with Home Assistant from within the sandbox
        is via the ``call_tool`` function, which delegates to the registered
        MCP tools.

        Example — a custom tool to turn off all lights:
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
            code: Python code that implements the custom tool.  The result of the
                  last expression becomes the tool's return value.  Use ``await``
                  when calling ``call_tool`` since it is async.
            justification: A brief explanation of why no existing tool can
                           accomplish this task.  This is logged and may be
                           shown to the user for approval.
            save_as: Optional name to save this tool for later reuse via
                     ha_run_saved_tool.  Overwrites any existing tool with
                     the same name.
        """
        if not code or not code.strip():
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "code parameter must not be empty",
                    suggestions=[
                        "Provide Python code that implements the custom tool"
                    ],
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
            "ha_create_custom_tool invoked — justification: %s",
            justification[:200],
        )

        try:
            result = await _run_sandboxed_code(
                code, ctx, settings, Monty, ResourceLimits
            )
        except ToolError:
            raise
        except Exception as e:
            error_name = type(e).__name__
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

        response: dict[str, Any] = {
            "success": True,
            "data": result,
            "justification": justification,
        }

        if save_as:
            _saved_tools[save_as] = {
                "code": code,
                "justification": justification,
            }
            response["saved_as"] = save_as
            logger.info("Saved custom tool as '%s'", save_as)

        return response

    # -----------------------------------------------------------------
    # ha_run_saved_tool — re-run a previously saved custom tool
    # -----------------------------------------------------------------

    @mcp.tool(
        tags={"System"},
        annotations={
            "title": "Run Saved Custom Tool",
            "destructiveHint": True,
            "idempotentHint": False,
            "readOnlyHint": False,
        },
    )
    @log_tool_usage
    async def ha_run_saved_tool(
        name: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Re-run a previously saved custom tool by name.

        Custom tools saved via ha_create_custom_tool(save_as="name") can be
        re-run without re-synthesizing the code.  Saved tools persist for the
        current server session only (not across restarts).

        Use ha_list_saved_tools to see available saved tools.

        Args:
            name: Name of the saved tool to run.
        """
        if name not in _saved_tools:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"No saved tool named '{name}'",
                    suggestions=[
                        "Use ha_list_saved_tools to see available saved tools",
                        "Use ha_create_custom_tool with save_as to save a tool first",
                    ],
                    context={"tool_name": name},
                )
            )

        saved = _saved_tools[name]
        logger.info("Running saved tool '%s'", name)

        try:
            result = await _run_sandboxed_code(
                saved["code"], ctx, settings, Monty, ResourceLimits
            )
        except ToolError:
            raise
        except Exception as e:
            error_name = type(e).__name__
            exception_to_structured_error(
                e,
                context={
                    "sandbox_error_type": error_name,
                    "saved_tool_name": name,
                },
                suggestions=[
                    "The saved code may no longer work — check entity/tool availability",
                    "Use ha_create_custom_tool to create an updated version",
                ],
            )

        return {
            "success": True,
            "data": result,
            "saved_tool": name,
        }

    # -----------------------------------------------------------------
    # ha_list_saved_tools — list all saved custom tools
    # -----------------------------------------------------------------

    @mcp.tool(
        tags={"System"},
        annotations={
            "title": "List Saved Custom Tools",
            "readOnlyHint": True,
            "idempotentHint": True,
        },
    )
    @log_tool_usage
    async def ha_list_saved_tools() -> dict[str, Any]:
        """List all saved custom tools available for re-use.

        Shows the name, code, and original justification for each saved tool.
        Saved tools persist for the current server session only.
        """
        return {
            "success": True,
            "data": {
                name: {
                    "code": info["code"],
                    "justification": info["justification"],
                }
                for name, info in _saved_tools.items()
            },
            "count": len(_saved_tools),
        }
