"""The identity checks of ``ha_set_integration``'s reconfigure mode.

A reconfigure edits a live entry in place, so before each call that answers
the entry's reconfigure flow the caller's identity anchors are checked
against what the registries say now. The flow itself is answered step by
step in ``integration_flow_steps``; identity comparison lives in
``config_entry_identity``.
"""

from typing import Any

from ..errors import ErrorCode, create_error_response
from .config_entry_flow import refuse_component_entry
from .config_entry_identity import (
    ReconfigureIdentity,
    _classify_related_entries,
    _collect_reconfigure_identity,
    _has_reconfigure_identity_anchor,
    _normalise_identity_value,
    _raise_identity_mismatch,
    cross_domain_warnings,
)
from .helpers import raise_tool_error, validate_identifier_not_empty
from .ignored_entries import refuse_ignored_entry


async def check_reconfigure_identity(
    client: Any, entry_id: str, expected_identity: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """The entry and its cross-domain warnings, once it may be reconfigured.

    Read-only: the entry exists, it implements ``async_step_reconfigure``, the
    caller's identity anchors match what the registries currently say, no
    second entry in the same domain shares the device, and some identity
    anchor exists at all.
    """
    validated_entry_id = validate_identifier_not_empty(
        entry_id,
        "entry_id",
        suggestions=["Use ha_get_integration() to find valid config entry IDs"],
    )
    entry = await client.get_config_entry(validated_entry_id)
    domain = entry.get("domain")
    if not isinstance(domain, str) or not domain:
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                "Home Assistant returned a config entry without a valid domain",
                context={"entry_id": validated_entry_id, "entry": entry},
            )
        )
    refuse_component_entry(domain, action="reconfigure", entry_id=validated_entry_id)
    refuse_ignored_entry(entry, action="reconfigure", entry_id=validated_entry_id)
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
                    "entry_id": validated_entry_id,
                    "domain": domain,
                    "supports_reconfigure": False,
                },
            )
        )

    expected = expected_identity
    identity, warnings = await _validate_reconfigure_identity_and_duplicates(
        client,
        entry,
        entry_id=validated_entry_id,
        domain=domain,
        expected_identity=expected,
    )
    if not _has_reconfigure_identity_anchor(identity, expected):
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
                    "entry_id": validated_entry_id,
                    "domain": domain,
                    "available_identity": {
                        "unique_id": identity.unique_id,
                        "device_ids": identity.device_ids,
                        "entity_ids": identity.entity_ids,
                        "macs": identity.macs,
                    },
                    "expected_identity": expected,
                },
            )
        )
    return entry, warnings


async def _validate_reconfigure_identity_and_duplicates(
    client: Any,
    entry: dict[str, Any],
    *,
    entry_id: str,
    domain: str,
    expected_identity: dict[str, Any],
) -> tuple[ReconfigureIdentity, list[str]]:
    """Validate pre-flow identity anchors and same-domain duplicates."""
    before_identity = await _collect_reconfigure_identity(client, entry, entry_id)
    checks = (
        (
            expected_identity.get("device_id"),
            before_identity.device_ids,
            "device_id",
            "The entry does not match expected device_id before reconfigure",
        ),
        (
            expected_identity.get("unique_id"),
            (
                [before_identity.unique_id]
                if before_identity.unique_id_known
                and before_identity.unique_id is not None
                else []
            ),
            "unique_id",
            "The entry does not match expected unique_id before reconfigure",
        ),
        (
            expected_identity.get("mac"),
            before_identity.macs,
            "mac",
            "The entry does not match expected MAC before reconfigure",
        ),
        (
            expected_identity.get("entity_ids") or None,
            before_identity.entity_ids,
            "entity_ids",
            "The entry does not match expected entity_ids before reconfigure",
        ),
    )
    if (
        expected_identity.get("unique_id") is not None
        and not before_identity.unique_id_known
    ):
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
            before=before_identity,
            expected=expected_identity,
        )
    for expected, available, key, message in checks:
        if expected is None:
            continue
        if not available:
            _raise_identity_mismatch(
                entry_id,
                f"Cannot compare expected {key}: the registries report none for "
                "this entry",
                before=before_identity,
                expected=expected_identity,
            )
        if key == "mac":
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
                message,
                before=before_identity,
                expected=expected_identity,
            )
    related = await _classify_related_entries(
        client,
        before_identity,
        entry_id=entry_id,
        domain=domain,
    )
    if related.blocking:
        _raise_identity_mismatch(
            entry_id,
            "A second config entry in the same domain shares this entry's "
            "registered device",
            before=before_identity,
            expected=expected_identity,
        )
    return before_identity, cross_domain_warnings(related.cross_domain)
