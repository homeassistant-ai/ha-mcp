"""A scene ``id`` defined outside ``scenes.yaml``, read through the config tools.

Core's scene config API reads only ``scenes.yaml``; a YAML scene with an ``id``
in ``configuration.yaml`` still registers under that id. With the component
(``component_core_client``), its id names it, even while a stored scene holds
``scene.<id>``; without it (``core_client``) only the entity registry entry of
``scene.<id>`` and the config API answer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._component_frames import older_component
from ._config_dir_yaml import remove_yaml_only_scenes, setup_scenes, write_config_files
from ._tool_errors import error_body

_ON = {"sun.sun": "above_horizon"}

_CALLS: dict[str, Callable[[ConfigSceneTools], Awaitable[Any]]] = {
    "get": lambda t: t.ha_config_get_scene(scene_id="yid"),
    "set": lambda t: t.ha_config_set_scene(
        scene_id="yid",
        config={"name": "Rewritten", "entities": _ON},
        wait=False,
        MandatoryBPS=False,
    ),
    "remove": lambda t: t.ha_config_remove_scene(scene_id="yid"),
    "remove-entity": lambda t: t.ha_config_remove_scene(scene_id="scene.x_scene"),
}


@pytest.mark.parametrize(
    ("call", "collided", "disabled"),
    [
        ("get", False, False),
        ("set", False, False),
        ("remove", False, False),
        ("get", True, False),
        ("get", False, True),
        ("set", False, True),
        ("remove", False, True),
        ("remove-entity", False, True),
    ],
)
async def test_a_yaml_scene_id_names_that_scene(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    call: str,
    collided: bool,
    disabled: bool,
) -> None:
    """``yid`` is "X Scene" (``scene.x_scene``) in ``configuration.yaml``; with
    ``collided``, the stored scene ``zz`` named "Yid" holds ``scene.yid``; with
    ``disabled``, ``scene.x_scene`` is disabled and has no state. Each call
    names ``scene.x_scene`` as not editable, a disabled one with no activation
    hint, and leaves ``scenes.yaml`` as it was."""
    stored = [{"id": "zz", "name": "Yid", "entities": _ON}] if collided else []
    await setup_scenes(
        hass, stored, yaml_only=[{"id": "yid", "name": "X Scene", "entities": _ON}]
    )
    if disabled:
        er.async_get(hass).async_update_entity(
            "scene.x_scene", disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert hass.states.get("scene.x_scene") is None
    scenes_yaml = Path(hass.config.path("scenes.yaml"))
    before = await hass.async_add_executor_job(scenes_yaml.read_text)

    with pytest.raises(ToolError) as exc:
        await _CALLS[call](ConfigSceneTools(component_core_client))

    body = error_body(exc)
    assert body["error"]["code"] == "CONFIG_NOT_FOUND"
    suggestions = " ".join(body["error"].get("suggestions") or body["suggestions"])
    assert "'scene.x_scene'" in suggestions
    assert "scene.yid" not in suggestions
    assert ("activate=True" in suggestions) is not disabled
    assert ("is disabled" in body["error"]["message"]) is disabled
    assert await hass.async_add_executor_job(scenes_yaml.read_text) == before


@pytest.mark.parametrize(
    "routed_client", ["core_client", "component_core_client"], indirect=True
)
async def test_without_the_component_a_create_by_a_yaml_scene_id_says_so(
    hass: HomeAssistant,
    routed_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the component, or with one that does not report ``loaded``,
    nothing tells ``yid`` ("X Scene") from a new key: a set by ``yid`` stores
    it and warns that the id went unchecked."""
    older_component(monkeypatch, "entity_lookup_loaded")
    await setup_scenes(
        hass, [], yaml_only=[{"id": "yid", "name": "X Scene", "entities": _ON}]
    )

    result = await _CALLS["set"](ConfigSceneTools(routed_client))

    assert result["success"] is True
    assert any("ha_mcp_tools component" in w for w in result["warnings"])


