"""Assistant checks and bulk ``homeassistant/expose_entity`` calls."""

import logging
from collections.abc import Callable, Iterable
from typing import Any

from ..client.exceptions import HomeAssistantCommandNotSent, HomeAssistantError
from .entity_exposure import get_entries_or_unregistered
from .helpers import raise_core_refusal

logger = logging.getLogger(__name__)


def changed_entity_ids(reply: dict[str, Any]) -> set[str]:
    """The entities Core reports its ``homeassistant/expose_entity`` call changed."""
    return set(reply["result"]["changed_entity_ids"])


async def read_expose_new(client: Any, assistant: str) -> bool:
    """The assistant's "expose new entities" flag, as Core reports it.

    Core answers with the stored value, else its default for the assistant.

    ``homeassistant/expose_new_entities/get`` validates its ``assistant``
    against Core's own list, so an unknown name fails here with Core's
    refusal instead of after a write already applied or as an empty filter
    result.
    """
    reply = await client.send_websocket_message(
        {"type": "homeassistant/expose_new_entities/get", "assistant": assistant},
        expected_error_codes=frozenset({"invalid_format"}),
    )
    if not reply.get("success"):
        raise_core_refusal(
            f"Checking assistant {assistant!r} failed",
            reply,
            {"assistant": assistant},
            suggestions=reply.get("suggestions"),
        )
    return bool(reply["result"]["expose_new"])


async def reject_unknown_assistants(
    client: Any,
    assistants: Iterable[str] | None,
) -> None:
    """Have Core judge each assistant name before it is used."""
    for assistant in assistants or ():
        await read_expose_new(client, assistant)


async def expose_write(
    client: Any,
    assistants: list[str],
    entity_ids: list[str],
    should_expose: bool,
    extract_error: Callable[[dict[str, Any]], str],
) -> tuple[str | None, dict[str, Any]]:
    """Send one ``homeassistant/expose_entity`` write: (error, Core's reply).

    error is None on success. A refusal, or a write that never left, failed
    for sure; one sent without an answer may have been applied, and says so.
    """
    try:
        result = await client.send_websocket_message(
            {
                "type": "homeassistant/expose_entity",
                "assistants": assistants,
                "entity_ids": entity_ids,
                "should_expose": should_expose,
            }
        )
    except HomeAssistantError as exc:
        detail = extract_error({"error": str(exc)})
        if isinstance(exc, HomeAssistantCommandNotSent):
            return f"Exposure failed: {detail}", {}
        return (
            f"Exposure outcome unknown: Home Assistant did not answer ({detail}); "
            "the exposure may have been applied, and sending it again is safe"
        ), {}
    if result.get("success"):
        return None, result
    return f"Exposure failed: {extract_error(result)}", result


async def apply_bulk_expose(
    client: Any,
    entity_ids: list[str],
    parsed_expose_to: dict[str, bool],
    extract_error: Callable[[dict[str, Any]], str],
) -> tuple[str | None, set[str], dict[str, bool]]:
    """Expose/hide many entities in one homeassistant/expose_entity call per set.

    The command's schema accepts the full entity_ids list, so bulk exposure
    needs at most two WS calls (one for the assistants being turned on, one
    for those being turned off) regardless of entity count — not one per
    entity. Returns (error, changed, applied). error is None on success, else
    the row error for the first failing set, which applies to every id in the
    list: a refusal or an unsent command failed, while a connection lost after
    sending leaves the outcome unknown. changed holds the ids Core reports it
    changed, and is empty on error. applied maps each assistant whose set
    landed to its value.
    """
    expose_true = [a for a, v in parsed_expose_to.items() if v]
    expose_false = [a for a, v in parsed_expose_to.items() if not v]
    applied: dict[str, bool] = {}
    changed: set[str] = set()
    for assistants, should_expose in [(expose_true, True), (expose_false, False)]:
        if not assistants:
            continue
        logger.info(
            f"{'Exposing' if should_expose else 'Hiding'} {len(entity_ids)} "
            f"entities {'to' if should_expose else 'from'} {assistants}"
        )
        error, result = await expose_write(
            client, assistants, entity_ids, should_expose, extract_error
        )
        if error is None:
            applied.update(dict.fromkeys(assistants, should_expose))
            changed |= changed_entity_ids(result)
            continue
        if applied:
            # The earlier assistant group already changed — say so, or
            # the all-failed report implies no exposure was touched.
            error = (
                f"{error} (note: expose changes for "
                f"{', '.join(applied)} were already applied before "
                "this failure)"
            )
        return error, set(), applied
    return None, changed, applied


async def expose_targets(
    client: Any,
    entity_ids: list[str],
    entry_by_id: dict[str, dict[str, Any] | None],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """The id to send Core for each entity's exposure, and the ids that fail.

    The id sent is the one Core reads the exposure under: the registry
    entry's, else the one ``unregistered_lookup`` finds. Entries read here
    land in entry_by_id. An id Home Assistant does not know, or whose
    registry entry or state cannot be read, fails before anything is
    written.
    """
    unread = [eid for eid in entity_ids if entry_by_id.get(eid) is None]
    found, known, errors = await get_entries_or_unregistered(client, unread)
    entry_by_id.update(found)
    targets: dict[str, str] = {}
    failed: list[dict[str, Any]] = []
    for eid in entity_ids:
        entry = entry_by_id.get(eid)
        if entry is not None:
            targets[eid] = entry["entity_id"]
        elif eid in known:
            targets[eid] = known[eid][0]
        else:
            failed.append({"entity_id": eid, "error": errors[eid]})
    return targets, failed


def expose_update(parsed_expose_to: dict[str, bool], unchanged: bool) -> str:
    """The updates entry for expose_to, saying so when Core changed nothing."""
    if unchanged:
        return f"expose_to={parsed_expose_to} (already set, nothing changed)"
    return f"expose_to={parsed_expose_to}"


def refresh_warning(entity_id: str, error: str) -> str:
    """The warning for an entry that could not be re-read after its exposure."""
    return (
        f"{entity_id}: the exposure was applied, but re-reading the registry "
        f"entry failed ({error}); an entity_entry in this reply shows it as before."
    )
