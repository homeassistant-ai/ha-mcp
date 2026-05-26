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
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)

logger = logging.getLogger(__name__)

# Strips ANSI terminal escape codes from container/log output.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def websocket_error_message(error: Any) -> str:
    """Extract a readable message from a Home Assistant websocket error."""
    if isinstance(error, dict):
        return str(error.get("message", error))
    return str(error)


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


def _parse_json_to_str_list(s: str, param_name: str) -> list[str]:
    """Parse a JSON string as a list of strings, raising ValueError on failure."""
    try:
        parsed = json.loads(s)
        if not isinstance(parsed, list):
            raise ValueError(f"{param_name} must be a JSON array")
        if not all(isinstance(item, str) for item in parsed):
            raise ValueError(f"{param_name} must be a JSON array of strings")
        return parsed
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {param_name}: {e}") from e


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
        if param.strip().startswith("["):
            return _parse_json_to_str_list(param, param_name)
        if allow_csv:
            return [item.strip() for item in param.split(",") if item.strip()]
        return _parse_json_to_str_list(param, param_name)

    raise ValueError(f"{param_name} must be string, list, or None")


def project_entity_record(
    record: dict[str, Any],
    fields: list[str] | None,
    attribute_keys: list[str] | None,
) -> tuple[dict[str, Any], str | None]:
    """Apply optional field projection to a HA entity record.

    ``fields`` filters which top-level keys to keep (e.g. ["state", "attributes"]).
    ``attribute_keys`` further filters the ``attributes`` sub-dict.
    Both default None = full payload (no-op).

    Returns ``(projected_record, warning_string | None)``.  *warning_string* is
    non-None when ``attribute_keys`` was specified, the original ``attributes``
    dict was non-empty, and the filter produced an empty result — i.e. the caller
    supplied only unknown attribute keys (typo guard).  Callers should append the
    warning to the response ``warnings`` list so the user receives a diagnostic
    rather than a silently empty ``attributes: {}``.

    Both parameters are already parsed into ``list[str] | None`` — string/CSV inputs
    must be normalised at the call site via ``parse_string_list_param`` (see
    ``ha_get_state`` which parses once before the bulk loop to avoid re-parsing per
    entity record).

    Unlike ``project_fields``, this helper does not auto-retain ``success`` — entity
    records have no ``success`` field, so the asymmetry is intentional.

    Non-dict ``attributes`` handling: when ``attribute_keys`` is set but the
    record's ``attributes`` value is not a dict, the key-set filter cannot be
    applied and the ``attributes`` value is returned unchanged. A
    ``warning``-level log line records the short-circuit so it is visible at
    default log levels.
    """
    if not isinstance(record, dict):
        return record, None
    if fields is not None:
        keep = set(fields)
        record = {k: v for k, v in record.items() if k in keep}
    attr_warn: str | None = None
    if attribute_keys is not None:
        attrs = record.get("attributes")
        if isinstance(attrs, dict):
            attr_keep = set(attribute_keys)
            filtered_attrs = {k: v for k, v in attrs.items() if k in attr_keep}
            if attrs and attribute_keys and not filtered_attrs:
                available = sorted(attrs.keys())
                attr_warn = (
                    f"attribute_keys {sorted(attribute_keys)!r} matched no attribute "
                    f"keys — attributes came out empty. "
                    f"Available keys: {available!r}"
                )
            record = {**record, "attributes": filtered_attrs}
        elif "attributes" in record:
            logger.warning(
                "project_entity_record: attribute_keys filter skipped — "
                "'attributes' is %s (expected dict) for record keys=%r",
                type(attrs).__name__,
                list(record.keys()),
            )
    return record, attr_warn


# Default compact-result projection for ha_call_service. Drops timestamp/context
# metadata at the top level and known-heavy enum-style attribute lists (issue
# #1446: a single WLED light's effect_list can be ~250 entries, emitted on
# every propagated state). Kept deliberately minimal — domain-specific trimming
# belongs in ha_get_state via explicit attribute_keys, not here.
_COMPACT_RESULT_DROP_TOP_LEVEL: frozenset[str] = frozenset(
    {"context", "last_changed", "last_reported", "last_updated"}
)
_COMPACT_RESULT_DROP_ATTRIBUTES: frozenset[str] = frozenset(
    {"effect_list", "hue_scenes"}
)


