"""
Sandboxed custom tool for Home Assistant MCP Server.

Provides an "escape hatch" (ha_manage_custom_tool) that lets LLMs write and run
custom Python code when no existing tool covers the request, with optional
save/reuse and listing of saved tools.  Code runs in pydantic-monty — a
Rust-based sandboxed Python interpreter with no filesystem or arbitrary
network access. Sandbox code can talk to Home Assistant through five external
functions: ``api_get`` and ``api_post`` for the REST API,
``ws_send`` for WebSocket commands, ``call_tool`` for delegating to other
registered MCP tools, and ``delete_saved_tool`` for removing a previously
saved custom tool. ``api_get``/``api_post`` reject absolute URLs so the HA
bearer token cannot be redirected off-instance.

Saved tools persist to disk when ``CODE_MODE_SAVED_TOOLS_PATH`` is set (the
addon sets this by default), letting users build their own "MCP within an
MCP" — a personal library of one-off tools that survives restarts.

**Requires** ``ENABLE_CODE_MODE=true`` (disabled by default).

See: https://github.com/homeassistant-ai/ha-mcp/issues/726
"""

import json
import logging
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context

from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error

logger = logging.getLogger(__name__)

# In-memory cache for saved custom tools, optionally persisted to disk.
# WARNING: This is shared across all clients in the same server process.
# In multi-user modes (OAuth, HTTP), one user's saved tools are visible to
# all other users. Scope to per-session/user before multi-user support.
# When ``settings.code_mode_saved_tools_path`` is set, the cache is hydrated
# from disk on registration and persisted on every save_as / delete.
_saved_tools: dict[str, dict[str, str]] = {}

# Tools that sandbox code must not call (prevents recursive self-invocation)
_BLOCKED_TOOLS = frozenset({"ha_manage_custom_tool"})

# Validation for save_as names
_SAVE_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

# Cap on the number of saved tools to prevent runaway growth (a buggy LLM
# loop could otherwise fill disk with unique save_as names).
_MAX_SAVED_TOOLS = 256

# Schema version for the on-disk saved-tools file. Bump when the shape
# changes so _load_saved_tools can migrate or refuse old files cleanly.
_SAVED_TOOLS_SCHEMA_VERSION = 1


def _load_saved_tools(path_str: str) -> dict[str, dict[str, str]]:
    """Load saved tools from a JSON file, filtering malformed entries.

    Returns an empty dict if the path is unset, the file doesn't exist
    yet, or the contents are unreadable / unparseable. A corrupt file is
    logged at WARNING but does not raise — the user can still save new
    tools and the bad file will be overwritten on the next persist.
    """
    if not path_str:
        return {}
    path = Path(path_str)
    if not path.exists():
        logger.debug("Saved-tools file %s does not exist yet; starting empty", path)
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Failed to load saved tools from %s (%s); starting empty",
            path,
            exc,
        )
        return {}

    if not isinstance(data, dict):
        logger.warning(
            "Saved-tools file %s top-level is %s, expected dict; ignoring",
            path,
            type(data).__name__,
        )
        return {}

    tools_raw = data.get("saved_tools", {})
    if not isinstance(tools_raw, dict):
        logger.warning(
            "Saved-tools file %s 'saved_tools' is %s, expected dict; ignoring",
            path,
            type(tools_raw).__name__,
        )
        return {}

    valid: dict[str, dict[str, str]] = {}
    for name, info in tools_raw.items():
        if not (isinstance(name, str) and _SAVE_NAME_PATTERN.match(name)):
            logger.warning(
                "Skipping saved tool with invalid name %r in %s", name, path
            )
            continue
        if not isinstance(info, dict):
            logger.warning(
                "Skipping saved tool %r in %s: entry is not a dict", name, path
            )
            continue
        code = info.get("code")
        justification = info.get("justification", "")
        if not isinstance(code, str) or not code:
            logger.warning(
                "Skipping saved tool %r in %s: missing or invalid code",
                name,
                path,
            )
            continue
        if not isinstance(justification, str):
            justification = ""
        valid[name] = {"code": code, "justification": justification}
        if len(valid) >= _MAX_SAVED_TOOLS:
            logger.warning(
                "Saved-tools file %s contains more than %d tools; truncating",
                path,
                _MAX_SAVED_TOOLS,
            )
            break

    logger.info("Loaded %d saved tool(s) from %s", len(valid), path)
    return valid


