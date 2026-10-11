"""Resolve the helper behind an entity_id for ha_remove_helpers_integrations."""

import logging
from typing import Any, Literal, NoReturn

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .config_helpers.schemas import SIMPLE_HELPER_TYPES
from .helper_flows import helper_flow_types, listed_helper_flows
from .helpers import raise_tool_error, ws_failure_code

logger = logging.getLogger(__name__)

YAML_HELPER_REMOVAL = (
    "YAML-configured helpers must be removed by editing the configuration file."
)
YAML_HELPER_SUGGESTION = "Edit the YAML file and reload the relevant integration."

FlowLookupReason = Literal[
    "ok",
    "wrong_helper_type",
    "bare_id_not_supported",
    "not_in_registry",
    "not_registry_managed",
    "no_config_entry",
    "lookup_failed",
]


async def _read_registry_entry(
    client: Any, entity_id: str, warnings: list[str] | None = None
) -> tuple[dict[str, Any] | None, FlowLookupReason]:
    """Read ``entity_id``'s entity-registry entry, or ``(None, reason)``.

    ``reason`` is ``lookup_failed`` for an OSError/TimeoutError from the
    WebSocket send. On Core's ``not_found`` the entity's state decides between
    ``not_registry_managed`` and ``not_in_registry``. A ``success: false``
    reply other than ``not_found``, and a reply without an entry, raise a
    ToolError: neither proves the entity is absent. HomeAssistantConnectionError, HomeAssistantAuthError
    and a non-404 HomeAssistantAPIError from the state read propagate to the
    caller's except chain.
    """
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
        # below, and catching them here would mask the bug as a
        # transient WEBSOCKET_DISCONNECTED.
        logger.debug(f"entity_registry/get failed for {entity_id}: {e}")
        if warnings is not None:
            warnings.append(f"entity_registry/get failed for {entity_id}: {e}")
        return None, "lookup_failed"

    if not isinstance(result, dict):
        _raise_unexpected_registry_reply(entity_id, result)
    if not result.get("success"):
        # Core answers an unknown entity with not_found; any other failure (a
        # proxy block, a malformed request) is no evidence of absence.
        if result.get("error_code") != "not_found":
            raise_tool_error(
                create_error_response(
                    ws_failure_code(result),
                    f"Reading the entity registry for {entity_id} failed: "
                    f"{result.get('error') or 'unknown error'}",
                    context={"entity_id": entity_id},
                    suggestions=result.get("suggestions"),
                )
            )
        return None, await _absence_reason(client, entity_id)

    entry = result.get("result")
    if not isinstance(entry, dict) or not entry:
        _raise_unexpected_registry_reply(entity_id, entry)
    return entry, "ok"


def _raise_unexpected_registry_reply(entity_id: str, reply: Any) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            (
                f"Reading the entity registry for {entity_id} returned no "
                f"entry ({type(reply).__name__}). Nothing was deleted."
            ),
            context={"entity_id": entity_id},
        )
    )


async def _absence_reason(client: Any, entity_id: str) -> FlowLookupReason:
    """Tell an entity Core keeps no registry entry for from a missing one.

    Core registers only entities with a ``unique_id``; one without it (a YAML
    template sensor without unique_id, ``zone.home``) still has a state. A
    state read failing with a status other than 404 propagates.
    """
    try:
        state = await client.get_entity_state(entity_id)
    except HomeAssistantAPIError as e:
        if e.status_code != 404:
            raise
        state = None
    return "not_registry_managed" if state else "not_in_registry"


def raise_unregistered_entity_error(
    target: str, helper_type: str | None, entity_id: str
) -> NoReturn:
    """Refuse an entity that has a state but no entity-registry entry."""
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            (
                f"{entity_id} has a state but no entity registry entry, so it "
                "is not managed through the registry and cannot be removed "
                "here. Nothing was deleted."
            ),
            context={
                "target": target,
                "helper_type": helper_type,
                "entity_id": entity_id,
            },
            suggestions=[
                (
                    "Remove it where it is defined, e.g. delete it from its "
                    "YAML file and reload that integration."
                ),
                (
                    "Entities Home Assistant builds from its core "
                    "configuration, such as zone.home, cannot be removed."
                ),
            ],
        )
    )


