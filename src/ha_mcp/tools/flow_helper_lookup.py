"""Resolve a flow-helper entity to its config entry for ha_remove_helpers_integrations."""

import logging
from typing import Any, Literal, NoReturn

from ..client.rest_client import HomeAssistantAuthError, HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from .config_entry_flow import FLOW_HELPER_TYPES
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

FlowLookupReason = Literal[
    "ok",
    "wrong_helper_type",
    "bare_id_not_supported",
    "not_in_registry",
    "platform_mismatch",
    "no_config_entry",
    "lookup_failed",
]


async def _get_entry_id_for_flow_helper(
    client: Any,
    helper_type: str,
    target: str,
    warnings: list[str] | None = None,
) -> tuple[str | None, FlowLookupReason]:
    """Resolve a flow-helper target to its config_entry_id via entity_registry.

    Used by ha_remove_helpers_integrations when target is an entity_id
    (contains a '.') and helper_type is a known flow-helper type.

    Args:
        client: HomeAssistantClient instance.
        helper_type: Flow-helper type (must be in FLOW_HELPER_TYPES).
        target: Full entity_id, e.g. "sensor.my_meter". Bare IDs not
            supported for flow helpers (caller must provide entity_id).
        warnings: Optional list — appended to on WebSocket failure.

    Returns:
        Tuple of (config_entry_id, reason). On success: (entry_id, "ok").
        On failure: (None, reason) where reason discriminates the cause so
        the caller can produce an accurate error response without an extra
        WebSocket round-trip. HomeAssistantConnectionError and
        HomeAssistantAuthError propagate; the caller's outer except chain
        converts them to structured errors.
    """
    if helper_type not in FLOW_HELPER_TYPES:
        return None, "wrong_helper_type"

    if "." not in target:
        return None, "bare_id_not_supported"
    entity_id = target

    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
    except (HomeAssistantConnectionError, HomeAssistantAuthError):
        # Typed errors must reach the outer handler — do not swallow.
        raise
    except (OSError, TimeoutError) as e:
        # Network / transport errors from the WS layer (ConnectionError,
        # BrokenPipeError, TimeoutError, …). Programmer-bug-shape
        # exceptions (KeyError, AttributeError, TypeError) intentionally
        # propagate — the response is shape-checked at the dict guard
        # below, and a raise here would otherwise mask the bug as a
        # transient WEBSOCKET_DISCONNECTED.
        logger.debug(f"entity_registry/get failed for {entity_id}: {e}")
        if warnings is not None:
            warnings.append(f"entity_registry/get failed for {entity_id}: {e}")
        return None, "lookup_failed"

    if not isinstance(result, dict) or not result.get("success"):
        return None, "not_in_registry"

    entry = result.get("result") or {}
    if not isinstance(entry, dict):
        return None, "not_in_registry"

    # A helper's entities are registered under its own integration, so a
    # foreign platform means the config_entry_id belongs to another integration.
    if entry.get("platform") != helper_type:
        return None, "platform_mismatch"

    config_entry_id = entry.get("config_entry_id")
    if not config_entry_id:
        return None, "no_config_entry"
    return config_entry_id, "ok"


def raise_flow_helper_lookup_error(
    reason: FlowLookupReason,
    helper_type: str,
    target: str,
) -> NoReturn:
    """Raise the structured error for a failed flow-helper entry_id lookup.

    ``reason`` discriminates the failure mode without a second WebSocket
    round-trip. The lookup helper already queried the registry; the response
    told us everything we need.
    """
    entity_id = target if "." in target else f"{helper_type}.{target}"
    if reason == "platform_mismatch":
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                (
                    f"{target} is not a {helper_type} helper: its registry "
                    "entry belongs to another integration. Nothing was "
                    "deleted."
                ),
                context={
                    "target": target,
                    "helper_type": helper_type,
                    "entity_id": entity_id,
                },
                suggestions=[
                    "Check the entity's platform with ha_get_entity(); a "
                    "helper's platform is its helper_type.",
                    "To delete an integration's config entry, pass its "
                    "entry_id as target and omit helper_type.",
                ],
            )
        )
    if reason == "no_config_entry":
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                (
                    f"Helper {target} is not a storage-based "
                    "helper (no config entry). YAML-configured "
                    "helpers must be removed by editing the "
                    "configuration file."
                ),
                context={
                    "target": target,
                    "helper_type": helper_type,
                    "entity_id": entity_id,
                },
                suggestions=[
                    "Edit the YAML file and reload the relevant integration.",
                ],
            )
        )
    if reason == "lookup_failed":
        # Registry WebSocket call failed transiently. Surface as
        # a connectivity error so the caller knows to retry,
        # rather than chasing a non-existent entity_id.
        raise_tool_error(
            create_error_response(
                ErrorCode.WEBSOCKET_DISCONNECTED,
                (
                    f"Registry lookup for {entity_id} failed "
                    "due to a WebSocket error."
                ),
                context={
                    "target": target,
                    "helper_type": helper_type,
                    "entity_id": entity_id,
                },
            )
        )
    # wrong_helper_type cannot occur here because the dispatcher
    # already checked SIMPLE_HELPER_TYPES / FLOW_HELPER_TYPES; the
    # assertion enforces that contract at runtime.
    assert reason != "wrong_helper_type"
    if reason == "not_in_registry":
        # Target is absent from the entity registry. Surface
        # as ENTITY_NOT_FOUND (entity-shaped target) so the
        # caller learns the identifier is unusable — the typo
        # case is the failure mode "absent → success" would
        # silently mask. Matches the bare_id_not_supported
        # branch below and sibling ha_remove_entity.
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                (
                    f"Helper {target} not found in entity "
                    f"registry (looked up as {entity_id}). "
                    "May indicate it was already removed, "
                    "never existed, or the identifier is a "
                    "typo. Verify with ha_search() "
                    "before retrying."
                ),
                context={
                    "target": target,
                    "helper_type": helper_type,
                    "entity_id": entity_id,
                },
                suggestions=[
                    "Use ha_search() — flow helper "
                    "types often expose entities under a "
                    "different domain than the helper_type "
                    "itself (e.g. utility_meter → sensor.*, "
                    "switch_as_x → switch.* / light.*).",
                ],
            )
        )
    # bare_id_not_supported → caller passed a bare ID where an
    # entity_id was required. That's a call-shape error, not
    # missing-target; surface as ENTITY_NOT_FOUND with the
    # search suggestion so the caller can self-correct.
    raise_tool_error(
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            (
                f"Helper {target} not found in entity registry "
                f"(looked up as {entity_id})."
            ),
            context={
                "target": target,
                "helper_type": helper_type,
                "entity_id": entity_id,
            },
            suggestions=[
                "For a config entry_id target, omit helper_type to delete it.",
                (
                    "Otherwise find the entity_id with ha_search(): flow "
                    "helpers often use another domain (utility_meter → sensor.*)."
                ),
            ],
        )
    )
