"""Config-entry identity: what a reconfigure must not silently change.

A reconfigure edits connection settings in place, so the check that it still
points at the same physical thing is the whole safety story. This module owns
reading that identity from the registries (and, for ``unique_id``, from the
ha_mcp_tools component, since Home Assistant's own API never exposes it),
classifying entries related through a shared device, and comparing before
against after.

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
from .component_config_entries import (
    fetch_config_entry_unique_id,
)
from .component_devices import fetch_device_list_via_component
from .component_registry_lookup import fetch_entities_for_config_entry_via_component
from .config_entry_flow_walker import ReconfigureStatus
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)


@dataclass
class ReconfigureIdentity:
    """Registry identity of one config entry, read before and after a flow.

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

    def as_payload(self) -> dict[str, Any]:
        """Render for an error context or a tool response."""
        return asdict(self)


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
# Read-back attempts and the backoff between them. A network integration's
# reload does real I/O, so the budget has to outlast a slow probe.


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
    client: Any,
    entry: dict[str, Any],
    entry_id: str,
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
    entries: list[dict[str, Any]] | None = None,
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
    ``entries`` lets a caller that already read the config-entry list pass it
    in rather than paying for a second list-all.
    """
    related_entry_ids = set(identity.related_entry_ids) - {entry_id}
    if not related_entry_ids:
        return RelatedEntries()

    if entries is None:
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
        raise (
            err
            if isinstance(err, HomeAssistantConnectionError)
            else HomeAssistantConnectionError(
                f"{method_name} transport unavailable for {entry_id}"
            )
        ) from err
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise HomeAssistantAPIError(
            f"Unexpected response from {method_name} for {entry_id}",
            status_code=500,
        )
    return result


def _has_reconfigure_identity_anchor(
    identity: ReconfigureIdentity, expected_identity: dict[str, Any]
) -> bool:
    """Return whether preflight has stable or caller-supplied identity evidence."""
    return bool(
        (identity.unique_id if identity.unique_id_known else None)
        or identity.device_ids
        or identity.entity_ids
        or identity.macs
        or expected_identity.get("device_id")
        or expected_identity.get("unique_id")
        or expected_identity.get("mac")
        or expected_identity.get("entity_ids")
    )


def _verification_state(before_present: bool, after_present: bool) -> str:
    """Describe what happened to one identity value across the flow."""
    if before_present and after_present:
        return "preserved"
    if after_present:
        return "gained_during_change"
    if before_present:
        return "lost_during_change"
    return "absent"


def _raise_identity_mismatch(
    entry_id: str,
    message: str,
    *,
    before: ReconfigureIdentity,
    after: ReconfigureIdentity,
    expected: dict[str, Any],
    status: str | None = None,
) -> NoReturn:
    """Raise for an identity anchor that does not hold.

    ``status`` is supplied by the post-commit callers so the outcome survives
    :func:`_raise_post_commit_verification_error`; pre-flow callers leave it
    unset, because nothing was applied.
    """
    context: dict[str, Any] = {
        "entry_id": entry_id,
        "expected_identity": expected,
        "before_identity": before.as_payload(),
        "after_identity": after.as_payload(),
    }
    if status is not None:
        context["status"] = status
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


def _verify_reconfigure_identity_fields(
    *,
    entry_id: str,
    before: dict[str, Any],
    before_identity: ReconfigureIdentity,
    after: dict[str, Any],
    after_identity: ReconfigureIdentity,
    expected_identity: dict[str, Any],
) -> None:
    """Verify the identity anchors that must survive a reconfigure flow.

    ``prepare_reconfigure_request`` has already proved ``expected == before``
    for device_id and entity_ids, and the guards below prove ``after ==
    before`` for them, so an expected-vs-after check on those two could never
    fire and is deliberately absent.

    unique_id and MAC are different, and both checks below are reachable.
    unique_id may legitimately change (an integration re-keying on
    reconfigure), so only LOSING it raises on its own and
    ``expected_unique_id`` is what pins the value. ``expected_mac``'s pre-flow
    comparison ran against the BEFORE MACs, so comparing it to the after MACs
    is a real check.
    """
    # Read from the identity objects: Home Assistant's config-entry fragment
    # has no unique_id, so before/after would both be None here forever and
    # this guard would never fire.
    comparable = before_identity.unique_id_known and after_identity.unique_id_known
    before_unique_id = before_identity.unique_id if comparable else None
    after_unique_id = after_identity.unique_id if comparable else None
    # LOSING a unique_id is the hazard: the entry becomes indistinguishable to
    # HA's discovery, which then creates a duplicate. CHANGING it is not — an
    # integration may legitimately re-key on reconfigure via
    # async_update_reload_and_abort(unique_id=...); `filesize` does exactly
    # that, since its unique_id is the file path being reconfigured. A caller
    # who needs the value pinned says so with expected_unique_id, checked next.
    if comparable and before_unique_id is not None and after_unique_id is None:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure flow cleared the entry unique_id, which lets the next "
            "discovery create a duplicate entry",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
            status=ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
        )
    expected_unique_id = expected_identity.get("unique_id")
    if (
        expected_unique_id is not None
        and comparable
        and after_unique_id != expected_unique_id
    ):
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure result does not match expected unique_id",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
            status=ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
        )

    before_device_ids = set(before_identity.device_ids)
    after_device_ids = set(after_identity.device_ids)
    if before_device_ids and after_device_ids != before_device_ids:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure flow changed the associated device_id",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
            status=ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
        )

    before_entity_ids = set(before_identity.entity_ids)
    after_entity_ids = set(after_identity.entity_ids)
    if before_entity_ids and after_entity_ids != before_entity_ids:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure flow changed the associated entity set",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
            status=ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
        )

    expected_mac = expected_identity.get("mac")
    before_macs = set(before_identity.macs)
    after_macs = set(after_identity.macs)
    if before_macs and after_macs and before_macs != after_macs:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure flow changed the associated MAC/IEEE identity",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
            status=ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
        )
    if expected_mac is not None and (
        not after_macs or _normalise_identity_value(expected_mac) not in after_macs
    ):
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure result does not match expected MAC",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
            status=ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
        )