async def get_entry_id_for_flow_helper(
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
        helper_type: Flow-helper type (one of Core's helper flows).
        target: Full entity_id, e.g. "sensor.my_meter". Bare IDs not
            supported for flow helpers (caller must provide entity_id).
        warnings: Optional list — appended to on WebSocket failure.

    Returns:
        Tuple of (config_entry_id, reason). On success: (entry_id, "ok").
        On failure: (None, reason) where reason discriminates the cause so
        the caller can produce an accurate error response without an extra
        WebSocket round-trip.

    Raises:
        ToolError: the entity belongs to another integration than
            helper_type, or the registry read failed for a reason other than
            ``not_found``.
        HomeAssistantConnectionError, HomeAssistantAuthError: propagated.
        HomeAssistantAPIError: the state read that tells an unregistered
            entity from a missing one failed with a status other than 404.
    """
    flow_types = await helper_flow_types(client)
    if helper_type not in flow_types:
        return None, "wrong_helper_type"

    if "." not in target:
        return None, "bare_id_not_supported"

    entry, reason = await _read_registry_entry(client, target, warnings)
    if entry is None:
        return None, reason

    # A helper's entities are registered under its own integration, so a
    # foreign platform means the config_entry_id belongs to another integration.
    platform = entry.get("platform")
    if platform != helper_type:
        _raise_platform_mismatch(target, helper_type, platform, flow_types)

    config_entry_id = entry.get("config_entry_id")
    if not config_entry_id:
        return None, "no_config_entry"
    return config_entry_id, "ok"


def _raise_platform_mismatch(
    target: str, helper_type: str, platform: Any, flow_types: frozenset[str]
) -> NoReturn:
    """Refuse an explicit flow-helper type that the entity's registry
    platform contradicts, naming the platform so the caller can tell whether
    omitting helper_type can work."""
    if platform in SIMPLE_HELPER_TYPES or platform in flow_types:
        retry = f"Pass helper_type='{platform}', or omit helper_type."
    else:
        retry = (
            f"Omit helper_type only if '{platform}' is a helper integration; "
            "otherwise use ha_remove_entity() to remove just this entity, or "
            "pass the integration's config entry_id as target."
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            (
                f"{target} is not a {helper_type} helper: it belongs to the "
                f"'{platform}' integration. Nothing was deleted."
            ),
            context={
                "target": target,
                "helper_type": helper_type,
                "platform": platform,
            },
            suggestions=[retry],
        )
    )


def raise_flow_helper_lookup_error(
    reason: FlowLookupReason,
    helper_type: str | None,
    target: str,
    detail: str | None = None,
) -> NoReturn:
    """Raise the structured error for a failed flow-helper entry_id lookup.

    ``reason`` discriminates the failure mode without a second WebSocket
    round-trip. The lookup helper already queried the registry; the response
    told us everything we need.
    """
    entity_id = target if "." in target else f"{helper_type}.{target}"
    if reason == "not_registry_managed":
        raise_unregistered_entity_error(target, helper_type, entity_id)
    if reason == "no_config_entry":
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                (
                    f"Helper {target} is not a storage-based helper "
                    f"(no config entry). {YAML_HELPER_REMOVAL}"
                ),
                context={
                    "target": target,
                    "helper_type": helper_type,
                    "entity_id": entity_id,
                },
                suggestions=[YAML_HELPER_SUGGESTION],
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
                    f"Registry lookup for {entity_id} failed due to a "
                    f"WebSocket error{f': {detail}' if detail else '.'}"
                ),
                context={
                    "target": target,
                    "helper_type": helper_type,
                    "entity_id": entity_id,
                },
            )
        )
    # wrong_helper_type cannot occur here because the dispatcher
    # already checked the storage types and Core's helper flows; the
    # assertion enforces that contract at runtime.
    assert reason != "wrong_helper_type"
    if reason == "not_in_registry":
        # Target is absent from the entity registry and has no
        # state. Surface as ENTITY_NOT_FOUND (entity-shaped target) so the
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
                    "switch_as_x → switch.* / light.*)."
                    if helper_type
                    else "Find the entity_id with ha_search().",
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


async def resolve_helper_entity(client: Any, entity_id: str) -> tuple[str, str]:
    """Return the ``(helper_type, target)`` that removes the helper behind ``entity_id``.

    The registry ``platform`` names the integration that owns the entity: a
    storage helper or one of Core's helper flows (custom helper integrations
    included) is removed as that helper type; an entity of any other
    integration is refused.
    """
    warnings: list[str] = []
    entry, reason = await _read_registry_entry(client, entity_id, warnings)
    if entry is None:
        raise_flow_helper_lookup_error(
            reason, None, entity_id, detail="; ".join(warnings) or None
        )
    platform = entry.get("platform")
    if platform in SIMPLE_HELPER_TYPES or platform in await listed_helper_flows(
        client, str(platform)
    ):
        return platform, entity_id
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            (
                f"{entity_id} belongs to the '{platform}' integration, which "
                "is not a helper. Nothing was deleted."
            ),
            context={"target": entity_id, "platform": platform},
            suggestions=[
                "To remove only this entity, use ha_remove_entity().",
                (
                    "To delete the integration's config entry, pass its "
                    "entry_id as target."
                ),
            ],
        )
    )
    return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable
