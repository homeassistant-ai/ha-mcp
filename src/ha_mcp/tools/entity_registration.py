"""Resolve scene and script storage keys to their registry entity IDs."""

import asyncio
import json
import logging
import time
from typing import Any, Literal

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_entity_not_found_error, create_error_response
from .component_config_reads import fetch_entity_lookup_via_component
from .helpers import exception_to_structured_error, raise_tool_error
from .scene_checks import leftover_step
from .ws_waiters import REGISTER_TIMEOUT

logger = logging.getLogger(__name__)

# A bounded registration budget, not a fixed delay: existing entries return on
# the first lookup. Keep this separate from the subsequent state-availability
# wait, which cannot discover a name-derived or registry-renamed entity ID.
RESOLVE_TIMEOUT = 5.0


def _matching_entity_id(
    entries: list[dict[str, Any]], storage_key: str, domain: Literal["scene", "script"]
) -> str | None:
    """UI scenes belong to platform homeassistant; scripts belong to script."""
    platform = "homeassistant" if domain == "scene" else "script"
    for entry in entries:
        entity_id = entry.get("entity_id") or ""
        if (
            entry.get("unique_id") == storage_key
            and entry.get("platform") == platform
            and entity_id.startswith(f"{domain}.")
        ):
            return str(entity_id)
    return None


LOOKUP_ERRORS = (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)


async def script_entity_id(
    client: Any, script_id: str, storage_key: str | None = None
) -> str | None:
    """The entity of the script ``script_id`` names, matched by its storage
    key: a renamed script's entity is not ``script.<key>``, and an
    unregistered ``script.<x>`` names the script stored under ``<x>``.
    None: the registry has no entity for the key. A failed registry read
    raises."""
    bare_id = script_id.removeprefix("script.")
    storage_key = storage_key or await client._resolve_script_id(script_id)
    if storage_key != bare_id:
        # Only the registry entry of script.<bare_id> resolves to another
        # key (HomeAssistantClient._resolve_script_id).
        return f"script.{bare_id}"
    # No guess: script.<key> can be another script's entity, and Core
    # registers every loaded script under its key.
    return await lookup_entity_id(client, storage_key, "script")


async def script_entity_or_warning(
    client: Any,
    script_id: str,
    storage_key: str | None,
    warnings: list[str],
    failure: str,
) -> str | None:
    """:func:`script_entity_id`, with a failed lookup added to ``warnings``
    as ``"<failure> for <script_id>: <error>"`` and None returned."""
    try:
        return await script_entity_id(client, script_id, storage_key)
    except LOOKUP_ERRORS as e:
        warnings.append(f"{failure} for {script_id}: {e}")
        return None


async def lookup_entity_id(
    client: Any, storage_key: str, domain: Literal["scene", "script"]
) -> str | None:
    """One lookup without retry; an unavailable component falls back to the
    legacy registry reads. None: the registry has no entry for the key. A
    failed registry read raises; no id is constructed."""
    matches = await fetch_entity_lookup_via_component(
        client, storage_key, domain=domain
    )
    if matches is None:
        if domain == "script":
            direct = await client.send_websocket_message(
                {
                    "type": "config/entity_registry/get",
                    "entity_id": f"script.{storage_key}",
                }
            )
            row = direct.get("result")
            if direct.get("success") and isinstance(row, dict):
                entity_id = _matching_entity_id([row], storage_key, domain)
                if entity_id is not None:
                    return entity_id
        listing = await client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
        if listing.get("success") is False:
            raise HomeAssistantAPIError(
                f"Entity registry lookup failed: {listing.get('error')} "
                f"(code={listing.get('error_code')})"
            )
        matches = listing.get("result") or []
    return _matching_entity_id(matches, storage_key, domain)


async def resolve_entity_id_after_write(
    client: Any,
    storage_key: str,
    domain: Literal["scene", "script"],
    *,
    timeout: float | None = None,
    poll_interval: float = 0.2,
) -> str | None:
    """Poll for the entity ID before post-write steps or a scene activation.

    An empty registry read can precede asynchronous registration even inside
    the component. Retry against one deadline, checking component availability
    on every attempt. A positive budget includes in-flight lookups and sleeps;
    zero requests one lookup without a time limit, for test fixtures. No entry
    within the budget returns None, also when the budget ends during a lookup
    or a sleep; no id is constructed, since ``<domain>.<key>`` can be another
    object's entity. A failed registry read raises; cancellation always
    propagates.

    Config reads, removals and script runs do not use this helper;
    ``post_write_entity`` skips it when a write requests no post-write step.
    The optional budget and interval support deterministic tests without
    extending production waits.
    """
    storage_key = storage_key.removeprefix(f"{domain}.")
    budget = RESOLVE_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + budget
    try:
        async with asyncio.timeout(budget if budget > 0 else None):
            while True:
                entity_id = await lookup_entity_id(client, storage_key, domain)
                if entity_id is not None:
                    return entity_id
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(poll_interval, remaining))
                if time.monotonic() >= deadline:
                    break
    except TimeoutError:
        # The timer cancels the lookup or sleep in flight at the deadline.
        pass
    logger.debug("Registry budget exhausted for %s.%s", domain, storage_key)
    return None


