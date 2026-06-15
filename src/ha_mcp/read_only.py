"""Read-only mode — catalog filtering and call-time write blocking (#1569).

When ``Settings.read_only_mode`` is on (Tools-tab toggle in the web UI,
``read_only_mode`` addon option, or ``READ_ONLY_MODE`` env var):

- ``ReadOnlyToolsTransform`` hides write-capable tools from the MCP
  catalog at list time, except the exempt mixed read/write tools in
  ``READ_ONLY_EXEMPT_TOOLS``.
- ``ReadOnlyMiddleware`` blocks every write operation at call time with
  a structured ``READ_ONLY_MODE`` error — including the write actions
  of the exempt mixed tools, which stay callable for their read
  operations only.

Both consult the live settings singleton per request, so flipping the
toggle in standalone HTTP mode takes effect without a restart (addon
and stdio modes pick it up on restart, like every other feature flag).

A tool counts as write-capable when its ``readOnlyHint`` annotation is
not ``True`` — the same fail-closed default the policy handlers and the
search-proxy categorizer apply to unannotated tools.

The exempt tools are the mixed read/write tools whose read surface has
no pure-read duplicate elsewhere in the catalog — disabling them
outright would make that data unreachable in read-only mode. Each entry
carries an argument-level predicate that decides, per invocation,
whether the call is a read (allowed) or a write (blocked).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.transforms import Transform
from fastmcp.tools import Tool

from .config import get_global_settings
from .errors import ErrorCode, create_error_response
from .policy.middleware import PROXY_META_TOOLS
from .tools.helpers import raise_tool_error

if TYPE_CHECKING:
    from fastmcp.server.transforms import GetToolNext
    from fastmcp.utilities.versions import VersionSpec

logger = logging.getLogger(__name__)


class ReadOnlyExemption(NamedTuple):
    """One mixed read/write tool that stays enabled in read-only mode.

    ``blocked_write`` inspects the call arguments and returns ``None``
    when the invocation is a read, or a short human-readable description
    of the write operation when it must be blocked. ``allowed`` is a
    one-line summary of what remains available, surfaced in the error so
    the LLM can self-correct.
    """

    blocked_write: Callable[[dict[str, Any]], str | None]
    allowed: str


def _backup_write(args: dict[str, Any]) -> str | None:
    scope = args.get("scope")
    action = args.get("action")
    # Read-only-safe: per-edit backup listing/viewing, and snapshot listing
    # (issue #1586 — pure ``backup/info`` read, no tarball mutation).
    if scope == "edits" and action in ("list", "view"):
        return None
    if scope == "snapshot" and action == "list":
        return None
    return f"scope={scope!r}, action={action!r}"


# Add-on parameters that, when present, mean the call mutates add-on
# configuration (so the read-only middleware must block it). Module-level
# so the schema-drift guard test (test_read_only.py) can pin an
# independent manifest against it — see item 10b / ha_manage_addon.
_ADDON_CONFIG_WRITE_PARAMS = ("options", "network", "boot", "auto_update", "watchdog")


def _addon_write(args: dict[str, Any]) -> str | None:
    action = args.get("action")
    if action:
        return f"action={action!r}"
    for param in _ADDON_CONFIG_WRITE_PARAMS:
        if args.get(param) is not None:
            return f"add-on configuration change ({param}=...)"
    if args.get("array_patch") is not None:
        return "array_patch modification"
    if args.get("websocket"):
        # A WebSocket session's initial message can command mutations
        # (e.g. ESPHome /compile), so it is not statically classifiable
        # as a read — fail closed.
        return "WebSocket proxy session"
    method = str(args.get("method") or "GET").strip().upper()
    if method != "GET":
        return f"HTTP {method} proxy request"
    return None


def _energy_write(args: dict[str, Any]) -> str | None:
    mode = args.get("mode")
    if mode == "get":
        return None
    # dry_run=True previews validate/simulate without saving (every
    # write mode short-circuits before energy/save_prefs). Strict
    # ``is True``: the middleware sees RAW pre-validation arguments, so
    # a non-bool truthy value (e.g. the string "false") that schema
    # coercion could turn into False must fail closed here.
    if args.get("dry_run") is True:
        return None
    return f"mode={mode!r}"


def _pipeline_write(args: dict[str, Any]) -> str | None:
    action = args.get("action")
    if action in ("list", "get"):
        return None
    return f"action={action!r}"


def _custom_tool_write(args: dict[str, Any]) -> str | None:
    if args.get("list_saved") and not args.get("code") and not args.get("run_saved"):
        return None
    # Sandbox execution gets api_post / ws_send bridges that can write
    # to HA directly, so running code (new or saved) is never a read.
    return "sandbox code execution"


# Mixed read/write tools whose read surface has no pure-read duplicate
# (verified per tool: ha_get_addon cannot proxy-read addon-internal
# APIs; energy prefs and assist pipelines are reachable only through
# these tools; edit-backup listing exists nowhere else; the saved-tools
# cache is only listable here). Everything NOT in this table and not
# ``readOnlyHint=True`` is hidden and blocked outright.
#
# ``MANDATORY_TOOLS`` (settings_ui.py) intentionally needs no special
# case here: every mandatory tool is either ``readOnlyHint=True`` or
# present in this table (``ha_manage_backup``). The e2e test
# ``test_real_catalog_mandatory_tools_stay_available``
# (tests/src/e2e/policy/test_readonly_mode.py) guards that invariant
# against the real registered catalog at PR time, so the two sets
# cannot drift apart silently.
READ_ONLY_EXEMPT_TOOLS: dict[str, ReadOnlyExemption] = {
    "ha_manage_backup": ReadOnlyExemption(
        _backup_write,
        "listing and viewing per-edit backups (scope='edits', action='list' or "
        "'view') and listing snapshots (scope='snapshot', action='list')",
    ),
    "ha_manage_addon": ReadOnlyExemption(
        _addon_write,
        "HTTP GET proxy reads of add-on APIs (slug + path, method='GET')",
    ),
    "ha_manage_energy_prefs": ReadOnlyExemption(
        _energy_write,
        "reading the energy configuration (mode='get') and dry-run "
        "previews (dry_run=true)",
    ),
    "ha_manage_pipeline": ReadOnlyExemption(
        _pipeline_write,
        "listing and inspecting pipelines (action='list' or 'get')",
    ),
    "ha_manage_custom_tool": ReadOnlyExemption(
        _custom_tool_write,
        "listing saved tools (list_saved=True)",
    ),
}


def is_read_safe(tool: Tool) -> bool:
    """Return True when the tool's annotations declare it read-only."""
    annotations = getattr(tool, "annotations", None)
    return bool(annotations and getattr(annotations, "readOnlyHint", None) is True)


