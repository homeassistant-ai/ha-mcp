"""Config-entry identity: what a reconfigure must not silently change.

A reconfigure edits connection settings in place, so before each call that
may answer the entry's reconfigure flow (``integration_flow_steps``) the
caller's identity anchors are checked against what the registries say now.
This module reads that identity from the registries (and, for ``unique_id``,
from the ha_mcp_tools component, since Home Assistant's own API never exposes
it), classifies entries related through a shared device, and compares both
with the anchors.

Every read here raises rather than producing a half-filled identity: an empty
list means "the entry has none", never "we could not tell".
"""

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, NoReturn

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .component_config_entries import fetch_config_entry_unique_id
from .component_devices import fetch_device_list_via_component
from .component_registry_lookup import fetch_entities_for_config_entry_via_component
from .config_entry_flow import refuse_component_entry
from .helpers import raise_tool_error
from .ignored_entries import refuse_ignored_entry

logger = logging.getLogger(__name__)


@dataclass
class ReconfigureIdentity:
    """Registry identity of one config entry, read before each flow step.

    Every field is a real registry read: a registry that cannot be read raises
    rather than producing a half-filled instance, so an empty list means "the
    entry has none", never "we could not tell".
    """

    unique_id: str | None = None
    #: False when nothing could read the entry's unique_id — no custom
    #: component, or one predating the field. Home Assistant's own API never
    #: exposes it (``as_json_fragment`` omits it), so on an add-on / Docker /
    #: PyPI install without the component this is the normal state, and
    #: ``unique_id`` being None means "unknown", NOT "the entry has none".
    unique_id_known: bool = False
    device_ids: list[str] = field(default_factory=list)
    entity_ids: list[str] = field(default_factory=list)
    macs: list[str] = field(default_factory=list)
    related_entry_ids: list[str] = field(default_factory=list)


# Domains whose entries Home Assistant deliberately attaches to another
# integration's device (helper platforms built on a source entity). Membership
# only silences the cross-domain warning below — it never decides whether a
# relation blocks, so a domain missing from this list costs a line of noise,
# not a refused reconfigure.
_KNOWN_AUXILIARY_ENTRY_DOMAINS = frozenset(
    {
        "derivative",
        "history_stats",
        "integration",
        "statistics",
        "switch_as_x",
        "threshold",
        "trend",
        "utility_meter",
    }
)


_DEVICE_CONNECTION_ID_TYPES = frozenset({"ieee", "mac", "zigbee"})


def _normalise_identity_value(value: Any) -> str:
    """Normalise registry identifiers for stable comparisons."""
    return "".join(character for character in str(value).upper() if character.isalnum())


def _is_hardware_identifier(value: Any) -> bool:
    """Return whether ``value`` looks like a MAC or IEEE hardware ID."""
    normalised = _normalise_identity_value(value)
    return len(normalised) in {12, 16} and all(
        character in "0123456789ABCDEF" for character in normalised
    )


def _device_hardware_ids(rows: list[dict[str, Any]]) -> list[str]:
    """Collect the MAC/IEEE-shaped identifiers carried by device rows."""
    identifiers: list[Any] = []
    for row in rows:
        for identifier in (
            *(row.get("connections") or []),
            *(row.get("identifiers") or []),
        ):
            if not isinstance(identifier, (list, tuple)) or len(identifier) < 2:
                continue
            identifier_type = str(identifier[0]).lower()
            identifier_value = identifier[1]
            if (
                identifier_type in _DEVICE_CONNECTION_ID_TYPES
                or _is_hardware_identifier(identifier_value)
            ):
                identifiers.append(identifier_value)
    return sorted(
        {
            _normalise_identity_value(value)
            for value in identifiers
            if _normalise_identity_value(value)
        }
    )


