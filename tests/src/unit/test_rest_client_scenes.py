"""The client's scene config reads, writes and removals, on a real ``hass``.

Core's config API keeps only the scenes in scenes.yaml, under their id. A scene
renamed in the UI keeps its id under a new entity_id, so the client resolves an
id through Core's entity registry first; the registry also tells a scene of
another integration, which the config API cannot edit, from a missing one
(#1971).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
    SceneResolution,
    SceneStorageConfigNotFoundError,
)

from ._command_faults import dropped_on, pooled_socket
from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client
from ._rest_client_config_core import (
    config_client as config_client,
)
from ._rest_client_config_core import error_status, load, write_config_file

ENTITIES = {"light.kitchen": {"state": "on", "brightness": 200}}
MOVIE_NIGHT = {"id": "movie_night", "name": "Movie night", "entities": ENTITIES}
# Created as "Renamed scene" under another id: entity_id and id differ.
RENAMED = {"id": "original_storage_key", "name": "Renamed scene", "entities": ENTITIES}


async def _stored(client: HomeAssistantClient, storage_key: str) -> dict[str, Any]:
    result = await client.get_scene_config(storage_key, _resolved=True)
    config: dict[str, Any] = result["config"]
    return config


def _hue_scene(entity_registry: er.EntityRegistry) -> None:
    """``scene.bedroom_sleepy``, a scene of the Hue integration."""
    entity_registry.async_get_or_create(
        "scene", "hue", "hue-uuid-123", suggested_object_id="bedroom_sleepy"
    )


# --- removal ----------------------------------------------------------------------


async def test_removing_a_scene_removes_its_config(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await load(hass, "scene", [MOVIE_NIGHT])

    result = await config_client.delete_scene_config("movie_night")

    assert result["success"] is True
    assert (result["scene_id"], result["operation"]) == ("movie_night", "deleted")
    assert await error_status(_stored(config_client, "movie_night")) == 404


async def test_removing_a_missing_scene_reports_not_found(
    config_client: HomeAssistantClient,
) -> None:
    """Core answers a removal of a missing id with a 400; passed on as one,
    the tool reports a failed request instead of a missing scene."""
    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.delete_scene_config("nonexistent_scene")

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value).lower()


async def test_removing_another_integrations_scene_says_it_is_not_editable(
    config_client: HomeAssistantClient, entity_registry: er.EntityRegistry
) -> None:
    """A Hue scene exists; reported as a failed request, the caller cannot
    tell it from a broken one."""
    _hue_scene(entity_registry)

    with pytest.raises(SceneStorageConfigNotFoundError) as exc_info:
        await config_client.delete_scene_config("bedroom_sleepy")

    assert (exc_info.value.platform, exc_info.value.scene_id) == (
        "hue",
        "bedroom_sleepy",
    )


async def test_a_proxy_refusing_removal_explains_the_way_around_it(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    gateway: Gateway,
    gateway_client: HomeAssistantClient,
) -> None:
    """Supervisor's ingress proxy answers DELETE with 405."""
    await load(hass, "scene", [MOVIE_NIGHT])
    gateway.outages.append(405)

    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await gateway_client.delete_scene_config("movie_night")

    assert exc_info.value.status_code == 405
    message = str(exc_info.value).lower()
    for hint in ("cannot delete", "add-on", "supervisor", "yaml", "workaround"):
        assert hint in message
    assert "delete_movie_night" in message
    assert gateway.requests == ["DELETE /api/config/scene/config/movie_night"]


