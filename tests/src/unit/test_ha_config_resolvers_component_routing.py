"""Routing of the scene entity resolver over the component's ``entity_lookup``.

Removing a scene needs its entity_id, which Core derives from the scene's name,
not from its storage key. The legacy resolve lists the whole entity registry
(with one retry); with the component one ``entity_lookup`` frame answers. An
empty answer right after a write can be Core's registration lag (#1168), so it
is rechecked once; an empty recheck falls back to ``scene.<key>``, and a
recheck that gets no answer falls to the legacy resolve instead of guessing.
Every miss of the component (no capability, a command it no longer knows, a
timeout, a socket that cannot be opened) runs the legacy resolve. Reading a
scene never asks the component (the config-get invariant).

The server's own client runs on a real ``hass``: the scene lives in Core's
``scenes.yaml`` and Core removes it. The tool waits for the entity it resolved
to disappear, so the entity_id whose state it polls is the one it resolved. A
spy on the pooled socket records the frames it sent.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from itertools import count
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import websocket_api as component_ws_api
from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import component_api, component_config_reads
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._client_spies import sent_commands
from ._config_dir_yaml import setup_scenes
from ._routing_core import (
    held_command,
    invalidations,
    pooled_socket,
    sent_frames,
    sent_types,
)

LOOKUP = "ha_mcp_tools/entity_lookup"
ENTITY_DUMP = "config/entity_registry/list"
SCENE_ID = "night_light_led_desk_strip"
# Core names the entity after the scene's name, not its storage key.
ENTITY_ID = "scene.led_desk_strip_night_light"
NAIVE_ID = f"scene.{SCENE_ID}"


class Removed(NamedTuple):
    lookups: list[dict[str, Any]]
    dumps: int
    polled: list[str]


@pytest.fixture
async def scene(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """The scene in ``scenes.yaml``, switching an input_boolean."""
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"tv": {}}}
    )
    await setup_scenes(
        hass,
        [
            {
                "id": SCENE_ID,
                "name": "LED Desk Strip Night Light",
                "entities": {"input_boolean.tv": "on"},
            }
        ],
    )
    assert er.async_get(hass).async_get_entity_id("scene", "homeassistant", SCENE_ID)


@pytest.fixture
def client(
    hass: HomeAssistant, scene: None, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    component_ws_api.async_register_commands(hass)
    return core_client


def _move_unique_id(hass: HomeAssistant, unique_id: str) -> None:
    """Point the scene's registry entry at ``unique_id``: the entry for the
    storage key is not (or, restored, is again) in the registry."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("scene", "homeassistant", SCENE_ID)
    entity_id = entity_id or registry.async_get_entity_id(
        "scene", "homeassistant", "registering"
    )
    assert entity_id is not None
    registry.async_update_entity(entity_id, new_unique_id=unique_id)


