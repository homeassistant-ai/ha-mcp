"""Config-entry reads over the ``ha_mcp_tools`` component gate.

Exists for one field: ``unique_id``. Home Assistant deliberately withholds it
from every config-entry endpoint — the REST list, ``config_entries/get`` and
``config_entries/get_single`` all serialize ``ConfigEntry.as_json_fragment``,
which has no ``unique_id`` key — so a server that wants it as an identity
anchor has no source but the component, which holds the live ``ConfigEntry``.

The component's ``config_entries`` command returns that field as a deliberate
superset of the fragment. This module owns the caps-gated fetch, following the
routing discipline the sibling component modules established: probe caps, send
one frame, invalidate on ``unknown_command``, fall back on any component error.

**Three states, and callers must keep them apart.** ``unique_id`` is not a
two-valued field here:

- **known, with a value** — the component answered and the entry has one.
- **known to be absent** — the component answered and the entry has none
  (MQTT-style entries genuinely have no unique_id). The key is present, the
  value is ``None``.
- **unknown** — no component, an older component predating the field, or a
  transport failure. Nothing can be concluded.

:func:`fetch_config_entry_unique_id` returns exactly that distinction so a
caller never mistakes "we could not read it" for "the entry has none". That is
what lets the field be additive within ``schema_version`` 1 with no version
gate: an older component omits the KEY, which is distinguishable from sending
it as ``None`` — the same discipline as ``device_get``'s opt-in entities join.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)

logger = logging.getLogger(__name__)

WS_CONFIG_ENTRIES = "ha_mcp_tools/config_entries"


class EntryUniqueId(NamedTuple):
    """A config entry's ``unique_id``, and whether it could be read at all."""

    #: False when no component answered, or the component predates the field.
    known: bool
    #: The value when ``known``; always ``None`` when not.
    value: str | None


UNKNOWN_UNIQUE_ID = EntryUniqueId(known=False, value=None)


async def fetch_config_entry_unique_id(client: Any, entry_id: str) -> EntryUniqueId:
    """Read one config entry's ``unique_id`` through the component.

    Returns :data:`UNKNOWN_UNIQUE_ID` whenever the answer cannot be trusted —
    no ``config_entries`` capability, a downgraded component, a transport
    failure, a malformed payload, or a row that predates the field. Never
    raises: an unreadable ``unique_id`` degrades the anchor, and the caller
    decides what that means, rather than failing the whole reconfigure here.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "config_entries"):
        return UNKNOWN_UNIQUE_ID
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_CONFIG_ENTRIES, entry_id=entry_id)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning(
                "%s failed; unique_id unavailable: %r", WS_CONFIG_ENTRIES, exc
            )
        return UNKNOWN_UNIQUE_ID
    except Exception as exc:
        logger.warning(
            "%s connection error; unique_id unavailable: %r", WS_CONFIG_ENTRIES, exc
        )
        return UNKNOWN_UNIQUE_ID

    result = raw.get("result") if isinstance(raw, dict) else None
    entries = result.get("entries") if isinstance(result, dict) else None
    if not isinstance(entries, list) or not entries:
        logger.debug(
            "%s returned no row for %s; unique_id unavailable",
            WS_CONFIG_ENTRIES,
            entry_id,
        )
        return UNKNOWN_UNIQUE_ID
    row = entries[0]
    # Key presence is the capability probe: a component older than the field
    # omits it entirely, which must not read as "this entry has no unique_id".
    if not isinstance(row, dict) or "unique_id" not in row:
        logger.debug(
            "%s row for %s predates the unique_id field; unique_id unavailable",
            WS_CONFIG_ENTRIES,
            entry_id,
        )
        return UNKNOWN_UNIQUE_ID
    if row.get("entry_id") != entry_id:
        # Reading another entry's anchor would be worse than reading none.
        logger.debug(
            "%s answered for %r when %r was asked; unique_id unavailable",
            WS_CONFIG_ENTRIES,
            row.get("entry_id"),
            entry_id,
        )
        return UNKNOWN_UNIQUE_ID
    value = row["unique_id"]
    if value is not None and not isinstance(value, str):
        # Malformed is unknown, NOT known-absent: silently turning junk into
        # "this entry has no unique_id" is the exact conflation this type
        # exists to prevent.
        logger.debug(
            "%s returned a non-string unique_id (%r) for %s; unique_id unavailable",
            WS_CONFIG_ENTRIES,
            value,
            entry_id,
        )
        return UNKNOWN_UNIQUE_ID
    return EntryUniqueId(known=True, value=value)
