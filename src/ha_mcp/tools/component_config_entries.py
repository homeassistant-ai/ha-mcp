"""Config-entry reads over the ``ha_mcp_tools`` component gate.

The component's ``config_entries`` command returns Core's
``ConfigEntry.as_json_fragment`` rows plus ``unique_id``, the entry's
materialized ``options`` (secret-scrubbed) and its ``subentries`` identity rows.
:func:`fetch_config_entries` is the one reader of that command, following the
routing discipline the sibling component modules established: probe caps, send
one frame, invalidate on ``unknown_command``, fall back on any component error.

``unique_id`` is the field no other source has: Home Assistant withholds it
from every config-entry endpoint — the REST list, ``config_entries/get`` and
``config_entries/get_single`` all serialize ``ConfigEntry.as_json_fragment``,
which has no ``unique_id`` key — so the component, which holds the live
``ConfigEntry``, is the only identity anchor.

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


SCRUB_DEGRADED_WARNING = (
    "options: the ha_mcp_tools component could not read secrets.yaml, so "
    "option values that match a secret were not replaced with '**redacted**'"
)


class ComponentEntries(NamedTuple):
    """One trusted ``config_entries`` answer."""

    rows: list[dict[str, Any]]
    #: The component could not read ``secrets.yaml``, so ``options`` went out
    #: unscrubbed.
    secret_scrub_degraded: bool


async def fetch_config_entries(
    client: Any, *, entry_id: str | None = None, domain: str | None = None
) -> ComponentEntries | None:
    """One ``config_entries`` read; ``None`` when the answer cannot be trusted.

    Pass ``entry_id`` for the single entry (no rows ⇒ no such entry) or
    ``domain`` to filter the list (Core's exact match); neither lists all.

    ``None`` means "use your legacy path": no capability, a downgrade
    (``unknown_command`` also invalidates the cached caps), any other command
    error or timeout, any failure to reach the pooled WS, or a malformed
    payload. The connection failure is caught on purpose: the callers' legacy
    paths are REST or the REST client's WS bridge, not this pooled socket, so
    a pooled-WS outage must not kill a tool that can still serve the read.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "config_entries"):
        return None
    query: dict[str, Any] = {}
    if entry_id is not None:
        query["entry_id"] = entry_id
    if domain is not None:
        query["domain"] = domain
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            # verify_ssl keys the client pool, so omitting it hands back a
            # DIFFERENT pooled client than the capability probe used and a
            # self-signed setup fails the read after passing detection.
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(WS_CONFIG_ENTRIES, **query)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed: %r", WS_CONFIG_ENTRIES, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s connection error: %r", WS_CONFIG_ENTRIES, exc)
        return None
    result = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(result, dict):
        result = {}
    entries = result.get("entries")
    if not isinstance(entries, list) or any(
        not isinstance(row, dict) for row in entries
    ):
        logger.debug("%s returned a malformed result", WS_CONFIG_ENTRIES)
        return None
    return ComponentEntries(
        rows=list(entries),
        secret_scrub_degraded=result.get("secret_scrub_degraded") is True,
    )


async def fetch_domain_unique_ids(client: Any, domain: str) -> dict[str, str] | None:
    """Map ``entry_id -> unique_id`` for one domain, or ``None`` if unreadable.

    One read, not one per candidate. The duplicate scan needs the unique_id of
    every same-domain entry, and Home Assistant's own config-entry rows carry
    none — so without this the scan can only ever compare entry_ids and must
    not claim it checked unique_ids.

    A genuinely ``None`` ``unique_id`` is the ONLY value omitted from the map,
    so a present key always means a real value. Anything malformed returns
    ``None`` for the whole map instead: the duplicate scan reads a returned map
    as a COMPLETED check, so an incomplete one could drop the very duplicate
    row it exists to catch.
    """
    read = await fetch_config_entries(client, domain=domain)
    if read is None:
        return None
    mapping: dict[str, str] = {}
    for row in read.rows:
        if "unique_id" not in row:
            # An older component: the field is missing wholesale.
            logger.debug(
                "%s rows for domain %s predate the unique_id field",
                WS_CONFIG_ENTRIES,
                domain,
            )
            return None
        entry_id = row.get("entry_id")
        unique_id = row["unique_id"]
        if not isinstance(entry_id, str) or (
            unique_id is not None and not isinstance(unique_id, str)
        ):
            # A partial map would be read as a COMPLETED scan by the duplicate
            # check, so a malformed duplicate row could be dropped and let
            # verification pass. Incomplete must be indistinguishable from
            # unreadable.
            logger.debug(
                "%s returned a malformed row for domain %s; map unusable",
                WS_CONFIG_ENTRIES,
                domain,
            )
            return None
        # unique_id None is a valid answer (the entry has none) and is the one
        # value legitimately omitted from the map.
        if unique_id is not None:
            mapping[entry_id] = unique_id
    return mapping


async def fetch_config_entry_unique_id(client: Any, entry_id: str) -> EntryUniqueId:
    """Read one config entry's ``unique_id`` through the component.

    Returns :data:`UNKNOWN_UNIQUE_ID` whenever the answer cannot be trusted —
    no ``config_entries`` capability, a downgraded component, a transport
    failure, a malformed payload, or a row that predates the field. Never
    raises: an unreadable ``unique_id`` degrades the anchor, and the caller
    decides what that means, rather than failing the whole reconfigure here.
    """
    read = await fetch_config_entries(client, entry_id=entry_id)
    if read is None or not read.rows:
        return UNKNOWN_UNIQUE_ID
    row = read.rows[0]
    # Key presence is the capability probe: a component older than the field
    # omits it entirely, which must not read as "this entry has no unique_id".
    if "unique_id" not in row:
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