async def test_a_server_error_on_removal_is_not_reported_as_not_found(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await write_config_file(hass, "scenes.yaml", "- [unclosed\n")

    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.delete_scene_config("movie_night")

    assert exc_info.value.status_code == 500


async def test_a_renamed_scene_is_removed_under_its_storage_key(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await load(hass, "scene", [RENAMED])

    result = await config_client.delete_scene_config("renamed_scene")

    assert result["scene_id"] == "original_storage_key"
    assert await error_status(_stored(config_client, "original_storage_key")) == 404


async def test_a_resolution_given_by_the_caller_is_removed_without_a_lookup(
    hass: HomeAssistant,
    config_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    sent: AsyncMock,
) -> None:
    await load(hass, "scene", [MOVIE_NIGHT])
    entry = entity_registry.async_get("scene.movie_night")
    assert entry is not None
    resolution = SceneResolution(
        storage_key=entry.unique_id, registry_hit=True, platform=entry.platform
    )

    result = await config_client.delete_scene_config(
        "movie_night", resolution=resolution
    )

    assert result["scene_id"] == "movie_night"
    sent.assert_not_awaited()
    assert await error_status(_stored(config_client, "movie_night")) == 404


# --- reading ------------------------------------------------------------------------


async def test_reading_a_scene_returns_cores_config(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    await load(hass, "scene", [MOVIE_NIGHT])

    result = await config_client.get_scene_config("movie_night")

    assert (result["success"], result["scene_id"]) == (True, "movie_night")
    assert result["config"]["entities"] == ENTITIES


async def test_reading_a_missing_scene_is_plainly_not_found(
    config_client: HomeAssistantClient,
) -> None:
    """Not the not-editable case: no entity of that id exists anywhere."""
    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await config_client.get_scene_config("truly_missing")

    assert not isinstance(exc_info.value, SceneStorageConfigNotFoundError)
    assert exc_info.value.status_code == 404


async def test_reading_another_integrations_scene_says_it_is_not_editable(
    config_client: HomeAssistantClient, entity_registry: er.EntityRegistry
) -> None:
    _hue_scene(entity_registry)

    with pytest.raises(SceneStorageConfigNotFoundError) as exc_info:
        await config_client.get_scene_config("bedroom_sleepy")

    error = exc_info.value
    assert error.status_code == 404
    assert (error.platform, error.scene_id, error.storage_key) == (
        "hue",
        "bedroom_sleepy",
        "hue-uuid-123",
    )


async def test_reading_a_yaml_scene_without_an_id_says_it_is_not_editable(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    """A YAML scene with no ``id`` has no registry row but has a state."""
    hass.states.async_set("scene.yaml_only", "unknown")

    with pytest.raises(SceneStorageConfigNotFoundError) as exc_info:
        await config_client.get_scene_config("yaml_only")

    assert (exc_info.value.platform, exc_info.value.scene_id) == (None, "yaml_only")


async def test_a_resolved_storage_key_is_read_without_a_lookup(
    hass: HomeAssistant, config_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    await load(hass, "scene", [RENAMED])

    result = await config_client.get_scene_config(
        "original_storage_key", _resolved=True
    )

    assert result["config"]["name"] == RENAMED["name"]
    sent.assert_not_awaited()


# --- writing ------------------------------------------------------------------------


async def test_a_scene_with_entities_is_written(
    config_client: HomeAssistantClient,
) -> None:
    result = await config_client.upsert_scene_config(
        {"name": "Test scene", "entities": ENTITIES}, "test_scene"
    )

    assert (result["success"], result["scene_id"]) == (True, "test_scene")
    assert (await _stored(config_client, "test_scene"))["entities"] == ENTITIES


async def test_a_scene_without_entities_is_refused(
    config_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        await config_client.upsert_scene_config({"name": "Empty"}, "test_scene")

    assert "entities" in str(exc_info.value)
    assert await error_status(_stored(config_client, "test_scene")) == 404


async def test_a_scene_without_a_name_is_named_after_its_id(
    config_client: HomeAssistantClient,
) -> None:
    await config_client.upsert_scene_config({"entities": ENTITIES}, "test_scene")

    assert (await _stored(config_client, "test_scene"))["name"] == "test_scene"


async def test_a_resolved_storage_key_is_written_without_a_lookup(
    config_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    result = await config_client.upsert_scene_config(
        {"name": "S", "entities": ENTITIES}, "renamed_scene", resolved_id="storage_key"
    )

    assert result["scene_id"] == "storage_key"
    sent.assert_not_awaited()
    assert (await _stored(config_client, "storage_key"))["name"] == "S"


@pytest.mark.parametrize(
    ("scene_id", "resolved_id", "name"),
    [
        ("new_slug", "old_storage_key", "new_slug"),
        ("scene.movie_night", "movie_night", "movie_night"),
    ],
    ids=["renamed-scene", "entity-id-form"],
)
async def test_a_scene_without_a_name_keeps_the_callers_slug(
    config_client: HomeAssistantClient, scene_id: str, resolved_id: str, name: str
) -> None:
    """Core makes the entity_id from the name: named after the storage key
    or the ``scene.`` form, the update would rename the scene (#1935)."""
    await config_client.upsert_scene_config(
        {"entities": ENTITIES}, scene_id, resolved_id=resolved_id
    )

    assert (await _stored(config_client, resolved_id))["name"] == name


# --- resolving an id ----------------------------------------------------------------


@pytest.mark.parametrize("identifier", ["renamed_scene", "scene.renamed_scene"])
async def test_a_renamed_scene_resolves_to_its_storage_key(
    hass: HomeAssistant, config_client: HomeAssistantClient, identifier: str
) -> None:
    await load(hass, "scene", [RENAMED])

    assert await config_client.resolve_scene_id(identifier) == "original_storage_key"


async def test_an_id_the_registry_does_not_know_resolves_to_itself(
    config_client: HomeAssistantClient,
) -> None:
    assert await config_client.resolve_scene_id("ghost_scene") == "ghost_scene"


async def test_no_answer_to_the_registry_lookup_is_not_guessed_past(
    hass: HomeAssistant, config_client: HomeAssistantClient
) -> None:
    """Guessing the storage key from the slug writes a renamed scene's update
    to a new scene (#1947)."""
    await load(hass, "scene", [RENAMED])
    socket = await pooled_socket(config_client)

    with (
        dropped_on(hass, "config/entity_registry/get", socket),
        pytest.raises(HomeAssistantConnectionError),
    ):
        await config_client.resolve_scene_id("renamed_scene")
