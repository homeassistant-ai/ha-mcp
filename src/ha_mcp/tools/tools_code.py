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
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context

from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .helpers import log_tool_usage, raise_tool_error

logger = logging.getLogger(__name__)

# In-memory cache for saved custom tools, optionally persisted to disk.
#
# Lives at module level deliberately: ``ha_manage_custom_tool`` is a
# stateless MCP tool that needs ``run_saved`` / ``list_saved`` to see
# entries written by earlier ``save_as`` calls in the same process.
# Hydrated from ``settings.code_mode_saved_tools_path`` on
# ``register_code_tools`` startup (one-time) and persisted on every
# subsequent ``save_as`` / ``delete_saved_tool``. Per-call request
# scope wouldn't work because ``run_saved`` would never see prior
# saves; ``code_mode_saved_tools_path`` is the documented persistence
# boundary.
#
# WARNING: This is shared across all clients in the same server process.
# In multi-user modes (OAuth, HTTP), one user's saved tools are visible to
# all other users. Scope to per-session/user before multi-user support.
_saved_tools: dict[str, dict[str, str]] = {}

# Tools that sandbox code must not call. Includes ``ha_manage_custom_tool``
# itself (prevents recursive self-invocation) plus the four synthetics that
# the categorized-search transform exposes when ``ENABLE_TOOL_SEARCH=true``
# (``ha_search_tools``, ``ha_call_{read,write,delete}_tool``). Without those
# four entries, sandbox code could "launder" a recursive call as
# ``call_tool("ha_call_write_tool", {"name": "ha_manage_custom_tool", ...})``
# — the proxy would then dispatch the underlying tool and the in-sandbox
# guard never fires. The architectural fix lives in
# ``CategorizedSearchTransform`` (excludes pinned tools from category sets
# when code mode is on); this set is the defense-in-depth that closes the
# inner-call path even if the proxy is reachable some other way.
_BLOCKED_TOOLS = frozenset({
    "ha_manage_custom_tool",
    "ha_search_tools",
    "ha_call_read_tool",
    "ha_call_write_tool",
    "ha_call_delete_tool",
})

# Validation for save_as names
_SAVE_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

# Path-prefix denylist for ``_api_post``. Two flavours of entry, kept in
# one list because the matching logic is identical:
#   1. Endpoints with no legitimate sandbox use case at all — currently
#      just ``states/`` (raw state writes can conjure ghost entities and
#      override real ones in the in-memory state machine).
#   2. Endpoints whose corresponding wrapping MCP tool performs
#      validation / lint / hash-locking that raw ``api_post`` would skip
#      — currently ``config/{automation,script,scene}/config/``.
# The prefixes are matched after ``_normalize_endpoint`` strips the
# leading ``api/`` so they are written as plain HA-relative paths.
_API_POST_BLOCKED_PREFIXES: tuple[tuple[str, str, str], ...] = (
    (
        "states/",
        "Direct writes to /api/states/<entity_id>",
        "use the appropriate service via call_tool('ha_call_service', ...) "
        "so the change goes through the integration's state machine",
    ),
    (
        "config/automation/config/",
        "Direct writes to /api/config/automation/config/*",
        "use call_tool('ha_config_set_automation', ...) so schema "
        "validation, reference checks, and hash-locking run",
    ),
    (
        "config/script/config/",
        "Direct writes to /api/config/script/config/*",
        "use call_tool('ha_config_set_script', ...)",
    ),
    (
        "config/scene/config/",
        "Direct writes to /api/config/scene/config/*",
        "use call_tool('ha_config_set_scene', ...) so the entities-must-be-dict "
        "shape check, reference validation, and hash-locking run",
    ),
)

# HA Core internal events. Firing one of these via POST /api/events/<name>
# spoofs HA's own bookkeeping bus and can fan out into user automations
# listening for ``state_changed`` / ``automation_reloaded`` / etc. without
# the real subsystem ever having fired. Custom event types stay allowed —
# only the names HA Core itself emits are blocked.
_BLOCKED_HA_INTERNAL_EVENTS: frozenset[str] = frozenset({
    "state_changed",
    "service_registered",
    "service_removed",
    "service_executed",
    "automation_reloaded",
    "script_started",
    "script_finished",
    "homeassistant_start",
    "homeassistant_started",
    "homeassistant_stop",
    "homeassistant_close",
    "homeassistant_final_write",
    "core_config_updated",
    "device_registry_updated",
    "entity_registry_updated",
    "area_registry_updated",
    "category_registry_updated",
    "floor_registry_updated",
    "label_registry_updated",
    # ``logbook_entry`` is the documented logbook write API — the Logbook
    # integration consumes it to render rows. Sandbox code firing this
    # event would inject attacker-fabricated rows directly into the
    # user's primary investigation tool, which is a data-integrity issue.
    "logbook_entry",
    "lovelace_updated",
    "panels_updated",
    "themes_updated",
    "component_loaded",
    "recorder_5min_statistics_generated",
    "recorder_hourly_statistics_generated",
})

