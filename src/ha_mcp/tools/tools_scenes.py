"""
Configuration management tools for Home Assistant scenes.

This module provides tools for listing, retrieving, creating, deleting,
and activating Home Assistant scenes via the REST API and service calls.

Home Assistant scenes are managed differently from automations/scripts:
- Listing: Query states filtered by scene.* domain
- Create: Use scene.create service
- Delete: Use scene.delete service (only for dynamically created scenes)
- Activate: Use scene.turn_on service
"""

import logging
from typing import Annotated, Any, cast

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)


def register_scene_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant scene management tools."""

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_list_scenes() -> dict[str, Any]:
        """
        List all configured scenes in Home Assistant.

        Returns a list of all scenes with their entity_ids, friendly names, and states.

        EXAMPLES:
        - List all scenes: ha_list_scenes()

        Use ha_get_scene() to get detailed information about a specific scene.
        """
        try:
            # Get all states and filter for scene entities
            all_states = await client.get_states()

            scenes = []
            for state in all_states:
                entity_id = state.get("entity_id", "")
                if entity_id.startswith("scene."):
                    attributes = state.get("attributes", {})
                    scenes.append(
                        {
                            "entity_id": entity_id,
                            "friendly_name": attributes.get("friendly_name", entity_id),
                            "icon": attributes.get("icon"),
                            "entity_count": len(attributes.get("entity_id", [])),
                            "state": state.get("state"),
                        }
                    )

            return {
                "success": True,
                "count": len(scenes),
                "scenes": scenes,
            }
        except Exception as e:
            logger.error(f"Error listing scenes: {e}")
            return {
                "success": False,
                "error": str(e),
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify API access is available",
                ],
            }

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_get_scene(
        entity_id: Annotated[
            str,
            Field(
                description="Scene entity_id (e.g., 'scene.movie_night'). Use ha_list_scenes() to find entity_ids."
            ),
        ],
    ) -> dict[str, Any]:
        """
        Get detailed information about a specific scene.

        Returns the scene state and all available attributes including
        the list of entities the scene controls.

        EXAMPLES:
        - Get scene details: ha_get_scene("scene.movie_night")
        - Get scene details: ha_get_scene("scene.bedtime")

        Note: Use ha_list_scenes() first to find available scene entity_ids.
        """
        try:
            # Ensure entity_id has the scene. prefix
            if not entity_id.startswith("scene."):
                entity_id = f"scene.{entity_id}"

            # Get the scene state
            state = await client.get_entity_state(entity_id)

            if not state:
                return {
                    "success": False,
                    "error": f"Scene '{entity_id}' not found",
                    "reason": "not_found",
                    "suggestions": [
                        "Use ha_list_scenes() to find valid scene entity_ids",
                        "Check entity_id spelling",
                    ],
                }

            attributes = state.get("attributes", {})

            return {
                "success": True,
                "entity_id": entity_id,
                "friendly_name": attributes.get("friendly_name", entity_id),
                "icon": attributes.get("icon"),
                "state": state.get("state"),
                "last_changed": state.get("last_changed"),
                "last_updated": state.get("last_updated"),
                "entity_ids": attributes.get("entity_id", []),
                "attributes": attributes,
            }
        except Exception as e:
            error_str = str(e).lower()
            if "404" in error_str or "not found" in error_str:
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": f"Scene '{entity_id}' not found",
                    "reason": "not_found",
                    "suggestions": [
                        "Use ha_list_scenes() to find valid scene entity_ids",
                    ],
                }
            logger.error(f"Error getting scene {entity_id}: {e}")
            return {
                "success": False,
                "entity_id": entity_id,
                "error": str(e),
                "suggestions": [
                    "Check Home Assistant connection",
                    "Use ha_list_scenes() to find valid scene entity_ids",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_create_scene(
        scene_id: Annotated[
            str,
            Field(
                description="Scene identifier without 'scene.' prefix (e.g., 'movie_night')"
            ),
        ],
        entities: Annotated[
            str | dict[str, dict[str, Any]],
            Field(
                description="Entity states dictionary: {entity_id: {state: 'on', attributes...}}. Example: {'light.living_room': {'state': 'on', 'brightness': 128}}"
            ),
        ],
        snapshot_entities: Annotated[
            list[str] | str | None,
            Field(
                description="Optional list of entity_ids to snapshot current state from instead of specifying state explicitly",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create a new scene dynamically using the scene.create service.

        Creates a scene that captures specific states for multiple entities.
        When activated, the scene will restore all entities to these states.

        IMPORTANT:
        - Scenes created with this method can be deleted with ha_delete_scene()
        - The scene_id should not include the 'scene.' prefix
        - Either 'entities' or 'snapshot_entities' should be provided

        EXAMPLES:

        Create scene with explicit entity states:
        ha_create_scene(
            scene_id="movie_night",
            entities={
                "light.living_room": {"state": "on", "brightness": 50},
                "light.ceiling": {"state": "off"},
                "media_player.tv": {"state": "on"}
            }
        )

        Create scene by snapshotting current states:
        ha_create_scene(
            scene_id="current_mood",
            entities={},
            snapshot_entities=["light.living_room", "light.bedroom"]
        )

        After creating a scene, use ha_activate_scene() to activate it.
        """
        try:
            # Parse entities if provided as string
            try:
                parsed_entities = parse_json_param(entities, "entities")
            except ValueError as e:
                return {
                    "success": False,
                    "error": f"Invalid entities parameter: {e}",
                    "provided_entities_type": type(entities).__name__,
                }

            if parsed_entities is None:
                parsed_entities = {}

            if not isinstance(parsed_entities, dict):
                return {
                    "success": False,
                    "error": "Entities parameter must be a JSON object mapping entity_ids to states",
                    "provided_type": type(parsed_entities).__name__,
                }

            entities_dict = cast(dict[str, dict[str, Any]], parsed_entities)

            # Parse snapshot_entities if provided as string
            snapshot_list = None
            if snapshot_entities is not None:
                if isinstance(snapshot_entities, str):
                    try:
                        parsed_snapshot = parse_json_param(
                            snapshot_entities, "snapshot_entities"
                        )
                        if isinstance(parsed_snapshot, list):
                            snapshot_list = parsed_snapshot
                        else:
                            # Single entity as string
                            snapshot_list = [snapshot_entities]
                    except ValueError:
                        # Treat as single entity_id
                        snapshot_list = [snapshot_entities]
                elif isinstance(snapshot_entities, list):
                    snapshot_list = snapshot_entities

            # Validate that at least one method is provided
            if not entities_dict and not snapshot_list:
                return {
                    "success": False,
                    "error": "Either 'entities' or 'snapshot_entities' must be provided",
                    "suggestions": [
                        "Provide entity states: {'light.living_room': {'state': 'on'}}",
                        "Or provide entities to snapshot: ['light.living_room']",
                    ],
                }

            # Build service data
            service_data: dict[str, Any] = {"scene_id": scene_id}

            if entities_dict:
                service_data["entities"] = entities_dict

            if snapshot_list:
                service_data["snapshot_entities"] = snapshot_list

            # Call scene.create service
            await client.call_service("scene", "create", service_data)

            entity_id = f"scene.{scene_id}"

            return {
                "success": True,
                "operation": "created",
                "entity_id": entity_id,
                "scene_id": scene_id,
                "entity_count": (
                    len(entities_dict) if entities_dict else len(snapshot_list or [])
                ),
                "note": f"Scene created. Use ha_activate_scene('{entity_id}') to activate.",
            }
        except Exception as e:
            logger.error(f"Error creating scene: {e}")
            return {
                "success": False,
                "error": str(e),
                "suggestions": [
                    "Check entity IDs exist",
                    "Verify entity states are valid",
                    "Use ha_search_entities() to find valid entity_ids",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_delete_scene(
        entity_id: Annotated[
            str,
            Field(
                description="Scene entity_id to delete (e.g., 'scene.movie_night'). Must be a dynamically created scene."
            ),
        ],
    ) -> dict[str, Any]:
        """
        Delete a dynamically created scene from Home Assistant.

        IMPORTANT: Only scenes created with scene.create (or ha_create_scene) can be deleted.
        Scenes defined in YAML configuration cannot be deleted via this method.

        EXAMPLES:
        - Delete scene: ha_delete_scene("scene.movie_night")
        - Delete scene: ha_delete_scene("scene.temporary_mood")

        **WARNING:** This permanently removes the scene.
        """
        try:
            # Ensure entity_id has the scene. prefix
            if not entity_id.startswith("scene."):
                entity_id = f"scene.{entity_id}"

            # Call scene.delete service
            service_data = {"entity_id": entity_id}
            await client.call_service("scene", "delete", service_data)

            return {
                "success": True,
                "operation": "deleted",
                "entity_id": entity_id,
            }
        except Exception as e:
            error_str = str(e).lower()

            # Check for specific error conditions
            if "not found" in error_str or "404" in error_str:
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": f"Scene '{entity_id}' not found",
                    "reason": "not_found",
                }

            if "not created" in error_str or "cannot be deleted" in error_str:
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": "Only dynamically created scenes can be deleted",
                    "reason": "not_deletable",
                    "suggestions": [
                        "This scene was defined in YAML configuration",
                        "Remove it from configuration.yaml instead",
                    ],
                }

            logger.error(f"Error deleting scene {entity_id}: {e}")
            return {
                "success": False,
                "entity_id": entity_id,
                "error": str(e),
                "suggestions": [
                    "Use ha_list_scenes() to verify scene exists",
                    "Only dynamically created scenes can be deleted",
                    "Check Home Assistant connection",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_activate_scene(
        entity_id: Annotated[
            str,
            Field(description="Scene entity_id (e.g., 'scene.movie_night')"),
        ],
        transition: Annotated[
            float | None,
            Field(
                description="Transition time in seconds for supported entities (lights, etc.)",
                default=None,
                ge=0,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Activate a scene, restoring all entities to their saved states.

        This calls the scene.turn_on service to activate the scene.
        Activating a scene immediately sets all entities to their configured states.

        EXAMPLES:

        Activate scene:
        ha_activate_scene("scene.movie_night")

        Activate with transition (lights fade over 2 seconds):
        ha_activate_scene("scene.bedtime", transition=2.0)

        Note: The transition parameter only affects entities that support transitions (like lights).
        """
        try:
            # Ensure entity_id has the scene. prefix
            if not entity_id.startswith("scene."):
                entity_id = f"scene.{entity_id}"

            # Build service data
            service_data: dict[str, Any] = {"entity_id": entity_id}

            if transition is not None:
                service_data["transition"] = transition

            # Call scene.turn_on service
            result = await client.call_service("scene", "turn_on", service_data)

            return {
                "success": True,
                "operation": "activated",
                "entity_id": entity_id,
                "transition": transition,
                "affected_entities": len(result) if isinstance(result, list) else 0,
            }
        except Exception as e:
            error_str = str(e).lower()

            if "not found" in error_str or "404" in error_str:
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": f"Scene '{entity_id}' not found",
                    "reason": "not_found",
                    "suggestions": [
                        "Use ha_list_scenes() to find valid scenes",
                        "Check scene entity_id format: scene.your_scene_name",
                    ],
                }

            logger.error(f"Error activating scene {entity_id}: {e}")
            return {
                "success": False,
                "entity_id": entity_id,
                "error": str(e),
                "suggestions": [
                    "Use ha_list_scenes() to find valid scenes",
                    "Check scene entity_id format: scene.your_scene_name",
                    "Verify scene exists in Home Assistant",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_apply_scene(
        entities: Annotated[
            str | dict[str, dict[str, Any]],
            Field(
                description="Entity states dictionary: {entity_id: {state: 'on', attributes...}}. Example: {'light.living_room': {'state': 'on', 'brightness': 128}}"
            ),
        ],
        transition: Annotated[
            float | None,
            Field(
                description="Transition time in seconds for supported entities",
                default=None,
                ge=0,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Apply entity states immediately without creating a persistent scene.

        This is useful for temporary state changes that don't need to be saved.
        Unlike ha_create_scene, this doesn't create a scene entity.

        EXAMPLES:

        Apply temporary movie mode:
        ha_apply_scene(
            entities={
                "light.living_room": {"state": "on", "brightness": 50},
                "light.ceiling": {"state": "off"}
            },
            transition=2.0
        )

        Quick dim all lights:
        ha_apply_scene(
            entities={
                "light.living_room": {"state": "on", "brightness": 25},
                "light.bedroom": {"state": "on", "brightness": 25}
            }
        )
        """
        try:
            # Parse entities if provided as string
            try:
                parsed_entities = parse_json_param(entities, "entities")
            except ValueError as e:
                return {
                    "success": False,
                    "error": f"Invalid entities parameter: {e}",
                    "provided_entities_type": type(entities).__name__,
                }

            if parsed_entities is None or not isinstance(parsed_entities, dict):
                return {
                    "success": False,
                    "error": "Entities parameter must be a JSON object mapping entity_ids to states",
                    "provided_type": type(parsed_entities).__name__,
                }

            entities_dict = cast(dict[str, dict[str, Any]], parsed_entities)

            if not entities_dict:
                return {
                    "success": False,
                    "error": "Entities dictionary cannot be empty",
                    "suggestions": [
                        "Provide at least one entity with its desired state",
                        "Example: {'light.living_room': {'state': 'on', 'brightness': 128}}",
                    ],
                }

            # Build service data
            service_data: dict[str, Any] = {"entities": entities_dict}

            if transition is not None:
                service_data["transition"] = transition

            # Call scene.apply service
            await client.call_service("scene", "apply", service_data)

            return {
                "success": True,
                "operation": "applied",
                "entity_count": len(entities_dict),
                "transition": transition,
            }
        except Exception as e:
            logger.error(f"Error applying scene: {e}")
            return {
                "success": False,
                "error": str(e),
                "suggestions": [
                    "Check entity IDs exist",
                    "Verify entity states are valid",
                    "Use ha_search_entities() to find valid entity_ids",
                ],
            }