def compact_service_result(
    result: Any,
    target_entity_id: str | None,
) -> Any:
    """Trim a ha_call_service ``result`` list to the compact default.

    Issue #1446: HA returns a state record for every entity affected by a
    service call. With nested HA-native groups (group → group → group) and
    WLED-style entities carrying ~250-entry ``effect_list`` attributes, this
    list blows up token usage with no corresponding signal — agents only need
    confirmation that the targeted entity reached its new state.

    Compact rules:

    1. When ``target_entity_id`` is a single string, filter the list to records
       whose ``entity_id`` matches — drops the propagation chain (parent groups).
       Falls back to the full list if no record matches (e.g. HA returned
       only parent states).
    2. Drop top-level metadata keys (``context``, ``last_*``) from every record.
    3. Drop known-heavy attribute keys (``effect_list``, ``hue_scenes``) from
       every record's ``attributes`` dict.

    Returns ``result`` unchanged when not a list (e.g. dict from
    ``return_response=True`` services), or when the list is empty.
    """
    if not isinstance(result, list) or not result:
        return result

    records: list[Any] = result
    if isinstance(target_entity_id, str) and target_entity_id:
        matched = [
            r
            for r in records
            if isinstance(r, dict) and r.get("entity_id") == target_entity_id
        ]
        if matched:
            records = matched

    compacted: list[Any] = []
    for record in records:
        if not isinstance(record, dict):
            compacted.append(record)
            continue
        trimmed = {
            k: v for k, v in record.items() if k not in _COMPACT_RESULT_DROP_TOP_LEVEL
        }
        attrs = trimmed.get("attributes")
        if isinstance(attrs, dict):
            trimmed["attributes"] = {
                k: v
                for k, v in attrs.items()
                if k not in _COMPACT_RESULT_DROP_ATTRIBUTES
            }
        compacted.append(trimmed)
    return compacted


def project_fields(
    data: dict[str, Any],
    fields: str | list[str] | None,
) -> dict[str, Any]:
    """Apply optional field projection to a response data dict.

    Always retains ``success`` and ``warnings``.  Accepts a list or a
    CSV/JSON-array string for *fields*.  Apply to the inner payload before any
    outer wrapper that adds top-level keys you want to preserve.

    Typo guard: if any requested key does not exist in *data* (excluding the
    always-retained ``success``/``warnings`` keys), a diagnostic is appended to
    ``result["warnings"]`` listing the unknown keys and the available ones.
    This mirrors the per-record ``result_fields_warning`` guard and ensures
    callers get a signal rather than a mysteriously empty response.
    """
    if fields is None:
        return data
    parsed = parse_string_list_param(fields, "fields", allow_csv=True) or []
    keep = set(parsed) | {"success", "warnings"}
    result = {k: v for k, v in data.items() if k in keep}
    # Typo guard — flag any requested keys that are absent from the response.
    # Exclude the always-retained sentinels so fields=["success"] never warns.
    _force_retain = {"success", "warnings"}
    unknown = sorted(set(parsed) - set(data.keys()) - _force_retain)
    if unknown:
        available = sorted(k for k in data.keys() if k not in _force_retain)
        result.setdefault("warnings", []).append(
            f"fields {unknown!r} not found in response — available keys: {available!r}"
        )
    return result