# WebSocket commands the sandbox must not send. Each one either changes
# persistent state in a way that bypasses a wrapping tool's validation
# (lovelace, registry mutations) or has no sandbox-appropriate use case
# at all (``config/core/update`` rewrites the HA installation's location/
# timezone/currency/lat-long).
_BLOCKED_WS_COMMANDS: frozenset[str] = frozenset({
    "config/core/update",
    "lovelace/config/save",
    "lovelace/dashboards/create",
    "lovelace/dashboards/delete",
    "lovelace/dashboards/update",
    "config/area_registry/delete",
    "config/area_registry/disable",
    "config/area_registry/update",
    "config/device_registry/delete",
    "config/device_registry/disable",
    "config/device_registry/update",
    # Device registry deletion is registered as ``remove_config_entry`` on
    # HA Core, not ``delete`` — see ``tools_registry.py:753`` for the
    # actually-emitted command. ``ha_remove_device`` wraps it; raw
    # ``ws_send`` would skip those checks.
    "config/device_registry/remove_config_entry",
    "config/entity_registry/delete",
    "config/entity_registry/disable",
    "config/entity_registry/update",
    # Entity registry deletion is registered as ``remove`` on HA Core,
    # not ``delete`` — see ``tools_entities.py:1130`` for the
    # actually-emitted command. ``ha_remove_entity`` wraps it.
    "config/entity_registry/remove",
    # Floor / label / category registries follow the same rationale as
    # area / device / entity above: each has a wrapping MCP tool
    # (``ha_set_area_or_floor``, ``ha_config_set_label``,
    # ``ha_config_set_category``) that performs invariant checks the
    # raw WS command skips.
    "config/floor_registry/create",
    "config/floor_registry/delete",
    "config/floor_registry/update",
    "config/label_registry/create",
    "config/label_registry/delete",
    "config/label_registry/update",
    "config/category_registry/create",
    "config/category_registry/delete",
    "config/category_registry/update",
})