async def scene_entity_to_activate(client: Any, scene_id: str) -> str:
    """The entity a standalone activation of ``scene_id`` turns on.

    ``scene.<x>`` is taken as-is. A bare id is the registry entry of the scene
    with that id, wherever its YAML defines it. Without one, a key stored in
    ``scenes.yaml`` waits for its entry and activates nothing when none appears,
    never ``scene.<id>``, which can be another scene's; any other bare id
    becomes ``scene.<id>`` (an integration scene, or a YAML scene without an
    ``id``), and so does every bare id without an entry for a non-admin token,
    which Core does not let read ``scenes.yaml``. A failed registry, config or
    state read raises, an entity without a state is ENTITY_NOT_FOUND and an
    unavailable one (also the restored placeholder of a removed YAML scene)
    ENTITY_UNAVAILABLE.
    """
    if scene_id.startswith("scene."):
        entity_id: str | None = scene_id
    else:
        entity_id = await lookup_entity_id(client, scene_id, "scene")
        if entity_id is None:
            try:
                stored = await client._config_exists("scene", scene_id)
            except HomeAssistantAdminRequiredError:
                stored = False
            if stored:
                entity_id = await resolve_entity_id_after_write(
                    client, scene_id, "scene"
                )
            else:
                entity_id = f"scene.{scene_id}"
    if entity_id is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"The scene stored under key '{scene_id}' has no registered "
                "entity yet, so no scene was activated",
                suggestions=["Retry once Home Assistant has loaded the scene"],
                context={"scene_id": scene_id},
            )
        )
    await refuse_unactivatable_scene(client, entity_id)
    return entity_id


async def refuse_unactivatable_scene(client: Any, entity_id: str) -> None:
    """Raise when HA's ``scene.turn_on``, a silent no-op on an unknown or
    unavailable entity, would skip ``entity_id``: ENTITY_NOT_FOUND without a
    state (also a disabled entity), ENTITY_UNAVAILABLE when unavailable. A
    failed state read raises as it is."""
    try:
        state = await client.get_entity_state(entity_id)
    except HomeAssistantAPIError as e:
        if e.status_code == 404:
            raise_tool_error(create_entity_not_found_error(entity_id))
        raise
    if state.get("state") != "unavailable":
        return
    if state.get("attributes", {}).get("restored"):
        step = leftover_step(entity_id)
    else:
        step = "Retry once the integration that provides the scene is available"
    raise_tool_error(
        create_error_response(
            ErrorCode.ENTITY_UNAVAILABLE,
            f"Scene {entity_id} is unavailable; Home Assistant skips an "
            "unavailable scene, so nothing would be activated",
            context={"entity_id": entity_id},
            suggestions=[step],
        )
    )


async def activate_scene(
    client: Any, response: dict[str, Any], entity_id: str, *, standalone: bool
) -> None:
    """Call scene.turn_on; raise when standalone, warn after a config write.

    After a write, a scene HA would skip (:func:`refuse_unactivatable_scene`)
    is not activated either; a standalone caller has already refused it.
    """
    try:
        if not standalone:
            await refuse_unactivatable_scene(client, entity_id)
        await client.call_service("scene", "turn_on", {"entity_id": entity_id})
    except ToolError as exc:  # the write path's refusal
        reason: str | None = json.loads(str(exc))["error"]["message"]
    except LOOKUP_ERRORS as exc:
        if standalone:
            exception_to_structured_error(
                exc, context={"action": "activate", "entity_id": entity_id}
            )
        logger.warning("Scene %s not activated after config write: %s", entity_id, exc)
        reason = str(exc)
    else:
        reason = None
    response["activated"] = reason is None
    if reason is not None:
        response.setdefault("warnings", []).append(
            f"Scene config was written, but the scene could not be activated: {reason}"
        )


async def post_write_entity(
    client: Any,
    storage_key: str,
    domain: Literal["scene", "script"],
    result: dict[str, Any],
    *,
    wait: bool,
    category: str | None,
    activate: bool = False,
    caller_id: str | None = None,
) -> str | None:
    """Resolve the entity for the post-write steps (wait, category, activate).

    With none of them requested this returns None without a lookup. When no
    entity is found, a warning in ``result`` names the cause and the skipped
    steps, and a requested activation is reported as ``activated: False``.
    ``<domain>.<bare caller id>`` stands in, without a warning, only when the
    caller's id resolved to another storage key before the write: only the
    script write passes ``caller_id``, takes that key from the registry entry
    of ``script.<id>`` and refuses a bare id that names another key, so the
    registry named it as this script's entity.
    """
    steps = {"wait": wait, "category": category, "activate": activate}
    if not any(steps.values()):
        return None
    storage_key = storage_key.removeprefix(f"{domain}.")
    bare_id = (caller_id or storage_key).removeprefix(f"{domain}.")
    fallback = f"{domain}.{bare_id}" if bare_id != storage_key else None
    cause = f"the entity registry showed no entity for {domain} '{storage_key}' in time"
    try:
        # With a wait, the lookup also gets the state wait's budget on top of
        # its own; after a hit the state wait still runs its full budget.
        entity_id = await resolve_entity_id_after_write(
            client,
            storage_key,
            domain,
            timeout=RESOLVE_TIMEOUT + REGISTER_TIMEOUT if wait else None,
        )
    except LOOKUP_ERRORS as exc:
        logger.warning(
            "Post-write registry read for %s.%s failed: %r", domain, storage_key, exc
        )
        entity_id = None
        cause = (
            f"the entity registry read for {domain} '{storage_key}' failed ({exc!r})"
        )
    entity_id = entity_id or fallback
    if entity_id is None:
        result.setdefault("warnings", []).append(
            f"Saved, but {cause}, so these steps were skipped: "
            f"{', '.join(name for name, on in steps.items() if on)}; retry them later"
        )
        if activate:
            result["activated"] = False
    return entity_id
