"""Refusals of the scene tools: the write checks return their problems,
``raise_problems`` reports all of a call's problems in one response, and the
``raise_*`` refusals refuse at once."""

from __future__ import annotations

import json
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from .config_helpers.registry import validate_registry_ids
from .helpers import raise_tool_error


def scene_body_problems(
    config: dict[str, Any], storage_key: Any, action: str
) -> list[dict[str, Any]]:
    """A config Core would not store as this scene under ``storage_key``.

    Core writes the body over the entry at the URL key but keeps the body's
    ``id``, so a different id moves the scene to it: the registry entry under
    the old key is orphaned and the scene reloads as a new entity (#1168).
    Renaming is remove + create. ``id`` is optional and ``None`` means the
    same as leaving it out, but Core refuses ``None``, so it is dropped.

    A ``platform`` key is refused too: Core then stores the body as a scene
    platform, loading only the scenes its ``states`` lists, not this scene.
    """
    problems = []
    if "platform" in config:
        problems.append(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                "config is one scene, not a 'platform' stanza; Core would store "
                "it as a list of scenes under this key instead of as this scene",
                suggestions=["Drop 'platform' and 'states'; pass name and entities"],
                context={"action": action, "scene_id": storage_key},
            )
        )
    if "id" in config and config["id"] is None:
        del config["id"]
    if "id" in config and config["id"] != storage_key:
        problems.append(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                f"config['id'] {config['id']!r} differs from the scene's storage "
                f"key {storage_key!r}; Core would store the scene under the new id",
                suggestions=[
                    f"Keep config['id'] as {storage_key!r}, or leave it out",
                    "To rename a scene, use ha_config_remove_scene + ha_config_set_scene",
                ],
                context={
                    "action": action,
                    "scene_id": storage_key,
                    "attempted_id": config["id"],
                },
            )
        )
    return problems


def raise_scene_id_refusal(
    identifier: str, storage_key: str, *, core: str = ""
) -> NoReturn:
    """``identifier`` is ``scene.<storage_key>``: no such entity is registered,
    while that storage key holds a scene. ``core`` is Core's refusal, which
    then leads the message."""
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_FAILED,
            f"{_lead(core)}{identifier} is no registered entity, while storage key "
            f"'{storage_key}' holds a scene; scene.<x> is read as an entity_id, "
            f"and the bare '{storage_key}' names the scene stored under that key",
            suggestions=[
                f"Pass '{storage_key}' for the scene stored under that key",
                "Pass the entity_id of the scene you mean; ha_search lists scenes",
                "To create a new scene, pass a storage key no scene uses",
            ],
            context={"scene_id": identifier, "storage_key": storage_key},
        )
    )


def entity_form_create_problems(
    scene_id: str, storage_key: str
) -> list[dict[str, Any]]:
    """A new scene asked for by entity_id. Core names the new entity after the
    scene's name, not after ``scene_id``, so the scene is created by storage key."""
    if scene_id == storage_key:
        return []
    return [
        create_error_response(
            ErrorCode.VALIDATION_FAILED,
            f"No scene {scene_id} exists; a new scene is created by storage key, "
            "and Core names its entity after the scene's name",
            suggestions=[
                f"Pass '{storage_key}' to create the scene under that storage key"
            ],
            context={"scene_id": scene_id, "storage_key": storage_key},
        )
    ]


async def category_problems(client: Any, category: str | None) -> list[dict[str, Any]]:
    """An unknown category, from ``category`` or ``config["category"]``: Core
    attaches any id unchecked, so it is refused before the upsert (#1168 R6/R8).
    A registry that cannot be read refuses the call at once (fail-closed)."""
    if not category:
        return []
    try:
        await validate_registry_ids(
            client, None, None, {"scene": category}, fail_closed=True
        )
    except ToolError as e:
        problem = json.loads(str(e))
        if problem["error"]["code"] != ErrorCode.VALIDATION_INVALID_PARAMETER:
            raise
        return [problem]
    return []


