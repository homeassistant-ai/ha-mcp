"""Scenes on a real ``hass`` for the scene config tool tests.

Core's config API keeps stored scenes in ``scenes.yaml`` in the test's own
config directory. A ``yaml_only`` scene sits in ``configuration.yaml``, which
that API cannot read or edit, and an integration's scene is a registry entry
of another platform with a state, as a Hue scene is.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import entity_registration
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._config_dir_yaml import setup_scenes

TV_ON = {"input_boolean.tv": {"state": "on"}}
HUE_SCENE = "scene.bedroom_sleepy"


def scene(scene_id: str, name: str, **extra: Any) -> dict[str, Any]:
    """A scene that switches ``input_boolean.tv`` on."""
    return {"id": scene_id, "name": name, "entities": dict(TV_ON), **extra}


async def scene_tools(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    stored: Iterable[dict[str, Any]] = (),
    *,
    yaml_only: list[dict[str, Any]] | None = None,
) -> ConfigSceneTools:
    """The scene tools on a ``hass`` with ``stored`` scenes in scenes.yaml."""
    # Registry entries exist when a lookup runs; no need to wait for them.
    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 0)
    monkeypatch.setattr(ConfigSceneTools, "RESOLVE_RETRY_DELAY", 0)
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"tv": {}}}
    )
    await setup_scenes(hass, list(stored), yaml_only=yaml_only)
    return ConfigSceneTools(client)


async def stored_scenes(hass: HomeAssistant) -> list[dict[str, Any]]:
    """What Core's config API keeps in scenes.yaml."""
    path = Path(hass.config.path("scenes.yaml"))
    scenes = await hass.async_add_executor_job(load_yaml, path)
    assert isinstance(scenes, list)
    return scenes


def integration_scene(hass: HomeAssistant) -> str:
    """A scene another integration (``hue``) owns: Core's config API has no
    config for it."""
    entry = er.async_get(hass).async_get_or_create(
        "scene", "hue", "hue-uuid-123", suggested_object_id="bedroom_sleepy"
    )
    assert entry.entity_id == HUE_SCENE
    hass.states.async_set(HUE_SCENE, "unknown", {"friendly_name": "Bedroom Sleepy"})
    return HUE_SCENE


def category(hass: HomeAssistant, name: str, scope: str = "scene") -> str:
    """The id Core gives a new category in ``scope``."""
    return cr.async_get(hass).async_create(scope=scope, name=name).category_id


async def config_hash(tools: ConfigSceneTools, scene_id: str) -> str:
    """The hash a caller reads before editing a scene."""
    current = await tools.ha_config_get_scene(scene_id=scene_id)
    value: str = current["config_hash"]
    return value


def sent_of_type(sent: AsyncMock, command: str) -> list[dict[str, Any]]:
    """The WebSocket messages of type ``command`` the client sent."""
    return [
        call.args[0] for call in sent.call_args_list if call.args[0]["type"] == command
    ]


@contextmanager
def upserts(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """Spy on the scene write; Core still answers it."""
    with patch.object(
        client, "upsert_scene_config", wraps=client.upsert_scene_config
    ) as spy:
        yield spy


@contextmanager
def deletes(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """Spy on the scene delete; Core still answers it."""
    with patch.object(
        client, "delete_scene_config", wraps=client.delete_scene_config
    ) as spy:
        yield spy


@contextmanager
def config_reads(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """Spy on the scene config read; Core still answers it."""
    with patch.object(client, "get_scene_config", wraps=client.get_scene_config) as spy:
        yield spy


@contextmanager
def registered_after_first_list(
    hass: HomeAssistant, entity_id: str, unique_id: str
) -> Iterator[None]:
    """``entity_id`` takes ``unique_id`` only once Core has answered the first
    registry listing, as a scene Core is still registering does."""
    registry = er.async_get(hass)
    registry.async_update_entity(entity_id, new_unique_id=f"{unique_id}_pending")
    command = "config/entity_registry/list"
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]

    def answer_then_register(core: HomeAssistant, connection: Any, msg: Any) -> None:
        core_handler(core, connection, msg)
        entry = registry.async_get(entity_id)
        assert entry is not None
        if entry.unique_id != unique_id:
            registry.async_update_entity(entity_id, new_unique_id=unique_id)

    websocket_api.async_register_command(hass, command, answer_then_register, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)
