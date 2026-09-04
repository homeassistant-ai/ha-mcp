"""
Voice Assistant Exposure Query Tools for Home Assistant.

This module provides tools for querying entity exposure to voice assistants
(Alexa, Google Home, Assist). To modify exposure, use ha_set_entity(expose_to=...).

Known assistant identifiers:
- "conversation" - Home Assistant Assist (local voice control)
- "cloud.alexa" - Alexa via Nabu Casa cloud
- "cloud.google_assistant" - Google Assistant via Nabu Casa cloud
"""

import logging
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)

logger = logging.getLogger(__name__)

# Known voice assistant identifiers in Home Assistant
KNOWN_ASSISTANTS = ["conversation", "cloud.alexa", "cloud.google_assistant"]

# The ha_mcp_tools/exposure WS command: the legacy expose_entity/list map PLUS a
# sibling entity_info enrichment (names/areas). Named once so the routing helper
# and its tests agree on the wire string.
WS_EXPOSURE = "ha_mcp_tools/exposure"

# Additive keys the component's exposure enrichment (entity_info) contributes to a
# single-entity response. friendly_name / state are live-state fields (omitted by
# the component when the entity has no state); domain / area / floor / labels are
# registry-derived and always present. Merged strictly on top of the byte-identical
# legacy keys (exposed_to / is_exposed_anywhere / has_custom_settings / note).
_EXPOSURE_ENRICHMENT_KEYS = (
    "friendly_name",
    "domain",
    "area",
    "floor",
    "labels",
    "state",
)


