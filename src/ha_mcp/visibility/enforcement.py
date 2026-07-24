"""Enforce mode for the opt-in entity visibility filter (issue #2015).

The visibility filter (``visibility/``) normally only declutters ``ha_search`` /
``ha_get_overview``. When a user turns on ``enforce`` (Entity Visibility tab, or
``"enforce": true`` in ``entity_visibility.json``), :class:`VisibilityEnforcementMiddleware`
makes the hidden set genuinely unreadable across ALL tools — "refuse on contact":

- **Direct reads are concealed.** A call whose arguments name a hidden entity_id
  exactly is refused BEFORE the tool runs with a canonical ``ENTITY_NOT_FOUND``,
  so the entity's data never flows. Existence concealment is best-effort: per-tool
  not-found shapes vary (details/suggestions differ; bulk reads normally
  partial-succeed), so a prober comparing error shapes may infer hidden-vs-absent.
- **Content reads are refused.** A call whose arguments *embed* a hidden entity_id
  (a template, an automation body, a service target list), or whose *output* would
  surface one, is refused with a generic ``ENTITY_VISIBILITY_ENFORCED`` error that
  never names the matched id.
- **Unscannable surfaces are refused wholesale** while enforce is active — sandbox
  code execution and screenshot/pixel output can read arbitrary state that no text
  scan can attribute to an entity.

This is a strong read barrier against *incidental* exposure, not a cryptographic
guarantee: a Jinja template that derives a hidden entity's state without ever
naming its entity_id cannot be caught (the issue author accepts that floor).

Consulted live per request (like read-only mode), so a settings-UI toggle takes
effect without a restart in standalone HTTP mode. When enforce is off, or the
config hides nothing, this is a pure passthrough with only the config-load cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NoReturn

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

from ..errors import ErrorCode, create_entity_not_found_error, create_error_response
from ..policy.middleware import PROXY_META_TOOLS
from ..tools.helpers import raise_tool_error
from . import resolver
from .model import VisibilityConfig
from .resolver import (
    VisibilityDataUnavailable,
    config_has_active_hide_dimensions,
    config_needs_device_registry,
    load_hidden_set,
)

if TYPE_CHECKING:
    from fastmcp.tools.tool import ToolResult

logger = logging.getLogger(__name__)

# The resolved hidden set is cached with a short TTL so a burst of tool calls
# does not re-fetch the registry each time; a config edit invalidates it sooner
# (the cache is keyed on the hide dimensions), so the ~30s window only applies to
# registry-side staleness (an area/label membership change in HA).
_CACHE_TTL_SECONDS = 30.0

# Tools whose output cannot be text-scanned for a hidden entity_id.
_SCREENSHOT_TOOL = "ha_get_dashboard_screenshot"
_DASHBOARD_TOOL = "ha_config_get_dashboard"
_CUSTOM_TOOL = "ha_manage_custom_tool"

_SANDBOX_REASON = (
    "'{name}' executes sandbox code that can read arbitrary Home Assistant state, "
    "which cannot be checked against the entity visibility filter."
)
_SCREENSHOT_REASON = (
    "'{name}' returns image output that cannot be checked against the entity "
    "visibility filter."
)
_FAIL_CLOSED_REASON = (
    "The entity visibility data could not be loaded, so enforce mode fails closed "
    "rather than risk leaking a restricted entity."
)
_CONFIG_LOAD_FAILED_REASON = (
    "The entity visibility config (entity_visibility.json) could not be loaded and "
    "no previously loaded config is available, so the server cannot determine "
    "whether enforce mode is active and refuses rather than risk leaking a "
    "restricted entity. Fix or remove the file (the Entity Visibility tab in the "
    "ha-mcp settings UI rewrites it cleanly)."
)


def _inbound_reason(name: str) -> str:
    return (
        f"The request to '{name}' references an entity restricted by the entity "
        "visibility filter."
    )


def _outbound_reason(name: str) -> str:
    return (
        f"The result of '{name}' would include an entity restricted by the entity "
        "visibility filter."
    )


def _raise_enforced(tool_name: str, reason: str) -> NoReturn:
    """Refuse a call with the generic enforce-mode error (never names an entity)."""
    raise_tool_error(
        create_error_response(
            ErrorCode.ENTITY_VISIBILITY_ENFORCED,
            "Entity visibility enforce mode is active on this Home Assistant MCP "
            f"server. {reason} No data was returned.",
            suggestions=[
                "The entity is restricted by the entity visibility filter's enforce "
                "mode; do not retry the same request.",
                "If the user wants this data, they must adjust or turn off enforce "
                "mode in the ha-mcp settings UI (Entity Visibility tab).",
            ],
            context={"tool_name": tool_name, "visibility_enforced": True},
        )
    )


def _conceal_as_not_found(entity_id: str) -> NoReturn:
    """Conceal a hidden entity as if it did not exist.

    Uses the canonical ``ENTITY_NOT_FOUND`` helper. This guarantees the data
    never flows (the tool is never called), but existence concealment is only
    best-effort: individual tools decorate their genuine not-founds differently
    (custom suggestions, per-item errors in bulk partial-success responses), so
    a caller deliberately comparing error shapes may still infer hidden-vs-absent.
    Mimicking every tool's exact not-found shape would mean encoding per-tool
    response knowledge here; the docs state the honest boundary instead.
    """
    raise_tool_error(create_entity_not_found_error(entity_id))


def _iter_strings(value: Any) -> Any:
    """Yield every string in a nested args/result structure (dicts/lists/strings).

    Dict KEYS are yielded too: HA config payloads key maps by entity_id (a scene's
    ``entities``, a group's members), so a key is as much a reference as a value —
    and the outbound scan (``json.dumps``) sees keys, so the inbound scan must
    traverse the same surface or a hidden id positioned as a key would execute.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _scan_value(
    value: Any, hidden: set[str], regex: re.Pattern[str] | None
) -> tuple[str | None, bool]:
    """Return ``(first_exact_hidden_id, any_embedded_hidden_id)`` over nested strings.

    A string that equals a hidden id exactly drives concealment; a string that
    merely embeds one (boundary regex) drives the generic refusal. An exact match
    takes priority, so an exact hit never also counts as embedded.
    """
    exact: str | None = None
    embedded = False
    for text in _iter_strings(value):
        if text in hidden:
            if exact is None:
                exact = text
        elif regex is not None and regex.search(text):
            embedded = True
    return exact, embedded


def _build_hidden_regex(hidden: set[str]) -> re.Pattern[str] | None:
    """Compile one boundary-aware alternation over the hidden set, or None if empty.

    The ``(?<![a-z0-9_]) ... (?![a-z0-9_])`` guards (case-insensitive) make
    ``sensor.foo`` match ``states.sensor.foo`` and a bare ``sensor.foo`` in a
    template, but NOT ``sensor.foo2`` or ``my_sensor.foo``.
    """
    if not hidden:
        return None
    alternation = "|".join(re.escape(eid) for eid in sorted(hidden))
    return re.compile(rf"(?<![a-z0-9_])(?:{alternation})(?![a-z0-9_])", re.IGNORECASE)


def _serialize_result(result: ToolResult) -> str:
    """Flatten a tool result's text content and structured content for scanning."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        try:
            parts.append(json.dumps(structured, default=str))
        except (TypeError, ValueError):
            parts.append(str(structured))
    return "\n".join(parts)


def _tool_error_text(exc: ToolError) -> str:
    """The scannable text of a tool-raised error (its JSON payload args)."""
    return "\n".join(str(arg) for arg in (getattr(exc, "args", None) or ()))


def _unscannable_reason(name: str, args: dict[str, Any]) -> str | None:
    """Reason this call's output cannot be scanned, or None if it can.

    ``ha_manage_custom_tool`` stays allowed for pure ``list_saved`` calls; only
    sandbox execution (``code`` / ``run_saved``) is unscannable. Plain dashboard
    config reads stay allowed and are outbound-scanned; only a screenshot request
    is unscannable.
    """
    if name == _SCREENSHOT_TOOL:
        return _SCREENSHOT_REASON.format(name=name)
    if name == _DASHBOARD_TOOL and args.get("include_screenshot"):
        return _SCREENSHOT_REASON.format(name=name)
    if name == _CUSTOM_TOOL and (args.get("code") or args.get("run_saved")):
        return _SANDBOX_REASON.format(name=name)
    return None


def _coerce_arguments(arguments: Any) -> dict[str, Any] | None:
    """Normalise a proxy envelope's ``arguments`` to a dict or None (mirrors read_only)."""
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


def _unwrap_proxy_call(args: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the innermost proxy-envelope ``arguments`` dict, or None if unusable.

    Mirrors ``ReadOnlyMiddleware._unwrap_proxy_call``: the categorized call proxies
    accept ``arguments`` as a JSON string, so an inner exact entity_id match would
    otherwise be seen only as an embedded (generic-refusal) hit on the raw string.
    Unwrapping parses it so the inner exact id yields concealment instead.
    """
    name = args.get("name")
    arguments = _coerce_arguments(args.get("arguments"))
    while (
        isinstance(name, str)
        and name in PROXY_META_TOOLS
        and isinstance(arguments, dict)
        and isinstance(arguments.get("name"), str)
    ):
        name = arguments.get("name")
        arguments = _coerce_arguments(arguments.get("arguments"))
    if not isinstance(name, str) or arguments is None:
        return None
    return arguments


def _config_cache_key(config: VisibilityConfig) -> str:
    """A stable key over the hide dimensions so a config edit invalidates the cache."""
    return json.dumps(config.to_wire(), sort_keys=True)


class VisibilityEnforcementMiddleware(Middleware):
    """Refuse-on-contact enforcement of the entity visibility filter (#2015).

    Installed innermost so its outbound scan sees the raw tool output before any
    other middleware transforms it. Consults the live config per request via the
    memoized loader; a passthrough (with only the config-load cost) whenever
    enforce is off or the config hides nothing.
    """

    def __init__(self, *, get_client: Callable[[], Any]) -> None:
        self._get_client = get_client
        self._lock = asyncio.Lock()
        self._cached_hidden: set[str] | None = None
        self._cached_regex: re.Pattern[str] | None = None
        self._cache_key: str | None = None
        self._cache_expiry = 0.0
        self._last_good_config: VisibilityConfig | None = None

    async def _load_config(self) -> VisibilityConfig:
        # Through the resolver module attribute so a test that redirects
        # ``resolver.get_data_dir`` steers this and ``load_hidden_set`` together.
        return await asyncio.to_thread(
            resolver.load_visibility_config, resolver.get_data_dir()
        )

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        # The active-mode gate must not crash the call with an opaque trace on an
        # unloadable config (a hand-edit typo would break EVERY tool call), nor
        # silently disable enforcement (the file is the only record of whether
        # enforce was on). Policy: fall back to the last config this process
        # loaded successfully — for the common non-enforce install that preserves
        # availability, and for an enforce install it preserves the boundary.
        # With no good load ever, fail CLOSED like PolicyMiddleware does.
        config: VisibilityConfig | None
        try:
            config = await self._load_config()
            self._last_good_config = config
        except Exception:
            config = self._last_good_config
            logger.warning(
                "visibility enforce: config load failed; %s",
                "using last-known-good config" if config else "failing closed",
                exc_info=True,
            )
        if config is None:
            _raise_enforced(context.message.name, _CONFIG_LOAD_FAILED_REASON)
        if not (
            config.enabled
            and config.enforce
            and config_has_active_hide_dimensions(config)
        ):
            return await call_next(context)

        name = context.message.name
        args = context.message.arguments or {}

        reason = _unscannable_reason(name, args)
        if reason is not None:
            logger.info("visibility enforce: refused unscannable call to %s", name)
            _raise_enforced(name, reason)

        try:
            hidden, regex = await self._hidden_and_regex(config)
        except VisibilityDataUnavailable:
            _raise_enforced(name, _FAIL_CLOSED_REASON)

        self._scan_inbound(name, args, hidden, regex)
        return await self._call_and_scan_outbound(name, context, call_next, regex)

    def _scan_inbound(
        self,
        name: str,
        args: dict[str, Any],
        hidden: set[str],
        regex: re.Pattern[str] | None,
    ) -> None:
        """Conceal on an exact hidden-id argument; refuse on an embedded one."""
        exact: str | None = None
        embedded = False
        if name in PROXY_META_TOOLS:
            inner = _unwrap_proxy_call(args)
            if inner is not None:
                exact, embedded = _scan_value(inner, hidden, regex)
        raw_exact, raw_embedded = _scan_value(args, hidden, regex)
        exact = exact or raw_exact
        embedded = embedded or raw_embedded
        if exact is not None:
            logger.info("visibility enforce: concealed hidden entity in %s call", name)
            _conceal_as_not_found(exact)
        if embedded:
            logger.info("visibility enforce: refused embedded hidden id in %s", name)
            _raise_enforced(name, _inbound_reason(name))

    async def _call_and_scan_outbound(
        self,
        name: str,
        context: MiddlewareContext,
        call_next: CallNext,
        regex: re.Pattern[str] | None,
    ) -> Any:
        """Run the tool; refuse if its output (or a tool error) names a hidden id."""
        try:
            result = await call_next(context)
        except ToolError as exc:
            if regex is not None and regex.search(_tool_error_text(exc)):
                logger.info("visibility enforce: refused tool error from %s", name)
                _raise_enforced(name, _outbound_reason(name))
            raise
        if regex is not None and regex.search(_serialize_result(result)):
            logger.info("visibility enforce: refused result from %s", name)
            _raise_enforced(name, _outbound_reason(name))
        return result

    async def _hidden_and_regex(
        self, config: VisibilityConfig
    ) -> tuple[set[str], re.Pattern[str] | None]:
        """Return the cached hidden set + regex, refreshing on TTL/config change.

        On a refresh failure while enforce is active: fall back to the last-known-
        good set; with no last-known-good ever, raise ``VisibilityDataUnavailable``
        so the caller fails closed. The lock prevents a refresh stampede.
        """
        key = _config_cache_key(config)
        now = time.monotonic()
        async with self._lock:
            if (
                self._cached_hidden is not None
                and self._cache_key == key
                and now < self._cache_expiry
            ):
                return self._cached_hidden, self._cached_regex
            try:
                hidden = await self._refresh_hidden_set(config)
            except Exception:
                if self._cached_hidden is not None:
                    logger.warning(
                        "visibility enforce: hidden-set refresh failed; using "
                        "last-known-good set",
                        exc_info=True,
                    )
                    return self._cached_hidden, self._cached_regex
                logger.warning(
                    "visibility enforce: hidden-set refresh failed with no "
                    "last-known-good; failing closed",
                    exc_info=True,
                )
                raise VisibilityDataUnavailable("hidden set unavailable") from None
            regex = _build_hidden_regex(hidden)
            self._cached_hidden = hidden
            self._cached_regex = regex
            self._cache_key = key
            self._cache_expiry = now + _CACHE_TTL_SECONDS
            return hidden, regex

    async def _refresh_hidden_set(self, config: VisibilityConfig) -> set[str]:
        """Fetch registry/states (+device registry when needed) and resolve strictly.

        Mirrors the search seam's fetches. Any fetch error, or a strict resolver
        degradation, raises so the caller applies its fallback policy.
        """
        client = self._get_client()
        need_device = config_needs_device_registry(config)
        coros: list[Any] = [
            client.get_states(),
            client.send_websocket_message({"type": "config/entity_registry/list"}),
        ]
        if need_device:
            coros.append(
                client.send_websocket_message({"type": "config/device_registry/list"})
            )
        fetched = await asyncio.gather(*coros, return_exceptions=True)
        for item in fetched:
            if isinstance(item, BaseException):
                raise VisibilityDataUnavailable("registry/state fetch failed") from item
        device_result = fetched[2] if need_device else None
        hidden, _warnings = await load_hidden_set(
            fetched[1], fetched[0], client, device_result, strict=True
        )
        return hidden
