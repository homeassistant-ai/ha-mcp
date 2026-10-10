"""
Shared utility functions for MCP tool modules.

This module provides common helper functions used across multiple tool registration modules.
"""

import logging
from typing import Any

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)

logger = logging.getLogger(__name__)

# Mapping from service name to the expected resulting primary state. The single
# source of truth (imported by both ``tools_service`` and ``device_control``): it
# is the confirmation HINT the server hands the component's ``call_service`` /
# ``bulk_call_service`` capability so the component confirms only on REACHING that
# state (skipping a multi-phase service's intermediate states and attribute-only
# noise) and short-circuits an idempotent no-op. ``.get(service)`` is ``None`` for
# every service without a known primary state (``set_temperature`` /
# ``set_fan_mode`` / …) — a ``None`` hint keeps today's any-first-event
# confirmation. Lives here (a low-level module both consumers already import from)
# so the two write paths share one map with no import cycle. The legacy
# WS-subscribe verifier (``tools_service._verify_state_change``) reads it too.
_SERVICE_TO_STATE: dict[str, str] = {
    "turn_on": "on",
    "turn_off": "off",
    "open": "open",
    "close": "closed",
    "lock": "locked",
    "unlock": "unlocked",
}


def is_single_entity_target(entity_id: str | None) -> bool:
    """True when ``entity_id`` looks like exactly one real, literal entity ID.

    False for two shapes a not-found/unavailable lookup miss cannot settle
    anything about:

    * A comma-separated multi-target list ("light.a,light.b") — a valid
      service-call payload the single-entity ``/api/states/<id>`` endpoint
      has no syntax for, so its 404 on the literal joined string proves
      nothing about the individual targets.
    * A Home Assistant magic broadcast target — ``ENTITY_MATCH_ALL`` ("all")
      or ``ENTITY_MATCH_NONE`` ("none"), both defined in
      ``homeassistant/const.py`` and neither a literal entity in the state
      machine, so a states-endpoint miss for either is expected and proves
      nothing about whether the underlying service call can succeed.

    A normal entity ID always has a ``domain.object_id`` shape (contains a
    dot); the absence of one is the general, defensive signal used here
    rather than hardcoding just "all"/"none" by name, so any other future
    magic target is covered the same way.
    """
    return entity_id is not None and "," not in entity_id and "." in entity_id


def websocket_error_message(error: Any) -> str:
    """Extract a readable message from a Home Assistant websocket error."""
    if isinstance(error, dict):
        return str(error.get("message", error))
    return str(error)


def summarize_theme_listing(raw_themes: dict[str, Any]) -> dict[str, Any]:
    """Summarize a ``frontend/get_themes`` result into names plus defaults.

    Returns theme NAMES, not the full per-theme CSS variable dicts (installed
    community themes can carry hundreds of variables; listings are a
    discovery/verify surface, not a content dump).
    """
    themes_value = raw_themes.get("themes") or {}
    theme_names = sorted(themes_value.keys() if isinstance(themes_value, dict) else [])
    return {
        "themes": theme_names,
        "count": len(theme_names),
        "default_theme": raw_themes.get("default_theme"),
        "default_dark_theme": raw_themes.get("default_dark_theme"),
    }


def unwrap_service_response(result: dict[str, Any]) -> dict[str, Any]:
    """Extract service_response from HA call_service result.

    HA's call_service with return_response wraps results in
    {"changed_states": [...], "service_response": {...}}.
    Returns service_response if present and is a dict, otherwise the original result.

    Deliberately NOT the same rule as ``ServiceTools._split_return_response_envelope``,
    which powers ha_call_service: that one returns the response whatever its type and
    reports the whole reply only when the key is absent. The two disagree solely for a
    NON-DICT ``service_response`` (this helper hands back the envelope, the split hands
    back the value). Consumers here read component services that always answer with a
    dict, so the divergence is unreachable — but do not "align" one to the other
    without checking those ~20 call sites.
    """
    sr = result.get("service_response")
    return sr if isinstance(sr, dict) else result