class VoiceAssistantTools:
    """Voice assistant exposure query tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _fetch_exposure_via_component(
        self, entity_id: str | None
    ) -> dict[str, Any] | None:
        """One ``ha_mcp_tools/exposure`` read; ``None`` ⇒ run the legacy path.

        Returns the component payload — ``{exposed_entities: {id: {assistant:
        True}}, entity_info: {id: {...}}}`` — where ``exposed_entities`` is
        byte-identical to the legacy ``homeassistant/expose_entity/list`` map so
        the existing ``_get_entity_exposure`` / ``_list_exposures`` shapers consume
        it unchanged. ``None`` on capability miss, downgrade (``unknown_command`` →
        invalidate the cached caps), command error/timeout (logged), or a
        connection-establishment failure (logged) — the caller falls back to the
        legacy WS list. A ``HomeAssistantConnectionError`` — a pooled-WS drop,
        or a failed (re)connect — is caught here and mapped to ``None``: the
        legacy ``homeassistant/expose_entity/list`` read rides the
        ``send_websocket_message`` bridge, which answers a component-side fault
        with ``{"success": False}`` - so a fault here falls back rather than
        escapes. It is the SAME pooled connection, so a genuinely dead
        transport raises there too (#1947) instead of degrading. Same caps-gate discipline as
        ``component_devices.fetch_device_via_component``.
        """
        caps = await get_component_caps(self._client)
        if not (
            component_supports(caps, "exposure")
            and component_supports(caps, DEVICE_REGISTRY_CHILD_SEMANTICS)
        ):
            return None
        kwargs: dict[str, Any] = {}
        if entity_id is not None:
            kwargs["entity_id"] = entity_id
        try:
            ws = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
            raw = await ws.send_command(WS_EXPOSURE, **kwargs)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            else:
                logger.warning("%s failed; fell back to legacy: %r", WS_EXPOSURE, exc)
            return None
        except Exception as exc:
            # HomeAssistantConnectionError / plain establish Exception → legacy (the
            # legacy expose_entity/list read rides the bridge).
            logger.warning(
                "%s connection error; falling back to legacy: %r", WS_EXPOSURE, exc
            )
            return None
        result = raw.get("result")
        if not isinstance(result, dict) or "exposed_entities" not in result:
            return None
        return result

    @staticmethod
    def _merge_exposure_enrichment(
        response: dict[str, Any], info: dict[str, Any] | None
    ) -> None:
        """Additively merge one entity's ``entity_info`` onto its exposure response.

        Adds friendly_name / domain / area / floor / labels (and state, when the
        component included it) on top of the byte-identical legacy keys. A ``None``
        / empty ``info`` is a no-op (capability miss ⇒ fields simply absent).
        """
        if not info:
            return
        for key in _EXPOSURE_ENRICHMENT_KEYS:
            if key in info:
                response[key] = info[key]

    @staticmethod
    def _get_entity_exposure(
        entity_id: str, exposed_entities: dict[str, Any]
    ) -> dict[str, Any]:
        """Build response for a specific entity's exposure settings."""
        entity_settings = exposed_entities.get(entity_id, {})
        is_exposed = any(entity_settings.get(asst) for asst in KNOWN_ASSISTANTS)
        return {
            "success": True,
            "entity_id": entity_id,
            "exposed_to": {
                asst: entity_settings.get(asst, False) for asst in KNOWN_ASSISTANTS
            },
            "is_exposed_anywhere": is_exposed,
            "has_custom_settings": entity_id in exposed_entities,
            "note": (
                "If has_custom_settings is False, the entity uses default exposure settings"
                if entity_id not in exposed_entities
                else None
            ),
        }

    @staticmethod
    def _list_exposures(
        exposed_entities: dict[str, Any], assistant: str | None
    ) -> dict[str, Any]:
        """Build response listing all exposed entities with optional filter."""
        filtered = exposed_entities
        if assistant:
            filtered = {
                eid: settings
                for eid, settings in filtered.items()
                if settings.get(assistant)
            }

        summary: dict[str, int] = dict.fromkeys(KNOWN_ASSISTANTS, 0)
        for settings in filtered.values():
            for asst in KNOWN_ASSISTANTS:
                if settings.get(asst):
                    summary[asst] += 1

        filters_applied: dict[str, Any] = {}
        if assistant:
            filters_applied["assistant"] = assistant

        return {
            "success": True,
            "exposed_entities": filtered,
            "count": len(filtered),
            "total_entities_with_settings": len(exposed_entities),
            "summary": (
                summary if not assistant else {assistant: summary.get(assistant, 0)}
            ),
            "filters_applied": filters_applied,
        }

    @tool(
        name="ha_get_entity_exposure",
        tags={"Entity Registry"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Entity Exposure",
        },
    )
    @log_tool_usage
    async def ha_get_entity_exposure(
        self,
        entity_id: Annotated[
            str | None,
            Field(
                description="Entity ID to check exposure settings for. "
                "If omitted, lists all entities with exposure settings.",
                default=None,
            ),
        ] = None,
        assistant: Annotated[
            str | None,
            Field(
                description=(
                    "Filter by assistant: 'conversation', 'cloud.alexa', or "
                    "'cloud.google_assistant'. If not specified, returns all."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get entity exposure settings - list all or get settings for a specific entity.

        Without an entity_id: Lists all entities and their exposure status to
        voice assistants (Alexa, Google Assistant, Assist).

        With an entity_id: Returns which voice assistants the specific entity
        is exposed to.

        EXAMPLES:
        - List all exposures: ha_get_entity_exposure()
        - Filter by assistant: ha_get_entity_exposure(assistant="cloud.alexa")
        - Get specific entity: ha_get_entity_exposure(entity_id="light.living_room")

        RETURNS (when listing):
        - exposed_entities: Dict mapping entity_ids to their exposure status
        - summary: Count of entities exposed to each assistant

        RETURNS (when getting specific entity):
        - exposed_to: Dict of assistant -> True/False for each assistant
        - is_exposed_anywhere: True if exposed to at least one assistant

        When the ha_mcp_tools component advertises the exposure capability, each
        record is additively enriched with the entity's name/area so no second
        ha_search is needed to identify it: friendly_name, domain, area, floor,
        and labels (plus state for entities that have one) on a single-entity
        lookup, and a parallel entity_info map keyed by entity_id when listing.
        These fields are absent when the component is unavailable.
        """
        try:
            if assistant and assistant not in KNOWN_ASSISTANTS:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid assistant: {assistant}",
                        context={
                            "assistant": assistant,
                            "valid_assistants": KNOWN_ASSISTANTS,
                        },
                        suggestions=[
                            f"Valid assistants are: {', '.join(KNOWN_ASSISTANTS)}",
                            "Check the assistant parameter spelling",
                        ],
                    )
                )

            # Prefer the component's in-process exposure read when advertised: it
            # returns the byte-identical expose_entity/list map PLUS the additive
            # entity_info enrichment (names/areas), so a caller no longer needs a
            # second ha_search to name an exposed entity. Falls back to the legacy
            # WS list on any miss/error (taxonomy in _fetch_exposure_via_component).
            component = await self._fetch_exposure_via_component(entity_id)
            if component is not None:
                exposed_entities = component.get("exposed_entities") or {}
                entity_info = component.get("entity_info") or {}
                if entity_id is not None:
                    response = self._get_entity_exposure(entity_id, exposed_entities)
                    self._merge_exposure_enrichment(
                        response, entity_info.get(entity_id)
                    )
                    return response
                response = self._list_exposures(exposed_entities, assistant)
                # Enrich only the ids surviving the assistant filter.
                response["entity_info"] = {
                    eid: entity_info[eid]
                    for eid in response["exposed_entities"]
                    if eid in entity_info
                }
                return response

            message: dict[str, Any] = {"type": "homeassistant/expose_entity/list"}

            result = await self._client.send_websocket_message(message)

            if not result.get("success"):
                error = result.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to get exposure settings: {error_msg}",
                        context={"entity_id": entity_id},
                    )
                )

            exposed_entities = result.get("result", {}).get("exposed_entities", {})

            if entity_id is not None:
                return self._get_entity_exposure(entity_id, exposed_entities)

            return self._list_exposures(exposed_entities, assistant)

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting entity exposure: {e}")
            exception_to_structured_error(e, context={"entity_id": entity_id})
            return None  # unreachable: exception_to_structured_error always raises


def register_voice_assistant_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register voice assistant exposure query tools."""
    register_tool_methods(mcp, VoiceAssistantTools(client))
