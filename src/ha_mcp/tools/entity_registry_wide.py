"""Registry-wide reads and writes for ``ha_get_entity`` / ``ha_set_entity``.

Called without an entity, ``ha_get_entity`` reads Core's entity registry
settings (``config/entity_registry/settings/get``) and the exposed entities,
and ``ha_set_entity`` writes the settings and each assistant's "expose new
entities" flag. Each value goes to and comes from Core as it is.
"""

from typing import Any

from ..errors import ErrorCode, create_error_response
from .entity_expose_batch import read_expose_new
from .entity_exposure import list_exposures, read_exposure
from .entity_param_parsing import _parse_expose_to_param
from .helpers import raise_tool_error, ws_failure_code

REGISTRY_WIDE_PARAMS = ("expose_new_entities", "entity_id_parts")


async def registry_ws(client: Any, message: dict[str, Any], action: str) -> Any:
    """Send one registry-wide command; Core's refusal ends the call."""
    reply = await client.send_websocket_message(message)
    if not reply.get("success"):
        raise_tool_error(
            create_error_response(
                ws_failure_code(reply), f"{action} failed: {reply.get('error')}"
            )
        )
    return reply.get("result")


async def get_registry_wide(client: Any, assistant: str | None) -> dict[str, Any]:
    """Core's registry settings and exposed entities, optionally for one assistant."""
    # Core judges the assistant name first, and its reply is the flag.
    expose_new = await read_expose_new(client, assistant) if assistant else None
    settings = await registry_ws(
        client,
        {"type": "config/entity_registry/settings/get"},
        "Reading entity registry settings",
    )
    exposed_entities, entity_info = await read_exposure(client)
    response = {
        "success": True,
        "settings": settings,
        **list_exposures(exposed_entities, entity_info, assistant),
    }
    if assistant:
        response["assistant"] = assistant
        response["expose_new_entities"] = expose_new
    return response


def is_registry_wide_call(
    entity_id: Any, params: dict[str, Any], entity_params: dict[str, Any]
) -> bool:
    """True for a call without entity_id; refuses one that mixes the two kinds."""
    registry_wide = [k for k in REGISTRY_WIDE_PARAMS if params.get(k) is not None]
    if entity_id is not None:
        if registry_wide:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{registry_wide} are registry-wide and take no entity_id.",
                    suggestions=[
                        "Send the registry-wide settings in a call without entity_id"
                    ],
                )
            )
        return False
    given = [k for k, v in entity_params.items() if v is not None]
    if given or not registry_wide:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "entity_id is required"
                + (f" for {given}" if given else "")
                + "; only expose_new_entities and entity_id_parts take none.",
            )
        )
    return True


async def set_registry_wide(
    client: Any,
    expose_new_entities: dict[str, bool] | None,
    entity_id_parts: list[str] | None,
) -> dict[str, Any]:
    """Write the registry-wide settings, reporting the values they replace."""
    parsed = _parse_expose_to_param(expose_new_entities) or {}
    # Core judges each name, and its reply is the value to restore.
    previous_flags = {a: await read_expose_new(client, a) for a in parsed}
    response: dict[str, Any] = {"success": True}
    if entity_id_parts is not None:
        response["previous_settings"] = await registry_ws(
            client,
            {"type": "config/entity_registry/settings/get"},
            "Reading entity registry settings",
        )
        response["settings"] = await registry_ws(
            client,
            {
                "type": "config/entity_registry/settings/update",
                "entity_id_parts": entity_id_parts or None,
            },
            "Updating entity registry settings",
        )
    for assistant, expose_new in parsed.items():
        await registry_ws(
            client,
            {
                "type": "homeassistant/expose_new_entities/set",
                "assistant": assistant,
                "expose_new": expose_new,
            },
            f"Setting expose new entities for {assistant!r}"
            + (" (entity_id_parts already updated)" if "settings" in response else ""),
        )
    if parsed:
        response["expose_new_entities"] = parsed
        response["previous_expose_new_entities"] = previous_flags
    return response