async def test_without_the_component_an_edit_of_a_renamed_scene_does_not_warn(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Stored ``key_a`` named "First" holds ``scene.first``; a set by ``key_a``
    edits it, so no warning about an unchecked new id."""
    await setup_scenes(hass, [{"id": "key_a", "name": "First", "entities": _ON}])

    result = await ConfigSceneTools(core_client).ha_config_set_scene(
        scene_id="key_a",
        config={"name": "First", "entities": _ON},
        wait=False,
        MandatoryBPS=False,
    )

    assert result["success"] is True
    assert not any("ha_mcp_tools component" in w for w in result.get("warnings", []))


@pytest.mark.parametrize("call", ["set", "remove"])
@pytest.mark.parametrize("disabled", [False, True])
async def test_a_yaml_scene_whose_entity_is_its_id_is_not_written(
    hass: HomeAssistant, core_client: HomeAssistantClient, disabled: bool, call: str
) -> None:
    """``yid`` named "Yid" holds ``scene.yid``, live or disabled: a set or
    removal by ``yid`` is refused, saying when it is disabled, and leaves
    ``scenes.yaml`` as it was. Only a live scene's removal leads with the hint
    for a ``scene.create`` scene."""
    await setup_scenes(
        hass, [], yaml_only=[{"id": "yid", "name": "Yid", "entities": _ON}]
    )
    if disabled:
        er.async_get(hass).async_update_entity(
            "scene.yid", disabled_by=er.RegistryEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert hass.states.get("scene.yid") is None
    scenes_yaml = Path(hass.config.path("scenes.yaml"))
    before = await hass.async_add_executor_job(scenes_yaml.read_text)

    with pytest.raises(ToolError) as exc:
        await _CALLS[call](ConfigSceneTools(core_client))

    body = error_body(exc)
    assert body["error"]["code"] == "CONFIG_NOT_FOUND"
    assert ("is disabled" in body["error"]["message"]) is disabled
    first = (body["error"].get("suggestions") or body["suggestions"])[0]
    assert ("scene.create" in first) is (call == "remove" and not disabled)
    assert await hass.async_add_executor_job(scenes_yaml.read_text) == before


async def test_a_removal_by_the_entity_of_another_disabled_key_says_so(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """YAML scene ``other`` named "Yid" holds ``scene.yid`` and is disabled: a
    removal by ``yid`` names it as disabled, without an activation hint."""
    await setup_scenes(
        hass, [], yaml_only=[{"id": "other", "name": "Yid", "entities": _ON}]
    )
    er.async_get(hass).async_update_entity(
        "scene.yid", disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()

    with pytest.raises(ToolError) as exc:
        await _CALLS["remove"](ConfigSceneTools(core_client))

    body = error_body(exc)
    assert body["error"]["code"] == "CONFIG_NOT_FOUND"
    assert "is disabled" in body["error"]["message"]
    suggestions = " ".join(body["error"].get("suggestions") or body["suggestions"])
    assert "activate=True" not in suggestions


@pytest.mark.parametrize(
    ("name", "entity_id"), [("Gone Scene", "scene.gone_scene"), ("Gone", "scene.gone")]
)
async def test_the_registry_entry_of_a_removed_yaml_scene_blocks_no_create(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    name: str,
    entity_id: str,
) -> None:
    """``gone`` was a YAML scene; Core keeps its registry entry after it is
    removed and writes a ``restored`` state for it. A set by ``gone`` then
    stores a new scene under that key, also when the entity is ``scene.gone``;
    before that, a get by ``gone`` finds no scene, while a get by the entity_id
    still answers for the leftover entry."""
    await setup_scenes(
        hass, [], yaml_only=[{"id": "gone", "name": name, "entities": _ON}]
    )
    await remove_yaml_only_scenes(hass)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("scene", "homeassistant", "gone") == entity_id
    state = hass.states.get(entity_id)
    assert state is not None and state.attributes.get("restored") is True
    tools = ConfigSceneTools(component_core_client)
    with pytest.raises(ToolError) as exc:
        await tools.ha_config_get_scene(scene_id=entity_id)
    assert error_body(exc)["error"]["code"] == "CONFIG_NOT_FOUND"
    with pytest.raises(ToolError) as exc:
        await tools.ha_config_get_scene(scene_id="gone")
    assert error_body(exc)["error"]["code"] == "ENTITY_NOT_FOUND"

    result = await tools.ha_config_set_scene(
        scene_id="gone",
        config={"name": name, "entities": _ON},
        wait=False,
        MandatoryBPS=False,
    )

    assert "warnings" not in result
    stored = await component_core_client.get_scene_config("gone", _resolved=True)
    assert stored["config"]["name"] == name


@pytest.mark.parametrize(
    ("routed_client", "scene_id"),
    [
        ("component_core_client", "scene.gone"),
        ("core_client", "scene.gone"),
        ("core_client", "gone"),
    ],
    indirect=["routed_client"],
)
async def test_a_removed_yaml_scenes_leftover_entry_is_named_for_removal(
    hass: HomeAssistant, routed_client: HomeAssistantClient, scene_id: str
) -> None:
    """``gone`` ("Gone", ``scene.gone``) was a YAML scene; its restored entry
    gets no activation hint but the step to remove it. Without the component
    the bare id names that entry too."""
    await setup_scenes(
        hass, [], yaml_only=[{"id": "gone", "name": "Gone", "entities": _ON}]
    )
    await remove_yaml_only_scenes(hass)

    with pytest.raises(ToolError) as exc:
        await ConfigSceneTools(routed_client).ha_config_get_scene(scene_id=scene_id)

    body = error_body(exc)
    assert body["error"]["code"] == "CONFIG_NOT_FOUND"
    assert "restored state" in body["error"]["message"]
    suggestions = " ".join(body["error"].get("suggestions") or body["suggestions"])
    assert "ha_remove_entity(entity_id='scene.gone')" in suggestions
    assert "activate=True" not in suggestions


async def test_an_integration_scene_restored_at_startup_still_answers_by_its_slug(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A Hue scene ``evening`` whose bridge was offline at startup holds only a
    restored state; a get by ``evening`` still names it as an integration
    scene, with a retry instead of an activation hint."""
    await setup_scenes(hass, [])
    entry = er.async_get(hass).async_get_or_create(
        "scene", "hue", "evening", suggested_object_id="evening"
    )
    entry.write_unavailable_state(hass)

    with pytest.raises(ToolError) as exc:
        await ConfigSceneTools(component_core_client).ha_config_get_scene(
            scene_id="evening"
        )

    body = error_body(exc)
    assert body["error"]["code"] == "CONFIG_NOT_FOUND"
    assert body["platform"] == "hue"
    suggestions = " ".join(body["error"].get("suggestions") or body["suggestions"])
    assert "Retry once its integration has loaded" in suggestions
    assert "activate=True" not in suggestions


@pytest.mark.parametrize("call", ["get", "set"])
async def test_a_stored_scene_with_a_restored_state_still_answers_by_its_slug(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, call: str
) -> None:
    """Stored scene ``k1700`` named "Movie" holds ``scene.movie``; with
    ``scenes.yaml`` no longer included, Core restores its state. A get by
    ``movie`` still returns ``k1700`` and a set by ``movie`` writes nothing."""
    await setup_scenes(hass, [{"id": "k1700", "name": "Movie", "entities": _ON}])
    await write_config_files(hass, {"configuration.yaml": "homeassistant:\n"})
    await hass.services.async_call("scene", "reload", blocking=True)
    state = hass.states.get("scene.movie")
    assert state is not None and state.attributes.get("restored") is True
    scenes_yaml = Path(hass.config.path("scenes.yaml"))
    before = await hass.async_add_executor_job(scenes_yaml.read_text)
    tools = ConfigSceneTools(component_core_client)

    if call == "get":
        result = await tools.ha_config_get_scene(scene_id="movie")
        assert result["config"]["id"] == "k1700"
    else:
        with pytest.raises(ToolError) as exc:
            await tools.ha_config_set_scene(
                scene_id="movie",
                config={"name": "Movie", "entities": _ON},
                wait=False,
                MandatoryBPS=False,
            )
        assert error_body(exc)["error"]["code"] == "VALIDATION_FAILED"
    assert await hass.async_add_executor_job(scenes_yaml.read_text) == before


async def test_a_failed_state_read_is_no_create(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core answers 500 for ``scene.fresh``'s state (an attribute it cannot
    serialize): a set by ``fresh`` fails instead of storing a new scene."""
    await setup_scenes(hass, [])
    hass.states.async_set("scene.fresh", "on", {"unserializable": object()})

    with pytest.raises(ToolError):
        await ConfigSceneTools(core_client).ha_config_set_scene(
            scene_id="fresh",
            config={"name": "Fresh", "entities": _ON},
            wait=False,
            MandatoryBPS=False,
        )

    stored = await hass.async_add_executor_job(
        Path(hass.config.path("scenes.yaml")).read_text
    )
    assert "fresh" not in stored


_OWN_KEY_CALLS: dict[str, dict[str, Any]] = {
    "get": {},
    "set": {"config": {"name": "Mykey", "entities": _ON}},
    "hash": {"config": {"name": "Mykey", "entities": _ON}, "config_hash": True},
    "transform": {"python_transform": "config['name'] = 'Mykey'", "config_hash": True},
}
# The config reads each call makes of its own: the get's read, the set's
# existence check, the hash check's read, and the transform's read plus the
# re-read of the written config.
_OWN_READS = {"get": 1, "set": 1, "hash": 1, "transform": 2}


async def _config_reads_by_own_key(
    hass: HomeAssistant, client: HomeAssistantClient, call: str
) -> int:
    await setup_scenes(hass, [{"id": "mykey", "name": "Mykey", "entities": _ON}])
    tools = ConfigSceneTools(client)
    kwargs = dict(_OWN_KEY_CALLS[call])
    if kwargs.get("config_hash"):
        kwargs["config_hash"] = (await tools.ha_config_get_scene(scene_id="mykey"))[
            "config_hash"
        ]
    reads: list[str] = []

    async def count(request: httpx.Request) -> None:
        if request.method == "GET" and "/config/scene/config/" in request.url.path:
            reads.append(request.url.path)

    client.httpx_client.event_hooks["request"].append(count)
    if call == "get":
        await tools.ha_config_get_scene(scene_id="mykey")
    else:
        await tools.ha_config_set_scene(
            scene_id="mykey", wait=False, MandatoryBPS=False, **kwargs
        )
    return len(reads)


@pytest.mark.parametrize(
    "routed_client", ["component_core_client", "core_client"], indirect=True
)
@pytest.mark.parametrize("call", list(_OWN_KEY_CALLS))
async def test_a_stored_scenes_own_key_costs_no_extra_config_read(
    hass: HomeAssistant, routed_client: HomeAssistantClient, call: str
) -> None:
    """The component, or without it the registry entry of ``scene.mykey``,
    names ``mykey``'s own scene: resolving it reads no config."""
    reads = await _config_reads_by_own_key(hass, routed_client, call)
    assert reads == _OWN_READS[call]
