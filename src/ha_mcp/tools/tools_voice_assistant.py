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

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)

logger = logging.getLogger(__name__)

# Known voice assistant identifiers in Home Assistant
KNOWN_ASSISTANTS = ["conversation", "cloud.alexa", "cloud.google_assistant"]


class VoiceAssistantTools:
    """Voice assistant exposure query tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _get_entity_exposure(entity_id: str, exposed_entities: dict[str, Any]) -> dict[str, Any]:
        """Build response for a specific entity's exposure settings."""
        entity_settings = exposed_entities.get(entity_id, {})
        is_exposed = any(entity_settings.get(asst) for asst in KNOWN_ASSISTANTS)
        return {
            "success": True,
            "entity_id": entity_id,
            "exposed_to": {
                asst: entity_settings.get(asst, False)
                for asst in KNOWN_ASSISTANTS
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
    def _list_exposures(exposed_entities: dict[str, Any], assistant: str | None) -> dict[str, Any]:
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
                summary
                if not assistant
                else {assistant: summary.get(assistant, 0)}
            ),
            "filters_applied": filters_applied,
        }

    @tool(
        name="ha_get_entity_exposure",
        tags={"Entity Registry"},
        annotations={"idempotentHint": True, "readOnlyHint": True, "title": "Get Entity Exposure"},
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
        """
        try:
            if assistant and assistant not in KNOWN_ASSISTANTS:
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid assistant: {assistant}",
                    context={"assistant": assistant, "valid_assistants": KNOWN_ASSISTANTS},
                    suggestions=[
                        f"Valid assistants are: {', '.join(KNOWN_ASSISTANTS)}",
                        "Check the assistant parameter spelling",
                    ],
                ))

            message: dict[str, Any] = {"type": "homeassistant/expose_entity/list"}

            result = await self._client.send_websocket_message(message)

            if not result.get("success"):
                error = result.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to get exposure settings: {error_msg}",
                    context={"entity_id": entity_id},
                ))

            exposed_entities = result.get("result", {}).get("exposed_entities", {})

            if entity_id is not None:
                return self._get_entity_exposure(entity_id, exposed_entities)

            return self._list_exposures(exposed_entities, assistant)

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting entity exposure: {e}")
            exception_to_structured_error(e, context={"entity_id": entity_id})


def register_voice_assistant_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register voice assistant exposure query tools."""
    register_tool_methods(mcp, VoiceAssistantTools(client))
