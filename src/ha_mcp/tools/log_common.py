"""Shared plumbing for the ``ha_get_logs`` log sources.

Constants, parameter coercion and the cross-source warning rules that every
source uses, with no dependency on any particular one. Split from
``tools_utility`` under `.gemini/styleguide.md` § Tool Consolidation and
Module Size; per-source fetchers live in ``log_sources`` and
``log_sources_supervisor``, and the tool surface in ``tools_logs``.
"""

import logging
import re
from typing import Literal

from .._version import is_running_in_addon
from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error
from .tools_addons import _is_valid_supervisor_slug

DEFAULT_LIMIT = 50
DEFAULT_LOG_LIMIT = 100
# Journald window to request from Supervisor when a search filter is
# active: matches are found within the fetched window only, so a
# search over the caller's (often small) limit needs more history
# behind it than the limit itself.
SUPERVISOR_SEARCH_WINDOW_LINES = 2000
MAX_LIMIT = 500
# One Supervisor service or app name, as the component's commands accept it.
SLUG_PATTERN = r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*"
# One syslog identifier as one URL path segment; the host journal has names
# such as "(rpcbind)".
IDENTIFIER_PATTERN = r"(?!\.{1,2}$)[^/\\?#%\x00-\x20]+"


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
    level_names = logging.getLevelNamesMapping()
    # No record is logged at NOTSET.
    if level_upper not in level_names or level_upper == "NOTSET":
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid level '{level}'. Must be one of: {', '.join(sorted(set(level_names) - {'NOTSET'}))}",
                suggestions=["Use level='ERROR' to see only errors"],
            )
        )
    return logging.getLevelName(level_names[level_upper])


def _collect_log_warnings(
    source: str,
    level: str | None,
    entity_id: str | None,
    device_id: str | None,
    context_id: str | None,
    end_time: str | None,
    slug: str | None,
    order: Literal["newest", "oldest"],
    offset: int | str,
    hours_back: int | None = None,
    search: str | None = None,
    action_calls: bool = False,
) -> list[str]:
    warnings: list[str] = []
    if source not in ("logbook", "supervisor", "system_service") and hours_back:
        warnings.append(
            "Parameter 'hours_back' only applies to source='logbook', "
            f"'supervisor' or 'system_service'; ignored for source='{source}'"
        )
    if source == "logger" and order != "newest":
        warnings.append(
            "Parameter 'order' does not apply to source='logger' "
            "(entries are sorted by integration name); ignored"
        )
    if source != "logbook" and any(
        p is not None
        for p in [entity_id, device_id, context_id, end_time, action_calls or None]
    ):
        ignored = [
            p
            for p, v in [
                ("entity_id", entity_id),
                ("device_id", device_id),
                ("context_id", context_id),
                ("end_time", end_time),
                ("action_calls", action_calls or None),
            ]
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
    if source in ("supervisor", "system_service") and offset:
        if search or (hours_back is None and isinstance(offset, int)):
            warnings.append(
                f"Parameter 'offset' applies to source='{source}' only on a "
                "page read (hours_back and no search); ignored here"
            )
    elif source not in ("logbook", "error_log", "fault_log") and offset:
        warnings.append(
            "Parameter 'offset' only applies to source='logbook', 'error_log', "
            "'fault_log', or 'supervisor'/'system_service' with hours_back; "
            f"ignored for source='{source}'"
        )
    return warnings


def _validate_log_slug(source: str, slug: str | None) -> None:
    if (
        source in ("supervisor", "system_service")
        and slug
        and not re.fullmatch(SLUG_PATTERN, slug)
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "The slug must be a single service or app name.",
            )
        )
    if source == "system_service" and not slug:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "The slug parameter is required for system_service.",
                suggestions=["Provide a service name, e.g. supervisor."],
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
    elif source == "supervisor" and slug and not _is_valid_supervisor_slug(slug):
        # The slug is one segment of the app's log path, so it is checked as the
        # app tools check theirs.
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid app slug '{slug}': use only ASCII letters, numbers, "
                "hyphens, underscores and periods, and not '.' or '..' alone",
                suggestions=[
                    "Provide the app (add-on) slug, e.g. slug='core_mosquitto'",
                    "Use ha_get_app() to list installed app slugs",
                ],
            )
        )


def _addon_auth_error_suggestions() -> list[str]:
    """What a log read adds to the classified auth guidance, which comes first."""
    if is_running_in_addon():
        return [
            "Verify SUPERVISOR_TOKEN is set inside the app (add-on), and reinstall "
            "the app if the token may have rotated"
        ]
    return [
        "Reading logs needs an admin user's token: check that the Long-Lived Access "
        "Token belongs to an admin (Settings → Profile → Long-Lived Access Tokens)",
    ]
