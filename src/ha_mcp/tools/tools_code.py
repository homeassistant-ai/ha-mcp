"""
Sandboxed custom tool for Home Assistant MCP Server.

Provides an "escape hatch" (ha_manage_custom_tool) that lets LLMs write and run
custom Python code when no existing tool covers the request, with optional
save/reuse and listing of saved tools.  Code runs in pydantic-monty — a
Rust-based sandboxed Python interpreter with no filesystem or network access.
The only I/O channel is ``call_tool(name, args)`` which delegates to the
registered MCP tools.

**Requires** ``ENABLE_CODE_MODE=true`` (disabled by default).

See: https://github.com/homeassistant-ai/ha-mcp/issues/726
"""

import json
import logging
import re
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context

from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error

logger = logging.getLogger(__name__)

# In-memory cache for saved custom tools (session-scoped, not persistent).
# WARNING: This is shared across all clients in the same server process.
# In multi-user modes (OAuth, HTTP), one user's saved tools are visible to
# all other users.  Scope to per-session/user before multi-user support.
_saved_tools: dict[str, dict[str, str]] = {}

# Tools that sandbox code must not call (prevents recursive self-invocation)
_BLOCKED_TOOLS = frozenset({"ha_manage_custom_tool"})

# Validation for save_as names
_SAVE_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _extract_tool_result(result: Any) -> Any:
    """Convert a FastMCP ToolResult to basic Python types for the sandbox.

    FastMCP call_tool may return a ToolResult, a list of content objects,
    or a basic type.  Monty can only handle basic Python types (str, int,
    float, bool, list, dict, None), so we must serialize.
    """
    # Already a basic type — pass through
    if isinstance(result, (str, int, float, bool, type(None), dict)):
        return result

    # ToolResult or similar: extract content list
    content = None
    if hasattr(result, "content"):
        content = result.content
    elif isinstance(result, list):
        content = result

    if content:
        texts = []
        for item in content:
            if hasattr(item, "text"):
                texts.append(item.text)
            elif isinstance(item, str):
                texts.append(item)
        if texts:
            combined = "\n".join(texts)
            try:
                return json.loads(combined)
            except (json.JSONDecodeError, TypeError):
                return combined

    # Fallback: string representation
    return str(result)


