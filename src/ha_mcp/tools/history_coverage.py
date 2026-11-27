"""Why a recorder read returned less than its window, as the recorder itself tells it.

Core answers a window it holds nothing for with an empty success. The reasons
come from the recorder instance through the component's ``recorder_status``
bridge, or from Core's own commands without it; no retention figure is
written down here. These lookups only explain a read that already
succeeded: one that gets no answer becomes a warning naming what was not read.
"""

from datetime import UTC, datetime
from typing import Any

from ..client.rest_client import HomeAssistantConnectionError
from .component_api import (
    ask_component,
    component_unusable_warning,
    get_component_caps,
)
from .helpers import core_error_message

_SHORT_TERM_PERIOD = "5minute"
_SHORT_TERM_SECONDS = 300
_STATUS_UNREAD = "the recorder's oldest state and purge settings were not read"
_STATUS_FIELDS = {"purge_keep_days", "auto_purge", "oldest_state", "entities"}


def when(timestamp: float) -> str:
    """A recorder timestamp as ISO text in UTC."""
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


MIGRATING = (
    "The recorder is migrating its database; its oldest state is read once "
    "the migration has finished."
)


def _oldest_state_reason(status: dict[str, Any], start_ts: float) -> list[str]:
    """The recorder's oldest state and purge settings, when the window starts before it."""
    oldest = status["oldest_state"]
    # A component released before the field came answers without it.
    if status.get("migration_in_progress"):
        return [MIGRATING]
    if oldest is None:
        return ["The recorder holds no states."]
    if start_ts >= oldest:
        return []
    return [
        f"The recorder's oldest state is from {when(oldest)} (auto_purge: "
        f"{status['auto_purge']}, purge_keep_days: {status['purge_keep_days']})."
    ]


async def _core_read(client: Any, message: dict[str, Any]) -> tuple[Any, str | None]:
    """A Core command's result, or the reason there is none."""
    try:
        reply = await client.send_websocket_message(message)
    except HomeAssistantConnectionError as lost:
        return None, str(lost)
    if reply.get("success"):
        return reply.get("result"), None
    return None, core_error_message(reply.get("error", "Unknown error"))


def _usable(status: dict[str, Any], entity_ids: list[str]) -> bool:
    """Whether a reply carries what the bridge is read for."""
    rows = status.get("entities")
    return (
        status.keys() >= _STATUS_FIELDS
        and isinstance(rows, dict)
        and all(
            isinstance(rows.get(entity_id), dict)
            and rows[entity_id].keys() >= {"recorded", "has_state"}
            for entity_id in entity_ids
        )
    )


