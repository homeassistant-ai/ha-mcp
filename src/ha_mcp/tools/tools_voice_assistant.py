"""
Voice Assistant Exposure Management Tools for Home Assistant.

This module provides tools for:
- Managing entity exposure to voice assistants (Alexa, Google Home, Assist)
- Listing which entities are exposed to which assistants
- Configuring auto-exposure settings for new entities

The exposure system is separate from the entity registry and controls
which entities are accessible to voice assistants like Alexa, Google Assistant,
and the built-in Assist pipeline.

Known assistant identifiers:
- "conversation" - Home Assistant Assist (local voice control)
- "cloud.alexa" - Alexa via Nabu Casa cloud
- "cloud.google_assistant" - Google Assistant via Nabu Casa cloud
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import log_tool_usage
from .util_helpers import coerce_bool_param, parse_string_list_param

logger = logging.getLogger(__name__)

# Known voice assistant identifiers in Home Assistant
KNOWN_ASSISTANTS = ["conversation", "cloud.alexa", "cloud.google_assistant"]


def register_voice_assistant_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register voice assistant exposure management tools."""

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "title": "Expose Entity to Voice Assistant",
        }
    )
    @log_tool_usage
    async def ha_expose_entity(
        entity_ids: Annotated[
            str | list[str],
            Field(
                description="Entity ID(s) to expose/hide. Can be a single entity ID string or a list."
            ),
        ],
        assistants: Annotated[
            str | list[str],
            Field(
                description=(
                    "Voice assistant(s) to modify. Options: "
                    "'conversation' (Assist), 'cloud.alexa', 'cloud.google_assistant'. "
                    "Can be a single assistant or list."
                )
            ),
        ],
        should_expose: Annotated[
            bool | str,
            Field(
                description="True to expose entities to the assistants, False to hide them"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Expose or hide entities from voice assistants (Alexa, Google Home, Assist).

        **DEPRECATED**: For single-entity exposure changes, prefer
        ha_set_entity(entity_id, expose_to={"conversation": true, "cloud.alexa": false})
        which supports exposure alongside other entity properties in a single call.
        This tool remains useful for bulk operations across multiple entities.

        This controls which entities are accessible via voice commands through
        Home Assistant's voice assistant integrations.

        ASSISTANTS:
        - "conversation" - Home Assistant Assist (built-in local voice)
        - "cloud.alexa" - Amazon Alexa (requires Nabu Casa subscription)
        - "cloud.google_assistant" - Google Assistant (requires Nabu Casa subscription)

        EXAMPLES:
        - Expose light to Alexa: ha_expose_entity("light.living_room", "cloud.alexa", True)
        - Hide from all assistants: ha_expose_entity("switch.secret", ["conversation", "cloud.alexa", "cloud.google_assistant"], False)
        - Expose multiple entities: ha_expose_entity(["light.bedroom", "light.kitchen"], "conversation", True)

        NOTE: Some entities cannot be exposed to cloud assistants (Alexa/Google) for security reasons,
        including sensitive domains like alarm_control_panel, lock, etc.
        """
        try:
            # Parse entity_ids - handle single string or list/JSON array
            if isinstance(entity_ids, str):
                # Try to parse as JSON first, otherwise treat as single entity_id
                try:
                    parsed_entity_ids = parse_string_list_param(
                        entity_ids, "entity_ids"
                    )
                except ValueError:
                    # Not valid JSON, treat as single entity_id
                    parsed_entity_ids = [entity_ids]
            elif isinstance(entity_ids, list):
                parsed_entity_ids = entity_ids
            else:
                parsed_entity_ids = None

            if not parsed_entity_ids:
                return {
                    "success": False,
                    "error": "entity_ids is required and cannot be empty",
                }

            # Parse assistants - handle single string or list/JSON array
            if isinstance(assistants, str):
                # Try to parse as JSON first, otherwise treat as single assistant
                try:
                    parsed_assistants = parse_string_list_param(
                        assistants, "assistants"
                    )
                except ValueError:
                    # Not valid JSON, treat as single assistant
                    parsed_assistants = [assistants]
            elif isinstance(assistants, list):
                parsed_assistants = assistants
            else:
                parsed_assistants = None

            if not parsed_assistants:
                return {
                    "success": False,
                    "error": "assistants is required and cannot be empty",
                }

            # Validate assistants
            invalid_assistants = [
                a for a in parsed_assistants if a not in KNOWN_ASSISTANTS
            ]
            if invalid_assistants:
                return {
                    "success": False,
                    "error": f"Invalid assistant(s): {invalid_assistants}",
                    "valid_assistants": KNOWN_ASSISTANTS,
                }

            # Parse should_expose
            expose = coerce_bool_param(should_expose, "should_expose")
            if expose is None:
                return {
                    "success": False,
                    "error": "should_expose is required (true/false)",
                }

            # Build WebSocket message
            message: dict[str, Any] = {
                "type": "homeassistant/expose_entity",
                "assistants": parsed_assistants,
                "entity_ids": parsed_entity_ids,
                "should_expose": expose,
            }

            action = "Exposing" if expose else "Hiding"
            logger.info(
                f"{action} {len(parsed_entity_ids)} entity(ies) "
                f"{'to' if expose else 'from'} {parsed_assistants}"
            )

            result = await client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    "entity_ids": parsed_entity_ids,
                    "assistants": parsed_assistants,
                    "exposed": expose,
                    "message": (
                        f"Successfully {'exposed' if expose else 'hidden'} "
                        f"{len(parsed_entity_ids)} entity(ies) "
                        f"{'to' if expose else 'from'} {len(parsed_assistants)} assistant(s)"
                    ),
                }
            else:
                error = result.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                return {
                    "success": False,
                    "error": f"Failed to update exposure: {error_msg}",
                    "entity_ids": parsed_entity_ids,
                    "assistants": parsed_assistants,
                }

        except ValueError as e:
            return create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                str(e),
            )
        except Exception as e:
            logger.error(f"Error updating entity exposure: {e}")
            return {
                "success": False,
                "error": f"Failed to update entity exposure: {str(e)}",
            }

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Entity Exposure",
        }
    )
    @log_tool_usage
    async def ha_get_entity_exposure(
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
            # Validate assistant filter if provided
            if assistant and assistant not in KNOWN_ASSISTANTS:
                return {
                    "success": False,
                    "error": f"Invalid assistant: {assistant}",
                    "valid_assistants": KNOWN_ASSISTANTS,
                }

            message: dict[str, Any] = {"type": "homeassistant/expose_entity/list"}

            result = await client.send_websocket_message(message)

            if not result.get("success"):
                error = result.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                return {
                    "success": False,
                    "error": f"Failed to get exposure settings: {error_msg}",
                    "entity_id": entity_id,
                }

            exposed_entities = result.get("result", {}).get("exposed_entities", {})

            # If entity_id provided, return specific entity exposure
            if entity_id is not None:
                entity_settings = exposed_entities.get(entity_id, {})

                # Check if entity is exposed to any assistant
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

            # List mode - return all exposed entities with optional assistant filter
            filtered = exposed_entities
            if assistant:
                # Filter to only show entities exposed to this assistant
                filtered = {
                    eid: settings
                    for eid, settings in filtered.items()
                    if settings.get(assistant)
                }

            # Build summary
            summary = {
                "conversation": 0,
                "cloud.alexa": 0,
                "cloud.google_assistant": 0,
            }
            for settings in filtered.values():
                for asst in KNOWN_ASSISTANTS:
                    if settings.get(asst):
                        summary[asst] += 1

            # Build filters_applied dict
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

        except Exception as e:
            logger.error(f"Error getting entity exposure: {e}")
            return {
                "success": False,
                "error": f"Failed to get entity exposure: {str(e)}",
                "entity_id": entity_id,
            }
