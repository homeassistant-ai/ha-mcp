"""``ha_mcp_tools/logbook_events`` — Core's own logbook processor, in-process.

``ha_get_logs(source='logbook')`` runs Core's logbook query in the live Home
Assistant process through this command, so row shape, entity names and the
context-derived fields come from Core's ``EventProcessor`` itself (the exact
flags the REST ``/api/logbook`` view uses: ``timestamp=False`` for ISO ``when``
values and ``include_entity_name=True``) instead of a server-side
reimplementation of Core's naming rules.

Filters are the superset Core's ``logbook/get_events`` WebSocket command takes —
``entity_ids``, ``device_ids`` and ``context_id`` (the latter cannot be combined
with the other two; Core's ``EventProcessor`` asserts on it). Validation and the
event-type/entity filters run on the loop and the recorder query runs in the
executor, mirroring Core's own handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.logbook.helpers import (
    async_determine_event_types,
    async_filter_entities,
)
from homeassistant.components.logbook.processor import EventProcessor
from homeassistant.components.recorder import get_instance
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

CAPABILITIES = ("logbook_events",)


def _parse_time(value: str, label: str) -> datetime:
    """Parse an ISO datetime the way Core's handlers do, or raise."""
    # Function-local like the other ``HomeAssistantError`` raises in this
    # package: ``homeassistant.exceptions`` is not part of the component's
    # import-time surface (and the unit-test stubs may install it late).
    from homeassistant.exceptions import HomeAssistantError

    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        raise HomeAssistantError(f"Invalid {label}")
    return dt_util.as_utc(parsed)


async def _logbook_events_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Validate the request and run Core's logbook query in the executor."""
    start_time = _parse_time(msg["start_time"], "start_time")
    if end_time_str := msg.get("end_time"):
        end_time = _parse_time(end_time_str, "end_time")
    else:
        end_time = dt_util.utcnow()

    entity_ids = msg.get("entity_ids")
    device_ids = msg.get("device_ids")
    context_id = msg.get("context_id")
    if context_id and (entity_ids or device_ids):
        # Core's processor asserts on this; fail with a shaped error instead.
        from homeassistant.exceptions import HomeAssistantError

        raise HomeAssistantError(
            "context_id cannot be combined with entity_ids or device_ids"
        )

    if entity_ids:
        entity_ids = async_filter_entities(hass, entity_ids)
        if not entity_ids and not device_ids:
            # Everything has been filtered away
            return {"events": []}

    if start_time > dt_util.utcnow():
        return {"events": []}

    event_types = async_determine_event_types(hass, entity_ids, device_ids)
    event_processor = EventProcessor(
        hass,
        event_types,
        entity_ids,
        device_ids,
        context_id,
        timestamp=False,
        include_entity_name=True,
    )
    events = await get_instance(hass).async_add_executor_job(
        event_processor.get_events, start_time, end_time
    )
    return {"events": events}


def _do_logbook_events(
    hass: HomeAssistant, msg: dict[str, Any], *, events: list[Any] | None = None
) -> dict[str, Any]:
    """Return the rows Core produced (pure; the query ran in the prep)."""
    return {"events": events if events is not None else []}
