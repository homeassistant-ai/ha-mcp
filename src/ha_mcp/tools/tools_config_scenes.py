"""
Configuration management tools for Home Assistant scenes.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant scene configurations. Mirrors the scripts/automations
patterns; the key shape difference is that scene ``entities`` is a dict
keyed by entity_id (not a list).
"""

import asyncio
import logging
from typing import Annotated, Any, cast

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from ..utils.python_sandbox import (
    PythonSandboxError,
    format_sandbox_error,
    get_security_documentation,
    safe_execute,
)
from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .reference_validator import validate_config_references
from .util_helpers import (
    apply_entity_category,
    attach_skill_content,
    coerce_bool_param,
    fetch_entity_category,
    merge_validation_meta,
    parse_json_param,
    wait_for_entity_registered,
    wait_for_entity_removed,
)

# No scene-specific reference file exists in home-assistant-best-practices;
# SKILL.md is the top-level generic best-practice doc covering entity-naming,
# safe-refactoring, and helper-vs-template trade-offs the agent benefits
# from when authoring scenes. Scenes have NO actions/conditions/triggers
# (only an ``entities`` state-snapshot dict) so the automation/script
# reference files don't apply.
_SCENE_SKILL_FILES: tuple[str, ...] = ("SKILL.md",)

logger = logging.getLogger(__name__)