async def _run_sandboxed_code(
    code: str,
    ctx: Context,
    client: Any,
    settings: Any,
    Monty: Any,
    ResourceLimits: Any,
) -> Any:
    """Execute code in the pydantic-monty sandbox.

    External functions available to sandbox code:
    - api_get(endpoint) — GET request to HA REST API (primary escape hatch)
    - api_post(endpoint, data) — POST request to HA REST API
    - call_tool(name, args) — call a registered MCP tool (for existing tools)
    """
    call_count = 0

    def _normalize_endpoint(endpoint: str) -> str:
        """Normalize endpoint to be relative to the httpx base URL (/api).

        Leading slashes cause httpx to treat the path as absolute from the
        host root, bypassing the /api base path.  Strip them (and any
        accidental /api/ prefix) so all three forms work identically:
        "events", "/events", "/api/events" → "events".
        """
        ep = endpoint.lstrip("/")
        if ep.startswith("api/"):
            ep = ep[4:]
        return ep

    async def _api_get(endpoint: str) -> Any:
        """GET request to Home Assistant REST API."""
        nonlocal call_count
        call_count += 1
        if call_count > settings.code_mode_max_invocations:
            return {"error": f"API call limit exceeded ({settings.code_mode_max_invocations})"}
        try:
            response = await client.httpx_client.request("GET", _normalize_endpoint(endpoint))
            try:
                return response.json()
            except json.JSONDecodeError:
                return response.text
        except Exception as exc:
            return {"error": str(exc)[:200]}

    async def _api_post(endpoint: str, data: dict[str, Any] | None = None) -> Any:
        """POST request to Home Assistant REST API."""
        nonlocal call_count
        call_count += 1
        if call_count > settings.code_mode_max_invocations:
            return {"error": f"API call limit exceeded ({settings.code_mode_max_invocations})"}
        try:
            post_kwargs: dict[str, Any] = {}
            if data is not None:
                post_kwargs["json"] = data
            response = await client.httpx_client.request("POST", _normalize_endpoint(endpoint), **post_kwargs)
            try:
                return response.json()
            except json.JSONDecodeError:
                return response.text
        except Exception as exc:
            return {"error": str(exc)[:200]}

    async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
        """Bridge: sandbox code → MCP tool execution."""
        nonlocal call_count

        if tool_name in _BLOCKED_TOOLS:
            return {
                "success": False,
                "error": {
                    "message": f"Tool '{tool_name}' cannot be called from sandbox code"
                },
            }

        call_count += 1
        if call_count > settings.code_mode_max_invocations:
            return {
                "success": False,
                "error": {
                    "message": (
                        f"call_tool limit exceeded ({settings.code_mode_max_invocations} "
                        f"calls per execution)"
                    )
                },
            }

        try:
            result = await ctx.fastmcp.call_tool(tool_name, arguments)
        except ToolError as te:
            try:
                return json.loads(str(te))
            except (json.JSONDecodeError, TypeError):
                return {"success": False, "error": {"message": str(te)}}
        except Exception as exc:
            msg = str(exc)[:200]
            return {
                "success": False,
                "error": {"message": f"Tool call failed: {msg}"},
            }

        # FastMCP call_tool returns a ToolResult or list of content objects.
        # Monty can only handle basic Python types, so serialize everything.
        return _extract_tool_result(result)

    m = Monty(code, script_name="ha_manage_custom_tool.py")
    run_kwargs: dict[str, Any] = {
        "external_functions": {
            "api_get": _api_get,
            "api_post": _api_post,
            "call_tool": _call_tool,
        },
        "limits": ResourceLimits(
            max_duration_secs=settings.code_mode_max_duration,
            max_memory=settings.code_mode_max_memory,
            max_recursion_depth=settings.code_mode_max_recursion,
        ),
    }

    # Monty.run_async() is the preferred path but may not be available on
    # all platforms (e.g., ARM wheels).  Fall back to the deprecated
    # module-level run_monty_async.
    if hasattr(m, "run_async"):
        return await m.run_async(**run_kwargs)

    try:
        from pydantic_monty import run_monty_async

        return await run_monty_async(m, **run_kwargs)
    except ImportError:
        pass

    # No async execution path available — fail explicitly rather than
    # silently breaking call_tool with a sync fallback.
    raise RuntimeError(
        "pydantic-monty async execution is not available on this platform. "
        "ha_manage_custom_tool requires Monty.run_async() or run_monty_async()."
    )


