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
from typing import Annotated, Any, Literal

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
from .util_helpers import websocket_error_message

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

PipelineAction = Literal["list", "get", "create", "update", "set_preferred"]

# Mirrors HA Core's assist_pipeline/pipeline.py pipeline create/update schema.
_PIPELINE_FIELDS = (
    "conversation_engine",
    "conversation_language",
    "language",
    "name",
    "stt_engine",
    "stt_language",
    "tts_engine",
    "tts_language",
    "tts_voice",
    "wake_word_entity",
    "wake_word_id",
    "prefer_local_intents",
)

_NULLABLE_PIPELINE_FIELDS = {
    "stt_engine",
    "stt_language",
    "tts_engine",
    "tts_language",
    "tts_voice",
    "wake_word_entity",
    "wake_word_id",
}


def _normalize_pipeline_value(field: str, value: Any) -> Any:
    """Convert empty string to None for clearable pipeline fields."""
    if field in _NULLABLE_PIPELINE_FIELDS and value == "":
        return None
    return value


def _drop_pipeline_id(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields HA accepts for create/update pipeline commands."""
    return {field: pipeline[field] for field in _PIPELINE_FIELDS if field in pipeline}


class VoiceAssistantTools:
    """Voice assistant exposure query tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _send_pipeline_message(
        self,
        message: dict[str, Any],
        *,
        operation: str,
        pipeline_id: str | None = None,
    ) -> Any:
        """Send an Assist pipeline websocket message and map HA failures."""
        result = await self._client.send_websocket_message(message)

        if not result.get("success"):
            error_msg = websocket_error_message(result.get("error", "Operation failed"))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to {operation} Assist pipeline: {error_msg}",
                    context={"pipeline_id": pipeline_id, "operation": operation},
                )
            )

        return result.get("result")

    async def _get_pipeline_for_write(self, pipeline_id: str | None) -> dict[str, Any]:
        """Fetch a pipeline for create/update merge operations."""
        message: dict[str, Any] = {"type": "assist_pipeline/pipeline/get"}
        if pipeline_id is not None:
            message["pipeline_id"] = pipeline_id

        pipeline = await self._send_pipeline_message(
            message,
            operation="get",
            pipeline_id=pipeline_id,
        )
        if not isinstance(pipeline, dict):
            if pipeline_id is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "No preferred Assist pipeline is configured",
                        context={"pipeline_id": pipeline_id, "details": pipeline},
                        suggestions=[
                            "Call ha_manage_pipeline(action='list') to find pipeline IDs.",
                            "Pass base_pipeline_id explicitly when creating a pipeline.",
                        ],
                    )
                )
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected Assist pipeline response",
                    context={"pipeline_id": pipeline_id, "details": pipeline},
                )
            )
        return pipeline

    @staticmethod
    def _pipeline_updates(
        *,
        conversation_engine: str | None,
        conversation_language: str | None,
        language: str | None,
        name: str | None,
        stt_engine: str | None,
        stt_language: str | None,
        tts_engine: str | None,
        tts_language: str | None,
        tts_voice: str | None,
        wake_word_entity: str | None,
        wake_word_id: str | None,
        prefer_local_intents: bool | None,
    ) -> dict[str, Any]:
        """Collect supplied pipeline fields into HA's pipeline storage shape."""
        values = {
            "conversation_engine": conversation_engine,
            "conversation_language": conversation_language,
            "language": language,
            "name": name,
            "stt_engine": stt_engine,
            "stt_language": stt_language,
            "tts_engine": tts_engine,
            "tts_language": tts_language,
            "tts_voice": tts_voice,
            "wake_word_entity": wake_word_entity,
            "wake_word_id": wake_word_id,
            "prefer_local_intents": prefer_local_intents,
        }
        for field, value in values.items():
            if value == "" and field not in _NULLABLE_PIPELINE_FIELDS:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{field} cannot be an empty string",
                        context={"field": field},
                        suggestions=[
                            f"Omit {field} to keep the existing or cloned value.",
                            f"Pass a non-empty value for {field}.",
                        ],
                    )
                )
        return {
            field: _normalize_pipeline_value(field, value)
            for field, value in values.items()
            if value is not None
        }

    async def _manage_pipeline_read(
        self,
        *,
        action: PipelineAction,
        pipeline_id: str | None,
    ) -> dict[str, Any]:
        """Handle Assist pipeline list/get actions."""
        if action == "list":
            data = await self._send_pipeline_message(
                {"type": "assist_pipeline/pipeline/list"},
                operation="list",
            )
            pipelines = data.get("pipelines", []) if isinstance(data, dict) else []
            return {
                "success": True,
                "operation": "list",
                "count": len(pipelines),
                "pipelines": pipelines,
                "preferred_pipeline": (
                    data.get("preferred_pipeline") if isinstance(data, dict) else None
                ),
                "message": f"Found {len(pipelines)} Assist pipeline(s)",
            }

        if pipeline_id is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "action='get' requires pipeline_id",
                    context={"action": action},
                    suggestions=[
                        "Call ha_manage_pipeline(action='list') first to find pipeline IDs."
                    ],
                )
            )

        data = await self._send_pipeline_message(
            {"type": "assist_pipeline/pipeline/get", "pipeline_id": pipeline_id},
            operation="get",
            pipeline_id=pipeline_id,
        )
        return {
            "success": True,
            "operation": "get",
            "pipeline_id": pipeline_id,
            "pipeline": data,
            "message": (
                f"Found Assist pipeline: {data.get('name', pipeline_id)}"
                if isinstance(data, dict)
                else f"Found Assist pipeline: {pipeline_id}"
            ),
        }

    async def _set_preferred_pipeline(self, pipeline_id: str | None) -> dict[str, Any]:
        """Set the preferred Assist pipeline."""
        if pipeline_id is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "action='set_preferred' requires pipeline_id",
                    context={"action": "set_preferred"},
                    suggestions=[
                        "Call ha_manage_pipeline(action='list') first to find pipeline IDs."
                    ],
                )
            )

        await self._send_pipeline_message(
            {
                "type": "assist_pipeline/pipeline/set_preferred",
                "pipeline_id": pipeline_id,
            },
            operation="set preferred",
            pipeline_id=pipeline_id,
        )
        return {
            "success": True,
            "operation": "set_preferred",
            "pipeline_id": pipeline_id,
            "message": f"Successfully set preferred Assist pipeline: {pipeline_id}",
        }

    async def _write_pipeline(
        self,
        *,
        action: PipelineAction,
        pipeline_id: str | None,
        base_pipeline_id: str | None,
        updates: dict[str, Any],
        make_preferred: bool,
    ) -> dict[str, Any]:
        """Handle Assist pipeline create/update actions."""
        if action == "create" and (
            updates.get("name") is None or updates.get("conversation_engine") is None
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "action='create' requires name and conversation_engine",
                    context={"action": action},
                    suggestions=[
                        "Provide name and conversation_engine.",
                        "Use ha_manage_pipeline(action='list') to inspect current pipeline values.",
                    ],
                )
            )

        if action == "update" and pipeline_id is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "action='update' requires pipeline_id",
                    context={"action": action},
                    suggestions=[
                        "Call ha_manage_pipeline(action='list') first to find pipeline IDs."
                    ],
                )
            )

        if not updates and not make_preferred:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "No Assist pipeline changes requested",
                    context={"action": action, "pipeline_id": pipeline_id},
                    suggestions=[
                        "Pass at least one pipeline field to update.",
                        "Use action='set_preferred' to only change the preferred pipeline.",
                    ],
                )
            )

        source_pipeline_id = pipeline_id if action == "update" else base_pipeline_id
        pipeline = await self._get_pipeline_for_write(source_pipeline_id)
        payload = _drop_pipeline_id(pipeline)
        payload.update(updates)

        message = {
            "type": f"assist_pipeline/pipeline/{action}",
            **payload,
        }
        if action == "update":
            message["pipeline_id"] = pipeline_id

        result_pipeline = await self._send_pipeline_message(
            message,
            operation=action,
            pipeline_id=pipeline_id,
        )
        if not isinstance(result_pipeline, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected Assist pipeline write response",
                    context={
                        "action": action,
                        "pipeline_id": pipeline_id,
                        "details": result_pipeline,
                    },
                )
            )

        result_pipeline_id = str(result_pipeline.get("id", pipeline_id))
        preferred_changed = False
        if make_preferred:
            await self._set_preferred_pipeline(result_pipeline_id)
            preferred_changed = True

        operation = "created" if action == "create" else "updated"
        return {
            "success": True,
            "operation": operation,
            "pipeline_id": result_pipeline_id,
            "pipeline": result_pipeline,
            "preferred_changed": preferred_changed,
            "message": (
                f"Assist pipeline {operation}: "
                f"{result_pipeline.get('name', result_pipeline_id)}"
            ),
        }

    @tool(
        name="ha_manage_pipeline",
        tags={"Assist"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "readOnlyHint": False,
            "title": "Manage Assist Pipeline",
        },
    )
    @log_tool_usage
    async def ha_manage_pipeline(
        self,
        action: Annotated[
            PipelineAction,
            Field(
                description=(
                    "Pipeline operation: list, get, create, update, or set_preferred."
                ),
            ),
        ],
        pipeline_id: Annotated[
            str | None,
            Field(
                description=(
                    "Assist pipeline ID. Required for get, update, and set_preferred."
                ),
                default=None,
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description="Pipeline display name. Required when action='create'.",
                default=None,
            ),
        ] = None,
        conversation_engine: Annotated[
            str | None,
            Field(
                description=(
                    "Conversation agent entity ID or engine ID. Required when action='create'."
                ),
                default=None,
            ),
        ] = None,
        base_pipeline_id: Annotated[
            str | None,
            Field(
                description=(
                    "Pipeline ID to clone when creating. Omit to clone the preferred "
                    "pipeline. Ignored for non-create actions."
                ),
                default=None,
            ),
        ] = None,
        conversation_language: Annotated[
            str | None,
            Field(description="Conversation language, usually '*'.", default=None),
        ] = None,
        language: Annotated[
            str | None,
            Field(description="Pipeline language, e.g. 'en'.", default=None),
        ] = None,
        stt_engine: Annotated[
            str | None,
            Field(
                description="Speech-to-text engine. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        stt_language: Annotated[
            str | None,
            Field(
                description="Speech-to-text language. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        tts_engine: Annotated[
            str | None,
            Field(
                description="Text-to-speech engine. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        tts_language: Annotated[
            str | None,
            Field(
                description="Text-to-speech language. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        tts_voice: Annotated[
            str | None,
            Field(
                description="Text-to-speech voice. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        wake_word_entity: Annotated[
            str | None,
            Field(
                description="Wake-word entity ID. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        wake_word_id: Annotated[
            str | None,
            Field(
                description="Wake-word ID. Pass empty string to clear.", default=None
            ),
        ] = None,
        prefer_local_intents: Annotated[
            bool | None,
            Field(
                description=(
                    "Whether Home Assistant local intents should be preferred before "
                    "the conversation engine."
                ),
                default=None,
            ),
        ] = None,
        make_preferred: Annotated[
            bool,
            Field(
                description=(
                    "For create/update only, also set the resulting pipeline as "
                    "preferred with an extra websocket call. Ignored for other actions."
                ),
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage Home Assistant Assist pipelines.

        Use action='list' to discover pipeline IDs, action='get' to inspect one
        pipeline, action='create' or action='update' to write pipeline settings,
        and action='set_preferred' to choose the preferred pipeline.

        EXAMPLES:
        - List pipelines: ha_manage_pipeline(action="list")
        - Get one pipeline: ha_manage_pipeline(action="get", pipeline_id="preferred")
        - Create by cloning preferred: ha_manage_pipeline(
              action="create",
              name="Local Assist",
              conversation_engine="conversation.local_llm",
          )
        - Create by cloning a specific pipeline: ha_manage_pipeline(
              action="create",
              base_pipeline_id="preferred",
              name="Local Assist",
              conversation_engine="conversation.local_llm",
          )
        - Update conversation agent and clear TTS voice: ha_manage_pipeline(
              action="update",
              pipeline_id="preferred",
              conversation_engine="conversation.local_llm",
              tts_voice="",
          )
        - Set preferred: ha_manage_pipeline(
              action="set_preferred",
              pipeline_id="preferred",
          )

        Empty string clears nullable STT/TTS/wake-word fields. Non-nullable
        fields such as name, language, conversation_language, and
        conversation_engine must be omitted or non-empty.
        """
        try:
            if action in {"list", "get"}:
                return await self._manage_pipeline_read(
                    action=action,
                    pipeline_id=pipeline_id,
                )

            if action == "set_preferred":
                return await self._set_preferred_pipeline(pipeline_id)

            updates = self._pipeline_updates(
                conversation_engine=conversation_engine,
                conversation_language=conversation_language,
                language=language,
                name=name,
                stt_engine=stt_engine,
                stt_language=stt_language,
                tts_engine=tts_engine,
                tts_language=tts_language,
                tts_voice=tts_voice,
                wake_word_entity=wake_word_entity,
                wake_word_id=wake_word_id,
                prefer_local_intents=prefer_local_intents,
            )
            return await self._write_pipeline(
                action=action,
                pipeline_id=pipeline_id,
                base_pipeline_id=base_pipeline_id,
                updates=updates,
                make_preferred=make_preferred,
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"action": action, "pipeline_id": pipeline_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Use ha_manage_pipeline(action='list') to inspect existing pipeline values",
                    "Use ha_search(domain_filter='conversation') to find conversation agent IDs",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _fetch_exposure_via_component(
        self, entity_id: str | None
    ) -> dict[str, Any] | None:
        """One ``ha_mcp_tools/exposure`` read; ``None`` ⇒ run the legacy path.

        Returns the component payload — ``{exposed_entities: {id: {assistant:
        True}}, entity_info: {id: {...}}}`` — where ``exposed_entities`` is
        byte-identical to the legacy ``homeassistant/expose_entity/list`` map so
        the existing ``_get_entity_exposure`` / ``_list_exposures`` shapers consume
        it unchanged. ``None`` on capability miss, downgrade (``unknown_command`` →
        invalidate the cached caps), or command error/timeout (logged) — the caller
        falls back to the legacy WS list. A ``HomeAssistantConnectionError`` (WS
        down) is not caught here; it propagates and the legacy path, sharing the
        same socket, would fail identically. Same caps-gate discipline as
        ``component_devices.fetch_device_via_component``.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "exposure"):
            return None
        kwargs: dict[str, Any] = {}
        if entity_id is not None:
            kwargs["entity_id"] = entity_id
        try:
            ws = await get_websocket_client(
                url=self._client.base_url, token=self._client.token
            )
            raw = await ws.send_command(WS_EXPOSURE, **kwargs)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            else:
                logger.warning("%s failed; fell back to legacy: %r", WS_EXPOSURE, exc)
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