def _save_saved_tools(path_str: str, tools: dict[str, dict[str, str]]) -> None:
    """Persist the saved-tools cache to a JSON file atomically.

    Writes to ``path.tmp`` first and uses ``os.replace`` to swap it in,
    so a crash mid-write cannot corrupt the existing file. Failures are
    logged at WARNING — a write failure does not raise into the sandbox
    or the MCP client because the in-memory cache still holds the new
    entry; persistence is best-effort.
    """
    if not path_str:
        return
    path = Path(path_str)
    payload = {
        "version": _SAVED_TOOLS_SCHEMA_VERSION,
        "saved_at": datetime.now(UTC).isoformat(),
        "saved_tools": tools,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory so os.replace is
        # guaranteed to be atomic (cross-filesystem replace on POSIX is
        # not). delete=False because we'll replace it ourselves.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)
    except OSError as exc:
        logger.warning(
            "Failed to persist saved tools to %s (%s); cache remains in memory",
            path,
            exc,
        )


def _extract_tool_result(result: Any) -> Any:
    """Convert a FastMCP ToolResult to basic Python types for the sandbox.

    FastMCP call_tool may return a ToolResult, a list of content objects,
    or a basic type.  Monty can only handle basic Python types (str, int,
    float, bool, list, dict, None), so we must serialize.

    If the ToolResult flags ``isError``/``is_error``, returns ``{"error": ...}``
    so sandbox code sees the failure instead of treating the raw repr as a
    successful payload. The shape matches the ``api_get``/``api_post``/
    ``ws_send`` failure path so user code can do ``result.get("error")``
    uniformly.
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

    is_error = bool(
        getattr(result, "isError", False) or getattr(result, "is_error", False)
    )

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
                payload: Any = json.loads(combined)
            except (json.JSONDecodeError, TypeError):
                payload = combined
            if is_error:
                message = (
                    payload if isinstance(payload, str) else json.dumps(payload)
                )
                return {"error": message}
            return payload

    # Fallback: opaque object with no recognized content. Log so the
    # str(result) repr doesn't silently masquerade as a successful return.
    logger.warning(
        "_extract_tool_result fell through to str() for type=%s isError=%s",
        type(result).__name__,
        is_error,
    )
    repr_str = str(result)
    if is_error:
        return {"error": repr_str}
    return repr_str


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
    - api_get(endpoint) — GET request to HA REST API
    - api_post(endpoint, data) — POST request to HA REST API
    - ws_send(message) — send a HA WebSocket command and return its result
    - call_tool(name, args) — call a registered MCP tool (for existing tools)
    """
    call_count = 0

    def _sandbox_error(code: ErrorCode, message: str) -> dict[str, Any]:
        """Build an error dict to return to sandbox code (not a tool-level error).

        These are returned to the sandbox caller, not to the MCP client,
        so they intentionally do NOT use raise_tool_error.
        """
        err: dict[str, Any] = {"error": {"code": str(code), "message": message}}
        err["success"] = False
        return err

    def _normalize_endpoint(endpoint: Any) -> str:
        """Normalize a path-only endpoint to be relative to the httpx base URL.

        Strips leading slashes and any accidental ``api/`` prefix so the same
        path works whether the caller wrote ``"events"``, ``"/events"``, or
        ``"/api/events"``.

        Rejects anything that looks like an absolute URL or a userinfo
        injection: an ``://`` substring, a leading ``//`` (protocol-relative),
        or an ``@`` before the first ``/``. Without this the httpx client
        will dispatch the request to the absolute host *with the HA bearer
        token still attached*, leaking credentials to whoever the LLM was
        prompted to point at. The sandbox is supposed to be on-instance
        only.
        """
        if not isinstance(endpoint, str):
            raise ValueError("endpoint must be a string path (e.g. '/states')")
        if "://" in endpoint or endpoint.startswith("//"):
            raise ValueError(
                "endpoint must be a HA-relative path; absolute URLs are blocked"
            )
        first_slash = endpoint.find("/")
        userinfo_marker = endpoint.find("@")
        if userinfo_marker >= 0 and (
            first_slash < 0 or userinfo_marker < first_slash
        ):
            raise ValueError("endpoint must not contain userinfo")
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
            normalized = _normalize_endpoint(endpoint)
        except ValueError as exc:
            logger.warning("api_get rejected endpoint %r: %s", endpoint, exc)
            return {"error": str(exc)}
        try:
            response = await client.httpx_client.request("GET", normalized)
            try:
                return response.json()
            except json.JSONDecodeError:
                return response.text
        except Exception as exc:
            logger.warning("api_get(%r) failed", endpoint, exc_info=True)
            return {"error": str(exc)[:200]}

    async def _api_post(endpoint: str, data: dict[str, Any] | None = None) -> Any:
        """POST request to Home Assistant REST API."""
        nonlocal call_count
        call_count += 1
        if call_count > settings.code_mode_max_invocations:
            return {"error": f"API call limit exceeded ({settings.code_mode_max_invocations})"}
        try:
            normalized = _normalize_endpoint(endpoint)
        except ValueError as exc:
            logger.warning("api_post rejected endpoint %r: %s", endpoint, exc)
            return {"error": str(exc)}
        try:
            post_kwargs: dict[str, Any] = {}
            if data is not None:
                post_kwargs["json"] = data
            response = await client.httpx_client.request("POST", normalized, **post_kwargs)
            try:
                return response.json()
            except json.JSONDecodeError:
                return response.text
        except Exception as exc:
            logger.warning("api_post(%r) failed", endpoint, exc_info=True)
            return {"error": str(exc)[:200]}

    async def _ws_send(message: Any) -> Any:
        """Send a Home Assistant WebSocket command and return its result.

        ``message`` must be a dict with at least a ``type`` field, e.g.
        ``{"type": "config/area_registry/list"}``.  The MCP server's shared
        WebSocket client adds the message ``id`` and handles auth, so the
        sandbox should not include either. Typed as ``Any`` because sandbox
        code is dynamic and may pass non-dict values; the runtime guard
        below converts that into an error dict.
        """
        nonlocal call_count
        call_count += 1
        if call_count > settings.code_mode_max_invocations:
            return {"error": f"WebSocket call limit exceeded ({settings.code_mode_max_invocations})"}
        if not isinstance(message, dict):
            return {"error": "ws_send(message) requires a dict with a 'type' field"}
        if "type" not in message:
            return {"error": "ws_send(message) requires a 'type' field"}
        try:
            return await client.send_websocket_message(message)
        except Exception as exc:
            logger.warning(
                "ws_send(type=%r) failed", message.get("type"), exc_info=True
            )
            return {"error": str(exc)[:200]}

    async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
        """Bridge: sandbox code → MCP tool execution."""
        nonlocal call_count

        # Counter increments first so blocked-tool calls also count toward
        # the per-execution cap; otherwise a tight loop on a blocked name
        # would never trip the limit.
        call_count += 1
        if call_count > settings.code_mode_max_invocations:
            return _sandbox_error(
                ErrorCode.VALIDATION_FAILED,
                f"call_tool limit exceeded ({settings.code_mode_max_invocations} "
                f"calls per execution)",
            )

        if tool_name in _BLOCKED_TOOLS:
            return _sandbox_error(
                ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS,
                f"Tool '{tool_name}' cannot be called from sandbox code",
            )

        try:
            result = await ctx.fastmcp.call_tool(tool_name, arguments)
        except ToolError as te:
            try:
                return json.loads(str(te))
            except (json.JSONDecodeError, TypeError):
                return _sandbox_error(ErrorCode.INTERNAL_ERROR, str(te))
        except Exception as exc:
            logger.warning(
                "call_tool(%r) failed", tool_name, exc_info=True
            )
            return _sandbox_error(
                ErrorCode.INTERNAL_ERROR,
                f"Tool call failed: {str(exc)[:200]}",
            )

        # FastMCP call_tool returns a ToolResult or list of content objects.
        # Monty can only handle basic Python types, so serialize everything.
        return _extract_tool_result(result)

    def _delete_saved_tool(name: Any) -> dict[str, Any]:
        """Remove a previously saved custom tool by name.

        Sandbox helper. Returns ``{"deleted": True, "name": name}`` on
        success, ``{"error": "..."}`` on validation failure or if the
        named tool does not exist. Persists the change immediately when
        the saved-tools file path is configured.
        """
        if not isinstance(name, str):
            return {"error": "delete_saved_tool(name) requires a string name"}
        if not _SAVE_NAME_PATTERN.match(name):
            return {
                "error": (
                    f"Invalid saved-tool name {name!r}. "
                    "Use alphanumeric characters and underscores, 1-64 chars."
                )
            }
        if name not in _saved_tools:
            return {"error": f"No saved tool named {name!r}"}
        del _saved_tools[name]
        logger.info("Deleted saved custom tool '%s'", name)
        _save_saved_tools(settings.code_mode_saved_tools_path, _saved_tools)
        return {"deleted": True, "name": name}

    m = Monty(code, script_name="ha_manage_custom_tool.py")
    run_kwargs: dict[str, Any] = {
        "external_functions": {
            "api_get": _api_get,
            "api_post": _api_post,
            "ws_send": _ws_send,
            "call_tool": _call_tool,
            "delete_saved_tool": _delete_saved_tool,
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

    # Import in its own try so an ImportError raised by the body of
    # run_monty_async (e.g. a missing native shim) propagates instead of
    # being misattributed to "module-level run_monty_async not found".
    try:
        from pydantic_monty import run_monty_async
    except ImportError:
        run_monty_async = None  # type: ignore[assignment]
    if run_monty_async is not None:
        return await run_monty_async(m, **run_kwargs)

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

    # Hydrate the saved-tools cache from disk if persistence is enabled.
    # _saved_tools is a module-level dict; clear-and-update keeps the
    # same identity so other module-level references (e.g. the run_saved
    # / list_saved branches below) see the loaded data without lookup
    # changes.
    if settings.code_mode_saved_tools_path:
        loaded = _load_saved_tools(settings.code_mode_saved_tools_path)
        _saved_tools.clear()
        _saved_tools.update(loaded)

    @mcp.tool(
        tags={"System", "beta"},
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
        - ``api_get(endpoint)`` — GET request to HA REST API
        - ``api_post(endpoint, data)`` — POST request to HA REST API
        - ``ws_send(message)`` — send a HA WebSocket command (e.g. registry
          lookups, ``render_template``, dashboard ops). ``message`` must include
          a ``"type"`` field; the MCP server adds ``id`` and handles auth.
        - ``call_tool(name, args)`` — call a registered MCP tool
        - ``delete_saved_tool(name)`` — remove a previously saved custom
          tool by name. Returns ``{"deleted": True, "name": name}`` or
          ``{"error": ...}``.

        Use ``api_get``/``api_post`` for REST operations not covered by existing
        tools.  Use ``ws_send`` when the operation is only available over the
        Home Assistant WebSocket API (most registry CRUD, template rendering,
        and Lovelace operations).  Use ``call_tool`` when an existing tool
        already does what you need. Use ``delete_saved_tool`` to clean up
        saved tools you no longer need.

        Saved tools persist across server restarts when
        ``CODE_MODE_SAVED_TOOLS_PATH`` is set (the addon sets this by
        default to ``/data/saved_tools.json``).

        Example — check repairs (no built-in tool for this):
        ```python
        repairs = await api_get("/repairs/issues")
        repairs
        ```

        Example — list areas via WebSocket:
        ```python
        result = await ws_send({"type": "config/area_registry/list"})
        result.get("result", [])
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

        Example — delete an obsolete saved tool:
        ```python
        delete_saved_tool("old_movie_mode")
        ```

        Args:
            code: Python code to execute.  Last expression is the return value.
            justification: Why no existing tool works (required with code).
            save_as: Save the tool under this name for reuse (alphanumeric/underscores, max 64 chars).
            run_saved: Name of a previously saved tool to re-run.
            list_saved: Set True to list all saved tools.
        """
        # --- Validate that exactly one mode is specified ---
        # ``code`` and ``run_saved`` are mutually exclusive (either run new
        # code or re-run a saved tool, not both). ``list_saved`` is also
        # exclusive — it inspects state and must not coexist with execution.
        # ``save_as`` and ``justification`` are modifiers for the ``code``
        # mode and don't count as a "mode" on their own.
        modes_active = sum(
            1 for v in (
                bool(code and code.strip()),
                bool(run_saved is not None),
                bool(list_saved),
            )
            if v
        )
        if modes_active > 1:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "code, run_saved, and list_saved are mutually exclusive — "
                    "specify exactly one.",
                    suggestions=[
                        "ha_manage_custom_tool(code='...', justification='...')",
                        "ha_manage_custom_tool(run_saved='tool_name')",
                        "ha_manage_custom_tool(list_saved=True)",
                    ],
                )
            )

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
            if (
                save_as not in _saved_tools
                and len(_saved_tools) >= _MAX_SAVED_TOOLS
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_FAILED,
                        f"Saved-tools cache is full ({_MAX_SAVED_TOOLS} entries). "
                        "Delete a tool with delete_saved_tool(name) before "
                        "saving a new one.",
                        suggestions=[
                            "Use list_saved=True to see existing saved tools",
                            "Use code='delete_saved_tool(\"<name>\")' to remove one",
                        ],
                    )
                )
            _saved_tools[save_as] = {
                "code": code,
                "justification": justification,
            }
            response["data"]["saved_as"] = save_as
            logger.info("Saved custom tool as '%s'", save_as)
            _save_saved_tools(settings.code_mode_saved_tools_path, _saved_tools)

        return response