async def _entry_entity_rows(client: Any, entry_id: str) -> list[dict[str, Any]]:
    """Entity-registry rows belonging to one config entry.

    Prefers the custom component's ``registry_lookup``, which filters
    server-side, so the common path no longer pulls the whole entity registry
    across the wire once per identity collection. Falls back to the legacy
    whole-registry dump when the component is absent or older.
    """
    rows = await fetch_entities_for_config_entry_via_component(client, entry_id)
    if rows is not None:
        return [row for row in rows if isinstance(row, dict)]
    all_rows = await _registry_rows(
        client.list_entity_registry, "list_entity_registry", entry_id
    )
    return [row for row in all_rows if row.get("config_entry_id") == entry_id]


async def _device_registry_rows(client: Any, entry_id: str) -> list[dict[str, Any]]:
    """Every device-registry row, via the component when it offers the read."""
    payload = await fetch_device_list_via_component(client)
    if payload is not None:
        return [row for row in payload["devices"] if isinstance(row, dict)]
    return await _registry_rows(
        client.list_device_registry, "list_device_registry", entry_id
    )


async def _collect_reconfigure_identity(
    client: Any, entry_id: str
) -> ReconfigureIdentity:
    """Collect registry identity without requiring the physical device online."""
    entity_rows = await _entry_entity_rows(client, entry_id)
    device_rows = await _device_registry_rows(client, entry_id)
    entry_unique_id = await fetch_config_entry_unique_id(client, entry_id)

    entity_ids = sorted(
        row["entity_id"] for row in entity_rows if isinstance(row.get("entity_id"), str)
    )
    device_ids = {row["device_id"] for row in entity_rows if row.get("device_id")}

    matching_device_rows = [
        row
        for row in device_rows
        if row.get("id") in device_ids or entry_id in (row.get("config_entries") or [])
    ]
    device_ids |= {
        row["id"] for row in matching_device_rows if isinstance(row.get("id"), str)
    }
    # Which other config entries share this entry's devices. HA maintains
    # ``DeviceEntry.config_entries`` as the set of entries that contributed to
    # the device, so it answers this without a second whole-registry scan.
    related_entry_ids: set[str] = set()
    for row in matching_device_rows:
        related_entry_ids.update(
            item for item in (row.get("config_entries") or []) if isinstance(item, str)
        )

    return ReconfigureIdentity(
        unique_id=entry_unique_id.value,
        unique_id_known=entry_unique_id.known,
        device_ids=sorted(device_ids),
        entity_ids=entity_ids,
        macs=_device_hardware_ids(matching_device_rows),
        related_entry_ids=sorted(related_entry_ids),
    )


@dataclass(frozen=True)
class RelatedEntries:
    """How the entries sharing this entry's device were classified."""

    #: Same-domain entries — a genuine duplicate-integration risk.
    blocking: list[str] = field(default_factory=list)
    #: Entries from other integrations attached to the same device.
    cross_domain: list[str] = field(default_factory=list)


def cross_domain_warnings(entry_ids: list[str]) -> list[str]:
    """Warn about entries from other integrations sharing this entry's device."""
    if not entry_ids:
        return []
    return [
        "This entry's device is shared with config entries from other "
        f"integrations: {', '.join(entry_ids)}. Reconfiguring this entry may "
        "change what those read."
    ]


