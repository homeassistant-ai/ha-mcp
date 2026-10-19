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
event-type/entity filters run on the loop; the recorder query AND the result
encoding run in the executor (Core's own ``_ws_formatted_get_events`` approach),
and the finished message goes out through :class:`seam.PreSerializedResult` so
a large window cannot stall the event loop during serialization.
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
from homeassistant.components.websocket_api import messages
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes
from homeassistant.util import dt as dt_util

from .seam import PreSerializedResult

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
    result: datetime = dt_util.as_utc(parsed)
    return result


def _empty_result(msg_id: int) -> bytes:
    """The pre-serialized empty result message (tiny; safe on the loop)."""
    payload: bytes = json_bytes(messages.result_message(msg_id, []))
    return payload


def _serialize_logbook_result(
    msg_id: int,
    start_time: datetime,
    end_time: datetime,
    event_processor: EventProcessor,
) -> bytes:
    """Fetch the rows and encode the result message in the executor.

    Mirrors Core's own ``_ws_formatted_get_events``: the recorder query and the
    JSON encoding both run off the event loop, so a large window cannot stall
    Home Assistant while its response is built.
    """
    payload: bytes = json_bytes(
        messages.result_message(
            msg_id, event_processor.get_events(start_time, end_time)
        )
    )
    return payload


async def _logbook_events_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Validate the request and run Core's query + encoding in the executor."""
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
            return {"payload": _empty_result(msg["id"])}

    if start_time > dt_util.utcnow():
        return {"payload": _empty_result(msg["id"])}

    if "logbook" not in hass.data:
        # Core's processor reads its config from ``hass.data[DOMAIN]``; without
        # the integration loaded the query cannot run (the REST/WS views are
        # registered by logbook too). Surface a clear error instead of a
        # KeyError; the server's command-error path reports it.
        from homeassistant.exceptions import HomeAssistantError

        raise HomeAssistantError(
            "The logbook integration is not set up on this instance; enable it "
            "to query logbook events."
        )

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
    payload = await get_instance(hass).async_add_executor_job(
        _serialize_logbook_result, msg["id"], start_time, end_time, event_processor
    )
    return {"payload": payload}


def _do_logbook_events(
    hass: HomeAssistant, msg: dict[str, Any], *, payload: bytes
) -> PreSerializedResult:
    """Return Core's pre-encoded result message (built in the prep)."""
    return PreSerializedResult(payload=payload)