def _classify_sandbox_error(exc: Exception) -> tuple[ErrorCode, str, list[str]]:
    """Map a sandbox exception to ``(code, message, suggestions)``.

    Monty wraps inner exceptions in ``MontyRuntimeError`` with the inner
    type name embedded in the string representation, so we inspect both
    ``type(exc).__name__`` and ``str(exc)`` and pick the most specific
    bucket. Three categories:

    * ``SANDBOX_LIMIT_EXCEEDED`` — memory / time / recursion / invocation
      limits the sandbox runtime enforces.
    * ``SANDBOX_SYNTAX_UNSUPPORTED`` — features Monty doesn't implement
      (imports, classes, ``with``, ``match``, etc.) or hard syntax errors.
    * ``SANDBOX_RUNTIME_ERROR`` — anything else; opaque runtime failure
      whose root cause is in the LLM-authored code.

    The suggestions are tailored per category so the LLM can self-recover
    instead of seeing every failure as "check the Python code for syntax
    errors" (which was the prior behaviour and actively misled callers
    when the real cause was a memory cap or a missing module import).
    """
    exc_text = str(exc)
    exc_type = type(exc).__name__
    short = exc_text[:200]

    def _matches(*needles: str) -> bool:
        return any(needle in exc_type or needle in exc_text for needle in needles)

    if _matches("MemoryError"):
        return (
            ErrorCode.SANDBOX_LIMIT_EXCEEDED,
            f"Sandbox memory limit exceeded: {short}",
            [
                "Reduce memory usage in your code (smaller intermediate "
                "data structures, no large list accumulation).",
                "Stream results via call_tool calls rather than building "
                "them up in-process.",
                "Operator can raise CODE_MODE_MAX_MEMORY (max 256 MB).",
            ],
        )
    if _matches("RecursionError"):
        return (
            ErrorCode.SANDBOX_LIMIT_EXCEEDED,
            f"Sandbox recursion limit exceeded: {short}",
            [
                "Convert deep recursion to iteration (while/for with an "
                "explicit stack).",
                "Operator can raise CODE_MODE_MAX_RECURSION (max 10000).",
            ],
        )
    if _matches("TimeoutError", "timed out", "wall-clock", "max_duration"):
        return (
            ErrorCode.SANDBOX_LIMIT_EXCEEDED,
            f"Sandbox time limit exceeded: {short}",
            [
                "Optimise the code to finish within the wall-clock limit.",
                "Break the work into smaller chunks across multiple calls.",
                "Operator can raise CODE_MODE_MAX_DURATION (max 300s).",
            ],
        )

    if _matches("ModuleNotFoundError", "No module named"):
        return (
            ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED,
            f"Imports are not allowed in the sandbox: {short}",
            [
                "Remove all 'import' / 'from ... import' statements.",
                "Use the injected helpers instead: api_get, api_post, "
                "ws_send, call_tool, delete_saved_tool.",
            ],
        )
    if _matches("NotImplementedError", "does not yet support", "context manager"):
        return (
            ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED,
            f"Sandbox does not support this Python feature: {short}",
            [
                "Monty's sandboxed interpreter is a Python subset — "
                "context managers (with), match statements, class "
                "definitions, and some builtins are unavailable.",
                "Rewrite using basic statements and the injected helpers.",
            ],
        )
    if _matches("SyntaxError"):
        return (
            ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED,
            f"Code did not parse: {short}",
            [
                "Fix the syntax error and retry.",
                "Reminder: no class definitions, no imports, no 'with' "
                "or 'match' statements.",
            ],
        )

    return (
        ErrorCode.SANDBOX_RUNTIME_ERROR,
        f"Sandbox runtime error: {short}",
        [
            f"Exception type: {exc_type}",
            "Some Python builtins behave differently in Monty (e.g. "
            "next() requires an iterator, not a list).",
            "Check the values you're passing to api_get/api_post/"
            "ws_send/call_tool.",
            "Use 'await' before any call to api_get/api_post/ws_send/"
            "call_tool.",
        ],
    )


def _check_api_post_blocked(normalized: str) -> str | None:
    """Return a rejection message if ``normalized`` matches the api_post
    blocklist, or ``None`` if the call should proceed.

    ``normalized`` is the path after ``_normalize_endpoint`` stripped any
    leading ``/`` and ``api/`` prefix, so for example
    ``"/api/states/sun.sun"`` arrives here as ``"states/sun.sun"``.
    """
    for prefix, what, alternative in _API_POST_BLOCKED_PREFIXES:
        if normalized.startswith(prefix) or normalized == prefix.rstrip("/"):
            return f"{what} are blocked from the sandbox; {alternative}."
    if normalized.startswith("events/"):
        event_name = normalized[len("events/"):]
        if event_name in _BLOCKED_HA_INTERNAL_EVENTS:
            return (
                f"Firing HA-internal event {event_name!r} from the sandbox "
                "is blocked because it can spoof HA's own bookkeeping and "
                "trigger user automations without the underlying real "
                "event ever happening. Custom event types are allowed."
            )
    return None

# Cap on the number of saved tools to prevent runaway growth. A buggy
# LLM loop could otherwise fill the on-disk file with unique save_as
# names. Enforced both at load (truncate-with-warning) and at save
# (reject the call before mutating the in-memory cache).
_MAX_SAVED_TOOLS = 256

# Schema version for the on-disk saved-tools file. Bumped when the shape
# changes so _load_saved_tools can refuse old/new files cleanly. The
# load path checks data["version"] explicitly and refuses anything that
# isn't this number rather than silently re-interpreting it as v1.
_SAVED_TOOLS_SCHEMA_VERSION = 1

# Module-level flag set when _load_saved_tools fails for a reason other
# than "the file doesn't exist yet" (e.g. PermissionError reading an
# existing file). Persistence is suppressed while this is set so we
# don't atomically replace a temporarily-unreadable file with empty
# content and destroy whatever was on disk. Cleared by a successful
# load at register_code_tools time. The variable is module-level
# (not closure-captured) so save sites in ha_manage_custom_tool /
# _delete_saved_tool can read it without parameter plumbing.
_saved_tools_load_failed = False