class ConfigSceneTools:
    """Scene configuration management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # Time to wait between the first and second registry-list query when the
    # scene was just upserted but hasn't yet been indexed. Exposed as a class
    # attribute so tests can patch it down to ``0`` instead of waiting 200 ms
    # in suite runs.
    _RESOLVE_RETRY_DELAY = 0.2

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
        by matching the scene_id to ``unique_id`` in the entity registry.

        On a freshly-upserted scene the registry can lag the storage write
        by tens to ~200 ms — the first query returns no match and the naive
        ``scene.{scene_id}`` fallback is then chased by
        ``wait_for_entity_registered`` to its phantom-404 timeout. Retry
        the registry list once after a short delay so the post-upsert
        callsites see the real entity_id instead of trailing the lookup
        with a phantom (issue #1168 R3 blocker 1).

        Accepts a bare ``scene_id`` ("movie_night") or a fully-qualified
        ``entity_id`` ("scene.movie_night") — the leading ``scene.`` is
        stripped so callers don't accidentally produce ``scene.scene.movie_night``
        on fallback. Mirrors ``rest_client.resolve_scene_id`` ergonomics.
        """
        scene_id = scene_id.removeprefix("scene.")
        retried = False
        for attempt in range(2):
            try:
                result = await self._client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                )
                if result.get("success") is not False:
                    for entry in result.get("result") or []:
                        entity_id = entry.get("entity_id") or ""
                        if entry.get("unique_id") == scene_id and entity_id.startswith(
                            "scene."
                        ):
                            return entity_id
            except (TimeoutError, HomeAssistantAPIError, HomeAssistantConnectionError):
                # Programming bugs (AttributeError, KeyError, …) propagate; only
                # genuine HA-API failures fall through to the naive form so the
                # caller still gets a best-effort entity_id rather than a 500.
                logger.warning(
                    f"Entity registry resolve failed for scene_id={scene_id}, "
                    f"falling back to scene.{scene_id}",
                    exc_info=True,
                )
                # API failure is sticky — retrying won't change a 500 / auth
                # error. Bail to the naive form immediately.
                break
            if attempt == 0:
                # Registry list succeeded but no row matched. On a freshly-
                # upserted scene the registry index lags the storage write —
                # one short retry catches the common case before falling
                # back to the naive entity_id.
                await asyncio.sleep(self._RESOLVE_RETRY_DELAY)
                retried = True
        # Issue #1168 R5 blocker 13 (refined per R6 blocker 19): only log on
        # the genuine exhausted-retry exit (both list calls succeeded, neither
        # matched). The first-pass-success path also falls through here on the
        # legitimate "registry caught up before retry" case, but that path
        # never sets ``retried``; gating on it stops the phantom-noise.
        if retried:
            logger.debug(
                f"_resolve_scene_entity_id: registry retry exhausted for "
                f"scene_id={scene_id!r}, falling back to scene.{scene_id}"
            )
        return f"scene.{scene_id}"

    async def _validate_category_id(self, category: str) -> None:
        """Confirm ``category`` exists in the ``scene`` category registry.

        Issue #1168 R5 blocker 9: ``apply_entity_category`` forwards the
        supplied ID blindly to the entity-registry update; HA accepts a
        non-existent category ID without complaint, leaving the registry
        with a phantom reference invisible in
        ``ha_config_get_category(scope='scene')`` results. Pre-validating
        the ID against the live category registry is the only place the
        phantom can be caught without changing HA itself.
        """
        try:
            result = await self._client.send_websocket_message(
                {"type": "config/category_registry/list", "scope": "scene"}
            )
        except (
            TimeoutError,
            HomeAssistantAPIError,
            HomeAssistantConnectionError,
        ) as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to validate category {category!r}: {e}",
                    context={"category": category, "scope": "scene"},
                )
            )
        if not isinstance(result, dict) or not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list categories for validation of {category!r}",
                    context={"category": category, "scope": "scene"},
                )
            )
        valid_ids = {
            c.get("category_id")
            for c in (result.get("result") or [])
            if isinstance(c, dict)
        }
        if category not in valid_ids:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Category {category!r} does not exist in scope 'scene'",
                    suggestions=[
                        "Use ha_config_get_category(scope='scene') to list available categories",
                        "Use ha_config_set_category(name=..., scope='scene') to create a new category",
                    ],
                    context={"category": category, "scope": "scene"},
                )
            )

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

        EXAMPLES:
        - Get scene: ha_config_get_scene("movie_night")
        - Get scene: ha_config_get_scene("bedroom_dim")

        RELATED TOOLS:
        - ha_config_set_scene — pass the returned ``config_hash`` for
          ``python_transform`` updates.

        For detailed scene configuration help, use ha_get_skill_guide.
        """
        try:
            # Issue #1168 R6 blocker 16: empty ``scene_id`` previously
            # surfaced as ``RESOURCE_NOT_FOUND`` with a misleading
            # `entities`-related suggestion. Pre-flight here so the caller
            # gets the actual problem. Migrated to the shared
            # ``validate_identifier_not_empty`` helper (#1314) — message
            # and ``context["scene_id"]`` key preserved for callers.
            validate_identifier_not_empty(
                scene_id,
                "scene_id",
                message="scene_id must not be empty",
                suggestions=[
                    "Pass a non-empty scene identifier (e.g. 'movie_night')",
                    "Use ha_search_entities(domain_filter='scene') "
                    "to find existing scene_ids",
                ],
                context={"scene_id": scene_id},
            )
            # Issue #1168 R3 blockers 3 + 6: unwrap the rest-client envelope
            # so the response carries the scene body directly (no nested
            # `success`/`scene_id`/`config` chain), and use the storage key
            # consistently for `scene_id` regardless of whether the caller
            # passed the entity_id slug or the storage key.
            envelope = await self._client.get_scene_config(scene_id)
            actual_config = envelope.get("config", envelope)
            resolved_id = envelope.get("scene_id", scene_id)
            config_hash_value = compute_config_hash(actual_config)

            # Resolve real entity_id via registry — see _resolve_scene_entity_id
            # for the reasoning. Category fetch on the wrong entity_id is a
            # silent no-op, masking real category assignments.
            entity_id = await self._resolve_scene_entity_id(resolved_id)
            cat_id = await fetch_entity_category(self._client, entity_id, "scene")

            response: dict[str, Any] = {
                "success": True,
                "action": "get",
                "scene_id": resolved_id,
                "config": actual_config,
                "config_hash": config_hash_value,
            }
            if cat_id:
                response["category"] = cat_id
            return response
        except ToolError:
            raise
        except Exception as e:
            # Pass `entity_id` so a 404 from rest_client surfaces as
            # ENTITY_NOT_FOUND (not the generic RESOURCE_NOT_FOUND fallback).
            # The naive ``scene.{scene_id}`` form is good enough at error
            # time — registry resolution happens inside the try block and
            # may not have run when an exception escapes to here.
            exception_to_structured_error(
                e,
                context={
                    "scene_id": scene_id,
                    "entity_id": f"scene.{scene_id.removeprefix('scene.')}",
                },
                suggestions=[
                    "Verify scene_id exists using ha_search_entities(domain_filter='scene')",
                    "Check Home Assistant connection",
                    "Use ha_get_skill_guide for help",
                ],
            )

    async def _get_scene_config_internal(
        self, scene_id: str
    ) -> tuple[dict[str, Any], str, str]:
        """Fetch scene config without logging or category injection.

        Returns ``(actual_config, config_hash, resolved_id)`` where
        ``actual_config`` is the inner scene body (not the REST wrapper) and
        ``resolved_id`` is the storage key the rest-client resolved the input
        to (issue #1168 R3 blocker 6). Used by ``_fetch_and_verify_hash``.
        """
        envelope = await self._client.get_scene_config(scene_id)
        actual_config = envelope.get("config", envelope)
        resolved_id = envelope.get("scene_id", scene_id.removeprefix("scene."))
        config_hash_value = compute_config_hash(actual_config)
        return actual_config, config_hash_value, resolved_id

    async def _fetch_and_verify_hash(
        self, scene_id: str, config_hash: str, action: str
    ) -> tuple[dict[str, Any], str]:
        """Fetch current scene config and verify config_hash for optimistic locking.

        Returns ``(actual_config, resolved_id)`` — the inner scene body and
        the storage key the rest-client resolved the input to. Raises
        ``ToolError`` if the hash does not match (conflict). Issue #1168 R3
        blocker 6: callers thread ``resolved_id`` into responses so the
        outer ``scene_id`` matches the inner body's ``id`` regardless of
        whether the caller passed the entity_id slug or the storage key.
        """
        (
            actual_config,
            current_hash,
            resolved_id,
        ) = await self._get_scene_config_internal(scene_id)
        if current_hash != config_hash:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Scene modified since last read (conflict)",
                    suggestions=[
                        "Retry with the fresh config_hash returned in this error's context",
                        "Or call ha_config_get_scene() again to fetch a new hash",
                    ],
                    context={
                        "action": action,
                        "scene_id": resolved_id,
                        "current_config_hash": current_hash,
                    },
                )
            )
        return actual_config, resolved_id

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
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_JSON,
                    f"Invalid config parameter: {e}",
                    context={
                        "scene_id": scene_id,
                        "provided_config_type": type(config).__name__,
                    },
                )
            )

        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Config parameter must be a JSON object",
                    context={
                        "scene_id": scene_id,
                        "provided_type": type(parsed_config).__name__,
                    },
                )
            )

        config_dict = cast(dict[str, Any], parsed_config)

        # Extract category before sending to HA REST API (rejects unknown keys).
        # Parameter takes precedence over config dict value.
        config_category = config_dict.pop("category", None)
        effective_category = category if category is not None else config_category

        # Required field check. ``entities`` must be a dict keyed by entity_id.
        if "entities" not in config_dict:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "config must include an 'entities' field (a dict keyed by entity_id)",
                    context={"scene_id": scene_id, "required_fields": ["entities"]},
                )
            )

        if not isinstance(config_dict["entities"], dict):
            raise_tool_error(
                create_error_response(
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
                )
            )

        return config_dict, effective_category

    @tool(
        name="ha_config_set_scene",
        tags={"Scenes"},
        annotations={
            "destructiveHint": True,
            "title": "Create or Update Scene",
        },
    )
    @with_auto_backup(domain="scene", id_param="scene_id")
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
        include_skill: Annotated[
            bool,
            Field(
                description=(
                    "When True (default), the response includes the top-level "
                    "Home Assistant best-practice SKILL.md under a "
                    "'skill_content' field. No scene-specific reference file "
                    "exists; SKILL.md covers entity-naming, safe-refactoring, "
                    "and helper-vs-template trade-offs that intersect with "
                    "scene authoring. Set False on subsequent calls in the "
                    "same session if you've already read it."
                ),
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant scene.

        Supports two modes: full config replacement (``config``) or
        Python transformation of an existing scene (``python_transform``).
        See the field descriptions for ``python_transform`` examples and
        the ``config`` shape contract.

        WHEN TO USE:
        - ``python_transform``: surgical edits to an existing scene
          (add/remove/update a single entity entry). Requires ``config_hash``
          from ha_config_get_scene() for optimistic locking.
        - ``config``: creating a new scene, or wholesale replacement.

        WHEN NOT TO USE:
        - To activate a scene at runtime, use ha_call_service(domain="scene",
          service="turn_on", target=...) — this tool only manages scene
          *configuration*, not the runtime turn-on/off side.
        - To list or look up existing scenes, use
          ha_search_entities(domain_filter="scene") or ha_deep_search.

        SCENE SHAPE: ``entities`` is a dict keyed by entity_id (e.g.,
        ``{'light.kitchen': {'state': 'on', 'brightness': 200}}``), NOT a
        list. Automations use a list of actions; scenes capture a snapshot
        of states as a dict.

        EXAMPLE:

        ha_config_set_scene(scene_id="movie_night", config={
            "name": "Movie Night",
            "entities": {
                "light.living_room": {"state": "on", "brightness": 50},
            },
            "icon": "mdi:movie",
        })

        The top-level ``SKILL.md`` for home-assistant-best-practices ships in
        this response under ``skill_content`` by default (see ``include_skill``)
        — generic best-practice index covering entity-naming and
        safe-refactoring patterns that intersect with scene authoring. For
        detailed scene configuration help beyond that, use ha_get_skill_guide.
        """
        try:
            # Issue #1168 R6 blocker 16: empty ``scene_id`` pre-flight before
            # any config dispatch — keeps the error code/message aligned with
            # the actual problem rather than the misleading
            # ``RESOURCE_NOT_FOUND`` from a downstream lookup. Migrated to
            # the shared ``validate_identifier_not_empty`` helper (#1314) —
            # message and ``context["scene_id"]`` key preserved for callers.
            validate_identifier_not_empty(
                scene_id,
                "scene_id",
                message="scene_id must not be empty",
                suggestions=[
                    "Pass a non-empty scene identifier (e.g. 'movie_night')",
                    "For a fresh create, use a name-derived slug",
                ],
                context={"scene_id": scene_id},
            )
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
                            context={
                                "action": "python_transform",
                                "scene_id": scene_id,
                            },
                        )
                    )

                actual_config, resolved_id = await self._fetch_and_verify_hash(
                    scene_id, config_hash, "python_transform"
                )

                try:
                    transformed_config = safe_execute(python_transform, actual_config)
                except PythonSandboxError as e:
                    message, suggestions = format_sandbox_error(e, python_transform)
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            message,
                            suggestions=suggestions,
                            context={
                                "action": "python_transform",
                                "scene_id": resolved_id,
                            },
                        )
                    )

                # Issue #1168 R5 blocker 8: a transform that reassigns
                # config to ``None`` (or returns ``None`` from a list-comp
                # mistake) used to crash the next ``in`` check with a
                # ``TypeError`` that surfaced as ``INTERNAL_ERROR``. Catch
                # the rebind here so the user gets the same VALIDATION
                # signal the dict-shape checks below produce.
                if transformed_config is None:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            "Transform must not reassign config to None",
                            suggestions=[
                                "The transform must produce a dict matching the scene config shape",
                                "Mutate ``config`` in-place rather than reassigning it",
                            ],
                            context={
                                "action": "python_transform",
                                "scene_id": resolved_id,
                            },
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
                            context={
                                "action": "python_transform",
                                "scene_id": resolved_id,
                            },
                        )
                    )
                if not isinstance(transformed_config["entities"], dict):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            "Transformed 'entities' must remain a dict keyed by entity_id",
                            context={
                                "action": "python_transform",
                                "scene_id": resolved_id,
                                "resulting_type": type(
                                    transformed_config["entities"]
                                ).__name__,
                            },
                        )
                    )

                # Issue #1168 R5 blocker 10: a transform that mutates
                # ``config['id']`` produces a duplicate scene at the new
                # storage key AND orphans the original (state goes
                # ``unavailable``, registry row left behind). HA itself
                # accepts the mismatched-id upsert silently. Reject the
                # rename here — renaming a scene means delete-old +
                # create-new through the explicit tools, not an in-place
                # ``id`` rebind. R6 blocker 18: only reject an explicit
                # mismatched id; a transform that ``del config['id']`` is
                # legitimate (HA treats ``id`` as optional) and should pass
                # through. R7 blocker 24: ``transformed_config["id"] = None``
                # is the in-place equivalent of ``del`` — normalise both
                # by checking against ``(None, resolved_id)``, so only an
                # explicit non-None mismatch triggers the reject.
                if "id" in transformed_config and transformed_config["id"] not in (
                    None,
                    resolved_id,
                ):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            "Transform must not change ``config['id']``",
                            suggestions=[
                                f"Original scene_id is {resolved_id!r} — keep it unchanged",
                                "To rename a scene, use ha_config_remove_scene + ha_config_set_scene",
                            ],
                            context={
                                "action": "python_transform",
                                "scene_id": resolved_id,
                                "attempted_id": transformed_config.get("id"),
                            },
                        )
                    )

                # Issue #1168 R3 blocker 5: prune orphan ``metadata`` keys
                # whose entity was deleted by the transform (e.g. a list
                # comprehension that filters ``entities`` doesn't touch
                # ``metadata`` and HA keeps the orphan entries on disk).
                # Full-replace via ``config=`` clears metadata cleanly; the
                # transform path needs to mirror that contract.
                metadata = transformed_config.get("metadata")
                if isinstance(metadata, dict):
                    valid_keys = set(transformed_config["entities"].keys())
                    pruned_keys = sorted(k for k in metadata if k not in valid_keys)
                    if pruned_keys:
                        # Issue #1168 R5 blocker 12: a buggy transform that
                        # accidentally drops entities used to silently
                        # rewrite metadata. Surface the prune so a stray
                        # ``del entities[k]`` doesn't lose the friendly_name
                        # without trace.
                        logger.info(
                            f"python_transform pruned {len(pruned_keys)} orphan "
                            f"metadata key(s) from scene {resolved_id}: {pruned_keys}"
                        )
                    transformed_config["metadata"] = {
                        k: v for k, v in metadata.items() if k in valid_keys
                    }

                # Issue #1168 R6 blocker 15: pre-validate ``category`` BEFORE
                # the upsert commits. Validating after the write produced the
                # exact partial-state-mutation pattern this PR's auto-attach
                # contract was meant to eliminate — scene was rewritten,
                # caller saw VALIDATION_INVALID_PARAMETER on a phantom
                # category, and the storage state had already moved.
                if category:
                    await self._validate_category_id(category)

                result = await self._client.upsert_scene_config(
                    transformed_config, resolved_id
                )

                # Re-fetch to get authoritative hash (HA may normalise after save).
                _, new_config_hash, _ = await self._get_scene_config_internal(
                    resolved_id
                )

                # Resolve actual entity_id and apply wait + category — same
                # post-upsert finalisation the full-config branch runs. Without
                # these, ``wait`` and ``category`` are silently dropped on
                # python_transform calls.
                wait_bool = coerce_bool_param(wait, "wait", default=True)
                entity_id = await self._resolve_scene_entity_id(resolved_id)
                if wait_bool:
                    try:
                        registered = await wait_for_entity_registered(
                            self._client, entity_id
                        )
                        if not registered:
                            result.setdefault("warnings", []).append(
                                f"Scene updated but {entity_id} not yet queryable. "
                                "It may take a moment to become available."
                            )
                    except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                        result.setdefault("warnings", []).append(
                            f"Scene updated but verification failed: {e}"
                        )
                if category and entity_id:
                    # Pre-validation of ``category`` already ran before the
                    # upsert (R6 blocker 15) — apply directly here.
                    await apply_entity_category(
                        self._client,
                        entity_id,
                        category,
                        "scene",
                        result,
                        "scene",
                    )

                # Issue #1168 R3 blocker 6: build the response from
                # ``resolved_id`` directly (not the caller-input ``scene_id``)
                # so the outer field always matches the storage key carried
                # in ``result``. ``result["scene_id"]`` is also the storage
                # key from rest_client; the kwarg-after-spread order means
                # the explicit assignment wins on key-collision regardless.
                response: dict[str, Any] = {
                    "success": True,
                    **{k: v for k, v in result.items() if k != "success"},
                    "action": "python_transform",
                    "scene_id": resolved_id,
                    "config_hash": new_config_hash,
                    "python_expression": python_transform,
                    "message": f"Scene {resolved_id} updated via Python transform",
                }
                attach_skill_content(
                    response,
                    include_skill=include_skill,
                    canonical_files=_SCENE_SKILL_FILES,
                    referenced_files=None,
                )
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
                config,
                scene_id,
                category,
            )

            # Issue #1168 R6 blocker 15: pre-validate ``category`` BEFORE
            # the hash check + upsert commits. Same partial-state hazard as
            # the python_transform branch — a phantom category must not
            # rewrite the scene before erroring.
            #
            # Issue #1168 R8 (post-merge follow-up): gate on
            # ``effective_category`` truthy, not on the user-facing
            # ``category`` param. ``_validate_scene_config`` promotes a
            # top-level ``config["category"]`` into ``effective_category``
            # when the user passed ``category=None``; the R7 fix gated on
            # ``category is not None`` to skip the WS round-trip when no
            # category was supplied, but that left the dict-promoted path
            # uncovered — a phantom category in ``config["category"]``
            # would skip validation and reach ``apply_entity_category``,
            # which attaches the phantom ID to the entity registry without
            # checking it exists. Truthy check covers both sources and
            # still skips the WS call when neither produces a category.
            if effective_category:
                await self._validate_category_id(effective_category)

            # Issue #1168 R3 blocker 7: when caller passes ``config_hash``,
            # honor the optimistic-locking semantics promised by the field
            # description. ``_fetch_and_verify_hash`` raises ToolError on
            # mismatch. We capture ``resolved_id`` from the verified fetch
            # so subsequent upsert/response builders use the storage key
            # consistently (issue #1168 R3 blocker 6).
            #
            # Path branching: if a hash is supplied for a non-existent
            # scene, the inner fetch raises 404 and surfaces as
            # ENTITY_NOT_FOUND via the outer except — which is the right
            # caller-facing semantics ("you can't lock against a scene
            # that doesn't exist"). The no-hash branch resolves separately
            # so a fresh create still threads the resolved id correctly.
            if config_hash:
                _, resolved_id = await self._fetch_and_verify_hash(
                    scene_id, config_hash, "set"
                )
            else:
                resolved_id = await self._client.resolve_scene_id(scene_id)

            # Cross-check literal service and entity references against the
            # live registries. Soft warnings only.
            validation_meta = await validate_config_references(
                self._client, config_dict
            )

            result = await self._client.upsert_scene_config(config_dict, resolved_id)

            # Resolve actual entity_id via registry — HA derives scene
            # entity_ids from the 'name' slug, not the scene_id storage key,
            # so f"scene.{scene_id}" is wrong whenever a name is supplied.
            entity_id = await self._resolve_scene_entity_id(resolved_id)

            # Wait for scene to be queryable
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            if wait_bool:
                try:
                    registered = await wait_for_entity_registered(
                        self._client, entity_id
                    )
                    if not registered:
                        result.setdefault("warnings", []).append(
                            f"Scene saved but {entity_id} not yet queryable. "
                            "It may take a moment to become available."
                        )
                except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                    result.setdefault("warnings", []).append(
                        f"Scene saved but verification failed: {e}"
                    )

            # Apply category to entity registry if provided.
            if effective_category and entity_id:
                # Pre-validation of ``effective_category`` already ran before
                # the upsert (R6 blocker 15) — apply directly here.
                await apply_entity_category(
                    self._client,
                    entity_id,
                    effective_category,
                    "scene",
                    result,
                    "scene",
                )

            merge_validation_meta(result, validation_meta)

            attach_skill_content(
                result,
                include_skill=include_skill,
                canonical_files=_SCENE_SKILL_FILES,
                referenced_files=None,
            )

            # Issue #1168 R3 blocker 6: build response from ``resolved_id``
            # so the outer ``scene_id`` always matches the storage key.
            # ``result["scene_id"]`` is also the storage key (from
            # rest_client); explicit assignment after the spread guards
            # against any future result-shape drift.
            return {
                "success": True,
                **result,
                "scene_id": resolved_id,
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
                    "Use ha_get_skill_guide for help",
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
    @with_auto_backup(domain="scene", id_param="scene_id")
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
            # Issue #1168 R6 blocker 16: empty ``scene_id`` pre-flight before
            # the resolver — keeps the error code/message aligned with the
            # actual problem. Migrated to the shared
            # ``validate_identifier_not_empty`` helper (#1314) — message
            # and ``context["scene_id"]`` key preserved for callers.
            validate_identifier_not_empty(
                scene_id,
                "scene_id",
                message="scene_id must not be empty",
                suggestions=[
                    "Pass a non-empty scene identifier (e.g. 'old_scene')",
                    "Use ha_search_entities(domain_filter='scene') "
                    "to find existing scene_ids",
                ],
                context={"scene_id": scene_id},
            )
            # Issue #1168 R3 blocker 6: resolve once up-front so every later
            # callsite (entity_id resolver, delete call, response) uses the
            # storage key consistently — outer ``scene_id`` matches the
            # inner body regardless of whether the caller passed the
            # entity_id slug or the storage key.
            resolved_id = await self._client.resolve_scene_id(scene_id)

            # Resolve actual entity_id BEFORE delete — once the registry
            # entry is gone, the unique_id lookup can no longer find it.
            # Falls back to f"scene.{resolved_id}" if the registry has no
            # matching unique_id (e.g. scene_id-as-entity-id slug case).
            entity_id = await self._resolve_scene_entity_id(resolved_id)

            result = await self._client.delete_scene_config(resolved_id)

            wait_bool = coerce_bool_param(wait, "wait", default=True)
            if wait_bool:
                try:
                    removed = await wait_for_entity_removed(self._client, entity_id)
                    if not removed:
                        result.setdefault("warnings", []).append(
                            f"Deletion confirmed by API but {entity_id} may still appear briefly."
                        )
                except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                    result.setdefault("warnings", []).append(
                        f"Deletion confirmed but removal verification failed: {e}"
                    )

            return {
                "success": True,
                "action": "delete",
                **result,
                "scene_id": resolved_id,
            }
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"scene_id": scene_id},
                suggestions=[
                    "Verify scene_id exists using ha_search_entities(domain_filter='scene')",
                    "Check if scene is being used by automations or scripts",
                    "Use ha_get_skill_guide for help",
                ],
            )


def register_config_scene_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant scene configuration tools."""
    register_tool_methods(mcp, ConfigSceneTools(client))
