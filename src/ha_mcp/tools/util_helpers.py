"""
Shared utility functions for MCP tool modules.

This module provides common helper functions used across multiple tool registration modules.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, overload

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)

logger = logging.getLogger(__name__)

# Strips ANSI terminal escape codes from container/log output.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_internal_fields(obj: Any, _seen: set[int] | None = None) -> Any:
    """Remove leading-underscore keys from ``obj`` and any nested dicts
    or lists in place.

    The ha-mcp tool layer enriches entity / area dicts with internal
    fields like ``_hidden_by`` and ``_aliases`` so downstream branches
    can rank without re-querying the entity registry. Those keys must
    not leak through public tool returns: this helper centralises the
    convention so individual call sites don't have to remember to strip.

    Mutates in place and returns the same reference for chaining. Cycle
    guard via ``_seen`` (id-tracked) keeps the recursion safe if a
    future caller ever feeds it a non-tree structure — JSON payloads
    don't, but the helper is now a generic utility (importable from
    ``server.py``) so the protection is cheap insurance.
    """
    if _seen is None:
        _seen = set()
    obj_id = id(obj)
    if obj_id in _seen:
        return obj
    if isinstance(obj, dict):
        _seen.add(obj_id)
        for key in [k for k in obj if isinstance(k, str) and k.startswith("_")]:
            obj.pop(key, None)
        for value in obj.values():
            strip_internal_fields(value, _seen)
    elif isinstance(obj, list):
        _seen.add(obj_id)
        for item in obj:
            strip_internal_fields(item, _seen)
    return obj


def public_fields(d: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``d`` with leading-underscore keys
    removed. Non-mutating counterpart to :func:`strip_internal_fields`.
    Shallow only — list/dict values are shared with the source, so a
    later mutation of those values would propagate.
    """
    return {
        k: v for k, v in d.items() if not (isinstance(k, str) and k.startswith("_"))
    }


def coerce_bool_param(
    value: bool | str | None,
    param_name: str = "parameter",
    default: bool | None = None,
) -> bool | None:
    """
    Coerce a value to a boolean, handling string inputs from AI tools.

    AI assistants using XML-style function calls pass boolean parameters as strings
    (e.g., "true" instead of true). This function safely converts such inputs.

    Args:
        value: The value to coerce (bool, str, or None)
        param_name: Parameter name for error messages
        default: Default value to return if value is None

    Returns:
        The coerced boolean value, or default if value is None

    Raises:
        ValueError: If the value cannot be converted to a boolean
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        value = value.strip().lower()
        if not value:
            return default
        if value in ("true", "1", "yes", "on"):
            return True
        if value in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{param_name} must be a boolean value, got '{value}'")

    raise ValueError(f"{param_name} must be bool or string, got {type(value).__name__}")


@overload
def coerce_int_param(
    value: int | str | None,
    param_name: str = ...,
    *,
    default: int,
    min_value: int | None = ...,
    max_value: int | None = ...,
) -> int: ...


@overload
def coerce_int_param(
    value: int | str | None,
    param_name: str = ...,
    *,
    default: None = ...,
    min_value: int | None = ...,
    max_value: int | None = ...,
) -> int | None: ...


def coerce_int_param(
    value: int | str | None,
    param_name: str = "parameter",
    *,
    default: int | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int | None:
    """
    Coerce a value to an integer, handling string inputs from AI tools.

    AI assistants often pass numeric parameters as strings (e.g., "100" instead of 100).
    This function safely converts such inputs to integers.

    Args:
        value: The value to coerce (int, str, or None)
        param_name: Parameter name for error messages
        default: Default value to return if value is None
        min_value: Optional minimum value constraint
        max_value: Optional maximum value constraint

    Returns:
        The coerced integer value, or default if value is None

    Raises:
        ValueError: If the value cannot be converted to an integer
    """
    if value is None:
        return default

    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        value = value.strip()
        if not value:
            return default
        try:
            # Handle float strings like "100.0" by converting via float first
            result = int(float(value))
        except ValueError:
            raise ValueError(
                f"{param_name} must be a valid integer, got '{value}'"
            ) from None
    else:
        raise ValueError(
            f"{param_name} must be int or string, got {type(value).__name__}"
        )

    # Apply constraints — raise for below-minimum (indicates caller bug),
    # clamp for above-maximum (soft cap for oversized requests)
    if min_value is not None and result < min_value:
        raise ValueError(f"{param_name} must be at least {min_value}, got {result}")
    if max_value is not None and result > max_value:
        result = max_value

    return result


def parse_json_param(
    param: str | dict | list | None, param_name: str = "parameter"
) -> dict | list | None:
    """
    Parse flexibly JSON string or return existing dict/list.

    Args:
        param: JSON string, dict, list, or None
        param_name: Parameter name for error context

    Returns:
        Parsed dict/list or original value if already correct type

    Raises:
        ValueError: If JSON parsing fails
    """
    if param is None:
        return None

    if isinstance(param, (dict, list)):
        return param

    if isinstance(param, str):
        try:
            parsed = json.loads(param)
            if not isinstance(parsed, (dict, list)):
                raise ValueError(
                    f"{param_name} must be a JSON object or array, got {type(parsed).__name__}"
                )
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {param_name}: {e}") from e

    raise ValueError(
        f"{param_name} must be string, dict, list, or None, got {type(param).__name__}"
    )


def parse_string_list_param(
    param: str | list[str] | None,
    param_name: str = "parameter",
    allow_csv: bool = False,
) -> list[str] | None:
    """Parse JSON string array or return existing list of strings.

    Args:
        param: Value to parse.
        param_name: Name for error messages.
        allow_csv: When True, plain strings are split on commas
            (e.g. ``"light,sensor"`` → ``["light", "sensor"]``).
            When False (default), non-JSON strings raise ValueError.
    """
    if param is None:
        return None

    if isinstance(param, list):
        if all(isinstance(item, str) for item in param):
            return param
        raise ValueError(f"{param_name} must be a list of strings")

    if isinstance(param, str):
        # Try JSON array first
        if param.strip().startswith("["):
            try:
                parsed = json.loads(param)
                if not isinstance(parsed, list):
                    raise ValueError(f"{param_name} must be a JSON array")
                if not all(isinstance(item, str) for item in parsed):
                    raise ValueError(f"{param_name} must be a JSON array of strings")
                return parsed
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in {param_name}: {e}") from e
        # Comma-separated fallback (opt-in)
        if allow_csv:
            return [item.strip() for item in param.split(",") if item.strip()]
        # Original behavior: attempt JSON parse (will fail for plain strings)
        try:
            parsed = json.loads(param)
            if not isinstance(parsed, list):
                raise ValueError(f"{param_name} must be a JSON array")
            if not all(isinstance(item, str) for item in parsed):
                raise ValueError(f"{param_name} must be a JSON array of strings")
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {param_name}: {e}") from e

    raise ValueError(f"{param_name} must be string, list, or None")


def build_pagination_metadata(
    total_count: int, offset: int, limit: int, count: int
) -> dict[str, Any]:
    """Build standardized pagination metadata for paginated responses.

    Args:
        total_count: Total number of items matching filters (before pagination).
        offset: Current pagination offset.
        limit: Maximum items per page (must be positive).
        count: Number of items in this page.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    has_more = (offset + count) < total_count
    return {
        "total_count": total_count,
        "offset": offset,
        "limit": limit,
        "count": count,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }


def unwrap_service_response(result: dict[str, Any]) -> dict[str, Any]:
    """Extract service_response from HA call_service result.

    HA's call_service with return_response wraps results in
    {"changed_states": [...], "service_response": {...}}.
    Returns service_response if present and is a dict, otherwise the original result.
    """
    sr = result.get("service_response")
    return sr if isinstance(sr, dict) else result


# Fields surfaced from each repair issue. Includes `ignored` / `dismissed_version`
# so callers can distinguish active vs. user-dismissed repairs when both are
# returned (e.g., `include_dismissed_repairs=True`).
_REPAIR_PROJECTION_FIELDS = (
    "issue_id",
    "domain",
    "severity",
    "translation_key",
    "ignored",
    "dismissed_version",
    "is_fixable",
    "breaks_in_ha_version",
    "created",
    "issue_domain",
)


def filter_active_repairs(
    issues: list[dict[str, Any]], *, include_dismissed: bool = False
) -> list[dict[str, Any]]:
    """Drop user-dismissed repairs unless ``include_dismissed`` is set.

    HA's `repairs/list_issues` returns both active and ignored repairs (the
    Repairs UI hides ignored ones by default). Mirror that UI default so
    overview / system-health responses don't surface repairs the user has
    already dismissed.
    """
    if include_dismissed:
        return list(issues)
    return [r for r in issues if not r.get("ignored")]


def project_repair_fields(issue: dict[str, Any]) -> dict[str, Any]:
    """Project a repair issue dict to the public-facing field subset.

    Drops verbose fields (`translation_placeholders`, `learn_more_url`) to
    keep overview payloads compact.
    """
    return {k: issue[k] for k in _REPAIR_PROJECTION_FIELDS if k in issue}


# Python logging numeric-level → canonical level name.
# Mirrors the values in HA's LOGSEVERITY constant (components/logger/const.py).
_LOG_LEVEL_NAMES: dict[int, str] = {
    0: "NOTSET",
    10: "DEBUG",
    20: "INFO",
    30: "WARNING",
    40: "ERROR",
    50: "CRITICAL",
}


