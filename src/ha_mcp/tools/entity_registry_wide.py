"""Registry-wide reads and writes for ``ha_get_entity`` / ``ha_set_entity``.

Called without an entity, ``ha_get_entity`` reads Core's entity registry
settings (``config/entity_registry/settings/get``) and the exposed entities,
and ``ha_set_entity`` writes the settings and each assistant's "expose new
entities" flag. The tools check only the parameter shapes (an expose map,
which parameters go together) and send an empty entity_id_parts as null,
Core's default; every value is left to Core, and the replies carry Core's
settings as it returns them.
"""

from typing import Any

from ..client.rest_client import HomeAssistantCommandNotSent, HomeAssistantError
from ..errors import ErrorCode, create_error_response
from .entity_expose_batch import read_expose_new
from .entity_exposure import list_exposures, read_exposure
from .entity_param_parsing import _parse_expose_to_param
from .helpers import (
    exception_to_structured_error,
    raise_core_refusal,
    raise_tool_error,
)

_SETTINGS_GET = {"type": "config/entity_registry/settings/get"}


async def _registry_ws(
    client: Any,
    message: dict[str, Any],
    action: str,
    context: dict[str, Any] | None = None,
) -> Any:
    """Send one registry-wide command; Core's refusal or a failed send ends
    the call, with context in the error either way. A write (one sent with a
    context) that may have gone out without an answer names itself as
    ``write_outcome_unknown``."""
    try:
        reply = await client.send_websocket_message(message)
    except HomeAssistantError as exc:
        if context is not None and not isinstance(exc, HomeAssistantCommandNotSent):
            context = {**context, "write_outcome_unknown": action}
        exception_to_structured_error(exc, context=context)
    if not reply.get("success"):
        raise_core_refusal(f"{action} failed", reply, context)
    return reply.get("result")


async def get_registry_wide(client: Any, assistant: str | None) -> dict[str, Any]:
    """Core's registry settings and exposed entities, optionally for one assistant."""
    # Core judges the assistant name first, and its reply is the flag.
    expose_new = None if assistant is None else await read_expose_new(client, assistant)
    settings = await _registry_ws(
        client, _SETTINGS_GET, "Reading entity registry settings"
    )
    exposed_entities, entity_info, warning = await read_exposure(client)
    response = {
        "settings": settings,
        **list_exposures(exposed_entities, entity_info, assistant),
    }
    if warning:
        response["warnings"] = [warning]
    if assistant is not None:
        response["assistant"] = assistant
        response["expose_new_entities"] = expose_new
    return response


def is_registry_wide_call(
    entity_id: Any, params: dict[str, Any], entity_params: dict[str, Any]
) -> bool:
    """False when entity_id is given, True for a registry-wide call.

    Raises on a call that mixes the two kinds and on one with neither.
    """
    registry_wide = [k for k, v in params.items() if v is not None]
    if entity_id is not None:
        if registry_wide:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{', '.join(registry_wide)} are registry-wide and take no entity_id.",
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
                + (f" for {', '.join(given)}" if given else "")
                + f"; only {', '.join(params)} take none.",
            )
        )
    return True


async def set_registry_wide(
    client: Any,
    expose_new_entities: dict[str, bool] | None,
    entity_id_parts: list[str] | None,
) -> dict[str, Any]:
    """Write the registry-wide settings, reporting the values they replace.

    A failed write reports in its context what was already written and the
    values to restore.
    """
    parsed = _parse_expose_to_param(expose_new_entities, "expose_new_entities") or {}
    if not parsed and entity_id_parts is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "expose_new_entities names no assistant; nothing to change.",
            )
        )
    # Core judges each name, and its reply is the value to restore.
    previous: dict[str, Any] = {}
    if parsed:
        previous["previous_expose_new_entities"] = {
            a: await read_expose_new(client, a) for a in parsed
        }
    if entity_id_parts is not None:
        previous["previous_settings"] = await _registry_ws(
            client, _SETTINGS_GET, "Reading entity registry settings"
        )
    applied: list[str] = []
    context = {"applied": applied, **previous}
    response: dict[str, Any] = {"success": True, **previous}
    if entity_id_parts is not None:
        response["settings"] = await _registry_ws(
            client,
            {
                "type": "config/entity_registry/settings/update",
                "entity_id_parts": entity_id_parts or None,
            },
            "Updating entity registry settings",
            context,
        )
        applied.append("entity_id_parts")
    for assistant, expose_new in parsed.items():
        await _registry_ws(
            client,
            {
                "type": "homeassistant/expose_new_entities/set",
                "assistant": assistant,
                "expose_new": expose_new,
            },
            f"Setting expose new entities for {assistant!r}",
            context,
        )
        applied.append(f"expose_new_entities.{assistant}")
    if parsed:
        response["expose_new_entities"] = parsed
    return response