def project_records(
    records: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    """Project each record dict to only the specified keys.

    Returns *records* unchanged when *fields* is ``None``.  Unknown keys are
    silently dropped from each record.  Call :func:`result_fields_warning`
    on the original and projected lists if you want a diagnostic when all keys
    were unknown (typo guard).
    """
    if fields is None:
        return records
    keep = set(fields)
    return [{k: v for k, v in r.items() if k in keep} for r in records]


def result_fields_warning(
    original: list[dict[str, Any]],
    projected: list[dict[str, Any]],
    fields: list[str],
    param_name: str = "result_fields",
) -> str | None:
    """Return a diagnostic string when all projected records are empty dicts.

    Fires only when *original* is non-empty and every projected record is
    ``{}`` — the typical cause is specifying only unknown field names
    (e.g. a typo in ``result_fields``).  The caller should append the
    returned string to the response ``warnings`` list.
    """
    if not original or not projected:
        return None
    if all(not r for r in projected):
        # Sample up to 3 records for the available-keys hint so we don't
        # iterate the whole (potentially large) list.
        available = sorted({k for r in original[:3] for k in r})
        return (
            f"{param_name} {sorted(fields)!r} matched no record keys — "
            f"records came out empty. Available keys: {available!r}"
        )
    return None


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


async def add_timezone_metadata(
    client: Any, data: dict[str, Any], include_metadata: bool = True
) -> dict[str, Any]:
    """Add Home Assistant timezone to tool responses for local time context.

    Wraps *data* in ``{"data": ..., "metadata": {...}}``.  Pass
    ``include_metadata=False`` to return *data* unchanged — the ``metadata``
    wrapper is then omitted entirely.
    """
    if not include_metadata:
        return data
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
    except (
        HomeAssistantConnectionError,
        HomeAssistantAPIError,
        HomeAssistantAuthError,
        TimeoutError,
        OSError,
    ) as _tz_exc:
        logger.warning(
            "add_timezone_metadata: failed to fetch HA timezone config — "
            "falling back to 'Unknown': %s",
            _tz_exc,
            exc_info=True,
        )
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
    identifier: str,
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
    ``sample`` are swallowed there. ``identifier`` is the human-readable
    name used in log lines — usually an entity_id but may be a
    descriptor like ``automation[unique_id=...]`` for discovery waits
    that don't know the entity_id up front.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            result = await sample()
            if result is not None:
                logger.debug(
                    f"REST waiter: {description} for {identifier} resolved "
                    f"after {time.monotonic() - start:.2f}s"
                )
                return result
        except (HomeAssistantConnectionError, HomeAssistantAuthError):
            raise
        await asyncio.sleep(poll_interval)
    logger.warning(
        f"REST fallback: {description} for {identifier} timed out after {timeout}s"
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
    # Per-client credentials are only meaningful when both are strings.
    # If the caller is a test rig passing a ``MagicMock`` client (which
    # returns ``MagicMock`` for any attribute), forwarding those into the
    # WS pool trips URL-parsing TypeErrors deep inside ``WebSocketManager``.
    # Treat any non-string credential as "no WS available" and fall
    # through to REST polling — production callers always have a real
    # string ``base_url`` and ``token``, so this only matters for tests.
    if not (isinstance(base_url, str) and isinstance(token, str)):
        return None
    try:
        ws_client = await get_websocket_client(url=base_url, token=token)
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


async def _ws_subscribe_all(
    ws_client: Any,
    event_types: tuple[str, ...],
    handler: Any,
    attached_handlers: list[str],
    sub_ids: list[int],
    description: str,
    identifier: str,
) -> bool:
    """Attach event handler and subscribe to all event_types.

    Populates attached_handlers and sub_ids in-place.
    Returns True on success, False if a non-auth error triggers REST fallback.
    """
    for et in event_types:
        ws_client.add_event_handler(et, handler)
        attached_handlers.append(et)
    for et in event_types:
        try:
            sub_ids.append(await ws_client.subscribe_events(et))
        except HomeAssistantAuthError:
            raise
        except (
            HomeAssistantConnectionError,
            HomeAssistantCommandError,
            OSError,
            TimeoutError,
        ) as e:
            logger.debug(
                "subscribe_events(%s) failed during %s for %s: %s — falling back to REST polling",
                et,
                description,
                identifier,
                e,
            )
            return False
    return True


async def _ws_post_subscribe_check(
    ws_client: Any,
    sample: Callable[[], Awaitable[Any]],
    start: float,
    timeout: float,
    poll_interval: float,
    description: str,
    identifier: str,
) -> tuple[Any, bool]:
    """Run post-subscribe sample and connection check.

    Returns (result, is_done). When is_done=True the caller should return result
    immediately (either an early success or a REST-poll fallback).
    """
    try:
        result = await sample()
        if result is not None:
            logger.debug(
                f"WS waiter: {description} for {identifier} resolved by "
                f"post-subscribe sample after {time.monotonic() - start:.2f}s"
            )
            return result, True
    except (HomeAssistantConnectionError, HomeAssistantAuthError):
        raise

    if not ws_client.is_connected:
        logger.debug(
            "WS connection dropped before wait loop for %s on %s — completing via REST polling",
            description,
            identifier,
        )
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            return None, True
        return await _legacy_poll_until(
            identifier,
            sample,
            timeout=remaining,
            poll_interval=poll_interval,
            description=description,
        ), True

    return None, False


async def _ws_run_wait_loop(
    ws_client: Any,
    sample: Callable[[], Awaitable[Any]],
    nudge: asyncio.Event,
    start: float,
    timeout: float,
    poll_interval: float,
    description: str,
    identifier: str,
) -> Any:
    """Event-driven wait loop: nudge on event, backstop polling, REST fallback on disconnect."""
    while time.monotonic() - start < timeout:
        remaining = timeout - (time.monotonic() - start)
        wait_budget = min(remaining, _POLLING_BACKSTOP_INTERVAL)
        try:
            await asyncio.wait_for(nudge.wait(), timeout=wait_budget)
            nudge.clear()
        except TimeoutError:
            pass  # polling backstop expired — loop continues to check connection and sample

        if not ws_client.is_connected:
            logger.debug(
                "WS connection dropped during %s for %s — completing wait via REST polling",
                description,
                identifier,
            )
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                return None
            return await _legacy_poll_until(
                identifier,
                sample,
                timeout=remaining,
                poll_interval=poll_interval,
                description=description,
            )

        try:
            result = await sample()
            if result is not None:
                logger.debug(
                    f"WS waiter: {description} for {identifier} resolved "
                    f"after {time.monotonic() - start:.2f}s"
                )
                return result
        except (HomeAssistantConnectionError, HomeAssistantAuthError):
            raise

    logger.warning(
        f"WS waiter: {description} for {identifier} timed out after {timeout}s"
    )
    return None


async def _ws_cleanup(
    ws_client: Any,
    attached_handlers: list[str],
    sub_ids: list[int],
    handler: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    for et in attached_handlers:
        ws_client.remove_event_handler(et, handler)
    for sub_id in sub_ids:
        try:
            await ws_client.unsubscribe_events(sub_id)
        except (HomeAssistantConnectionError, OSError, TimeoutError) as e:
            logger.warning(
                "unsubscribe_events(%s) cleanup failed (subscription "
                "may leak until WS pool reconnects): %s",
                sub_id,
                e,
            )
        except HomeAssistantCommandTimeout:
            logger.warning(
                "unsubscribe_events(%s) cleanup timed out on WS "
                "round-trip; subscription may leak until WS pool "
                "reconnects",
                sub_id,
            )


async def _ws_wait_for_condition(
    client: Any,
    identifier: str,
    sample: Callable[[], Awaitable[Any]],
    *,
    event_types: tuple[str, ...],
    timeout: float,
    poll_interval: float,
    description: str,
    event_filter: Callable[[dict[str, Any]], bool] | None = None,
) -> Any:
    """Subscribe to ``event_types``, sample after subscribe, wait on event.

    Implements the standard "subscribe → sample → wait" pattern from #1152:

    - The handler nudges a single ``asyncio.Event`` whenever HA pushes an
      event matching ``event_filter``. The main loop wakes on that nudge
      or on the polling-backstop timeout, then re-runs ``sample`` (the
      REST source-of-truth check) to decide whether the wait succeeded.
    - Sample-after-subscribe (not before) closes the gap between the
      caller's write returning and our subscription landing on the HA
      side. The event for the write may have already fired by the time we
      subscribe; the post-subscribe sample catches that.
    - If the WS path fails to set up (no WS client, no subscription, …)
      we fall back to ``_legacy_poll_until``. The helpers' contract is
      identical to the pre-#1152 REST loop in that case.

    ``identifier`` is used only for log lines — usually an entity_id but
    may be a descriptor like ``automation[unique_id=...]`` for discovery
    waits (#1395) that don't know the entity_id up front. When
    ``event_filter`` is None the default predicate matches events whose
    ``data["entity_id"]`` equals ``identifier`` — i.e. the standard
    "watch this entity_id" shape used by ``wait_for_entity_*`` /
    ``wait_for_state_change``. Callers that need a different match shape
    (e.g. "any automation with attributes.id == unique_id") pass a custom
    ``event_filter``.

    Returns ``sample``'s truthy return value, or ``None`` on timeout.
    """
    ws_client = await _get_waiter_ws_client(client)
    if ws_client is None:
        return await _legacy_poll_until(
            identifier,
            sample,
            timeout=timeout,
            poll_interval=poll_interval,
            description=description,
        )

    nudge = asyncio.Event()

    def _default_filter(event: dict[str, Any]) -> bool:
        # HA nests ``entity_id`` under ``event["data"]`` for both
        # state_changed and entity_registry_updated. The top-level fallback
        # is defensive only — it lets a future schema drift degrade to a
        # missed nudge rather than an AttributeError.
        data = event.get("data") or {}
        evt_entity = data.get("entity_id") or event.get("entity_id")
        return bool(evt_entity == identifier)

    filter_fn = event_filter if event_filter is not None else _default_filter

    async def handler(event: dict[str, Any]) -> None:
        if filter_fn(event):
            nudge.set()

    # Track which handlers / subscriptions we actually attached so cleanup
    # is exact even if subscribe_events raises partway through.
    attached_handlers: list[str] = []
    sub_ids: list[int] = []
    try:
        if not await _ws_subscribe_all(
            ws_client,
            event_types,
            handler,
            attached_handlers,
            sub_ids,
            description,
            identifier,
        ):
            return await _legacy_poll_until(
                identifier,
                sample,
                timeout=timeout,
                poll_interval=poll_interval,
                description=description,
            )

        start = time.monotonic()
        # Sample-after-subscribe: covers the "event fired before subscribe
        # landed" race. This is where most happy-path waits resolve.
        early_result, is_done = await _ws_post_subscribe_check(
            ws_client, sample, start, timeout, poll_interval, description, identifier
        )
        if is_done:
            return early_result

        return await _ws_run_wait_loop(
            ws_client,
            sample,
            nudge,
            start,
            timeout,
            poll_interval,
            description,
            identifier,
        )
    finally:
        await _ws_cleanup(ws_client, attached_handlers, sub_ids, handler)


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


async def _sample_state_change(
    client: Any,
    entity_id: str,
    expected_state: str | None,
    baseline: dict[str, str | None],
) -> dict[str, Any] | None:
    """Sample entity state for wait_for_state_change; returns state dict on match."""
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
        return await _sample_state_change(client, entity_id, expected_state, baseline)

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


async def _discover_automation_sample(
    client: Any,
    unique_id: str,
    captured: dict[str, str | None],
) -> str | None:
    """Sample get_states() looking for an automation whose attributes.id matches unique_id."""
    if captured["entity_id"] is not None:
        return captured["entity_id"]
    try:
        states = await client.get_states()
    except HomeAssistantAPIError as e:
        logger.debug(f"API error sampling get_states() for unique_id {unique_id}: {e}")
        captured["last_api_error"] = str(e)
        return None
    for state in states:
        entity_id = state.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith("automation."):
            continue
        if state.get("attributes", {}).get("id") == unique_id:
            return entity_id
    return None


def _automation_event_filter(
    event: dict[str, Any],
    unique_id: str,
    captured: dict[str, str | None],
) -> bool:
    """Filter state_changed events to those matching an automation by unique_id.

    Defensive isinstance guards mirror the sample() callback — the WS dispatcher
    swallows handler exceptions broadly, so a malformed payload reaching
    .startswith would silently fail to nudge and the wait would time out.
    """
    data = event.get("data") or {}
    evt_entity = data.get("entity_id")
    if not isinstance(evt_entity, str) or not evt_entity.startswith("automation."):
        return False
    new_state = data.get("new_state") or {}
    attrs = new_state.get("attributes") if isinstance(new_state, dict) else None
    if not isinstance(attrs, dict) or attrs.get("id") != unique_id:
        return False
    # Guard against last-writer-wins collision (HA forbids duplicate unique_id,
    # but don't coin-flip silently if it ever happens).
    if captured["entity_id"] is None:
        captured["entity_id"] = evt_entity
    elif captured["entity_id"] != evt_entity:
        logger.warning(
            "Duplicate automation match for unique_id %s: %s already captured, ignoring %s",
            unique_id,
            captured["entity_id"],
            evt_entity,
        )
    return True


async def wait_for_automation_entity_by_unique_id(
    client: Any,
    unique_id: str,
    timeout: float = 6.0,
    poll_interval: float = 0.3,
) -> str | None:
    """
    Discover the entity_id assigned to a newly-created automation by unique_id.

    Used after ``POST /config/automation/config/{unique_id}`` to resolve the
    ``automation.<slug>`` entity_id Home Assistant assigned. Listens to
    ``state_changed`` events filtered to ``automation.*`` entities whose
    ``new_state.attributes.id`` equals ``unique_id`` — HA's
    ``BaseAutomationEntity.capability_attributes`` exposes ``unique_id`` as
    ``CONF_ID`` on every emit, so the first state event for a fresh
    automation carries the match. Falls back to REST polling of
    ``get_states()`` when the WebSocket is unavailable. See #1152 / #1395.

    Args:
        client: HomeAssistantClient instance
        unique_id: The unique_id passed to ``POST /config/automation/config/{unique_id}``
        timeout: Maximum time to wait in seconds (preserves the legacy 6s budget)
        poll_interval: REST poll interval used for the WS-unavailable fallback

    Returns:
        The discovered entity_id (e.g. ``"automation.morning_routine"``)
        or ``None`` on timeout.
    """
    # Mutable cells shared between sample and event_filter.
    # ``entity_id``: stashes the discovered entity_id when filter sees a match.
    # ``last_api_error``: tracks REST failures for the timeout warning.
    captured: dict[str, str | None] = {"entity_id": None, "last_api_error": None}

    async def sample() -> str | None:
        return await _discover_automation_sample(client, unique_id, captured)

    def event_filter(event: dict[str, Any]) -> bool:
        return _automation_event_filter(event, unique_id, captured)

    result = await _ws_wait_for_condition(
        client,
        identifier=f"automation[unique_id={unique_id}]",
        sample=sample,
        event_types=("state_changed",),
        timeout=timeout,
        poll_interval=poll_interval,
        description="automation entity discovery",
        event_filter=event_filter,
    )
    if isinstance(result, str):
        return result
    # `_ws_wait_for_condition` / `_legacy_poll_until` already logged the
    # generic "timed out" warning before returning None; just surface the
    # discovery-specific signal when REST sampling was wedged the whole
    # budget so operators can distinguish "automation never published"
    # from "REST channel down."
    if captured["last_api_error"] is not None:
        logger.warning(
            "Automation discovery for unique_id %s timed out with every "
            "REST sample failing; last error: %s",
            unique_id,
            captured["last_api_error"],
        )
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


async def _fetch_raw_diagnostics(
    client: Any,
    endpoint: str,
    timeout_seconds: float,
    device_id: str | None,
    result: dict[str, Any],
) -> None:
    """Fetch diagnostics from HA, populating result['data'] or result['error']."""
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

    await _fetch_raw_diagnostics(client, endpoint, timeout_seconds, device_id, result)

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


def _apply_data_path_resolution(
    result: dict[str, Any],
    data: Any,
    data_path: str,
    data_limit: int | None,
    data_offset: int,
) -> tuple[Any, bool]:
    """Walk data_path, apply pagination, and update result. Returns (data, paginated).

    Called only when data_path is set; when absent the caller handles the
    orphan data_offset warning directly.
    """
    resolved, path_error = _resolve_data_path(data, data_path)
    if path_error is not None:
        result["data"] = None
        result["data_path_error"] = path_error
        return None, False

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
        return items, True

    # Pagination intent has nowhere to apply: either data_limit is set but the
    # resolved value isn't a list, or data_offset is set without data_limit.
    if data_limit is not None:
        type_name = "null" if resolved is None else type(resolved).__name__
        result["data_pagination_warning"] = (
            f"data_limit ignored: resolved value at '{data_path}' is {type_name}, not a list"
        )
    elif data_offset > 0:
        result["data_pagination_warning"] = (
            "data_offset ignored: data_limit not set (no pagination window to slice)"
        )
    result["data"] = resolved
    return resolved, False


def _apply_truncation_cap(
    result: dict[str, Any],
    data: Any,
    truncate_at_bytes: int | None,
    paginated: bool,
) -> None:
    if truncate_at_bytes is None or data is None:
        return
    try:
        serialized = json.dumps(data, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    bytes_total = len(serialized.encode("utf-8"))
    if bytes_total > truncate_at_bytes:
        result["truncated"] = True
        result["bytes_total"] = bytes_total
        result["byte_cap"] = truncate_at_bytes
        if paginated:
            envelope = result["data"]
            preserved = {k: v for k, v in envelope.items() if k != "items"}
            preserved["truncated"] = True
            result["data"] = preserved
        else:
            if isinstance(data, dict):
                result["available_fields"] = sorted(data.keys())
            del result["data"]


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
        data, paginated = _apply_data_path_resolution(
            result, data, data_path, data_limit, data_offset
        )
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

    _apply_truncation_cap(result, data, truncate_at_bytes, paginated)


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