def raise_problems(problems: list[dict[str, Any]]) -> None:
    """Refuse the call when there are problems: one keeps its own response,
    several become one VALIDATION_FAILED listing each under ``problems``."""
    if not problems:
        return
    if len(problems) == 1:
        raise_tool_error(problems[0])
    errors = [p["error"] for p in problems]
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_FAILED,
            "; ".join(e["message"] for e in errors),
            suggestions=[
                s
                for e in errors
                for s in e.get("suggestions") or [e.get("suggestion")]
                if s
            ],
            context={
                "problems": [
                    {
                        "code": p["error"]["code"],
                        "message": p["error"]["message"],
                        "context": {
                            k: v for k, v in p.items() if k not in ("success", "error")
                        },
                    }
                    for p in problems
                ]
            },
        )
    )


def raise_bare_id_of_another_key(scene_id: str, storage_key: str) -> NoReturn:
    """No scene is stored under the bare ``scene_id``, while ``scene.<scene_id>``
    is the scene stored under ``storage_key``: the mirror of
    :func:`raise_scene_id_refusal`. A delete never falls back to that scene."""
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"No scene is stored under key '{scene_id}', so nothing was deleted; "
            f"scene.{scene_id} is the scene stored under '{storage_key}'",
            suggestions=[
                f"Pass 'scene.{scene_id}' to delete that scene",
                "Use ha_search(domain_filter='scene') to find storage keys",
            ],
            context={"scene_id": scene_id, "storage_key": storage_key},
        )
    )


def _lead(core: str) -> str:
    return f"{core.rstrip('.')}. " if core else ""


def raise_scene_not_storage_error(
    scene_id: str, platform: str | None, *, remove: bool = False, core: str = ""
) -> NoReturn:
    """Raise ``CONFIG_NOT_FOUND`` for a scene that exists but has no editable config.

    The scene entity was confirmed to exist - in the entity registry, or, for an
    ``id``-less YAML scene that has no registry entry, in the state machine - but
    ``config/scene/config/{id}``, backed only by the managed ``scenes.yaml``, has
    no entry for it. That is the not-a-storage-scene case (a Hue/vendor scene, a
    scene defined directly in YAML, or one made by ``scene.create``): the entity
    EXISTS, so ``ENTITY_NOT_FOUND`` would be wrong (issue #1971). ``platform``
    names the owning integration when the registry exposed it; the message stays
    generic otherwise. With ``remove`` and a bare ``scene_id`` on that generic
    branch, the first suggestion names the entity_id form, by which a
    ``scene.create`` scene is deleted. ``core`` is Core's refusal, which then
    leads the message.
    """
    entity_id = f"scene.{scene_id.removeprefix('scene.')}"
    suggestions = [
        f"Activate it with ha_config_set_scene(scene_id='{entity_id}', activate=True).",
    ]
    if platform and platform != "homeassistant":
        platform_note = (
            f" It is provided by the '{platform}' integration, which manages its "
            "own scenes outside Home Assistant's editable scene storage."
        )
        suggestions.append(
            f"Edit this scene in the '{platform}' integration or its companion app; "
            "ha_config_get_scene / ha_config_set_scene only edit Home Assistant "
            "UI/storage scenes."
        )
    else:
        platform_note = ""
        suggestions.append(
            "ha_config_get_scene / ha_config_set_scene only read and edit Home "
            "Assistant UI/storage scenes (Settings > Automations & Scenes > Scenes). "
            "Scenes defined directly in YAML or made by scene.create are not "
            "editable through this API."
        )
        if remove and not scene_id.startswith("scene."):
            suggestions.insert(
                0,
                f"Pass '{entity_id}' to delete it if scene.create made it; a bare "
                "id names a storage key only.",
            )
    raise_tool_error(
        create_error_response(
            ErrorCode.CONFIG_NOT_FOUND,
            f"{_lead(core)}Scene '{entity_id}' exists but has no editable Home Assistant "
            f"storage configuration.{platform_note}",
            suggestions=suggestions,
            context={"scene_id": scene_id, "platform": platform},
        )
    )