async def _recorder_status(
    client: Any,
    entity_ids: list[str],
    *,
    statistic_ids: list[str] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """The component's recorder status, or the warning that it was not read.

    A read that fails, or a reply without the bridge's fields, counts as no
    component (docs/agents/custom-component.md).
    """
    try:
        params = {"statistic_ids": statistic_ids} if statistic_ids else {}
        status = await ask_component(
            client, "recorder_status", entity_ids=entity_ids, **params
        )
    except HomeAssistantConnectionError:
        status = None
    if (
        status is not None
        and _usable(status, entity_ids)
        and (
            not statistic_ids
            or isinstance(status.get("first_short_term_statistics"), dict)
        )
    ):
        return status, []
    return None, [
        component_unusable_warning(
            await get_component_caps(client),
            "the first short-term statistics and purge settings were not read"
            if statistic_ids
            else _STATUS_UNREAD,
        )
    ]


async def _unrecorded_by_core(
    client: Any, entity_ids: list[str]
) -> tuple[list[str], list[str]]:
    """The entities Core's ``recorder/entity_options/get`` says are not recorded,
    and a warning for those it gave no verdict on."""
    unrecorded: list[str] = []
    unread: list[str] = []
    for entity_id in entity_ids:
        options, error = await _core_read(
            client, {"type": "recorder/entity_options/get", "entity_id": entity_id}
        )
        if error is not None:
            unread.append(f"{entity_id} ({error})")
        elif isinstance(options, dict) and options.get("recording_disabled_by"):
            unrecorded.append(entity_id)
    if not unread:
        return unrecorded, []
    return unrecorded, [
        f"Whether the recorder records these was not read: {', '.join(unread)}."
    ]


async def _recorder_reasons(
    client: Any, missing: list[str], late: list[str], start_ts: float
) -> list[str]:
    entity_ids = [*missing, *late]
    status, reasons = await _recorder_status(client, entity_ids)
    stateless: list[str] = []
    if status is not None:
        rows = status["entities"]
        unrecorded = [e for e in entity_ids if not rows[e]["recorded"]]
        # An entity whose rows came back existed; only an empty one may be a typo.
        stateless = [
            e for e in missing if rows[e]["recorded"] and not rows[e]["has_state"]
        ]
        reasons = _oldest_state_reason(status, start_ts)
    else:
        unrecorded, unread = await _unrecorded_by_core(client, entity_ids)
        reasons = unread + reasons
    if unrecorded:
        reasons.insert(0, f"The recorder does not record: {', '.join(unrecorded)}.")
    if stateless:
        reasons.insert(
            0, f"No entity has a state under: {', '.join(stateless)}; check the IDs."
        )
    return reasons


async def history_coverage_warnings(
    client: Any,
    first_rows: dict[str, float | None],
    start_ts: float,
    *,
    window_start_state: bool,
) -> list[str]:
    """Name each entity without rows, or whose rows start after ``start_ts``.

    ``first_rows`` maps an entity to its first row's ``lu``, None without rows.
    Core stamps the state an entity had at the window start with the window
    start, so a later first row means it returned no earlier state; a query
    that asked for no such state (``window_start_state`` False) proves nothing.
    """
    missing = [e for e, first in first_rows.items() if first is None]
    late = {
        e: first
        for e, first in first_rows.items()
        if first is not None and window_start_state and first > start_ts
    }
    if not missing and not late:
        return []
    warnings = []
    if missing:
        warnings.append(f"No recorded states in this window for: {', '.join(missing)}.")
    if late:
        listed = ", ".join(f"{e} (from {when(first)})" for e, first in late.items())
        warnings.append(
            f"Recorded states start after start_time for: {listed}; Core returned "
            "no earlier state."
        )
    return warnings + await _recorder_reasons(client, missing, list(late), start_ts)


async def _statistics_issues(client: Any, statistic_ids: list[str]) -> list[str]:
    """Core's own validation issues for ``statistic_ids``, one line each."""
    issues, error = await _core_read(client, {"type": "recorder/validate_statistics"})
    if error is not None:
        return [f"Core's statistics issues were not read ({error})."]
    lines = []
    for statistic_id in statistic_ids:
        for issue in (issues or {}).get(statistic_id, []):
            detail = ", ".join(
                f"{key}: {value}"
                for key, value in (issue.get("data") or {}).items()
                if key != "statistic_id"
            )
            lines.append(
                f"{statistic_id}: Core reports the statistics issue {issue['type']}"
                + (f" ({detail})." if detail else ".")
            )
    return lines


async def empty_statistics_warnings(
    client: Any,
    empty: list[str],
    metadata: dict[str, dict[str, Any]],
    metadata_failure: str | None,
    *,
    nothing: str = "No statistics in this window for",
) -> list[str]:
    """Name the statistics that returned nothing, with Core's own reason where it has one."""
    if not empty:
        return []
    if metadata_failure is not None:
        warnings = [
            f"No statistics returned for: {', '.join(empty)}. Their metadata was "
            "not read, so it is unknown whether they exist."
        ]
        return warnings + await _statistics_issues(client, empty)
    unknown = [i for i in empty if i not in metadata]
    warnings = []
    if unknown:
        warnings.append(f"The recorder has no statistics under: {', '.join(unknown)}.")
    if known := [i for i in empty if i in metadata]:
        warnings.append(f"{nothing}: {', '.join(known)}.")
    return warnings + await _statistics_issues(client, empty)


async def statistics_coverage_warnings(
    client: Any,
    rows_by_id: dict[str, list[dict[str, Any]]],
    statistic_ids: list[str],
    metadata: dict[str, dict[str, Any]],
    metadata_failure: str | None,
    period: str,
    start_ts: float,
) -> list[str]:
    """``empty_statistics_warnings``, plus 5-minute rows whose first period
    starts at least one period after ``start_ts``."""
    warnings = await empty_statistics_warnings(
        client,
        [i for i in statistic_ids if not rows_by_id.get(i)],
        metadata,
        metadata_failure,
    )
    if period != _SHORT_TERM_PERIOD:
        return warnings
    late = {
        i: rows_by_id[i][0]["start"] / 1000
        for i in statistic_ids
        if rows_by_id.get(i)
        and rows_by_id[i][0]["start"] / 1000 >= start_ts + _SHORT_TERM_SECONDS
    }
    if late:
        listed = ", ".join(f"{i} (from {when(first)})" for i, first in late.items())
        warnings.append(
            f"5-minute statistics start after start_time for: {listed}; a longer "
            "period reads the long-term statistics."
        )
        status, unread = await _recorder_status(client, [], statistic_ids=list(late))
        warnings.extend(unread)
        if status is not None:
            warnings.extend(
                f"First short-term statistic for {i}: {when(first)}."
                for i, first in status["first_short_term_statistics"].items()
            )
            warnings.append(
                f"auto_purge: {status['auto_purge']}, purge_keep_days: {status['purge_keep_days']}."
            )
    return warnings
