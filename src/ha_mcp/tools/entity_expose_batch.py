"""Bulk ``homeassistant/expose_entity`` calls for ``ha_set_entity``."""

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def changed_entity_ids(reply: dict[str, Any]) -> set[str] | None:
    """The entities Core reports it changed, or None before Core 2026.11."""
    result = reply.get("result")
    if isinstance(result, dict) and isinstance(result.get("changed_entity_ids"), list):
        return set(result["changed_entity_ids"])
    return None


async def apply_bulk_expose(
    client: Any,
    entity_ids: list[str],
    parsed_expose_to: dict[str, bool],
    extract_error: Callable[[dict[str, Any]], str],
) -> tuple[str | None, set[str] | None]:
    """Expose/hide many entities in one homeassistant/expose_entity call per set.

    The command's schema accepts the full entity_ids list, so bulk exposure
    needs at most two WS calls (one for the assistants being turned on, one
    for those being turned off) regardless of entity count — not one per
    entity. Returns (error, changed): error is None on success, or the message
    of the first failing set, which applies to every id in the list. changed
    holds the ids Core reports it changed, or None when Core does not report
    them (before 2026.11).
    """
    expose_true = [a for a, v in parsed_expose_to.items() if v]
    expose_false = [a for a, v in parsed_expose_to.items() if not v]
    applied: list[str] = []
    changed: set[str] | None = set()
    for assistants, should_expose in [(expose_true, True), (expose_false, False)]:
        if not assistants:
            continue
        logger.info(
            f"{'Exposing' if should_expose else 'Hiding'} {len(entity_ids)} "
            f"entities {'to' if should_expose else 'from'} {assistants}"
        )
        result = await client.send_websocket_message(
            {
                "type": "homeassistant/expose_entity",
                "assistants": assistants,
                "entity_ids": entity_ids,
                "should_expose": should_expose,
            }
        )
        if not result.get("success"):
            error = extract_error(result)
            if applied:
                # The earlier assistant group already changed — say so, or
                # the all-failed report implies no exposure was touched.
                error = (
                    f"{error} (note: expose changes for "
                    f"{', '.join(applied)} were already applied before "
                    "this failure)"
                )
            return error, None
        applied.extend(assistants)
        reported = changed_entity_ids(result)
        changed = None if reported is None or changed is None else changed | reported
    return None, changed
