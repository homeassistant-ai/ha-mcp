"""ha_set_entity with a list of entity_id.

Core's ``config/entity_registry/update`` takes one entity, so labels,
categories, enabled and hidden go out once per entity; expose_to goes out as
one ``homeassistant/expose_entity`` call per assistant set. Each succeeded row
keeps what Core answered for its entity besides the entry, such as
``require_restart`` or ``reload_delay`` after an enable. With auto-backup on,
each entity gets its own pre-write snapshot, as a single-entity call does.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..backup_manager import _CAPTURE_TRANSIENT_ERRORS, get_backup_manager
from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .entity_expose_batch import (
    apply_bulk_expose,
    expose_targets,
    expose_update,
    refresh_warning,
)
from .entity_registry_reads import _extract_ws_error, get_entries_raw
from .helpers import extract_tool_error_message, raise_tool_error

logger = logging.getLogger(__name__)

# Keys of the single-entity reply that a bulk row carries in its own fields.
_ROW_KEYS = frozenset({"success", "entity_id", "entity_entry", "updates"})


async def _back_up_entities(client: Any, entity_ids: list[str]) -> list[str]:
    """Snapshot each entity before the write; returns why a due backup was not taken."""
    settings = get_global_settings()
    if not getattr(settings, "enable_auto_backup", False):
        return []
    skipped: list[str] = []
    try:
        mgr = get_backup_manager(client, settings)
        for eid in entity_ids:
            await mgr.maybe_snapshot(
                "entity", eid, tool_name="ha_set_entity", skip_reasons=skipped
            )
    except _CAPTURE_TRANSIENT_ERRORS as err:
        logger.warning("Bulk entity backup failed: %s", err)
        skipped.append(f"No pre-write backup was taken: {type(err).__name__}")
    # One reason per cause, not one per entity: a bulk reply stays small.
    return list(dict.fromkeys(skipped))


async def _registry_phase(
    entity_ids: list[str],
    update_one: Callable[[str], Awaitable[dict[str, Any]]] | None,
) -> tuple[
    dict[str, dict[str, Any] | None],
    dict[str, list[str]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[str],
]:
    """Per-entity registry updates (no expose).

    Returns (entry_by_id, updates_by_id, extras_by_id, failed, eligible_ids);
    eligible_ids are the ids whose registry update succeeded (all ids when
    there is no registry work — expose-only bulk).
    """
    entry_by_id: dict[str, dict[str, Any] | None] = {}
    updates_by_id: dict[str, list[str]] = {}
    extras_by_id: dict[str, dict[str, Any]] = {}
    failed: list[dict[str, Any]] = []

    if update_one is None:
        for eid in entity_ids:
            updates_by_id[eid] = []
        return entry_by_id, updates_by_id, extras_by_id, failed, list(entity_ids)

    eligible_ids: list[str] = []
    results = await asyncio.gather(
        *[update_one(eid) for eid in entity_ids], return_exceptions=True
    )
    for eid, result in zip(entity_ids, results, strict=True):
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            # Never swallow cancellation/shutdown into per-entity errors.
            raise result
        if isinstance(result, BaseException):
            error_msg = (
                extract_tool_error_message(result)
                if isinstance(result, ToolError)
                else str(result)
            )
            failed.append({"entity_id": eid, "error": error_msg})
        else:
            # update_one returns success-shape or raises ToolError (caught above).
            entry_by_id[eid] = result.get("entity_entry")
            updates_by_id[eid] = list(result.get("updates") or [])
            extras_by_id[eid] = {k: v for k, v in result.items() if k not in _ROW_KEYS}
            eligible_ids.append(eid)
    return entry_by_id, updates_by_id, extras_by_id, failed, eligible_ids


async def _expose_phase(
    client: Any,
    eligible_ids: list[str],
    parsed_expose_to: dict[str, bool],
    entry_by_id: dict[str, dict[str, Any] | None],
    updates_by_id: dict[str, list[str]],
    refetch: bool,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Batch-expose eligible_ids, then refetch the registry entries among them.

    Returns (still_eligible_ids, newly_failed, warnings). An id that
    ``expose_targets`` cannot resolve fails before the write. A failed
    expose call is reported against every id it was sent for (each call
    acts on the whole list) and clears eligibility. On success, options
    mutate, so one get_entries refetch refreshes each registry entry; a
    refetch failure keeps the pre-exposure entry, since the exposure
    already committed, and adds a warning. Without ``refetch`` (the reply
    carries no entries) nothing is read after the write.
    """
    targets, failed = await expose_targets(client, eligible_ids, entry_by_id)
    eligible_ids = [eid for eid in eligible_ids if eid in targets]
    if not eligible_ids:
        return [], failed, []
    expose_error, changed = await apply_bulk_expose(
        client,
        [targets[eid] for eid in eligible_ids],
        parsed_expose_to,
        _extract_ws_error,
    )
    if expose_error is not None:
        failed.extend(
            {"entity_id": eid, "error": f"Exposure failed: {expose_error}"}
            for eid in eligible_ids
        )
        return [], failed, []

    for eid in eligible_ids:
        unchanged = targets[eid] not in changed
        updates_by_id[eid].append(expose_update(parsed_expose_to, unchanged))
    if not refetch:
        return eligible_ids, failed, []
    registered = [eid for eid in eligible_ids if entry_by_id.get(eid) is not None]
    refetched, refetch_errors = await get_entries_raw(
        client, [targets[eid] for eid in registered]
    )
    warnings: list[str] = []
    for eid in registered:
        if targets[eid] in refetched:
            entry_by_id[eid] = refetched[targets[eid]]
        else:
            warnings.append(refresh_warning(eid, refetch_errors[targets[eid]]))
    if warnings:
        logger.warning("Bulk exposure applied; %s", " ".join(warnings))
    return eligible_ids, failed, warnings