async def _classify_related_entries(
    client: Any,
    identity: ReconfigureIdentity,
    *,
    entry_id: str,
    domain: str,
) -> RelatedEntries:
    """Split the entries sharing this entry's device into blocking and auxiliary.

    Only a second entry in the SAME domain is a duplicate-integration risk, so
    only that blocks. Home Assistant routinely attaches helper platforms
    (``utility_meter``, ``derivative``, ``statistics``, ``switch_as_x``, …) to
    the source device, and blocking on those made every entry carrying one
    unreconfigurable. Cross-domain relations outside the known-auxiliary list
    are reported as warnings instead.

    A relation whose domain cannot be resolved at all — the entry was removed
    mid-flight, or the row is malformed — still blocks: the safeguard fails
    closed when it cannot tell.

    Returns the classification rather than writing it back through
    ``identity``, so nothing mutates an argument the signature calls an input.
    """
    related_entry_ids = set(identity.related_entry_ids) - {entry_id}
    if not related_entry_ids:
        return RelatedEntries()

    entries = await _validated_config_entry_rows(client, entry_id)
    entries_by_id = {
        item.get("entry_id"): item
        for item in entries
        if isinstance(item, dict) and item.get("entry_id")
    }
    blocking: list[str] = []
    cross_domain: list[str] = []
    for related_id in sorted(related_entry_ids):
        related_entry = entries_by_id.get(related_id)
        related_domain = (
            related_entry.get("domain") if isinstance(related_entry, dict) else None
        )
        if not isinstance(related_domain, str) or not related_domain:
            related_domain = (
                related_entry.get("handler")
                if isinstance(related_entry, dict)
                else None
            )
        # An unresolvable domain (entry removed mid-flight, malformed row)
        # blocks: the safeguard fails closed when it cannot tell.
        if not isinstance(related_domain, str) or related_domain in ("", domain):
            blocking.append(related_id)
        elif related_domain not in _KNOWN_AUXILIARY_ENTRY_DOMAINS:
            cross_domain.append(f"{related_domain} ({related_id})")

    return RelatedEntries(blocking=blocking, cross_domain=cross_domain)


async def _validated_config_entry_rows(
    client: Any, entry_id: str
) -> list[dict[str, Any]]:
    """Read config entries and reject malformed rows before identity checks."""
    result = await client.list_config_entries()
    if not isinstance(result, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("entry_id"), str)
        for row in result
    ):
        raise HomeAssistantAPIError(
            f"Unexpected config-entry registry response for {entry_id}",
            status_code=500,
        )
    return result


async def _registry_rows(
    reader: Any,
    method_name: str,
    entry_id: str,
) -> list[dict[str, Any]]:
    """Read one registry, translating transport failures for fail-closed callers.

    Never degrades to "registry unavailable": a registry this reconfigure
    cannot read is a reason to stop, not to skip the identity and duplicate
    checks that depend on it. Callers that can tolerate the failure catch it.
    """
    try:
        result: Any = await reader()
    except (
        ConnectionError,
        OSError,
        TimeoutError,
        HomeAssistantConnectionError,
        HomeAssistantCommandError,
        HomeAssistantCommandTimeout,
    ) as err:
        logger.warning(
            "Registry read %s failed for %s (%s): %s",
            method_name,
            entry_id,
            type(err).__name__,
            err,
        )
        if isinstance(err, HomeAssistantConnectionError):
            # Re-raised as is: chaining it to itself would drop its cause, such
            # as the auth error behind a token rejected on reconnect.
            raise
        raise HomeAssistantConnectionError(
            f"{method_name} transport unavailable for {entry_id}"
        ) from err
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise HomeAssistantAPIError(
            f"Unexpected response from {method_name} for {entry_id}",
            status_code=500,
        )
    return result


def _raise_identity_mismatch(
    entry_id: str,
    message: str,
    *,
    before: ReconfigureIdentity,
    expected: dict[str, Any],
) -> NoReturn:
    """Raise for an identity anchor that does not hold; nothing was applied."""
    context: dict[str, Any] = {
        "entry_id": entry_id,
        "expected_identity": expected,
        "before_identity": asdict(before),
    }
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            message,
            suggestions=[
                "Do not retry automatically; inspect the config, device, and "
                "entity registries before attempting another reconfiguration.",
            ],
            context=context,
        )
    )