def read_only_visible(tool: Tool) -> bool:
    """Return True when the tool stays in the catalog in read-only mode."""
    return is_read_safe(tool) or tool.name in READ_ONLY_EXEMPT_TOOLS


def _raise_read_only_error(
    name: str, *, blocked_operation: str | None = None, allowed: str | None = None
) -> NoReturn:
    context: dict[str, Any] = {"tool_name": name, "read_only_mode": True}
    if blocked_operation is not None:
        context["blocked_operation"] = blocked_operation
    if blocked_operation is not None and allowed is not None:
        message = (
            f"Read Only Mode is enabled on this Home Assistant MCP server. "
            f"This call to '{name}' is a write operation "
            f"({blocked_operation}) and was blocked — no changes were made. "
            f"While Read Only Mode is on, '{name}' only supports: {allowed}."
        )
    else:
        message = (
            f"Read Only Mode is enabled on this Home Assistant MCP server. "
            f"'{name}' is a write-capable tool, so the call was blocked — "
            f"no changes were made."
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.READ_ONLY_MODE,
            message,
            suggestions=[
                "Continue with read-only tools — searching, getting, and "
                + "listing data all remain available.",
                "If the user wants to allow changes, they must turn off "
                + "Read Only Mode in the ha-mcp settings UI (Tools tab) or "
                + "the add-on configuration.",
            ],
            context=context,
        )
    )