@contextmanager
def _before_each_lookup(
    hass: HomeAssistant, before: Callable[[int], None]
) -> Iterator[None]:
    """Run ``before(n)`` as Core receives the n-th entity_lookup, then let the
    component answer it."""
    core_handler, schema = hass.data[websocket_api.DOMAIN][LOOKUP]
    calls = count()

    @callback
    def observe(
        core: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        before(next(calls))
        core_handler(core, connection, msg)

    websocket_api.async_register_command(hass, LOOKUP, observe, schema)
    yield


async def _remove(hass: HomeAssistant, client: HomeAssistantClient) -> Removed:
    socket = await pooled_socket(client)
    with (
        sent_commands(socket) as sent,
        patch.object(
            client, "get_entity_state", AsyncMock(wraps=client.get_entity_state)
        ) as states,
    ):
        resp = await ConfigSceneTools(client).ha_config_remove_scene(
            scene_id=SCENE_ID, wait=True
        )
    assert resp["success"] is True
    # Core reloads its scenes after the delete; let that finish too.
    await hass.async_block_till_done()
    return Removed(
        sent_frames(sent, LOOKUP),
        sent_types(sent).count(ENTITY_DUMP),
        [str(call.args[0]) for call in states.call_args_list],
    )


async def test_one_component_frame_resolves_the_scene_without_a_registry_dump(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """A hit is not rechecked, so no settle delay is paid for it."""
    removed = await _remove(hass, client)

    assert removed.lookups == [{"unique_id": SCENE_ID, "domain": "scene"}]
    assert removed.dumps == 0
    assert ENTITY_ID in removed.polled
    assert hass.states.get(ENTITY_ID) is None


async def test_an_empty_lookup_is_rechecked_once_and_finds_a_late_registration(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The entry lands in the registry between the two reads (#1168): the real
    entity_id, not the ``scene.<key>`` guess, and no registry dump."""
    _move_unique_id(hass, "registering")

    def land(n: int) -> None:
        if n == 1:
            _move_unique_id(hass, SCENE_ID)

    with _before_each_lookup(hass, land):
        removed = await _remove(hass, client)

    assert len(removed.lookups) == 2
    assert removed.dumps == 0
    assert ENTITY_ID in removed.polled
    assert NAIVE_ID not in removed.polled


async def test_an_empty_recheck_falls_back_to_the_storage_key_guess(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The component said twice that no entity has the key: that answer is
    authoritative, so the registry is not dumped."""
    _move_unique_id(hass, "elsewhere")

    removed = await _remove(hass, client)

    assert len(removed.lookups) == 2
    assert removed.dumps == 0
    assert NAIVE_ID in removed.polled


async def test_an_unanswered_recheck_falls_back_to_the_registry_not_a_guess(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The first empty answer is no verdict once the recheck gets none: the
    legacy registry list resolves the scene."""
    _move_unique_id(hass, "registering")
    lookups = count()

    def hold_the_recheck(msg: dict[str, Any]) -> bool:
        if next(lookups) == 0:
            return False
        _move_unique_id(hass, SCENE_ID)
        return True

    async with held_command(hass, client, LOOKUP, when=hold_the_recheck):
        removed = await _remove(hass, client)

    assert len(removed.lookups) == 2
    assert removed.dumps == 1
    assert ENTITY_ID in removed.polled
    assert NAIVE_ID not in removed.polled


async def test_reading_a_scene_never_asks_the_component(
    client: HomeAssistantClient,
) -> None:
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await ConfigSceneTools(client).ha_config_get_scene(scene_id=SCENE_ID)

    assert resp["success"] is True
    types = sent_types(sent)
    assert LOOKUP not in types
    assert types.count(ENTITY_DUMP) == 1


async def test_a_component_without_the_capability_leaves_it_to_the_registry(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        system,
        "CAPABILITIES",
        [c for c in system.CAPABILITIES if c != "entity_lookup"],
    )

    removed = await _remove(hass, client)

    assert removed.lookups == []
    assert removed.dumps == 1
    assert ENTITY_ID in removed.polled


async def test_a_component_without_the_command_is_invalidated_and_core_resolves(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await component_api.get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], LOOKUP)

    with invalidations(component_config_reads) as invalidate:
        removed = await _remove(hass, client)

    assert removed.dumps == 1
    assert ENTITY_ID in removed.polled
    invalidate.assert_called_once_with(client)


async def test_a_timed_out_lookup_falls_back_and_keeps_the_capability(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    async with held_command(hass, client, LOOKUP):
        with invalidations(component_config_reads) as invalidate:
            removed = await _remove(hass, client)

    assert removed.dumps == 1
    assert ENTITY_ID in removed.polled
    invalidate.assert_not_called()


async def test_an_unreachable_component_socket_falls_back_to_the_registry(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """An escaping error would report a landed removal as failed; the legacy
    resolve rides the WebSocket bridge, not the component's socket."""
    assert await component_api.get_component_caps(client) is not None

    with (
        patch.object(
            component_config_reads,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
        ),
        invalidations(component_config_reads) as invalidate,
    ):
        removed = await _remove(hass, client)

    assert removed.dumps == 1
    assert ENTITY_ID in removed.polled
    invalidate.assert_not_called()