async def bulk_update_entities(
    client: Any,
    entity_ids: list[str],
    update_one: Callable[[str], Awaitable[dict[str, Any]]] | None,
    parsed_expose_to: dict[str, bool] | None,
    include_entity_entry: bool,
) -> dict[str, Any]:
    """Apply the registry updates (``update_one``, None when there are none)
    and the exposure across many entities, reporting each entity's outcome."""
    logger.info(f"Bulk updating {len(entity_ids)} entities")
    if update_one is None and parsed_expose_to is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "No updates specified",
                suggestions=[
                    "Provide at least one of: labels, categories, enabled, "
                    "hidden, or expose_to"
                ],
            )
        )

    warnings = await _back_up_entities(client, entity_ids)
    (
        entry_by_id,
        updates_by_id,
        extras_by_id,
        failed,
        eligible_ids,
    ) = await _registry_phase(entity_ids, update_one)

    if parsed_expose_to is not None and eligible_ids:
        eligible_ids, expose_failed, refresh_warnings = await _expose_phase(
            client,
            eligible_ids,
            parsed_expose_to,
            entry_by_id,
            updates_by_id,
            refetch=include_entity_entry,
        )
        failed.extend(expose_failed)
        warnings += refresh_warnings

    succeeded_ids = set(eligible_ids)
    succeeded_list = [
        {
            **extras_by_id.get(eid, {}),
            "entity_id": eid,
            **({"entity_entry": entry_by_id.get(eid)} if include_entity_entry else {}),
            "updates": updates_by_id.get(eid, []),
        }
        for eid in entity_ids
        if eid in succeeded_ids
    ]

    response: dict[str, Any] = {
        "success": len(failed) == 0,
        "total": len(entity_ids),
        "succeeded_count": len(succeeded_list),
        "failed_count": len(failed),
        "succeeded": succeeded_list,
    }
    if failed:
        response["failed"] = failed
        response["partial"] = len(succeeded_list) > 0
    if warnings:
        response["warnings"] = warnings
    return response