class ReadOnlyToolsTransform(Transform):
    """Hide write-capable tools from the catalog while read-only mode is on.

    Installed before the search transforms so the BM25 index never
    indexes hidden write tools. Consults the live flag per request —
    no-op (and no per-call cost beyond the flag check) while it is off.
    """

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        if not get_global_settings().read_only_mode:
            return tools
        return [t for t in tools if read_only_visible(t)]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        if tool is None or not get_global_settings().read_only_mode:
            return tool
        return tool if read_only_visible(tool) else None


class ReadOnlyMiddleware(Middleware):
    """Block write operations at call time while read-only mode is on.

    The catalog filter already hides plain write tools, but the
    middleware is the actual enforcement: it covers calls routed through
    the search proxies, the write actions of the exempt mixed tools, and
    direct calls to hidden tools. Annotation lookups go through an
    unfiltered catalog provider (injected by server.py) and are cached
    (rebuilt on a cache miss — see _classify).
    """

    def __init__(self, *, list_tools: Callable[[], Awaitable[Sequence[Tool]]]) -> None:
        self._list_tools = list_tools
        self._read_safe_cache: dict[str, bool] | None = None

    async def _classify(self, name: str) -> str:
        """Classify ``name`` as 'read', 'write', or 'unknown'.

        Backed by the unfiltered catalog; the cache rebuilds on a miss so
        late-registered tools classify correctly. 'unknown' means the
        tool is not registered at all — the call passes through so the
        caller gets the normal unknown-tool error (nothing executable,
        no write risk). An EMPTY catalog is abnormal (broken lookup) and
        classifies everything 'write' — fail closed rather than letting
        calls through unclassified. If the catalog lookup itself RAISES
        we cannot classify anything, so we block the call with the
        structured READ_ONLY_MODE error rather than let the exception
        propagate opaquely (or, worse, let a future try/except return
        'unknown' and silently fail open).
        """
        if self._read_safe_cache is None or name not in self._read_safe_cache:
            try:
                tools = await self._list_tools()
            except Exception:
                logger.exception(
                    "read-only mode: tool catalog lookup failed while "
                    "classifying %s — blocking the call conservatively",
                    name,
                )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.READ_ONLY_MODE,
                        f"Read Only Mode is enabled on this Home Assistant MCP "
                        f"server, and the tool catalog lookup needed to classify "
                        f"'{name}' as read or write failed. The call to '{name}' "
                        f"was blocked conservatively — no changes were made.",
                        suggestions=[
                            "Retry the call — the catalog lookup may succeed "
                            + "on the next attempt.",
                            "If this persists, the MCP server may be "
                            + "misconfigured; check the server logs.",
                        ],
                        context={"tool_name": name, "read_only_mode": True},
                    )
                )
            self._read_safe_cache = {t.name: is_read_safe(t) for t in tools}
        if name in self._read_safe_cache:
            return "read" if self._read_safe_cache[name] else "write"
        if not self._read_safe_cache:
            return "write"
        return "unknown"

    @staticmethod
    def _coerce_arguments(arguments: Any) -> dict[str, Any] | None:
        """Normalise a proxy envelope's ``arguments`` to a dict or None.

        The categorized proxies tolerate ``arguments`` arriving as a JSON
        string (small models sometimes serialize it) and json.loads it
        AFTER this middleware runs (categorized_search.py ~313-352). So:
        a dict is used as-is; an absent value (``None``) becomes ``{}``
        (a legitimate no-argument call, not malformed); a string is
        parsed and only a JSON *object* yields a dict. Anything else — an
        unparseable string, a non-dict JSON value (list/scalar/null), or
        a non-str/non-dict type — returns None, meaning "malformed
        envelope".
        """
        if isinstance(arguments, dict):
            return arguments
        if arguments is None:
            return {}
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except (json.JSONDecodeError, ValueError):
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    @classmethod
    def _unwrap_proxy_call(
        cls,
        args: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        """Extract the inner (tool, arguments) from a call-proxy envelope.

        The categorized call proxies validate the inner name against
        their category caches BEFORE dispatching — and in read-only mode
        those caches no longer contain the hidden write tools, so a
        proxied write would surface as a generic "tool not found" error
        instead of the explanatory READ_ONLY_MODE one. Unwrapping here
        lets the middleware decide on the inner call first. Mirrors the
        proxy's own double-wrap unwrapping.

        ``arguments`` is coerced at every level (see _coerce_arguments)
        because the proxy accepts it as a JSON string. Returns None when
        there is no usable envelope — including when ``arguments`` is a
        MALFORMED string (json.loads fails, or the JSON is not an object).
        Passing through on a malformed envelope is safe: the proxy
        rejects such ``arguments`` with its own VALIDATION error BEFORE
        the category check and before any dispatch
        (categorized_search.py ~313-352), so no write can occur.
        """
        name = args.get("name")
        arguments = cls._coerce_arguments(args.get("arguments"))
        while (
            isinstance(name, str)
            and name in PROXY_META_TOOLS
            and isinstance(arguments, dict)
            and isinstance(arguments.get("name"), str)
        ):
            name = arguments.get("name")
            arguments = cls._coerce_arguments(arguments.get("arguments"))
        if not isinstance(name, str):
            return None
        if arguments is None:
            # Malformed inner ``arguments`` (a string that fails
            # json.loads, or a non-object JSON value / non-dict type).
            # Pass through: the proxy raises its own VALIDATION error
            # before any dispatch, so nothing can be written. (An absent
            # ``arguments`` key is NOT malformed — it coerces to ``{}``.)
            return None
        return name, arguments

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        if not get_global_settings().read_only_mode:
            return await call_next(context)

        name = context.message.name
        args = context.message.arguments or {}

        # Call proxies: decide on the INNER call (see _unwrap_proxy_call).
        # ha_search_tools and envelope-less proxy calls pass through —
        # searching is a read, and the proxy raises its own validation
        # error for a missing inner name. When the inner call is allowed,
        # the proxy dispatch re-enters this middleware with the real tool
        # name anyway (harmless re-check, same verdict).
        if name in PROXY_META_TOOLS:
            unwrapped = self._unwrap_proxy_call(args)
            if unwrapped is None:
                return await call_next(context)
            inner_name, inner_args = unwrapped
            exemption = READ_ONLY_EXEMPT_TOOLS.get(inner_name)
            if exemption is not None:
                blocked = exemption.blocked_write(inner_args)
                if blocked is None:
                    return await call_next(context)
                logger.info(
                    "read-only mode blocked proxied write operation of %s (%s)",
                    inner_name,
                    blocked,
                )
                _raise_read_only_error(
                    inner_name, blocked_operation=blocked, allowed=exemption.allowed
                )
            if await self._classify(inner_name) != "write":
                # 'read' is allowed; 'unknown' falls through to the
                # proxy's own not-found error.
                return await call_next(context)
            logger.info(
                "read-only mode blocked proxied call to write tool %s", inner_name
            )
            _raise_read_only_error(inner_name)

        exemption = READ_ONLY_EXEMPT_TOOLS.get(name)
        if exemption is not None:
            blocked = exemption.blocked_write(args)
            if blocked is None:
                return await call_next(context)
            logger.info(
                "read-only mode blocked write operation of %s (%s)", name, blocked
            )
            _raise_read_only_error(
                name, blocked_operation=blocked, allowed=exemption.allowed
            )

        if await self._classify(name) != "write":
            # 'read' is allowed; 'unknown' falls through to FastMCP's
            # normal unknown-tool error.
            return await call_next(context)

        logger.info("read-only mode blocked call to write tool %s", name)
        _raise_read_only_error(name)
        return None  # py/mixed-returns: unreachable, _raise_read_only_error raises