def _load_saved_tools(path_str: str) -> dict[str, dict[str, str]]:
    """Load saved tools from a JSON file, filtering malformed entries.

    Returns an empty dict if the path is unset or the file doesn't exist
    yet (legitimate "starting empty" cases). A corrupt JSON body or an
    unexpected schema version is logged at WARNING and returns empty —
    the file will be overwritten on the next persist.

    A genuine I/O error reading an existing file (OSError that isn't
    FileNotFoundError) is logged at ERROR and ALSO sets the module-level
    ``_saved_tools_load_failed`` flag so callers know not to overwrite
    whatever is on disk while the load condition persists. This prevents
    a PermissionError at startup from cascading into "next save wipes
    out the unreadable file" data loss.
    """
    global _saved_tools_load_failed
    _saved_tools_load_failed = False
    if not path_str:
        return {}
    path = Path(path_str)
    if not path.exists():
        logger.debug("Saved-tools file %s does not exist yet; starting empty", path)
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Race: file disappeared between exists() and read_text().
        # Treat as legitimate "not yet" rather than an I/O failure.
        return {}
    except OSError as exc:
        # PermissionError / IsADirectoryError / etc. The file exists but
        # we can't read it. Block subsequent persistence so we don't
        # overwrite the unreadable original with empty content.
        logger.error(
            "Cannot read saved-tools file %s (%s); persistence will be "
            "suppressed until the load condition clears. Saves and "
            "deletes will still update the in-memory cache for the "
            "current session.",
            path,
            exc,
            exc_info=True,
        )
        _saved_tools_load_failed = True
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Saved-tools file %s is not valid JSON (%s); starting empty. "
            "The corrupt file will be overwritten on the next persist.",
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

    file_version = data.get("version")
    if file_version != _SAVED_TOOLS_SCHEMA_VERSION:
        # Refuse to interpret the file. We don't know whether this is a
        # newer file produced by a future ha-mcp version (which might
        # have shape changes we'd silently mangle) or an older file we
        # don't have a migration for. Setting the failed flag means the
        # current session won't overwrite it on next save.
        logger.error(
            "Saved-tools file %s has schema version %r; this build expects %d. "
            "Refusing to load. Persistence is suppressed for this session "
            "to avoid overwriting an unfamiliar file. Move or delete the "
            "file to recover.",
            path,
            file_version,
            _SAVED_TOOLS_SCHEMA_VERSION,
        )
        _saved_tools_load_failed = True
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