def register_code_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the ha_manage_custom_tool sandboxed code execution tool.

    Skips registration entirely when ``ENABLE_CODE_MODE`` is ``False``
    (the default) so the tool never appears in the tool catalog.
    """
    settings = get_global_settings()
    if not settings.enable_code_mode:
        logger.debug("Code mode disabled — skipping ha_manage_custom_tool registration")
        return

    try:
        from pydantic_monty import Monty, ResourceLimits
    except ImportError:
        logger.warning(
            "pydantic-monty is not installed — ha_manage_custom_tool will be "
            "unavailable. Install with: pip install pydantic-monty"
        )
        return

    logger.info(
        "Code mode enabled — registering ha_manage_custom_tool "
        "(max_duration=%.1fs, max_memory=%d bytes)",
        settings.code_mode_max_duration,
        settings.code_mode_max_memory,
    )

    @mcp.tool(
        tags={"System"},
        annotations={
            "title": "Custom Tool",
            "destructiveHint": True,
            "idempotentHint": False,
            "readOnlyHint": False,
        },
    )
    @log_tool_usage
    async def ha_manage_custom_tool(
        ctx: Context,
        code: str | None = None,
        justification: str | None = None,
        save_as: str | None = None,
        run_saved: str | None = None,
        list_saved: bool = False,
    ) -> dict[str, Any]:
        """Create and run a custom tool in a sandbox, or manage saved custom tools.

        ⚠️  **LAST RESORT** — search for existing tools first.

        **Modes** (mutually exclusive):
        - Provide ``code`` + ``justification`` to execute custom code
        - Set ``run_saved`` to re-run a previously saved tool by name
        - Set ``list_saved=True`` to list all saved tools

        **Available functions in sandbox:**
        - ``api_get(endpoint)`` — GET request to HA REST API (primary escape hatch)
        - ``api_post(endpoint, data)`` — POST request to HA REST API
        - ``call_tool(name, args)`` — call a registered MCP tool

        Use ``api_get``/``api_post`` for HA operations not covered by existing
        tools.  Use ``call_tool`` when an existing tool already does what you need.

        Example — check repairs (no built-in tool for this):
        ```python
        repairs = await api_get("/repairs/issues")
        repairs
        ```

        Example — chain existing tools:
        ```python
        result = await call_tool("ha_search_entities", {"query": "light", "limit": 5})
        data = result.get("data", result)
        lights = data.get("results", [])
        for e in lights:
            await call_tool("ha_call_service", {
                "domain": "light", "service": "turn_off",
                "entity_id": e["entity_id"]})
        {"turned_off": len(lights)}
        ```

        Args:
            code: Python code to execute.  Last expression is the return value.
            justification: Why no existing tool works (required with code).
            save_as: Save the tool under this name for reuse (alphanumeric/underscores, max 64 chars).
            run_saved: Name of a previously saved tool to re-run.
            list_saved: Set True to list all saved tools.
        """
        # --- Mode: list saved tools ---
        if list_saved:
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

        # --- Mode: run saved tool ---
        if run_saved is not None:
            if run_saved not in _saved_tools:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"No saved tool named '{run_saved}'",
                        suggestions=[
                            "Use ha_manage_custom_tool(list_saved=True) to see saved tools",
                            "Use ha_manage_custom_tool(code=...) to create a new tool",
                        ],
                        context={"tool_name": run_saved},
                    )
                )

            saved = _saved_tools[run_saved]
            logger.info("Running saved tool '%s'", run_saved)

            try:
                result = await _run_sandboxed_code(
                    saved["code"], ctx, client, settings, Monty, ResourceLimits
                )
            except ToolError:
                raise
            except Exception as e:
                exception_to_structured_error(
                    e,
                    context={
                        "sandbox_error_type": type(e).__name__,
                        "saved_tool_name": run_saved,
                    },
                    suggestions=[
                        "The saved code may no longer work",
                        "Use ha_manage_custom_tool(code=...) to create an updated version",
                    ],
                )

            return {"success": True, "data": {"result": result, "saved_tool": run_saved}}

        # --- Mode: execute code ---
        if not code or not code.strip():
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Provide code to execute, run_saved to reuse a saved tool, "
                    "or list_saved=True to list saved tools",
                    suggestions=[
                        "ha_manage_custom_tool(code='...', justification='...')",
                        "ha_manage_custom_tool(run_saved='tool_name')",
                        "ha_manage_custom_tool(list_saved=True)",
                    ],
                )
            )

        if not justification or not justification.strip():
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "justification is required when providing code",
                    suggestions=[
                        "Explain why no existing tool can accomplish this task"
                    ],
                )
            )

        if save_as is not None and not _SAVE_NAME_PATTERN.match(save_as):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid save_as name: '{save_as}'. "
                    "Use alphanumeric characters and underscores, 1-64 chars.",
                    suggestions=["Example: save_as='movie_mode'"],
                )
            )

        logger.info("ha_manage_custom_tool invoked — justification: %s", justification[:200])
        logger.debug("ha_manage_custom_tool code:\n%s", code)

        try:
            result = await _run_sandboxed_code(
                code, ctx, client, settings, Monty, ResourceLimits
            )
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "sandbox_error_type": type(e).__name__,
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
            "data": {"result": result, "justification": justification},
        }

        if save_as:
            _saved_tools[save_as] = {
                "code": code,
                "justification": justification,
            }
            response["data"]["saved_as"] = save_as
            logger.info("Saved custom tool as '%s'", save_as)

        return response