# WebSocket commands that mutate persistent state in a way that bypasses a
# wrapping MCP tool's validation (auto-backup, config-hash optimistic locking,
# registry invariant checks), or that have no escape-hatch-appropriate use case
# (`config/core/update` rewrites the installation's location/timezone/currency/
# lat-long). Shared by the code sandbox's `ws_send` bridge (tools_code.py) and
# ha_call_service's `ws_command` escape hatch (tools_service.py) so the two raw
# WebSocket surfaces stay in lockstep. Registry deletion command names differ on
# HA Core: device deletion is `remove_config_entry` and entity deletion is
# `remove` (not `delete`) -- ha_remove_device / ha_remove_entity emit those.
BLOCKED_WS_WRITE_COMMANDS: frozenset[str] = frozenset(
    {
        "config/core/update",
        "lovelace/config/save",
        "ha_mcp_tools/dashboard_edit",
        "lovelace/dashboards/create",
        "lovelace/dashboards/delete",
        "lovelace/dashboards/update",
        # Resource writes carry the same wrapping-tool validation as the
        # dashboard commands above -- auto-backup, the #1072 HA-config-YAML
        # misroute rejection, the inline size cap, and the data:-URL routing
        # guard all live in ha_config_set_dashboard_resource (#2060).
        "lovelace/resources/create",
        "lovelace/resources/delete",
        "lovelace/resources/update",
        "config/area_registry/delete",
        "config/area_registry/disable",
        "config/area_registry/update",
        "config/device_registry/delete",
        "config/device_registry/disable",
        "config/device_registry/update",
        "config/device_registry/remove_config_entry",
        "config/entity_registry/delete",
        "config/entity_registry/disable",
        "config/entity_registry/update",
        "config/entity_registry/remove",
        "config/floor_registry/create",
        "config/floor_registry/delete",
        "config/floor_registry/update",
        "config/label_registry/create",
        "config/label_registry/delete",
        "config/label_registry/update",
        "config/category_registry/create",
        "config/category_registry/delete",
        "config/category_registry/update",
    }
)


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

    Entries with ``active=False`` are always dropped, regardless of
    ``include_dismissed``: HA core's ``ws_list_issues`` never emits them
    (on restart the registry reloads each stored non-persistent issue as an
    inactive stub until it is re-raised or deleted), but the component's
    raw registry dump does.
    """
    if include_dismissed:
        return [r for r in issues if r.get("active") is not False]
    return [r for r in issues if r.get("active") is not False and not r.get("ignored")]


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


async def get_logger_levels(
    client: Any, warnings: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Fetch current HA integration log levels via the ``logger/log_info`` WS command.

    Returns a mapping of integration domain (e.g. ``"mqtt"``) to a dict with:

    - ``name``: canonical level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``,
      ``"ERROR"``, ``"CRITICAL"``, ``"NOTSET"``, or ``"LEVEL_<n>"`` for
      non-standard ints).
    - ``raw``: the original numeric level (``int``) when HA returned an int,
      otherwise ``None`` (e.g. when the level was already provided as a string).

    Best-effort enrichment: returns an empty dict on connection/IO failures.
    An empty map alone cannot say whether Home Assistant has no custom levels
    or whether nobody could ask, so a caller that passes ``warnings`` gets a
    line on failure and can report UNKNOWN instead of claiming DEFAULT (#1947).
    Programming errors are not suppressed — they surface as bugs during
    development/CI.
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
        if warnings is not None:
            warnings.append(f"log levels unavailable: {exc}")
        return {}

    if not isinstance(result, dict) or not result.get("success"):
        if warnings is not None:
            warnings.append("log levels unavailable: logger/log_info failed")
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


def merge_visibility_warnings(
    response: dict[str, Any], warnings: list[str]
) -> dict[str, Any]:
    """Attach ``warnings`` to a tool response's top-level ``warnings`` list
    (create-or-extend). Returns ``response`` for ``return`` composition."""
    if warnings:
        response.setdefault("warnings", []).extend(warnings)
    return response


# Error strings produced by the pooled WebSocket path when the transport (not
# the command) fails: the manager's connect raise, send timeouts, and socket
# drops. Callers that attach domain-specific suggestions match on these so an
# HA restart is not presented as a domain problem (issue #1832 review).
#
# Since #1947 ``send_websocket_message`` raises on a dead transport rather than
# collapsing it into ``{"success": False, "error": ...}``, so these signatures
# now only have to catch transport-shaped text that reaches a caller by another
# route (a component-side error frame, or an error string threaded through a
# response body). They are kept as a belt-and-braces match, not as the primary
# detection path.
WS_CONNECTION_SIGNATURES = (
    "failed to connect",
    "timed out",
    "timeout",
    "connection closed",
    # reset_connection: "WebSocket connection to Home Assistant closed while
    # waiting for a response" — "connection closed" is not adjacent there.
    "closed while waiting",
    # listener close reason: "connection dropped without a close frame".
    "connection dropped",
    "disconnected",
    "not connected",
    "not authenticated",
)


def is_connection_error_message(error_msg: Any) -> bool:
    """True when a pooled-WS failure payload is connection/transport-shaped.

    Accepts any payload shape: HA error frames can carry a dict (or None)
    in the error slot, so the value is stringified before matching.
    """
    lowered = str(error_msg).lower()
    return any(sig in lowered for sig in WS_CONNECTION_SIGNATURES)
