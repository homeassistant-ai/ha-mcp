"""Shared plumbing for the ``ha_get_logs`` log sources.

Constants, parameter coercion and the cross-source warning rules that every
source uses, with no dependency on any particular one. Split from
``tools_utility`` under `.gemini/styleguide.md` § Tool Consolidation and
Module Size; per-source fetchers live in ``log_sources`` and
``log_sources_supervisor``, and the tool surface in ``tools_logs``.
"""

import re
from typing import Any, Literal

from .._version import is_running_in_addon
from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

# Fields to keep in compact logbook mode (strips attribute dictionaries
# and other bulky fields that can cause context exhaustion — see #683)
COMPACT_LOGBOOK_FIELDS = {
    "when",
    "entity_id",
    "state",
    "name",
    "message",
    "domain",
    "context_id",
    "source",
}


# Supervisor-managed system services exposed via /<slug>/logs. Set mirrors
# HA Core's hassio HTTP proxy ``PATHS_ADMIN`` whitelist in
# ``homeassistant/components/hassio/http.py``. See #1116 (original 7-service
# scope) and #1260 (cli added — proxy supported it the whole time).
SYSTEM_SERVICE_SLUGS = frozenset(
    {"supervisor", "host", "core", "dns", "audio", "cli", "multicast", "observer"}
)

DEFAULT_LIMIT = 50
DEFAULT_LOG_LIMIT = 100
# Journald window to request from Supervisor when a search filter is
# active: matches are found within the fetched window only, so a
# search over the caller's (often small) limit needs more history
# behind it than the limit itself.
SUPERVISOR_SEARCH_WINDOW_LINES = 2000
MAX_LIMIT = 500

# Regex to match log level at the start of a log line
_LOG_LEVEL_RE = re.compile(
    r"(?:^|\s)(DEBUG|INFO|WARNING|ERROR|CRITICAL)(?:\s|:|\])", re.IGNORECASE
)

VALID_LOG_LEVELS = ("ERROR", "WARNING", "INFO", "DEBUG", "CRITICAL")


def _compact_logbook_entries(entries: list[Any]) -> list[dict[str, Any]]:
    """Strip logbook entries to essential fields only.

    Returns entries with only the fields in COMPACT_LOGBOOK_FIELDS,
    filtering out any non-dict entries.
    """
    return [
        {k: v for k, v in entry.items() if k in COMPACT_LOGBOOK_FIELDS}
        for entry in entries
        if isinstance(entry, dict)
    ]


def _coerce_limit(
    limit: int | None,
    default: int = DEFAULT_LIMIT,
    suggestion_example: str = "50",
    param_name: str = "limit",
) -> int:
    """Validate a limit parameter, raising a structured tool error on failure."""
    effective = limit if limit is not None else default
    if effective < 1:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{param_name} must be at least 1, got {effective}",
                suggestions=[
                    f"Provide {param_name} as an integer (e.g., {suggestion_example})"
                ],
            )
        )
    return min(effective, MAX_LIMIT)


def _validate_log_level(level: str | None) -> str | None:
    if level is None:
        return None
    level_upper = level.strip().upper()
    if level_upper not in VALID_LOG_LEVELS:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid level '{level}'. Must be one of: {', '.join(VALID_LOG_LEVELS)}",
                suggestions=["Use level='ERROR' to see only errors"],
            )
        )
    return level_upper


def _collect_log_warnings(
    source: str,
    level: str | None,
    entity_id: str | None,
    end_time: str | None,
    slug: str | None,
    order: Literal["newest", "oldest"],
    offset: int,
) -> list[str]:
    warnings: list[str] = []
    if source == "logger" and order != "newest":
        warnings.append(
            "Parameter 'order' does not apply to source='logger' "
            "(entries are sorted by integration name); ignored"
        )
    if source != "logbook" and any(p is not None for p in [entity_id, end_time]):
        ignored = [
            p
            for p, v in [("entity_id", entity_id), ("end_time", end_time)]
            if v is not None
        ]
        warnings.append(
            f"Parameters {', '.join(ignored)} only apply to source='logbook'; "
            f"ignored for source='{source}'"
        )
    if (
        source in ("logbook", "logger", "supervisor", "system_service", "fault_log")
        and level is not None
    ):
        warnings.append(
            "Parameter 'level' only applies to source='system' or 'error_log'; "
            f"ignored for source='{source}'"
        )
    if source not in ("supervisor", "system_service") and slug is not None:
        warnings.append(
            "Parameter 'slug' only applies to source='supervisor' or "
            f"'system_service'; ignored for source='{source}'"
        )
    if source not in ("logbook", "error_log", "fault_log") and offset:
        warnings.append(
            "Parameter 'offset' only applies to source='logbook', 'error_log' "
            f"or 'fault_log'; ignored for source='{source}'"
        )
    return warnings


def _validate_log_slug(source: str, slug: str | None) -> None:
    if source == "system_service":
        if not slug:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "The 'slug' parameter is required for source='system_service'",
                    suggestions=[
                        "Provide a service name, e.g. slug='supervisor' "
                        f"(allowed: {', '.join(sorted(SYSTEM_SERVICE_SLUGS))})",
                    ],
                )
            )
        if slug not in SYSTEM_SERVICE_SLUGS:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid system_service slug '{slug}'. Must be one of: "
                    f"{', '.join(sorted(SYSTEM_SERVICE_SLUGS))}",
                    suggestions=[
                        "Pick a valid service name (e.g. 'supervisor', 'host')",
                        "For app (add-on) container logs use source='supervisor' "
                        + "with the app slug instead",
                    ],
                )
            )
    elif source == "supervisor" and not slug:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "The 'slug' parameter is required for source='supervisor'",
                suggestions=[
                    "Provide the app (add-on) slug, e.g. slug='core_mosquitto'",
                    "Use ha_get_app() to list installed app slugs",
                ],
            )
        )


def _addon_auth_error_suggestions() -> list[str]:
    if is_running_in_addon():
        return [
            "Verify SUPERVISOR_TOKEN is set correctly inside the app (add-on)",
            "Reinstall the app if the token may have rotated",
        ]
    return [
        "Verify HOMEASSISTANT_TOKEN is a valid admin Long-Lived Access Token (Settings → Profile → Long-Lived Access Tokens)",
        "Re-create the LLAT if it has expired or been revoked",
    ]
