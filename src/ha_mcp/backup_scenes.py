"""The scene a scene snapshot holds, read and written back by its storage key.

A scene snapshot is filed under the id the tool call named, often the entity_id.
A rename of the entity since can leave that id naming no scene, or another
one; the storage key in the snapshot's config (``id``) does not change, so the
comparison, the restore's safety backup and the restore itself use it. Once no
scene is stored under that key the scene is gone, even when a bare key would
now name another scene's entity.
"""

from __future__ import annotations

from typing import Any

from .client.rest_client import HomeAssistantError


def scene_key(config: Any) -> str | None:
    """The storage key of the scene a snapshot holds: its config's ``id``."""
    key = config.get("id") if isinstance(config, dict) else None
    return key if isinstance(key, str) and key else None


async def current_scene_key(client: Any, entity_id: str, config: Any) -> str | None:
    """The storage key of the scene a snapshot holds, None once no scene is
    stored under it; the snapshot's own id for a config without one."""
    key = scene_key(config)
    if key is None:
        return entity_id
    return key if await client._config_exists("scene", key) else None


async def fetch_scene(client: Any, entity_id: str) -> Any:
    # A bare id is a storage key: its snapshot holds only the scene stored there.
    try:
        if not entity_id.startswith("scene."):
            return await client._request("GET", client.config_path("scene", entity_id))
        result = await client.get_scene_config(entity_id)
    except HomeAssistantError as err:
        if getattr(err, "status_code", None) == 404:
            return None
        raise
    return result.get("config", result) if isinstance(result, dict) else result


async def restore_scene(client: Any, entity_id: str, config: Any) -> Any:
    return await client.upsert_scene_config(
        config, entity_id, resolved_id=scene_key(config)
    )
