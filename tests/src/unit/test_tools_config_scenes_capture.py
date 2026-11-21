"""``ha_config_set_scene`` capturing current states into a scene, as the
scene editor does: ``capture_entities`` per entity, ``capture_devices`` per
device, with the states read over REST or in one component frame.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import hass_frontend
import pytest
from homeassistant.core import HomeAssistant, StateMachine
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import scene_editor
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import scene_capture
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._tools_config_scenes_core import (
    TV_ON,
    config_hash,
    scene,
    scene_tools,
    set_refused,
    stored_scene,
    stored_scenes,
    upserts,
)


@pytest.fixture
async def tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> ConfigSceneTools:
    """``test_scene`` named "Test Scene" (so ``scene.test_scene``) is stored."""
    return await scene_tools(
        hass, core_client, monkeypatch, [scene("test_scene", "Test Scene")]
    )


class TestCapture:
    """``capture_entities`` stores entities' current states, as the scene
    editor's live edit does."""

    async def test_a_captured_state_replaces_the_configs_entry(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        tv = hass.states.get("input_boolean.tv")
        assert tv is not None
        assert tv.state != TV_ON["input_boolean.tv"]["state"]

        await tools.ha_config_set_scene(
            scene_id="movie",
            config={"name": "Movie", "entities": dict(TV_ON)},
            capture_entities=["input_boolean.tv"],
            wait=False,
        )

        stored = await stored_scene(hass, "movie")
        assert stored["entities"] == {
            "input_boolean.tv": {**tv.attributes, "state": tv.state}
        }

    async def test_a_config_without_entities_gets_the_captured_ones(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        hass.states.async_set("light.desk", "on", {"brightness": 120})

        await tools.ha_config_set_scene(
            scene_id="movie",
            config={"name": "Movie"},
            capture_entities=["light.desk"],
            wait=False,
        )

        stored = await stored_scene(hass, "movie")
        assert stored["entities"] == {"light.desk": {"brightness": 120, "state": "on"}}

    async def test_an_added_entity_is_marked_entity_only_and_others_keep_theirs(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        """The editor marks an entity picked on its own ``entity_only``; the
        metadata of an entity already in the scene is the caller's."""
        hass.states.async_set("light.desk", "on")

        await tools.ha_config_set_scene(
            scene_id="movie",
            config={
                "name": "Movie",
                "entities": dict(TV_ON),
                "metadata": {"input_boolean.tv": {"entity_only": False}},
            },
            capture_entities=["input_boolean.tv", "light.desk"],
            wait=False,
        )

        stored = await stored_scene(hass, "movie")
        assert stored["metadata"] == {
            "input_boolean.tv": {"entity_only": False},
            "light.desk": {"entity_only": True},
        }

    async def test_a_new_entity_picked_in_another_case_is_entity_only(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        """Core names the light ``light.desk``; the editor's mark lands on that
        key whatever case the caller used."""
        hass.states.async_set("light.desk", "on")

        await tools.ha_config_set_scene(
            scene_id="movie",
            config={"name": "Movie"},
            capture_entities=["LIGHT.DESK"],
            wait=False,
        )

        movie = await stored_scene(hass, "movie")
        assert movie["metadata"] == {"light.desk": {"entity_only": True}}

    @pytest.mark.parametrize(
        ("stored", "captured"),
        [("light.desk", "LIGHT.DESK"), ("LIGHT.DESK", "light.desk")],
    )
    async def test_a_differently_cased_id_recaptures_the_entry_in_place(
        self, hass: HomeAssistant, tools: ConfigSceneTools, stored: str, captured: str
    ) -> None:
        """Core names the light ``light.desk`` whatever the case asked; the
        capture replaces the stored entry and keeps its metadata instead of
        adding a second one."""
        hass.states.async_set("light.desk", "on", {"brightness": 180})

        await tools.ha_config_set_scene(
            scene_id="movie",
            config={
                "name": "Movie",
                "entities": {stored: {"state": "off", "brightness": 5}},
                "metadata": {stored: {"entity_only": False}},
            },
            capture_entities=[captured],
            wait=False,
        )

        movie = await stored_scene(hass, "movie")
        assert movie["entities"] == {"light.desk": {"brightness": 180, "state": "on"}}
        assert movie["metadata"] == {"light.desk": {"entity_only": False}}

    async def test_a_recapture_with_a_fresh_hash_stores_the_live_state(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        """The recapture the 'requires config' refusal suggests: the config and hash
        from ``ha_config_get_scene`` plus ``capture_entities``."""
        hass.states.async_set("light.desk", "on", {"brightness": 77})
        await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={"name": "Test Scene"},
            capture_entities=["light.desk"],
            wait=False,
        )
        current = await tools.ha_config_get_scene(scene_id="test_scene")
        current["config"]["metadata"]["light.desk"] = {"entity_only": False}
        hass.states.async_set("light.desk", "on", {"brightness": 150})

        await tools.ha_config_set_scene(
            scene_id="test_scene",
            config=current["config"],
            config_hash=current["config_hash"],
            capture_entities=["light.desk"],
            wait=False,
        )

        stored = await stored_scene(hass, "test_scene")
        assert stored["entities"] == {"light.desk": {"brightness": 150, "state": "on"}}
        assert stored["metadata"] == {"light.desk": {"entity_only": False}}
        assert await config_hash(tools, "test_scene") != current["config_hash"]

    async def test_a_recapture_with_a_stale_hash_is_refused_unwritten(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        hass.states.async_set("light.desk", "on", {"brightness": 77})
        stale = await tools.ha_config_get_scene(scene_id="test_scene")
        await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={"name": "Edited", "entities": TV_ON},
            wait=False,
        )
        before = await stored_scenes(hass)

        body = await set_refused(
            tools,
            config=stale["config"],
            config_hash=stale["config_hash"],
            capture_entities=["light.desk"],
        )

        assert "current_config_hash" in body
        assert await stored_scenes(hass) == before

    async def test_independent_refusals_come_in_one_response(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        before = await stored_scenes(hass)
        body = await set_refused(
            tools,
            config={"id": "other", "platform": "homeassistant", "name": "Test Scene"},
            capture_entities=["light.nope_missing"],
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        problems = body["problems"]
        assert [p["code"] for p in problems] == [
            "VALIDATION_FAILED",
            "VALIDATION_FAILED",
            "ENTITY_NOT_FOUND",
        ]
        assert "'platform'" in problems[0]["message"]
        assert "'other'" in problems[1]["message"]
        assert problems[2]["context"]["entity_ids"] == ["light.nope_missing"]
        suggestions = body["error"]["suggestions"]
        assert "Drop 'platform' and 'states'; pass name and entities" in suggestions
        assert "Use ha_search to find the entity_ids" in suggestions
        assert await stored_scenes(hass) == before

    @pytest.mark.parametrize("capture", ["capture_entities", "capture_devices"])
    async def test_an_empty_capture_list_without_config_is_refused_unwritten(
        self, hass: HomeAssistant, tools: ConfigSceneTools, capture: str
    ) -> None:
        before = await stored_scenes(hass)
        hash_value = await config_hash(tools, "test_scene")
        body = await set_refused(
            tools,
            python_transform="config['icon'] = 'mdi:movie'",
            config_hash=hash_value,
            **{capture: []},
        )

        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert await stored_scenes(hass) == before

    async def test_capture_into_list_shaped_entities_leads_with_cores_refusal(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        before = await stored_scenes(hass)
        body = await set_refused(
            tools,
            scene_id="movie",
            config={"name": "Movie", "entities": [{"entity_id": "input_boolean.tv"}]},
            capture_entities=["input_boolean.tv"],
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "expected dict" in body["error"]["message"]
        assert await stored_scenes(hass) == before

    async def test_unknown_entities_are_refused_together_unwritten(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        """Every entity with no state is named in one refusal."""
        before = await stored_scenes(hass)
        with upserts(core_client) as upsert:
            body = await set_refused(
                tools,
                scene_id="movie",
                config={"name": "Movie"},
                capture_entities=["light.gone", "input_boolean.tv", "sensor.gone"],
            )

        assert body["error"]["code"] == "ENTITY_NOT_FOUND"
        assert body["entity_ids"] == ["light.gone", "sensor.gone"]
        upsert.assert_not_awaited()
        assert await stored_scenes(hass) == before

    async def test_a_failed_state_read_names_the_entity_unwritten(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A state Core fails to serve is no missing entity: the refusal names
        the entity and the capture, not the scene's shape."""
        real_get = StateMachine.get

        def get(states: StateMachine, entity_id: str) -> Any:
            if entity_id == "input_boolean.tv":
                raise HomeAssistantError("state machine unavailable")
            return real_get(states, entity_id)

        monkeypatch.setattr(StateMachine, "get", get)
        with upserts(core_client) as upsert:
            body = await set_refused(
                tools,
                scene_id="movie",
                config={"name": "Movie"},
                capture_entities=["input_boolean.tv"],
            )

        assert body["error"]["code"] != "ENTITY_NOT_FOUND"
        assert body["entity_id"] == "input_boolean.tv"
        assert body["action"] == "capture"
        upsert.assert_not_awaited()

    async def test_an_id_naming_another_entity_by_path_is_refused_unwritten(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        """The REST read of ``a/../b`` answers ``b``'s state: no capture of ``a``,
        and ``b`` is not what the call named."""
        before = await stored_scenes(hass)
        body = await set_refused(
            tools,
            scene_id="movie",
            config={"name": "Movie"},
            capture_entities=["light.nope/../input_boolean.tv"],
        )

        assert body["error"]["code"] == "ENTITY_NOT_FOUND"
        assert await stored_scenes(hass) == before

    async def test_a_lost_connection_names_the_entity_unwritten(
        self, core_client: HomeAssistantClient, tools: ConfigSceneTools
    ) -> None:
        """A read that never reaches Core is refused like one Core fails. The
        scene exists, so only the capture reads a state."""
        with (
            patch.object(
                core_client,
                "get_entity_state",
                AsyncMock(side_effect=HomeAssistantConnectionError("timed out")),
            ),
            upserts(core_client) as upsert,
        ):
            body = await set_refused(
                tools,
                scene_id="test_scene",
                config={"name": "Movie"},
                capture_entities=["input_boolean.tv"],
            )

        assert body["entity_id"] == "input_boolean.tv"
        assert body["action"] == "capture"
        upsert.assert_not_awaited()


class TestCaptureThroughTheComponent:
    """With the component, every state comes from one ``ha_mcp_tools/states``
    frame and none from a REST read."""

    @pytest.fixture
    async def tools(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> ConfigSceneTools:
        hass.states.async_set("light.desk", "on", {"brightness": 180})
        return await scene_tools(hass, component_core_client, monkeypatch)

    async def test_states_come_from_one_frame(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        with (
            patch.object(
                component_core_client,
                "get_entity_state",
                wraps=component_core_client.get_entity_state,
            ) as rest,
            patch.object(
                scene_capture,
                "fetch_states_via_component",
                wraps=scene_capture.fetch_states_via_component,
            ) as frames,
        ):
            await tools.ha_config_set_scene(
                scene_id="movie",
                config={"name": "Movie"},
                capture_entities=["LIGHT.DESK", "input_boolean.tv"],
                wait=False,
            )

        assert frames.await_count == 1
        # The scene's own existence check may read scene.movie; no captured id.
        assert all(c.args[0].startswith("scene.") for c in rest.call_args_list)
        stored = await stored_scene(hass, "movie")
        assert set(stored["entities"]) == {"light.desk", "input_boolean.tv"}
        assert stored["entities"]["light.desk"] == {"brightness": 180, "state": "on"}

    async def test_a_write_without_a_capture_sends_no_frame(
        self, tools: ConfigSceneTools
    ) -> None:
        with patch.object(
            scene_capture,
            "fetch_states_via_component",
            wraps=scene_capture.fetch_states_via_component,
        ) as frames:
            await tools.ha_config_set_scene(
                scene_id="movie",
                config={"name": "Movie", "entities": TV_ON},
                wait=False,
            )

        frames.assert_not_awaited()

    async def test_ids_the_component_misses_are_refused_unwritten(
        self, tools: ConfigSceneTools
    ) -> None:
        body = await set_refused(
            tools,
            scene_id="movie",
            config={"name": "Movie"},
            capture_entities=["light.desk", "light.gone"],
        )

        assert body["error"]["code"] == "ENTITY_NOT_FOUND"
        assert body["entity_ids"] == ["light.gone"]


def _lamp(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> str:
    """Device "lamp": ``light.lamp`` is on; the scene editor leaves out its
    sensor, its config light, its hidden light and its disabled light, which
    has no state."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "lamp")}
    )
    extra: dict[str, dict[str, Any]] = {
        "light.lamp": {},
        "sensor.lamp_power": {},
        "light.lamp_config": {"entity_category": EntityCategory.CONFIG},
        "light.lamp_hidden": {"hidden_by": er.RegistryEntryHider.USER},
        "light.lamp_off": {"disabled_by": er.RegistryEntryDisabler.USER},
    }
    for entity_id, kwargs in extra.items():
        domain, object_id = entity_id.split(".")
        entity_registry.async_get_or_create(
            domain,
            "test",
            object_id,
            suggested_object_id=object_id,
            device_id=device.id,
            config_entry=entry,
            **kwargs,
        )
        if entity_id != "light.lamp_off":
            hass.states.async_set(entity_id, "on", {"brightness": 180})
    return device.id


class TestDeviceCapture:
    """``capture_devices`` saves a device as the scene editor's device picker
    does: its entities the editor lists, without ``entity_only``."""

    @pytest.fixture
    async def tools(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> ConfigSceneTools:
        return await scene_tools(hass, component_core_client, monkeypatch)

    async def test_a_device_saves_the_entities_the_editor_lists(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        tools: ConfigSceneTools,
    ) -> None:
        lamp = _lamp(hass, entity_registry)

        await tools.ha_config_set_scene(
            scene_id="movie",
            config={"name": "Movie"},
            capture_devices=[lamp],
            wait=False,
        )

        movie = await stored_scene(hass, "movie")
        assert movie["entities"] == {"light.lamp": {"brightness": 180, "state": "on"}}
        assert not movie.get("metadata")

    async def test_an_entity_also_picked_on_its_own_is_entity_only(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        tools: ConfigSceneTools,
    ) -> None:
        lamp = _lamp(hass, entity_registry)

        await tools.ha_config_set_scene(
            scene_id="movie",
            config={"name": "Movie"},
            capture_entities=["light.lamp"],
            capture_devices=[lamp],
            wait=False,
        )

        movie = await stored_scene(hass, "movie")
        assert set(movie["entities"]) == {"light.lamp"}
        assert movie["metadata"] == {"light.lamp": {"entity_only": True}}

    async def test_an_unknown_device_is_refused_unwritten(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        before = await stored_scenes(hass)
        body = await set_refused(
            tools, scene_id="movie", config={"name": "Movie"}, capture_devices=["nope"]
        )

        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert body["device_id"] == "nope"
        assert await stored_scenes(hass) == before

    @pytest.mark.parametrize("known", [False, True])
    async def test_a_device_refusal_comes_with_an_entity_refusal(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        tools: ConfigSceneTools,
        known: bool,
    ) -> None:
        """A device refusal and an entity refusal of one call come back together,
        and nothing is written."""
        device = "nope"
        if known:
            device = _lamp(hass, entity_registry)
            hass.states.async_remove("light.lamp")
        before = await stored_scenes(hass)
        body = await set_refused(
            tools,
            scene_id="movie",
            config={"name": "Movie"},
            capture_devices=[device],
            capture_entities=["light.nope_missing"],
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert [p["code"] for p in body["problems"]] == [
            "ENTITY_NOT_FOUND" if known else "RESOURCE_NOT_FOUND",
            "ENTITY_NOT_FOUND",
        ]
        assert await stored_scenes(hass) == before

    async def test_a_device_with_nothing_to_capture_is_refused_unwritten(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        tools: ConfigSceneTools,
    ) -> None:
        lamp = _lamp(hass, entity_registry)
        hass.states.async_remove("light.lamp")
        before = await stored_scenes(hass)

        body = await set_refused(
            tools, scene_id="movie", config={"name": "Movie"}, capture_devices=[lamp]
        )

        assert body["error"]["code"] == "ENTITY_NOT_FOUND"
        assert body["device_ids"] == [lamp]
        assert await stored_scenes(hass) == before

    async def test_without_the_component_it_says_so_unwritten(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools = await scene_tools(hass, core_client, monkeypatch)
        lamp = _lamp(hass, entity_registry)
        before = await stored_scenes(hass)

        body = await set_refused(
            tools, scene_id="movie", config={"name": "Movie"}, capture_devices=[lamp]
        )

        assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
        assert await stored_scenes(hass) == before

    async def test_without_the_editors_domains_it_says_so_unwritten(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        tools: ConfigSceneTools,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The component found no ignored domains in the frontend build."""
        monkeypatch.setattr(scene_editor, "ignored_domains", lambda: None)
        lamp = _lamp(hass, entity_registry)
        before = await stored_scenes(hass)

        body = await set_refused(
            tools, scene_id="movie", config={"name": "Movie"}, capture_devices=[lamp]
        )

        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert "installed frontend" in body["error"]["message"]
        assert await stored_scenes(hass) == before

    async def test_a_component_without_the_editors_domains_says_so_unwritten(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        tools: ConfigSceneTools,
    ) -> None:
        """An older component serves the device read but not the domains."""
        lamp = _lamp(hass, entity_registry)
        before = await stored_scenes(hass)

        with patch.object(scene_capture, "ask_component", AsyncMock(return_value=None)):
            body = await set_refused(
                tools,
                scene_id="movie",
                config={"name": "Movie"},
                capture_devices=[lamp],
            )

        assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
        assert await stored_scenes(hass) == before


# The frontend's delete call of the scene config API (src/data/scene.ts).
SCENE_API = 'callApi("DELETE",`config/scene/config/'


class TestIgnoredDomainsFromTheFrontend:
    """The component reads the scene editor's ignored domains from the build
    Core serves, and answers ``None`` for a build it cannot read them from."""

    @pytest.fixture
    def build(self, tmp_path: Path) -> Path:
        """The installed build's files that hold the scene config API."""
        for path in (hass_frontend.where() / "frontend_latest").glob("*.js"):
            if SCENE_API in path.read_text(encoding="utf-8"):
                shutil.copy(path, tmp_path)
        return tmp_path

    def test_the_installed_build_names_them(self, build: Path) -> None:
        domains = scene_editor.read_ignored_domains(build)

        assert domains is not None
        assert {"scene", "sensor"} <= set(domains)
        assert "light" not in domains
        assert len(list(build.glob("*.js"))) > 1

    def test_copies_that_differ_are_no_answer(self, build: Path) -> None:
        path = next(build.glob("*.js"))
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"sensor",', "", 1), encoding="utf-8")

        assert scene_editor.read_ignored_domains(build) is None

    def test_a_second_array_in_the_module_is_no_answer(self, build: Path) -> None:
        for path in build.glob("*.js"):
            text = path.read_text(encoding="utf-8")
            anchor = text.index(SCENE_API)
            path.write_text(
                f'{text[:anchor]}["light"],{text[anchor:]}', encoding="utf-8"
            )

        assert scene_editor.read_ignored_domains(build) is None

    def test_a_build_without_the_scene_api_is_no_answer(self, tmp_path: Path) -> None:
        assert scene_editor.read_ignored_domains(tmp_path) is None
