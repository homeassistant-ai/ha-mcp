"""Response shaping helpers for tool results.

Field projection, pagination metadata, and Home Assistant timezone lookup with
timestamp localisation.
"""

import logging
from datetime import UTC, datetime
from datetime import tzinfo as _TZInfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from .coercion import parse_string_list_param
from .component_api import get_component_caps

logger = logging.getLogger(__name__)


def public_fields(d: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``d`` with leading-underscore keys removed.

    The ha-mcp tool layer enriches entity / area dicts with internal
    fields like ``_hidden_by`` and ``_aliases`` so downstream branches
    can rank without re-querying the entity registry. Those keys must
    not leak through public tool returns: this helper centralises the
    convention so individual call sites don't have to remember to strip.
    Shallow only — list/dict values are shared with the source, so a
    later mutation of those values would propagate.
    """
    return {
        k: v for k, v in d.items() if not (isinstance(k, str) and k.startswith("_"))
    }


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
    record's ``attributes`` value is not a dict (``None``, scalar, list — rare
    from HA's state API but possible from malformed records, partial error
    payloads, or mocked fixtures), the key-set filter cannot be applied. A
    ``warning``-level log line records the short-circuit AND a caller-facing
    warning is returned via ``attr_warn`` so the agent (MCP consumer) sees that
    its filter was skipped rather than just an operator tailing logs.
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
            attr_warn = (
                f"attribute_keys filter skipped — record 'attributes' is "
                f"{type(attrs).__name__} (expected dict)"
            )
    return record, attr_warn


# Default compact-result projection for ha_call_service (issue #1446). A single
# WLED light's `effect_list` can be ~250 entries, emitted on every propagated
# group state — `light.turn_on` on a nested group returned 16 state objects
# with that list carried four times. Drops timestamp/context metadata at the
# top level and known-heavy enum-style attribute lists.
#
# Extension policy: add a key here only when (a) it's universally heavy across
# installations (not just an unusual config), (b) it carries no signal callers
# would act on for service-call confirmation, and (c) callers who do want it
# can opt in via `result_fields` / `verbose=True`. Domain-specific or
# install-specific trimming belongs in `ha_get_state` via explicit
# `attribute_keys`, not here.
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
    """Trim a ha_call_service ``result`` list to the compact default (issue #1446).

    Compact rules:

    1. When ``target_entity_id`` is a single entity ID string OR a
       comma-separated list of entity IDs (HA accepts both as a service-call
       target), filter the list to records whose ``entity_id`` is in that set
       — drops the propagation chain (parent groups). Falls back to the full
       list if no record matches (e.g. HA returned only parent states).
    2. Drop top-level metadata keys (``context``, ``last_*``) from every record.
    3. Drop known-heavy attribute keys (``effect_list``, ``hue_scenes``) from
       every record's ``attributes`` dict.

    Returns ``result`` unchanged when it is not a list (defensive — every
    ha_call_service path now projects a changed-state list), or when the list
    is empty.
    """
    if not isinstance(result, list) or not result:
        return result

    records: list[Any] = result
    if isinstance(target_entity_id, str) and target_entity_id:
        targets = {t.strip() for t in target_entity_id.split(",") if t.strip()}
        if targets:
            matched = [
                r
                for r in records
                if isinstance(r, dict) and r.get("entity_id") in targets
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
    *,
    extra_always_keep: frozenset[str] | None = None,
    available_fields: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Apply optional field projection to a response data dict.

    Always retains ``success`` and ``warnings``.  Accepts a list or a
    CSV/JSON-array string for *fields*.  Apply to the inner payload before any
    outer wrapper that adds top-level keys you want to preserve.

    ``extra_always_keep`` lets a caller extend the retained set with its own
    contract / diagnostic keys (e.g. the orchestrator's pagination + partial-
    state keys) without having to reimplement the projection logic.

    ``available_fields`` supplies the complete response schema when *data* was
    collected narrowly. It affects typo diagnostics only; projection still
    returns only keys present in *data*.

    Typo guard: if any requested key does not exist in *data* (excluding the
    always-retained keys), a diagnostic is appended to ``result["warnings"]``
    listing the unknown keys and the available ones.  This mirrors the
    per-record ``result_fields_warning`` guard and ensures callers get a
    signal rather than a mysteriously empty response.
    """
    if fields is None:
        return data
    parsed = parse_string_list_param(fields, "fields", allow_csv=True) or []
    always_keep: set[str] = {"success", "warnings"}
    if extra_always_keep is not None:
        always_keep |= extra_always_keep
    keep = set(parsed) | always_keep
    result = {k: v for k, v in data.items() if k in keep}
    # Typo guard — flag any requested keys that are absent from the response.
    # Exclude the always-retained sentinels so fields=["success"] never warns.
    known_fields = set(available_fields) if available_fields is not None else set(data)
    unknown = sorted(set(parsed) - known_fields - always_keep)
    if unknown:
        available = sorted(k for k in known_fields if k not in always_keep)
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


_TIMESTAMP_METADATA_FIELDS = {
    "last_changed",
    "last_updated",
    "last_reported",
    "when",
    "last_triggered",
}


async def fetch_ha_timezone(client: Any) -> tuple[str, bool]:
    """Fetch the HA timezone, preferring the ``ha_mcp_tools`` component's cached
    ``info`` handshake over a fresh ``/api/config`` REST call.

    When ``get_component_caps(client)`` reports a non-empty ``timezone`` (an
    additive ``info`` field — see ``ComponentCaps.timezone``), return it
    directly with NO REST call. Otherwise falls back to the legacy
    ``client.get_config()`` fetch exactly as before — the path taken when the
    component is absent, predates the ``timezone`` field, or reports it empty.

    Staleness trade-off: the component route reads a cached, process-lifetime
    probe, so an HA timezone change mid-session keeps serving the value from
    the last successful negotiation (positive cache entries do not expire on a
    timer — only ``invalidate_caps`` or a process restart forces a re-probe)
    until then. The #1813 Phase 2 audit rated this Low risk and acceptable —
    instance timezone changes are rare — versus the legacy path, which always
    re-fetches fresh on every call.

    Returns ``(ha_timezone, fetch_failed)``. ``fetch_failed`` is ``True`` only
    when the legacy REST fetch raised, in which case *ha_timezone* is always
    ``"UTC"``.
    """
    caps = await get_component_caps(client)
    if caps is not None and caps.timezone:
        return caps.timezone, False

    try:
        config = await client.get_config()
        return config.get("time_zone", "UTC"), False
    except (
        HomeAssistantConnectionError,
        HomeAssistantAPIError,
        HomeAssistantAuthError,
        TimeoutError,
        OSError,
    ) as _tz_exc:
        logger.warning(
            "add_timezone_metadata: failed to fetch HA timezone config — "
            "falling back to UTC: %s",
            _tz_exc,
            exc_info=True,
        )
        return "UTC", True


def resolve_local_timezone(ha_timezone: str) -> tuple[_TZInfo, str]:
    """Resolve *ha_timezone* to a ``ZoneInfo``, falling back to UTC if unknown.

    Returns ``(local_tz, ha_timezone)``. ``ha_timezone`` is normalized to
    ``"UTC"`` when the lookup fails (tzdata package missing or bad name).
    """
    try:
        return ZoneInfo(ha_timezone), ha_timezone
    except ZoneInfoNotFoundError:
        logger.warning(
            "add_timezone_metadata: ZoneInfo(%r) not found "
            "(tzdata package missing?) — falling back to UTC",
            ha_timezone,
        )
        return UTC, "UTC"


def _convert_timestamp_fields(obj: Any, local_tz: _TZInfo) -> Any:
    """Recursively convert known timestamp fields in *obj* from UTC to *local_tz*.

    Offset-aware strings are converted directly; naive strings (no offset)
    are assumed to be UTC before conversion. Non-timestamp fields and
    unparseable values are returned unchanged.
    """
    if isinstance(obj, list):
        return [_convert_timestamp_fields(i, local_tz) for i in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _TIMESTAMP_METADATA_FIELDS and isinstance(v, str) and v:
                try:
                    parsed = datetime.fromisoformat(v)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    out[k] = parsed.astimezone(local_tz).isoformat()
                except (ValueError, TypeError):
                    out[k] = v
            else:
                out[k] = _convert_timestamp_fields(v, local_tz)
        return out
    return obj


async def add_timezone_metadata(
    client: Any,
    data: dict[str, Any],
    include_metadata: bool = True,
    *,
    convert_timestamps: bool = True,
) -> dict[str, Any]:
    """Add Home Assistant timezone to tool responses and convert timestamps to local time.

    Resolves the Home Assistant time zone via ``fetch_ha_timezone`` (which
    prefers the ``ha_mcp_tools`` component's cached handshake and falls back to
    ``/api/config``), converts every ``last_changed``, ``last_updated``,
    ``last_reported``, ``when``, and ``last_triggered`` field found anywhere in
    *data* from UTC to that local timezone, then wraps the result in
    ``{"data": ..., "metadata": {...}}``.

    Pass ``include_metadata=False`` to return *data* unchanged — the
    ``metadata`` wrapper is then omitted entirely.

    Pass ``convert_timestamps=False`` to retain native values while still
    including timezone context, without claiming that timestamps were converted.

    Conversion notes:
    - Offset-aware strings (``+00:00``) are converted directly.
    - Naive strings (no offset) are assumed to be UTC before conversion.
    - ``ZoneInfoNotFoundError`` (tzdata not installed) falls back to UTC.
    - Any config-fetch failure also falls back to UTC without conversion.
    """
    if not include_metadata:
        return data

    ha_timezone, fetch_failed = await fetch_ha_timezone(client)

    if not convert_timestamps:
        note = "Timestamp fields retain their native format; see the tool description for units."
        result: dict[str, Any] = {
            "data": data,
            "metadata": {
                "home_assistant_timezone": ha_timezone,
                "timestamp_format": "native",
                "note": note,
            },
        }
        if fetch_failed:
            result["warnings"] = [
                "Could not fetch Home Assistant timezone; timezone context defaults to UTC."
            ]
        return result

    if fetch_failed:
        return {
            "data": data,
            "metadata": {
                "home_assistant_timezone": "UTC",
                "timestamp_format": "ISO 8601 (UTC)",
                "note": "Could not fetch Home Assistant timezone — timestamps are in UTC.",
            },
        }

    local_tz, ha_timezone = resolve_local_timezone(ha_timezone)
    converted_data = _convert_timestamp_fields(data, local_tz)

    return {
        "data": converted_data,
        "metadata": {
            "home_assistant_timezone": ha_timezone,
            "timestamp_format": f"ISO 8601 ({ha_timezone})",
            "note": f"Per-record timestamp fields (last_changed, last_updated, last_reported, when, last_triggered) have been converted to {ha_timezone} local time. Query-window boundary fields (period, start_time, end_time) remain in UTC.",
        },
    }