def _save_saved_tools(
    path_str: str, tools: dict[str, dict[str, str]]
) -> bool:
    """Persist the saved-tools cache to a JSON file atomically.

    Returns ``True`` if persistence succeeded (or was disabled because
    ``path_str`` is empty — that's the configured-out case, not a
    failure). Returns ``False`` only when persistence was attempted and
    the underlying I/O raised. Callers that promised the user durability
    should surface a ``False`` return as a warning in the response.

    Writes to ``<dir>/.<name>.<rand>.tmp`` first and uses ``os.replace``
    to swap it in, so a crash mid-write cannot corrupt the existing
    file. Refuses to write at all when ``_saved_tools_load_failed`` is
    set — see _load_saved_tools for why we'd rather skip persistence
    than overwrite an unreadable file with empty content.
    """
    if not path_str:
        return True
    # Read-only access to the module-level flag; ``global`` declaration
    # only needed at the set sites in ``_load_saved_tools``.
    if _saved_tools_load_failed:
        logger.warning(
            "Skipping persist to %s because the prior load failed; "
            "saves and deletes are in-memory only for this session.",
            path_str,
        )
        return False
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
        logger.error(
            "Failed to persist saved tools to %s (%s); the in-memory "
            "cache holds the latest change but it will be lost on restart "
            "unless this resolves before the next save",
            path,
            exc,
            exc_info=True,
        )
        return False
    return True


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

    # ``list`` is also a basic type Monty handles, but a list might also
    # be FastMCP's "list of content objects" shape (each element having
    # a ``.text`` or being a content-block ``dict``). Distinguish: if the
    # first element looks like a content object, treat as a ToolResult
    # payload; otherwise pass through as a normal list of basic values.
    if isinstance(result, list):
        looks_like_content = bool(result) and (
            hasattr(result[0], "text") or hasattr(result[0], "type")
        )
        if not looks_like_content:
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

    **Error shape contract.** All bridge functions return a dict with an
    ``"error"`` key on failure; the value may be either a plain string
    (``api_get`` / ``api_post`` / ``ws_send`` / ``delete_saved_tool`` —
    transport-level failures, validation rejections) or a structured
    sub-dict ``{"code": "<ErrorCode>", "message": "<text>"}`` from
    ``_sandbox_error`` (``call_tool`` — propagates the underlying tool's
    ``ErrorCode`` so sandbox code can branch on category). The structured
    form also includes ``"success": False`` at the top level. **Consumers
    should always probe with ``if "error" in result:``** — never
    ``result["error"].lower()`` blindly, because the value isn't always a
    string.
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

        Rejects:

        * Absolute URL forms — ``://``, leading ``//`` (protocol-relative),
          or ``@`` before the first ``/`` (userinfo). Without this httpx
          will dispatch the request to the absolute host *with the HA
          bearer token still attached*, leaking credentials.

          Note: ``@`` later in the path (``events/foo@bar``) is fine —
          only userinfo position (before the first ``/``) is the
          credential-leaking shape that http URL parsers will treat as
          ``user@host``.
        * ``..`` path segments — httpx happily resolves
          ``base_url='http://ha:8123/api'`` + endpoint ``'../auth/providers'``
          to ``http://ha:8123/auth/providers``, escaping the ``/api/``
          prefix entirely. HA exposes other bearer-authenticated routes
          at root (``/auth/...``, ``/profile``, etc.) — every one of
          those becomes reachable from the sandbox unless we reject
          ``..`` here. Each segment is also percent-decoded once before
          the comparison so ``%2e%2e`` (and other percent-encoded
          variants of ``..``) can't slip past on reverse-proxy setups
          that decode-then-resolve.
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
        # ``..`` segments would let the sandbox escape the ``/api/`` prefix
        # via httpx URL resolution. Check after stripping so the comparison
        # is against actual path segments, and percent-decode each segment
        # so encoded forms (``%2e%2e`` and similar) don't slip past on
        # reverse-proxy setups that decode-then-resolve.
        for segment in ep.split("/"):
            if urllib.parse.unquote(segment) == "..":
                raise ValueError(
                    "endpoint must not contain '..' path segments "
                    "(including percent-encoded forms like %2e%2e); "
                    "the sandbox is restricted to /api/ routes"
                )
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
        block_reason = _check_api_post_blocked(normalized)
        if block_reason is not None:
            # INFO-level so blocked attempts are visible in operator logs
            # for forensics without flooding INFO during normal operation.
            logger.info(
                "sandbox.api_post.blocked endpoint=%r normalized=%r",
                endpoint,
                normalized,
            )
            return {"error": block_reason}
        # State-changing call: DEBUG-level audit trail. Operators can
        # bump the ha_mcp.tools.tools_code logger to DEBUG to see what
        # the sandbox is actually doing on their HA instance.
        # ``map(str, ...)`` on the keys because Monty allows mixed-type
        # dict keys (e.g. ``{1: "x", "a": "y"}``); a plain ``sorted``
        # would raise TypeError on the first invocation and the user
        # would see a confusing "api_post failed" with no hint that
        # the audit-log step was the real culprit.
        logger.debug(
            "sandbox.api_post endpoint=%r data_keys=%s",
            endpoint,
            sorted(map(str, data.keys())) if isinstance(data, dict) else None,
        )
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

        Commands listed in ``_BLOCKED_WS_COMMANDS`` are rejected with an
        explanatory error: those either rewrite persistent state in ways
        that have no sandbox-appropriate use case (``config/core/update``)
        or bypass the validation in their wrapping MCP tool
        (``lovelace/config/save`` skips the dashboard-collision check that
        ``ha_config_set_dashboard`` performs; registry mutations skip
        their corresponding wrapping tools' invariant checks).
        """
        nonlocal call_count
        call_count += 1
        if call_count > settings.code_mode_max_invocations:
            return {"error": f"WebSocket call limit exceeded ({settings.code_mode_max_invocations})"}
        if not isinstance(message, dict):
            return {"error": "ws_send(message) requires a dict with a 'type' field"}
        msg_type = message.get("type")
        if not isinstance(msg_type, str):
            return {"error": "ws_send(message) requires a 'type' field"}
        if msg_type in _BLOCKED_WS_COMMANDS:
            logger.info("sandbox.ws_send.blocked type=%r", msg_type)
            return {
                "error": (
                    f"WebSocket command {msg_type!r} is blocked from the "
                    "sandbox. Use the corresponding wrapping tool via "
                    "call_tool (e.g. ha_config_set_dashboard, "
                    "ha_set_area_or_floor, ha_set_device, ha_set_entity) "
                    "so validation runs."
                )
            }
        logger.debug("sandbox.ws_send type=%r", msg_type)
        try:
            return await client.send_websocket_message(message)
        except Exception as exc:
            logger.warning(
                "ws_send(type=%r) failed", msg_type, exc_info=True
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
        success, ``{"error": "..."}`` on validation failure, missing
        name, or persistence failure. When persistence is configured
        and the on-disk write fails, the in-memory deletion is rolled
        back so the next save_as / list_saved doesn't show a different
        view than the next process restart.
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
        # Snapshot the entry before deleting so we can restore it on
        # persist failure (otherwise the in-memory cache and disk would
        # disagree, and the on-restart hydration would resurrect the
        # entry the LLM already saw "deleted").
        previous = _saved_tools[name]
        del _saved_tools[name]
        if not _save_saved_tools(
            settings.code_mode_saved_tools_path, _saved_tools
        ):
            _saved_tools[name] = previous
            return {
                "error": (
                    f"Deleted {name!r} from in-memory cache but the "
                    "persistence write failed; rolled back. Check "
                    "operator logs for the underlying I/O error."
                )
            }
        logger.info("Deleted saved custom tool '%s'", name)
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
            # The saved-tools dict is nested under a stable ``saved_tools``
            # key rather than spread directly under ``data`` because the
            # name pattern (``^[a-zA-Z_][a-zA-Z0-9_]{0,63}$``) accepts
            # values like ``result``, ``count``, ``code`` — every one of
            # which is also a key the *other* response shapes use. A
            # consumer reading ``r["data"]["result"]`` after a list_saved
            # call would otherwise get a saved-tool entry instead of a
            # run-result.
            return {
                "success": True,
                "data": {
                    "saved_tools": {
                        name: {
                            "code": info["code"],
                            "justification": info["justification"],
                        }
                        for name, info in _saved_tools.items()
                    },
                    "count": len(_saved_tools),
                },
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
                code, message, suggestions = _classify_sandbox_error(e)
                raise_tool_error(
                    create_error_response(
                        code,
                        message,
                        suggestions=[
                            "The saved code may no longer work in the "
                            "current sandbox or HA configuration.",
                            "Use ha_manage_custom_tool(code=...) to "
                            "create an updated version.",
                            *suggestions,
                        ],
                        context={
                            "sandbox_error_type": type(e).__name__,
                            "saved_tool_name": run_saved,
                        },
                    )
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

        # ``%r`` (repr) defends against log-line injection by escaping
        # ``\r`` / ``\n`` / ``\t`` as literal ``\r``/``\n``/``\t``
        # sequences in the formatted output — same primitive used by
        # the audit-log endpoint/type fields below. Truncate to keep
        # log lines bounded; an LLM can supply a multi-KB
        # justification.
        logger.info(
            "ha_manage_custom_tool invoked — justification: %r",
            justification[:200],
        )
        # Code is logged at DEBUG and the multi-line shape is intentional
        # (the LLM-authored snippet is the operator's primary forensic
        # artefact). No control-char sanitisation here because the log
        # format string already opens a fresh line — there's nothing to
        # inject into.
        logger.debug("ha_manage_custom_tool code:\n%s", code)

        try:
            result = await _run_sandboxed_code(
                code, ctx, client, settings, Monty, ResourceLimits
            )
        except ToolError:
            raise
        except Exception as e:
            err_code, err_message, err_suggestions = _classify_sandbox_error(e)
            raise_tool_error(
                create_error_response(
                    err_code,
                    err_message,
                    suggestions=err_suggestions,
                    context={
                        "sandbox_error_type": type(e).__name__,
                        "justification": justification[:200],
                    },
                )
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
            previous = _saved_tools.get(save_as)
            _saved_tools[save_as] = {
                "code": code,
                "justification": justification,
            }
            response["data"]["saved_as"] = save_as
            logger.info("Saved custom tool as '%s'", save_as)
            persisted = _save_saved_tools(
                settings.code_mode_saved_tools_path, _saved_tools
            )
            if not persisted:
                # Roll back the in-memory write so the cache matches
                # what's on disk (or, on next restart, what's loaded).
                # Surface a warning in the response so the LLM knows
                # the save_as didn't actually durable, while still
                # returning success=True for the code execution itself.
                if previous is None:
                    _saved_tools.pop(save_as, None)
                else:
                    _saved_tools[save_as] = previous
                response["data"]["saved_as"] = None
                response["data"]["save_warning"] = (
                    f"save_as={save_as!r} was attempted but the persistence "
                    "write failed; the entry was rolled back from the "
                    "in-memory cache. Check operator logs for the "
                    "underlying I/O error."
                )

        return response