def normalize_log_level(level: Any) -> str | None:
    """Normalize a numeric or string log level to its canonical uppercase name.

    Returns None if the value can't be recognized as a log level.
    """
    if isinstance(level, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(level, int):
        return _LOG_LEVEL_NAMES.get(level, f"LEVEL_{level}")
    if isinstance(level, str):
        stripped = level.strip().upper()
        if not stripped:
            return None
        return stripped
    return None


async def get_logger_levels(client: Any) -> dict[str, dict[str, Any]]:
    """Fetch current HA integration log levels via the ``logger/log_info`` WS command.

    Returns a mapping of integration domain (e.g. ``"mqtt"``) to a dict with:

    - ``name``: canonical level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``,
      ``"ERROR"``, ``"CRITICAL"``, ``"NOTSET"``, or ``"LEVEL_<n>"`` for
      non-standard ints).
    - ``raw``: the original numeric level (``int``) when HA returned an int,
      otherwise ``None`` (e.g. when the level was already provided as a string).

    Best-effort enrichment: returns an empty dict on connection/IO failures so
    callers can treat it as "no custom levels". Programming errors are not
    suppressed — they surface as bugs during development/CI.
    """
    try:
        result = await client.send_websocket_message({"type": "logger/log_info"})
    except (
        HomeAssistantConnectionError,
        HomeAssistantAPIError,
        HomeAssistantAuthError,
        TimeoutError,
        OSError,
    ) as exc:
        logger.debug("logger/log_info fetch failed: %s", exc)
        return {}

    if not isinstance(result, dict) or not result.get("success"):
        return {}

    entries = result.get("result", [])
    if not isinstance(entries, list):
        return {}

    levels: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        domain = entry.get("domain")
        if not isinstance(domain, str) or not domain:
            continue
        raw_level = entry.get("level")
        name = normalize_log_level(raw_level)
        if name is None:
            continue
        levels[domain] = {
            "name": name,
            "raw": raw_level
            if isinstance(raw_level, int) and not isinstance(raw_level, bool)
            else None,
        }
    return levels


async def add_timezone_metadata(client: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Add Home Assistant timezone to tool responses for local time context."""
    try:
        config = await client.get_config()
        ha_timezone = config.get("time_zone", "UTC")

        return {
            "data": data,
            "metadata": {
                "home_assistant_timezone": ha_timezone,
                "timestamp_format": "ISO 8601 (UTC)",
                "note": f"All timestamps are in UTC. Home Assistant timezone is {ha_timezone}.",
            },
        }
    except Exception:
        # Fallback if config fetch fails
        return {
            "data": data,
            "metadata": {
                "home_assistant_timezone": "Unknown",
                "timestamp_format": "ISO 8601 (UTC)",
                "note": "All timestamps are in UTC. Could not fetch Home Assistant timezone.",
            },
        }


# --- WS-event-driven wait helpers (#1152) -----------------------------------
#
# Background: every config write tool (`ha_config_set_helper`, set_automation,
# set_script, …) calls one of these three helpers after the API write returns,
# to confirm the operation reached the entity registry / state machine before
# the tool itself returns. Until #1152, those checks polled REST every 300ms
# up to a 10s budget. On a slow HA instance the poll could time out before
# the entity hydrated, surfacing a "Helper created but … not yet queryable"
# soft-failure warning even though the write succeeded — see #1152 for the
# agent-misattribution failure mode.
#
# The new pattern is WS-event-driven with a REST sample after subscribe and a
# slow REST backstop, falling back to pure REST polling when the WebSocket is
# unavailable:
#
#   1. Open a `state_changed` (and, for registry-add/remove waits, an
#      `entity_registry_updated`) subscription via `subscribe_events`. The
#      subscription must be live BEFORE we look at the world so we don't miss
#      the event the write triggered.
#   2. Take a single REST sample. This closes the "the event fired between
#      the write returning and our subscribe landing" window — if the entity
#      is already in the desired shape, we return immediately and never
#      touch the event loop.
#   3. Await events for our entity_id, then re-sample. A
#      ``_POLLING_BACKSTOP_INTERVAL`` REST sample also runs every few seconds
#      independently of events, so a silent-broken subscription degrades to
#      a slow-polling REST loop rather than a 10s hang.
#   4. Drop the subscription and event handler in `finally`.
#
# Connection-drop awareness: if `get_websocket_client()` or `subscribe_events`
# fails, we fall through to ``_legacy_poll_until`` (the pre-#1152 REST loop)
# transparently, so the helpers still work on REST-only deployments and during
# HA-mid-restart windows. The legacy loop is also what we call when the WS
# subscription itself fails to set up — the helpers' contract (return bool or
# state dict, never raise on the happy path) is identical to before.

_POLLING_BACKSTOP_INTERVAL = 2.0
"""Seconds between independent REST samples while a WS subscription is open.

Bounded slow-poll backstop so a silent-broken WS subscription still
resolves within the helper's timeout. A 10s budget with a 2s backstop
costs at most ~6 REST calls per wait (one post-subscribe sample plus
~5 backstop samples), vs. ~33 calls for the previous 300ms loop."""


async def _legacy_poll_until(
    entity_id: str,
    sample: Callable[[], Awaitable[Any]],
    *,
    timeout: float,
    poll_interval: float,
    description: str,
) -> Any:
    """REST-polling waiter used as the WS-subscription fallback path.

    ``sample`` is the same callable the WS path runs after each event /
    backstop tick — it returns a truthy value when the wait should
    succeed, ``None`` otherwise. Connection / auth errors propagate
    (callers care about those); other transient errors raised inside
    ``sample`` are swallowed there.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            result = await sample()
            if result is not None:
                logger.debug(
                    f"REST waiter: {description} for {entity_id} resolved "
                    f"after {time.monotonic() - start:.2f}s"
                )
                return result
        except (HomeAssistantConnectionError, HomeAssistantAuthError):
            raise
        await asyncio.sleep(poll_interval)
    logger.warning(
        f"REST fallback: {description} for {entity_id} timed out after {timeout}s"
    )
    return None


async def _get_waiter_ws_client(client: Any) -> Any:
    """Return a connected WS client to use for waiter subscriptions, or None.

    Returning ``None`` triggers REST-only fallback in
    ``_ws_wait_for_condition``. Localised import avoids a top-level cycle
    (websocket_client → rest_client → util_helpers → websocket_client).
    """
    try:
        from ..client.websocket_client import get_websocket_client
    except ImportError as e:  # pragma: no cover - import-time defence
        logger.debug("WS waiter import failed: %s", e)
        return None

    base_url = getattr(client, "base_url", None)
    token = getattr(client, "token", None)
    try:
        if base_url and token:
            ws_client = await get_websocket_client(url=base_url, token=token)
        else:
            ws_client = await get_websocket_client()
    except HomeAssistantAuthError:
        # Auth failures must reach the caller — a bad token should surface
        # as a real error, not as a 10s "timed out" via REST fallback.
        # silent-failure-hunter #1382.
        raise
    except (HomeAssistantConnectionError, OSError, TimeoutError) as e:
        logger.debug("WS waiter could not obtain ws client: %s", e)
        return None

    if not getattr(ws_client, "is_connected", False):
        return None
    return ws_client


async def _ws_wait_for_condition(
    client: Any,
    entity_id: str,
    sample: Callable[[], Awaitable[Any]],
    *,
    event_types: tuple[str, ...],
    timeout: float,
    poll_interval: float,
    description: str,
) -> Any:
    """Subscribe to ``event_types``, sample after subscribe, wait on event.

    Implements the standard "subscribe → sample → wait" pattern from #1152:

    - The handler nudges a single ``asyncio.Event`` whenever HA pushes an
      event for our ``entity_id``. The main loop wakes on that nudge or on
      the polling-backstop timeout, then re-runs ``sample`` (the REST
      source-of-truth check) to decide whether the wait succeeded.
    - Sample-after-subscribe (not before) closes the gap between the
      caller's write returning and our subscription landing on the HA
      side. The event for the write may have already fired by the time we
      subscribe; the post-subscribe sample catches that.
    - If the WS path fails to set up (no WS client, no subscription, …)
      we fall back to ``_legacy_poll_until``. The helpers' contract is
      identical to the pre-#1152 REST loop in that case.

    Returns ``sample``'s truthy return value, or ``None`` on timeout.
    """
    ws_client = await _get_waiter_ws_client(client)
    if ws_client is None:
        return await _legacy_poll_until(
            entity_id,
            sample,
            timeout=timeout,
            poll_interval=poll_interval,
            description=description,
        )

    nudge = asyncio.Event()

    async def handler(event: dict[str, Any]) -> None:
        # HA nests ``entity_id`` under ``event["data"]`` for both
        # state_changed and entity_registry_updated. The top-level fallback
        # is defensive only — it lets a future schema drift degrade to a
        # missed nudge rather than an AttributeError.
        data = event.get("data") or {}
        evt_entity = data.get("entity_id") or event.get("entity_id")
        if evt_entity == entity_id:
            nudge.set()

    # Track which handlers / subscriptions we actually attached so cleanup
    # is exact even if subscribe_events raises partway through.
    attached_handlers: list[str] = []
    sub_ids: list[int] = []
    try:
        for et in event_types:
            ws_client.add_event_handler(et, handler)
            attached_handlers.append(et)
        for et in event_types:
            try:
                sub_id = await ws_client.subscribe_events(et)
            except HomeAssistantAuthError:
                # Auth errors must surface — see _get_waiter_ws_client.
                raise
            except (
                HomeAssistantConnectionError,
                HomeAssistantCommandError,
                OSError,
                TimeoutError,
            ) as e:
                logger.debug(
                    "subscribe_events(%s) failed during %s for %s: %s — "
                    "falling back to REST polling",
                    et,
                    description,
                    entity_id,
                    e,
                )
                return await _legacy_poll_until(
                    entity_id,
                    sample,
                    timeout=timeout,
                    poll_interval=poll_interval,
                    description=description,
                )
            sub_ids.append(sub_id)

        start = time.monotonic()
        # Sample-after-subscribe: covers the "event fired before subscribe
        # landed" race. This is where most happy-path waits resolve.
        try:
            result = await sample()
            if result is not None:
                logger.debug(
                    f"WS waiter: {description} for {entity_id} resolved by "
                    f"post-subscribe sample after {time.monotonic() - start:.2f}s"
                )
                return result
        except (HomeAssistantConnectionError, HomeAssistantAuthError):
            raise

        # If the WS dropped between subscribe and the post-subscribe sample,
        # skip the wait loop entirely — we'd burn up to one backstop interval
        # waiting for events that will never arrive. Connection-drop coverage
        # symmetric with the in-loop check below.
        if not ws_client.is_connected:
            logger.debug(
                "WS connection dropped before wait loop for %s on %s — "
                "completing via REST polling",
                description,
                entity_id,
            )
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                return None
            return await _legacy_poll_until(
                entity_id,
                sample,
                timeout=remaining,
                poll_interval=poll_interval,
                description=description,
            )

        while time.monotonic() - start < timeout:
            # Wait for either an event nudge or the polling backstop. The
            # backstop guards against silently-broken subscriptions and
            # late-binding state hydration the event stream doesn't
            # advertise.
            remaining = timeout - (time.monotonic() - start)
            wait_budget = min(remaining, _POLLING_BACKSTOP_INTERVAL)
            try:
                await asyncio.wait_for(nudge.wait(), timeout=wait_budget)
                nudge.clear()
            except TimeoutError:
                pass

            # Connection-drop awareness: if the WS dropped while we were
            # waiting, the OperationManager / pool will reconnect lazily
            # but our subscription is gone. Fall back to REST polling for
            # the remaining budget rather than wait silently for events
            # that will never arrive.
            if not ws_client.is_connected:
                logger.debug(
                    "WS connection dropped during %s for %s — completing "
                    "wait via REST polling",
                    description,
                    entity_id,
                )
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    return None
                return await _legacy_poll_until(
                    entity_id,
                    sample,
                    timeout=remaining,
                    poll_interval=poll_interval,
                    description=description,
                )

            try:
                result = await sample()
                if result is not None:
                    logger.debug(
                        f"WS waiter: {description} for {entity_id} resolved "
                        f"after {time.monotonic() - start:.2f}s"
                    )
                    return result
            except (HomeAssistantConnectionError, HomeAssistantAuthError):
                raise

        logger.warning(
            f"WS waiter: {description} for {entity_id} timed out after {timeout}s"
        )
        return None
    finally:
        for et in attached_handlers:
            ws_client.remove_event_handler(et, handler)
        for sub_id in sub_ids:
            # ``unsubscribe_events`` narrows internally: OSError → debug,
            # HomeAssistantCommandError → warning, everything else propagates.
            # The outer catch here guards against the round-trip's WS-level
            # failure modes (connection reset by another caller, send_command
            # timeout) so a cleanup hiccup never masks the wait's real result.
            # Narrow set — programming bugs (TypeError, AttributeError) must
            # propagate. ``send_command`` currently raises a bare
            # ``Exception("Command timeout")`` on timeout, which is matched
            # by name here until that helper is fixed upstream.
            try:
                await ws_client.unsubscribe_events(sub_id)
            except (HomeAssistantConnectionError, OSError, TimeoutError) as e:
                logger.warning(
                    "unsubscribe_events(%s) cleanup failed (subscription "
                    "may leak until WS pool reconnects): %s",
                    sub_id,
                    e,
                )
            except Exception as e:
                # ``send_command`` raises bare Exception("Command timeout") on
                # WS round-trip timeout. Treat that specifically as cleanup
                # noise; anything else re-raises.
                if str(e) == "Command timeout":
                    logger.warning(
                        "unsubscribe_events(%s) cleanup timed out on WS "
                        "round-trip; subscription may leak until WS pool "
                        "reconnects",
                        sub_id,
                    )
                else:
                    raise


async def wait_for_entity_registered(
    client: Any,
    entity_id: str,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
) -> bool:
    """
    Wait until an entity is registered and accessible via the state API.

    Used after config create/update operations to confirm the entity is queryable.
    Listens to ``state_changed`` and ``entity_registry_updated`` events on the
    WebSocket and falls back to REST polling (every ``poll_interval`` seconds)
    when the WebSocket is unavailable. See the module-level note above for the
    subscribe→sample→wait pattern and the failure mode it addresses (#1152).

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity ID to wait for (e.g., 'automation.morning_routine')
        timeout: Maximum time to wait in seconds
        poll_interval: REST poll interval used for the WS-unavailable fallback

    Returns:
        True if entity became accessible, False if timed out
    """

    async def sample() -> bool | None:
        try:
            state = await client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                return None
            logger.warning(f"Unexpected API error sampling {entity_id}: {e}")
            return None
        return True if state else None

    result = await _ws_wait_for_condition(
        client,
        entity_id,
        sample,
        # entity_registry_updated fires when the registry row is added,
        # state_changed when the state machine row hydrates. We watch both
        # so the post-event sample lands as soon as either side completes.
        event_types=("state_changed", "entity_registry_updated"),
        timeout=timeout,
        poll_interval=poll_interval,
        description="entity registration",
    )
    if result is True:
        return True
    logger.warning(f"Entity {entity_id} not registered within {timeout}s")
    return False


async def wait_for_entity_removed(
    client: Any,
    entity_id: str,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
) -> bool:
    """
    Wait until an entity is no longer accessible via the state API.

    Used after config delete operations to confirm the entity is gone. Listens
    to ``state_changed`` and ``entity_registry_updated`` removal events on the
    WebSocket and falls back to REST polling (every ``poll_interval`` seconds)
    when the WebSocket is unavailable. See #1152 for context.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity ID to wait for removal
        timeout: Maximum time to wait in seconds
        poll_interval: REST poll interval used for the WS-unavailable fallback

    Returns:
        True if entity was removed, False if timed out (entity still exists)
    """

    async def sample() -> bool | None:
        try:
            state = await client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                return True
            logger.warning(f"Unexpected API error sampling {entity_id} removal: {e}")
            return None
        # Falsy state == entity is gone from the state machine.
        return True if not state else None

    result = await _ws_wait_for_condition(
        client,
        entity_id,
        sample,
        event_types=("state_changed", "entity_registry_updated"),
        timeout=timeout,
        poll_interval=poll_interval,
        description="entity removal",
    )
    if result is True:
        return True
    logger.warning(f"Entity {entity_id} still exists after {timeout}s")
    return False


async def wait_for_state_change(
    client: Any,
    entity_id: str,
    expected_state: str | None = None,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
    initial_state: str | None = None,
) -> dict[str, Any] | None:
    """
    Wait until an entity's state changes (optionally to a specific value).

    Used after service calls to verify the operation took effect. Listens to
    ``state_changed`` events on the WebSocket and falls back to REST polling
    (every ``poll_interval`` seconds) when the WebSocket is unavailable. See
    #1152 for context.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity to monitor
        expected_state: If set, wait for this specific state value.
                        If None, wait for any change from initial_state.
        timeout: Maximum time to wait in seconds
        poll_interval: REST poll interval used for the WS-unavailable fallback
        initial_state: The state before the operation. If None, it will be
                       fetched automatically.

    Returns:
        The entity state dict if the change was detected, None if timed out
    """
    if initial_state is None:
        try:
            raw_initial = await client.get_entity_state(entity_id)
            if isinstance(raw_initial, dict):
                initial_state = raw_initial.get("state")
        except HomeAssistantAPIError:
            logger.debug(
                f"Could not fetch initial state for {entity_id} — will detect any change"
            )
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
            logger.warning(
                f"Connection/auth error fetching initial state for {entity_id}: {e}"
            )
            raise

    # Mutable closure cell so the sampler can adopt the first observed state
    # as the baseline when the initial fetch failed — matches the original
    # REST-loop semantics.
    baseline: dict[str, str | None] = {"state": initial_state}

    async def sample() -> dict[str, Any] | None:
        try:
            raw = await client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            logger.debug(f"API error sampling {entity_id} state: {e}")
            return None
        if not isinstance(raw, dict):
            return None
        current = raw.get("state")
        if expected_state is not None and current == expected_state:
            return raw
        if (
            expected_state is None
            and baseline["state"] is not None
            and current != baseline["state"]
        ):
            return raw
        if expected_state is None and baseline["state"] is None and current is not None:
            baseline["state"] = current
        return None

    result = await _ws_wait_for_condition(
        client,
        entity_id,
        sample,
        event_types=("state_changed",),
        timeout=timeout,
        poll_interval=poll_interval,
        description="state change",
    )
    if isinstance(result, dict):
        return result
    logger.warning(f"Entity {entity_id} state did not change within {timeout}s")
    return None


async def fetch_entity_category(client: Any, entity_id: str, scope: str) -> str | None:
    """Fetch a category ID for an entity from the entity registry.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity to look up (e.g., 'automation.morning_routine')
        scope: Category scope (e.g., 'automation', 'script', 'helpers')

    Returns:
        Category ID string if set, None otherwise
    """
    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
        if result.get("success"):
            categories = result.get("result", {}).get("categories", {})
            cat_id = categories.get(scope)
            return str(cat_id) if cat_id is not None else None
    except Exception as e:
        logger.warning(f"Failed to fetch category for {entity_id}: {e}")
    return None


async def apply_entity_category(
    client: Any,
    entity_id: str,
    category: str,
    scope: str,
    result_dict: dict[str, Any],
    entity_type: str = "entity",
) -> None:
    """Apply a category to an entity via the entity registry.

    Updates result_dict in-place: sets ``'category'`` on success, or appends
    to the top-level ``'warnings'`` list on failure. The list shape mirrors
    the canonical response contract documented in ``AGENTS.md`` →
    *Writing MCP Tools → Return Values*.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity to update
        category: Category ID to assign
        scope: Category scope (e.g., 'automation', 'script')
        result_dict: Tool result dict to update with category status
        entity_type: Human-readable type for warning messages
    """
    try:
        ws_result = await client.send_websocket_message(
            {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "categories": {scope: category},
            }
        )
        if ws_result.get("success"):
            result_dict["category"] = category
        else:
            error_detail = ws_result.get("error", {})
            error_msg = (
                error_detail.get("message", "Unknown error")
                if isinstance(error_detail, dict)
                else str(error_detail)
            )
            logger.warning(f"Failed to set category for {entity_id}: {error_msg}")
            result_dict.setdefault("warnings", []).append(
                f"{entity_type.capitalize()} saved but failed to set category: {error_msg}"
            )
    except Exception as e:
        logger.warning(f"Failed to set category for {entity_id}: {e}")
        result_dict.setdefault("warnings", []).append(
            f"{entity_type.capitalize()} saved but failed to set category: {e}"
        )


def coerce_to_list(value: Any) -> list[Any]:
    """Return value as a list: list → as-is, dict/other → [value], None/falsy → []."""
    if isinstance(value, list):
        return value
    return [value] if value else []


def merge_validation_meta(
    result: dict[str, Any], validation_meta: dict[str, Any]
) -> None:
    """Attach reference-validator output to a set-tool success ``result``.

    Produces a single nested ``validation`` field when there's anything
    worth reporting - warnings, skipped templates, or a blueprint
    short-circuit. Keeps the happy-path response unchanged.

    Shared between ``ha_config_set_automation`` and
    ``ha_config_set_script``; see
    :mod:`ha_mcp.tools.reference_validator` for the validator itself
    and #940 for background.
    """
    warnings = validation_meta.get("warnings") or []
    unvalidated_templates = validation_meta.get("unvalidated_templates") or 0
    blueprint_skipped = bool(validation_meta.get("blueprint_skipped"))

    if not warnings and not unvalidated_templates and not blueprint_skipped:
        return

    entry: dict[str, Any] = {}
    if warnings:
        entry["warnings"] = warnings
    if unvalidated_templates:
        entry["unvalidated_templates"] = unvalidated_templates
    if blueprint_skipped:
        entry["blueprint_skipped"] = True
    result["validation"] = entry


DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS = 60.0


def parse_diagnostics_fields(value: list[str] | str | None) -> list[str] | None:
    """Normalise the ``diagnostics_fields`` MCP-tool parameter to ``list[str] | None``.

    Accepts a native list of strings, a JSON-encoded list (e.g.
    ``'["home_assistant","issues"]'``), or a comma-separated string
    (e.g. ``'home_assistant, issues'``). Empty / whitespace-only input
    coerces to ``None`` so the diagnostics helper skips the projection.

    Raises:
        ValueError: when the input is not parseable as a list of strings —
            specifically: JSON that fails to decode, JSON that decodes to a
            non-list value (object, scalar), or any non-(list/str/None) type.
    """
    if value is None:
        return None
    if isinstance(value, list):
        parsed = [str(v).strip() for v in value if str(v).strip()]
        return parsed or None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"diagnostics_fields must be a valid JSON list, got '{stripped}': {e}"
                ) from e
            if not isinstance(decoded, list):
                raise ValueError(
                    f"diagnostics_fields JSON must decode to a list, got {type(decoded).__name__}"
                )
            parsed = [str(v).strip() for v in decoded if str(v).strip()]
            return parsed or None
        if stripped.startswith("{"):
            raise ValueError(
                "diagnostics_fields must decode to a list, got a JSON object"
            )
        parsed = [p.strip() for p in stripped.split(",") if p.strip()]
        return parsed or None
    raise ValueError(
        f"diagnostics_fields must be list, string, or None; got {type(value).__name__}"
    )


async def fetch_integration_diagnostics(
    client: Any,
    config_entry_id: str | None,
    device_id: str | None = None,
    *,
    timeout_seconds: float = DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS,
    fields: list[str] | None = None,
    truncate_at_bytes: int | None = None,
    data_path: str | None = None,
    data_offset: int = 0,
    data_limit: int | None = None,
) -> dict[str, Any]:
    """Get the integration diagnostics dump from HA's diagnostics REST endpoint.

    Hits ``GET /api/diagnostics/config_entry/{entry_id}`` (or the device-scoped
    variant when ``device_id`` is set). Requires a valid admin-scope token —
    401 surfaces as invalid/expired token, 403 as insufficient scope. Same
    artifact users grab via Settings → Devices & Services → [integration] → ⋯
    → Download diagnostics.

    Returns an embeddable sub-dict so callers can attach it to a larger response
    without raising on diagnostics-specific failures (matches the
    ``_fetch_repairs`` / ``_fetch_zha_network`` convention in
    ``tools_system.py``). The ``DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS`` default
    covers slow integrations like ZHA on large networks.

    Args:
        client: REST client exposing an async ``_request(method, endpoint,
            *, timeout)`` method that returns the decoded JSON body.
        config_entry_id: Config-entry id of the integration. Required;
            ``None`` / empty string short-circuits with a structured error
            sub-dict and no backend call.
        device_id: Optional device id under the entry. When set, switches
            the endpoint to the device-scoped variant.
        timeout_seconds: Per-request timeout (default
            ``DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS`` = 60.0s). ZHA dumps on
            large networks can run 30-60s, so the default is generous.
        fields: Optional list of top-level keys to keep from the integration's
            ``data`` payload (e.g. ``["home_assistant", "issues"]`` for Hue).
            Trims the payload before it hits the LLM context budget. Unknown
            keys are silently dropped; an ``omitted_fields`` list surfaces
            which requested keys weren't present. Only applies when ``data``
            is a dict. Applied before ``data_path``.
        truncate_at_bytes: Optional byte cap on the serialized resolved value
            (the post-``fields``/``data_path`` payload, or its paginated
            ``items`` when pagination applies). On hit, drops ``data`` /
            ``items`` and emits ``truncated: True``, ``bytes_total: <actual>``,
            ``byte_cap: <cap>``, plus ``available_fields: <top-level keys>``
            (when the capped value is a dict) so the model knows which
            ``fields`` or sub-path to request on the next call. Applied last.
        data_path: Optional dotted path into ``data`` to walk into a sub-tree
            (e.g. ``"data.devices"``, ``"home_assistant.version"``). Resolution
            failures (missing key, non-traversable value) replace ``data`` with
            ``null`` and add ``data_path_error`` to the result. When the
            resolved value is a list and ``data_limit`` is set, pagination
            applies — see ``data_limit``. Applied after ``fields``.
        data_offset: Pagination start index for list-valued ``data_path``
            results (default ``0``). Ignored when ``data_path`` is unset or
            ``data_limit`` is unset, or the resolved value is not a list.
        data_limit: Pagination window size for list-valued ``data_path``
            results. When set with a list-resolved path, swaps ``data`` for
            a pagination envelope ``{"path", "items", "offset", "limit",
            "total", "has_more"}``. Default ``None`` (return the full
            resolved value).
    """
    result: dict[str, Any] = {"config_entry_id": config_entry_id}
    if device_id:
        result["device_id"] = device_id

    if not config_entry_id:
        result["error"] = (
            "config_entry_id is required for diagnostics fetch. "
            "Use ha_get_integration() to find the config_entry_id for the "
            "integration."
        )
        return result

    endpoint = f"/diagnostics/config_entry/{config_entry_id}"
    if device_id:
        endpoint += f"/device/{device_id}"

    try:
        result["data"] = await client._request("GET", endpoint, timeout=timeout_seconds)
    except HomeAssistantAuthError as e:
        logger.warning("Diagnostics fetch auth error: %s", e)
        result["error"] = (
            "Authentication failed for diagnostics endpoint (HTTP 401): the "
            "configured access token is invalid or expired. Generate a new "
            "long-lived access token from the HA user profile page."
        )
    except HomeAssistantAPIError as e:
        status = getattr(e, "status_code", None)
        if status == 404:
            scope = "device" if device_id else "config entry"
            result["error"] = (
                f"Diagnostics not available for this {scope}: integration may "
                "not implement the diagnostics platform, or the id is invalid. "
                "Verify via ha_get_integration()."
            )
            logger.debug("Diagnostics not available (404): %s", e)
        elif status == 403:
            result["error"] = (
                "Diagnostics endpoint refused the request: admin scope required "
                "(HA's @http.require_admin gate)."
            )
            logger.warning("Diagnostics fetch refused (403): %s", e)
        else:
            result["error"] = (
                f"Diagnostics fetch failed (HTTP {status or '<status>'}): {e}"
            )
            logger.warning("Diagnostics fetch API error: %s", e)
    except HomeAssistantConnectionError as e:
        msg = str(e)
        if "timeout" in msg.lower():
            result["error"] = (
                f"Diagnostics fetch timed out after {timeout_seconds:.1f}s "
                "(ZHA dumps on large networks can exceed this; the integration "
                "may be too slow to return diagnostics on this network)"
            )
        else:
            result["error"] = f"Diagnostics fetch connection failed: {e}"
        logger.warning("Diagnostics fetch connection error: %s", e)
    except Exception as e:  # pragma: no cover - defensive last-resort guard
        logger.warning(
            "Diagnostics fetch unexpected error: %s: %s", type(e).__name__, e
        )
        result["error"] = f"Diagnostics fetch failed: {e}"

    if "data" in result and result["data"] is None and "error" not in result:
        # Empty response body — distinct from an explicit error or a
        # cap-driven drop. Surface it as an error so callers don't confuse
        # ``{"data": null}`` with a successful zero-payload fetch.
        result["error"] = "Diagnostics endpoint returned an empty body"
        del result["data"]

    if "data" in result:
        _project_cap_and_paginate_diagnostics(
            result, fields, truncate_at_bytes, data_path, data_offset, data_limit
        )

    return result


def _project_cap_and_paginate_diagnostics(
    result: dict[str, Any],
    fields: list[str] | None,
    truncate_at_bytes: int | None,
    data_path: str | None,
    data_offset: int,
    data_limit: int | None,
) -> None:
    """Apply field projection, data_path walk, optional pagination, then byte
    cap. Mutates ``result`` (adds keys, may replace or delete
    ``result["data"]``). When pagination produced an envelope and the cap
    fires, the envelope's metadata (``path``, ``offset``, ``limit``,
    ``total``, ``has_more``) is preserved sans ``items`` so the caller can
    issue a narrower follow-up; the unpaginated case drops ``data`` entirely.

    See ``fetch_integration_diagnostics`` for the public contract.
    """
    data = result.get("data")

    if fields and isinstance(data, dict):
        kept = {k: data[k] for k in fields if k in data}
        # Dedup caller-supplied duplicates while preserving order.
        omitted = list(dict.fromkeys(k for k in fields if k not in data))
        result["data"] = kept
        if omitted:
            result["omitted_fields"] = omitted
        data = kept

    # Whitespace-only (or empty) paths normalize to "unset"; surface a warning
    # so callers can tell their intent was swallowed instead of resolving
    # silently. The earlier whitespace branch nulls ``data_path``, so the
    # ``elif data_offset > 0`` branch below is guarded against clobbering
    # this warning when both inputs land together.
    if data_path is not None and not data_path.strip():
        result["data_pagination_warning"] = (
            "data_path ignored: value was empty or whitespace-only"
        )
        data_path = None

    paginated = False
    if data_path:
        resolved, path_error = _resolve_data_path(data, data_path)
        if path_error is not None:
            result["data"] = None
            result["data_path_error"] = path_error
            data = None
        else:
            result["data_path"] = data_path
            if isinstance(resolved, list) and data_limit is not None:
                total = len(resolved)
                start = max(0, data_offset)
                end = start + data_limit
                items = resolved[start:end]
                page: dict[str, Any] = {
                    "path": data_path,
                    "items": items,
                    "offset": start,
                    "limit": data_limit,
                    "total": total,
                    "has_more": end < total,
                }
                result["data"] = page
                data = items
                paginated = True
            else:
                # Pagination intent has nowhere to apply: either ``data_limit``
                # is set but the resolved value isn't a list, or ``data_offset``
                # is set without ``data_limit`` (no window to slice). Surface
                # a structured warning rather than silently dropping the kwarg.
                if data_limit is not None:
                    type_name = "null" if resolved is None else type(resolved).__name__
                    result["data_pagination_warning"] = (
                        f"data_limit ignored: resolved value at '{data_path}' "
                        f"is {type_name}, not a list"
                    )
                elif data_offset > 0:
                    result["data_pagination_warning"] = (
                        "data_offset ignored: data_limit not set "
                        "(no pagination window to slice)"
                    )
                result["data"] = resolved
                data = resolved
    elif data_offset > 0 and "data_pagination_warning" not in result:
        # ``data_offset`` set without ``data_path`` — the resolver branch is
        # skipped entirely, so the offset has no effect on the response.
        # Mirrors the orphan-warning gates at the tool layer. Guarded so the
        # whitespace-path warning above isn't clobbered when both inputs
        # land together (the whitespace input nulled ``data_path``, dropping
        # us into this elif; the earlier warning takes precedence).
        result["data_pagination_warning"] = (
            "data_offset ignored: data_path not set (no resolved sub-tree to paginate)"
        )

    if truncate_at_bytes is not None and data is not None:
        try:
            serialized = json.dumps(data, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            # Non-serialisable payload (shouldn't happen for HA diagnostics, but
            # don't suppress the data on a serializer hiccup).
            return
        bytes_total = len(serialized.encode("utf-8"))
        if bytes_total > truncate_at_bytes:
            result["truncated"] = True
            result["bytes_total"] = bytes_total
            result["byte_cap"] = truncate_at_bytes
            if paginated:
                # ``paginated=True`` is only set on the branch that writes a
                # dict envelope to ``result["data"]`` — preserve the metadata
                # (path / offset / limit / total / has_more) so the caller can
                # shrink the window in the next call. Only ``items`` is dropped.
                envelope = result["data"]
                preserved = {k: v for k, v in envelope.items() if k != "items"}
                preserved["truncated"] = True
                result["data"] = preserved
            else:
                if isinstance(data, dict):
                    result["available_fields"] = sorted(data.keys())
                del result["data"]


def _resolve_data_path(data: Any, path: str) -> tuple[Any, str | None]:
    """Walk ``data`` along the dotted ``path`` and return ``(value, error)``.

    Returns ``(value, None)`` on success or ``(None, error_message)`` when
    a segment can't be resolved (missing key, descent into non-dict / null,
    empty path). List indices are not supported: address a list-valued
    sub-tree by name and let the caller's pagination kwargs (``data_offset``
    / ``data_limit``) slice it. Index-segment support is a candidate
    follow-up.

    Limitation: dotted keys (e.g. ``sensor.zha_temp_42``, MQTT-style topics
    containing a literal ``.``) are not addressable — the path is split on
    ``.`` without escape support. Workaround: omit ``data_path`` and walk
    the returned payload in the caller. Escape syntax is a candidate
    follow-up.
    """
    if not path or not path.strip():
        return None, "data_path must be a non-empty dotted path"
    segments = path.split(".")
    current: Any = data
    walked: list[str] = []
    for seg in segments:
        if not seg:
            return None, (
                f"data_path '{path}' has an empty segment (after '{'.'.join(walked)}')"
            )
        if current is None:
            return None, (
                f"data_path '{path}' resolved to null at "
                f"'{'.'.join(walked) or '<root>'}' — "
                "sub-tree not present in this payload"
            )
        if not isinstance(current, dict):
            return None, (
                f"data_path '{path}' cannot descend into "
                f"{type(current).__name__} at '{'.'.join(walked) or '<root>'}'"
            )
        if seg not in current:
            available = sorted(current.keys()) if isinstance(current, dict) else []
            # Only mention the dotted-key limitation when the available keys
            # at this level actually contain one — a plain typo (e.g.
            # ``data.versionz``) shouldn't be told its ``.`` is being
            # mis-parsed as a separator when no sibling key has a ``.`` in it.
            ambiguous = any(isinstance(k, str) and "." in k for k in available)
            if ambiguous:
                hint = (
                    "; note: a sibling key here contains a literal '.' which "
                    "is not addressable via data_path — omit data_path and "
                    "walk the returned payload in the caller"
                )
            else:
                hint = ""
            return None, (
                f"data_path '{path}' missing key '{seg}' at "
                f"'{'.'.join(walked) or '<root>'}' "
                f"(available: {available}{hint})"
            )
        current = current[seg]
        walked.append(seg)
    return current, None
