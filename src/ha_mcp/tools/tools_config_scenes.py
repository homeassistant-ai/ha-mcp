"""
Configuration management tools for Home Assistant scenes.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant scene configurations. Mirrors the scripts/automations
patterns; the key shape difference is that scene ``entities`` is a dict
keyed by entity_id (not a list).
"""

import logging
from typing import Annotated, Any, cast

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from ..utils.python_sandbox import (
    PythonSandboxError,
    get_security_documentation,
    safe_execute,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .reference_validator import validate_config_references
from .util_helpers import (
    apply_entity_category,
    coerce_bool_param,
    fetch_entity_category,
    merge_validation_meta,
    parse_json_param,
    wait_for_entity_registered,
    wait_for_entity_removed,
)

logger = logging.getLogger(__name__)


class ConfigSceneTools:
    """Scene configuration management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _resolve_scene_entity_id(self, scene_id: str) -> str:
        """Resolve a scene's actual entity_id via the entity registry.

        Unlike scripts (where ``entity_id == 'script.<storage_key>'``), HA
        derives a scene's entity_id from the ``name`` field. So a scene
        upserted with ``scene_id='night_light_led_desk_strip'`` and
        ``name='LED Desk Strip Night Light'`` lands at entity_id
        ``scene.led_desk_strip_night_light`` while the storage key (and the
        unique_id) stays ``night_light_led_desk_strip``.

        Naively assuming ``f"scene.{scene_id}"`` for the wait/category
        callsites surfaces a false-negative warning ("not yet queryable")
        whenever a name is supplied. This helper finds the actual entity_id
        by matching the scene_id to ``unique_id`` in the entity registry,
        and falls back to the naive form if the registry lookup is
        inconclusive — keeping the previous behaviour intact for the case
        where the slugs happen to match.
        """
        try:
            result = await self._client.send_websocket_message(
                {"type": "config/entity_registry/list"}
            )
            if result.get("success") is not False:
                for entry in result.get("result") or []:
                    entity_id = entry.get("entity_id") or ""
                    if (
                        entry.get("unique_id") == scene_id
                        and entity_id.startswith("scene.")
                    ):
                        return entity_id
        except Exception:
            logger.debug(
                f"Entity registry resolve failed for scene_id={scene_id}, "
                f"falling back to scene.{scene_id}",
                exc_info=True,
            )
        return f"scene.{scene_id}"

    @tool(
        name="ha_config_get_scene",
        tags={"Scenes"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Scene Config",
        },
    )
    @log_tool_usage
    async def ha_config_get_scene(
        self,
        scene_id: Annotated[
            str, Field(description="Scene identifier (e.g., 'movie_night')")
        ],
    ) -> dict[str, Any]:
        """
        Retrieve Home Assistant scene configuration.

        Returns the complete configuration for a scene, including the ``entities``
        dict and other settings (``name``, ``icon``, ``id``).

        The returned ``config_hash`` is stable across consecutive reads of an
        unchanged config — ``compute_config_hash`` documents the underlying contract.

        EXAMPLES:
        - Get scene: ha_config_get_scene("movie_night")
        - Get scene: ha_config_get_scene("bedroom_dim")

        For detailed scene configuration help, use ha_get_skill_home_assistant_best_practices.
        """
        try:
            config_result = await self._client.get_scene_config(scene_id)
            actual_config = config_result.get("config", config_result)
            config_hash_value = compute_config_hash(actual_config)

            # Resolve real entity_id via registry — see _resolve_scene_entity_id
            # for the reasoning. Category fetch on the wrong entity_id is a
            # silent no-op, masking real category assignments.
            entity_id = await self._resolve_scene_entity_id(scene_id)
            cat_id = await fetch_entity_category(self._client, entity_id, "scene")
            if cat_id:
                config_result["category"] = cat_id

            return {
                "success": True,
                "action": "get",
                "scene_id": scene_id,
                "config": config_result,
                "config_hash": config_hash_value,
            }
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"scene_id": scene_id},
                suggestions=[
                    "Verify scene_id exists using ha_search_entities(domain_filter='scene')",
                    "Check Home Assistant connection",
                    "Use ha_get_skill_home_assistant_best_practices for help",
                ],
            )

    async def _get_scene_config_internal(
        self, scene_id: str
    ) -> tuple[dict[str, Any], str]:
        """Fetch scene config without logging or category injection.

        Returns ``(actual_config, config_hash)`` where ``actual_config`` is the
        inner scene body (not the REST wrapper). Used by ``_fetch_and_verify_hash``.
        """
        config_result = await self._client.get_scene_config(scene_id)
        actual_config = config_result.get("config", config_result)
        config_hash_value = compute_config_hash(actual_config)
        return actual_config, config_hash_value

    async def _fetch_and_verify_hash(
        self, scene_id: str, config_hash: str, action: str
    ) -> dict[str, Any]:
        """Fetch current scene config and verify config_hash for optimistic locking.

        Returns the actual scene config dict (inner body).
        Raises ToolError if the hash does not match (conflict).
        """
        actual_config, current_hash = await self._get_scene_config_internal(scene_id)
        if current_hash != config_hash:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Scene modified since last read (conflict)",
                    suggestions=[
                        "Call ha_config_get_scene() again",
                        "Use the fresh config_hash from that response",
                    ],
                    context={"action": action, "scene_id": scene_id},
                )
            )
        return actual_config

    @staticmethod
    def _validate_scene_config(
        config: str | dict[str, Any],
        scene_id: str,
        category: str | None,
    ) -> tuple[dict[str, Any], str | None]:
        """Parse and validate scene config, returning ``(config_dict, effective_category)``.

        Parses JSON string config, validates it is a dict, checks for the
        required ``entities`` field, and extracts category.
        """
        try:
            parsed_config = parse_json_param(config, "config")
        except ValueError as e:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_JSON,
                f"Invalid config parameter: {e}",
                context={"scene_id": scene_id, "provided_config_type": type(config).__name__},
            ))

        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Config parameter must be a JSON object",
                context={"scene_id": scene_id, "provided_type": type(parsed_config).__name__},
            ))

        config_dict = cast(dict[str, Any], parsed_config)

        # Extract category before sending to HA REST API (rejects unknown keys).
        # Parameter takes precedence over config dict value.
        config_category = config_dict.pop("category", None)
        effective_category = category if category is not None else config_category

        # Required field check. ``entities`` must be a dict keyed by entity_id.
        if "entities" not in config_dict:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "config must include an 'entities' field (a dict keyed by entity_id)",
                context={"scene_id": scene_id, "required_fields": ["entities"]},
            ))

        if not isinstance(config_dict["entities"], dict):
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Scene 'entities' must be a dict keyed by entity_id, not a list",
                suggestions=[
                    "Scene shape: {'entities': {'light.kitchen': {'state': 'on'}}}",
                    "Automations use a list of actions; scenes do not",
                ],
                context={
                    "scene_id": scene_id,
                    "provided_type": type(config_dict["entities"]).__name__,
                },
            ))

        return config_dict, effective_category

    @tool(
        name="ha_config_set_scene",
        tags={"Scenes"},
        annotations={
            "destructiveHint": True,
            "title": "Create or Update Scene",
        },
    )
    @log_tool_usage
    async def ha_config_set_scene(
        self,
        scene_id: Annotated[
            str, Field(description="Scene identifier (e.g., 'movie_night')")
        ],
        config: Annotated[
            str | dict[str, Any] | None,
            Field(
                description=(
                    "Scene configuration dictionary. Must include 'entities' "
                    "(a dict keyed by entity_id, NOT a list). Optional fields: "
                    "'name' (defaults to scene_id), 'icon', 'id'. "
                    "Mutually exclusive with python_transform."
                ),
                default=None,
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description=(
                    "Python expression to transform existing scene config. "
                    "Mutually exclusive with config. "
                    "Requires config_hash for validation. "
                    "WARNING: Expressions with infinite loops will hang the server. "
                    "Examples: "
                    "Add entity: python_transform=\"config['entities']['light.bed'] = {'state': 'on'}\" "
                    "Update brightness: python_transform=\"config['entities']['light.kitchen']['brightness'] = 50\" "
                    "Remove entity: python_transform=\"del config['entities']['light.kitchen']\" "
                    "\n\n" + get_security_documentation()
                ),
            ),
        ] = None,
        config_hash: Annotated[
            str | None,
            Field(
                description=(
                    "Config hash from ha_config_get_scene for optimistic locking. "
                    "REQUIRED for python_transform (validates scene unchanged). "
                    "Optional for config updates (validates before full replacement if provided)."
                ),
            ),
        ] = None,
        category: Annotated[
            str | None,
            Field(
                description=(
                    "Category ID to assign to this scene. Use ha_config_get_category(scope='scene') "
                    "to list available categories, or ha_config_set_category() to create one."
                ),
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool | str,
            Field(
                description=(
                    "Wait for scene to be queryable before returning. Default: True. "
                    "Set to False for bulk operations."
                ),
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant scene.

        Supports two modes: full config replacement OR Python transformation.

        WHEN TO USE WHICH MODE:
        - python_transform: RECOMMENDED for edits to existing scenes. Surgical updates.
        - config: Use for creating new scenes or full replacements.

        IMPORTANT: python_transform requires 'config_hash' from ha_config_get_scene().

        SCENE SHAPE: ``entities`` is a dict keyed by entity_id (e.g.,
        ``{'light.kitchen': {'state': 'on', 'brightness': 200}}``), NOT a list.
        Automations use a list of actions; scenes capture a snapshot of states
        as a dict.

        PYTHON TRANSFORM EXAMPLES:
        - Add an entity: python_transform="config['entities']['light.bed'] = {'state': 'on'}"
        - Adjust brightness: python_transform="config['entities']['light.kitchen']['brightness'] = 50"
        - Remove entity: python_transform="del config['entities']['light.kitchen']"

        EXAMPLES:

        Create a basic scene:
        ha_config_set_scene(scene_id="movie_night", config={
            "name": "Movie Night",
            "entities": {
                "light.living_room": {"state": "on", "brightness": 50},
                "light.tv_backlight": {"state": "on", "rgb_color": [120, 0, 200]}
            },
            "icon": "mdi:movie"
        })

        Create a wake-up scene:
        ha_config_set_scene(scene_id="wake_up", config={
            "name": "Wake Up",
            "entities": {
                "light.bedroom": {"state": "on", "brightness": 255, "color_temp": 250},
                "cover.bedroom_blinds": {"current_position": 100}
            }
        })

        Update an existing scene (full replacement):
        ha_config_set_scene(scene_id="movie_night", config={
            "name": "Movie Night (Cool)",
            "entities": {
                "light.living_room": {"state": "on", "brightness": 30}
            }
        })

        For detailed scene configuration help, use ha_get_skill_home_assistant_best_practices.
        """
        try:
            # Validate mutual exclusivity of config and python_transform
            if config is not None and python_transform is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Cannot use both config and python_transform simultaneously",
                        suggestions=[
                            "Use only ONE of: config or python_transform",
                            "config: Full replacement",
                            "python_transform: Python-based edits (recommended for existing scenes)",
                        ],
                        context={"action": "set", "scene_id": scene_id},
                    )
                )

            # python_transform branch
            if python_transform is not None:
                if config_hash is None:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "config_hash is required for python_transform",
                            suggestions=[
                                "Call ha_config_get_scene() first",
                                "Use the config_hash from that response",
                            ],
                            context={"action": "python_transform", "scene_id": scene_id},
                        )
                    )

                actual_config = await self._fetch_and_verify_hash(
                    scene_id, config_hash, "python_transform"
                )

                try:
                    transformed_config = safe_execute(python_transform, actual_config)
                except PythonSandboxError as e:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            str(e),
                            suggestions=[
                                "Check expression syntax",
                                "Ensure only allowed operations are used",
                                "See tool description for allowed operations",
                                f"Expression: {python_transform[:100]}{'...' if len(python_transform) > 100 else ''}",
                            ],
                            context={"action": "python_transform", "scene_id": scene_id},
                        )
                    )

                # Validate transformed config still has the required shape.
                if "entities" not in transformed_config:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            "Transformed config must still include 'entities'",
                            suggestions=[
                                "The transform may have removed the required field",
                                "Ensure the config still has an 'entities' key",
                            ],
                            context={"action": "python_transform", "scene_id": scene_id},
                        )
                    )
                if not isinstance(transformed_config["entities"], dict):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            "Transformed 'entities' must remain a dict keyed by entity_id",
                            context={
                                "action": "python_transform",
                                "scene_id": scene_id,
                                "resulting_type": type(transformed_config["entities"]).__name__,
                            },
                        )
                    )

                result = await self._client.upsert_scene_config(
                    transformed_config, scene_id
                )

                # Re-fetch to get authoritative hash (HA may normalise after save).
                _, new_config_hash = await self._get_scene_config_internal(scene_id)

                response: dict[str, Any] = {
                    "success": True,
                    "action": "python_transform",
                    "scene_id": scene_id,
                    "config_hash": new_config_hash,
                    "python_expression": python_transform,
                    "message": f"Scene {scene_id} updated via Python transform",
                    **{k: v for k, v in result.items() if k != "success"},
                }
                return response

            if config is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Either config or python_transform must be provided",
                        suggestions=[
                            "config: Full scene configuration for create/replace",
                            "python_transform: Python expression for surgical edits",
                        ],
                        context={"action": "set", "scene_id": scene_id},
                    )
                )

            config_dict, effective_category = self._validate_scene_config(
                config, scene_id, category,
            )

            # Optional hash check for full config updates
            if config_hash:
                await self._fetch_and_verify_hash(scene_id, config_hash, "set")

            # Cross-check literal service and entity references against the
            # live registries. Soft warnings only.
            validation_meta = await validate_config_references(
                self._client, config_dict
            )

            result = await self._client.upsert_scene_config(config_dict, scene_id)

            # Resolve actual entity_id via registry — HA derives scene
            # entity_ids from the 'name' slug, not the scene_id storage key,
            # so f"scene.{scene_id}" is wrong whenever a name is supplied.
            # Verified live during BAT validation of this PR.
            entity_id = await self._resolve_scene_entity_id(scene_id)

            # Wait for scene to be queryable
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            if wait_bool:
                try:
                    registered = await wait_for_entity_registered(self._client, entity_id)
                    if not registered:
                        result["warning"] = (
                            f"Scene created but {entity_id} not yet queryable. "
                            "It may take a moment to become available."
                        )
                except Exception as e:
                    result["warning"] = f"Scene created but verification failed: {e}"

            # Apply category to entity registry if provided.
            if effective_category and entity_id:
                await apply_entity_category(
                    self._client, entity_id, effective_category, "scene", result, "scene"
                )

            merge_validation_meta(result, validation_meta)

            return {
                "success": True,
                **result,
            }

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"scene_id": scene_id},
                suggestions=[
                    "Ensure config includes an 'entities' field (a dict keyed by entity_id)",
                    "Scene shape: {'entities': {'light.kitchen': {'state': 'on'}}}",
                    "Use ha_search_entities(domain_filter='scene') to find scenes",
                    "Use ha_get_skill_home_assistant_best_practices for help",
                ],
            )

    @tool(
        name="ha_config_remove_scene",
        tags={"Scenes"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Scene",
        },
    )
    @log_tool_usage
    async def ha_config_remove_scene(
        self,
        scene_id: Annotated[
            str, Field(description="Scene identifier to delete (e.g., 'old_scene')")
        ],
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for scene to be fully removed before returning. Default: True.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant scene.

        EXAMPLES:
        - Delete scene: ha_config_remove_scene("old_scene")
        - Delete scene: ha_config_remove_scene("temporary_scene")

        **IMPORTANT LIMITATION:**
        This tool can only delete scenes created via the Home Assistant UI.
        Scenes defined in YAML configuration files (scenes.yaml or configuration.yaml)
        cannot be deleted through the API and will return a 405 Method Not Allowed error.

        To remove YAML-defined scenes, you must edit the configuration file directly.

        **WARNING:** Deleting a scene that is referenced by automations or scripts
        (via ``scene.turn_on``) may cause those to fail.
        """
        try:
            # Resolve actual entity_id BEFORE delete — once the registry
            # entry is gone, the unique_id lookup can no longer find it.
            # Falls back to f"scene.{scene_id}" if the registry has no
            # matching unique_id (e.g. scene_id-as-entity-id slug case).
            entity_id = await self._resolve_scene_entity_id(scene_id)

            result = await self._client.delete_scene_config(scene_id)

            wait_bool = coerce_bool_param(wait, "wait", default=True)
            if wait_bool:
                try:
                    removed = await wait_for_entity_removed(self._client, entity_id)
                    if not removed:
                        result["warning"] = (
                            f"Deletion confirmed by API but {entity_id} may still appear briefly."
                        )
                except Exception as e:
                    result["warning"] = f"Deletion confirmed but removal verification failed: {e}"

            return {"success": True, "action": "delete", **result}
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"scene_id": scene_id},
                suggestions=[
                    "Verify scene_id exists using ha_search_entities(domain_filter='scene')",
                    "Check if scene is being used by automations or scripts",
                    "Use ha_get_skill_home_assistant_best_practices for help",
                ],
            )


def register_config_scene_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant scene configuration tools."""
    register_tool_methods(mcp, ConfigSceneTools(client))