async def check_reconfigure_identity(
    client: Any, entry_id: str, expected_identity: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """The entry and its cross-domain warnings, once it may be reconfigured.

    Read-only: the entry exists, it implements ``async_step_reconfigure``, the
    caller's identity anchors match what the registries currently say, no
    second entry in the same domain shares the device, and some identity
    anchor exists at all.
    """
    entry = await client.get_config_entry(entry_id)
    domain = entry.get("domain")
    if not isinstance(domain, str) or not domain:
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                "Home Assistant returned a config entry without a valid domain",
                context={"entry_id": entry_id, "entry": entry},
            )
        )
    refuse_component_entry(domain, action="reconfigure", entry_id=entry_id)
    refuse_ignored_entry(entry, action="reconfigure", entry_id=entry_id)
    if not entry.get("supports_reconfigure", False):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Integration '{domain}' does not support the official reconfigure flow",
                suggestions=[
                    "Use ha_get_integration(entry_id=...) to inspect the entry; "
                    "only integrations implementing async_step_reconfigure can be changed this way.",
                ],
                context={
                    "entry_id": entry_id,
                    "domain": domain,
                    "supports_reconfigure": False,
                },
            )
        )

    identity = await _collect_reconfigure_identity(client, entry_id)
    _check_identity_anchors(entry_id, identity, expected_identity)
    related = await _classify_related_entries(
        client, identity, entry_id=entry_id, domain=domain
    )
    if related.blocking:
        _raise_identity_mismatch(
            entry_id,
            "A second config entry in the same domain shares this entry's "
            "registered device",
            before=identity,
            expected=expected_identity,
        )
    # Every anchor the caller passed matched a non-empty registry value above,
    # so only the registries themselves can still be empty here.
    if not (
        identity.unique_id
        or identity.device_ids
        or identity.entity_ids
        or identity.macs
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Cannot safely reconfigure an entry without an identity anchor",
                suggestions=[
                    "Provide expected_device_id, expected_mac or "
                    "expected_entity_ids, or choose an entry with stable "
                    "registry identity. (expected_unique_id needs the "
                    "ha_mcp_tools custom component: Home Assistant does not "
                    "expose a config entry's unique_id.)"
                ],
                context={
                    "entry_id": entry_id,
                    "domain": domain,
                    "available_identity": {
                        "unique_id": identity.unique_id,
                        "device_ids": identity.device_ids,
                        "entity_ids": identity.entity_ids,
                        "macs": identity.macs,
                    },
                    "expected_identity": expected_identity,
                },
            )
        )
    return entry, cross_domain_warnings(related.cross_domain)


def _check_identity_anchors(
    entry_id: str, identity: ReconfigureIdentity, expected_identity: dict[str, Any]
) -> None:
    """Raise unless every anchor the caller passed matches the registries."""
    if expected_identity.get("unique_id") is not None and not identity.unique_id_known:
        # Not a mismatch — nothing could read the value. Home Assistant omits
        # unique_id from every config-entry endpoint, so only the ha_mcp_tools
        # custom component can supply it. Say that, rather than letting the
        # generic "registries report none" below imply the entry has none.
        _raise_identity_mismatch(
            entry_id,
            "Cannot verify expected_unique_id: Home Assistant does not expose a "
            "config entry's unique_id, and the ha_mcp_tools custom component "
            "(which does) is not installed or is too old. Anchor on "
            "expected_device_id, expected_mac or expected_entity_ids instead, "
            "or install/update the component.",
            before=identity,
            expected=expected_identity,
        )
    checks = (
        (expected_identity.get("device_id"), identity.device_ids, "device_id"),
        (
            expected_identity.get("unique_id"),
            [identity.unique_id] if identity.unique_id is not None else [],
            "unique_id",
        ),
        (expected_identity.get("mac"), identity.macs, "MAC"),
        (
            expected_identity.get("entity_ids") or None,
            identity.entity_ids,
            "entity_ids",
        ),
    )
    for expected, available, key in checks:
        if expected is None:
            continue
        if not available:
            _raise_identity_mismatch(
                entry_id,
                f"Cannot compare expected {key}: the registries report none for "
                "this entry",
                before=identity,
                expected=expected_identity,
            )
        if key == "MAC":
            matched = _normalise_identity_value(expected) in {
                _normalise_identity_value(value) for value in available
            }
        elif key == "entity_ids":
            matched = set(expected) == set(available)
        else:
            matched = expected in set(available)
        if not matched:
            _raise_identity_mismatch(
                entry_id,
                f"The entry does not match expected {key} before reconfigure",
                before=identity,
                expected=expected_identity,
            )
