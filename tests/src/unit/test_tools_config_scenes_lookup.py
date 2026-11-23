"""Finding a scene on real Core: get, remove, activation by key, and search.

A scene's entity_id comes from its name, while Core's config API is keyed by
its storage key (the registry's unique_id). Scenes another integration owns,
or that are written straight into configuration.yaml, exist as entities but
have no config that API can read.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._command_faults import registry_lookup_failing
from ._tool_errors import error_body, error_of, rejected_call_body
from ._tools_config_scenes_core import (
    TV_ON,
    category,
    config_hash,
    deletes,
    integration_scene,
    registered_after_first_list,
    scene,
    scene_tools,
    sent_of_type,
    stored_scenes,
    upserts,
)

_EXHAUSTED = "registry retry exhausted"
# A scene in configuration.yaml without an id: it has no registry entry.
_UNREGISTERED = {"name": "Test Scene", "entities": TV_ON}


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


async def _activate(tools: ConfigSceneTools, scene_id: str) -> dict[str, Any]:
    return await tools.ha_config_set_scene(
        scene_id=scene_id, activate=True, MandatoryBPS=False
    )


def _exhausted_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.message for r in caplog.records if _EXHAUSTED in r.message]


class TestEntityResolution:
    """A storage key finds the scene's entity through the registry."""

    async def test_get_reports_the_category_of_a_renamed_scene(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        entity_registry: er.EntityRegistry,
    ) -> None:
        tools = await scene_tools(
            hass,
            core_client,
            monkeypatch,
            [scene("night_light_led_desk_strip", "LED Desk Strip Night Light")],
        )
        category_id = category(hass, "Night")
        entity_registry.async_update_entity(
            "scene.led_desk_strip_night_light", categories={"scene": category_id}
        )

        result = await tools.ha_config_get_scene(scene_id="night_light_led_desk_strip")

        assert result["category"] == category_id

    async def test_activation_by_key_waits_out_a_registry_still_registering(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        sent: AsyncMock,
    ) -> None:
        """The first registry listing misses the scene; the recheck finds it."""
        tools = await scene_tools(
            hass,
            core_client,
            monkeypatch,
            [scene("night_light_led_desk_strip", "LED Desk Strip Night Light")],
        )
        monkeypatch.setattr("ha_mcp.tools.entity_registration.RESOLVE_TIMEOUT", 1.0)

        with registered_after_first_list(
            hass, "scene.led_desk_strip_night_light", "night_light_led_desk_strip"
        ):
            result = await _activate(tools, "night_light_led_desk_strip")

        assert result["entity_id"] == "scene.led_desk_strip_night_light"
        assert result["activated"] is True
        assert len(sent_of_type(sent, "config/entity_registry/list")) == 2

    async def test_activation_by_the_slug_of_a_scene_without_a_key(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No scene is stored under the bare id: ``scene.<id>`` is the scene."""
        tools = await scene_tools(
            hass, core_client, monkeypatch, yaml_only=[_UNREGISTERED]
        )

        result = await _activate(tools, "test_scene")

        assert result["entity_id"] == "scene.test_scene"
        assert result["activated"] is True

    @pytest.mark.parametrize("registered", [True, False])
    async def test_only_an_exhausted_registry_retry_logs(
        self,
        tools: ConfigSceneTools,
        entity_registry: er.EntityRegistry,
        caplog: pytest.LogCaptureFixture,
        registered: bool,
    ) -> None:
        """The debug line a get's guessed entity traces back to."""
        if not registered:
            entity_registry.async_update_entity("scene.test_scene", new_unique_id="x")
        caplog.set_level(logging.DEBUG, logger="ha_mcp.tools.tools_config_scenes")

        await tools.ha_config_get_scene(scene_id="test_scene")

        assert bool(_exhausted_logs(caplog)) is not registered

    async def test_activation_without_a_registry_answer_activates_nothing(
        self, core_client: HomeAssistantClient, tools: ConfigSceneTools
    ) -> None:
        """A dead connection will not answer a retry either."""
        with (
            patch.object(
                core_client,
                "send_websocket_message",
                AsyncMock(side_effect=HomeAssistantConnectionError("registry offline")),
            ) as websocket,
            patch.object(
                core_client, "call_service", wraps=core_client.call_service
            ) as call_service,
            pytest.raises(ToolError) as exc,
        ):
            await _activate(tools, "test_scene")

        body = error_body(exc)
        assert (body["error"]["code"], body.get("action")) == (
            "CONNECTION_FAILED",
            "activate",
        )
        call_service.assert_not_awaited()
        websocket.assert_awaited_once()


class TestGet:
    async def test_get_returns_the_stored_body(self, tools: ConfigSceneTools) -> None:
        result = await tools.ha_config_get_scene(scene_id="test_scene")

        assert result["action"] == "get"
        assert result["scene_id"] == "test_scene"
        assert result["config"] == scene("test_scene", "Test Scene")

    async def test_missing_scene_is_entity_not_found(
        self, tools: ConfigSceneTools
    ) -> None:
        with pytest.raises(ToolError) as exc:
            await tools.ha_config_get_scene(scene_id="missing_scene")

        assert error_of(exc)["code"] == "ENTITY_NOT_FOUND"
        assert error_body(exc)["entity_id"] == "scene.missing_scene"


_TURN_ON_TEST_SCENE = {
    "action": "scene.turn_on",
    "target": {"entity_id": "scene.test_scene"},
}


class TestRemove:
    @pytest.fixture(autouse=True)
    async def _search(self, hass: HomeAssistant) -> None:
        """Core's related search, as ``default_config`` loads it."""
        assert await async_setup_component(hass, "search", {})

    async def test_remove_names_the_automations_and_scripts_using_the_scene(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        """They fail once the scene is gone; the removal still goes ahead."""
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": {
                    "id": "movie_time",
                    "alias": "Movie time",
                    "triggers": [{"trigger": "event", "event_type": "movie"}],
                    "actions": [_TURN_ON_TEST_SCENE],
                }
            },
        )
        assert await async_setup_component(
            hass, "script", {"script": {"movie": {"sequence": [_TURN_ON_TEST_SCENE]}}}
        )

        result = await tools.ha_config_remove_scene(scene_id="test_scene")

        assert result["referenced_by"] == {
            "automation": ["automation.movie_time"],
            "script": ["script.movie"],
        }
        assert any(
            "automation.movie_time" in w and "script.movie" in w
            for w in result["warnings"]
        )
        assert await stored_scenes(hass) == []

    async def test_remove_by_slug_deletes_the_storage_key(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools = await scene_tools(
            hass,
            core_client,
            monkeypatch,
            [scene("night_light_led_desk_strip", "LED Desk Strip")],
        )

        result = await tools.ha_config_remove_scene(scene_id="scene.led_desk_strip")

        assert result["scene_id"] == "night_light_led_desk_strip"
        assert result["operation"] == "deleted"
        assert "warnings" not in result
        assert await stored_scenes(hass) == []
        assert hass.states.get("scene.led_desk_strip") is None

    async def test_remove_resolves_the_storage_key_once(
        self, hass: HomeAssistant, tools: ConfigSceneTools, sent: AsyncMock
    ) -> None:
        await tools.ha_config_remove_scene(scene_id="test_scene", wait=False)
        await hass.async_block_till_done()

        assert len(sent_of_type(sent, "config/entity_registry/get")) == 1


class TestNotAStorageScene:
    """A scene that exists but has no editable config is CONFIG_NOT_FOUND,
    never ENTITY_NOT_FOUND (#1971)."""

    async def test_get_names_the_owning_integration(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        integration_scene(hass)

        with pytest.raises(ToolError) as exc:
            await tools.ha_config_get_scene(scene_id="bedroom_sleepy")

        error = error_of(exc)
        assert error["code"] == "CONFIG_NOT_FOUND"
        assert "exists" in error["message"] and "hue" in error["message"]
        assert any("activate=True" in s for s in error["suggestions"])

    async def test_get_of_a_yaml_scene_names_no_integration(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools = await scene_tools(
            hass, core_client, monkeypatch, yaml_only=[_UNREGISTERED]
        )

        with pytest.raises(ToolError) as exc:
            await tools.ha_config_get_scene(scene_id="test_scene")

        error = error_of(exc)
        assert error["code"] == "CONFIG_NOT_FOUND"
        assert "integration" not in error["message"]
        assert any("activate=True" in s for s in error["suggestions"])

    async def test_remove_names_the_owning_integration(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        integration_scene(hass)

        with pytest.raises(ToolError) as exc:
            await tools.ha_config_remove_scene(scene_id="bedroom_sleepy", wait=False)

        error = error_of(exc)
        assert error["code"] == "CONFIG_NOT_FOUND"
        assert "hue" in error["message"]

    async def test_remove_by_the_name_of_a_yaml_scene_with_an_id_is_not_storage(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """scene.key_a is a YAML scene with id key_b, which scenes.yaml lacks."""
        tools = await scene_tools(
            hass, core_client, monkeypatch, yaml_only=[scene("key_b", "Key A")]
        )

        with deletes(core_client) as delete:
            body = await rejected_call_body(
                tools.ha_config_remove_scene(scene_id="key_a", wait=False)
            )

        assert body["error"]["code"] == "CONFIG_NOT_FOUND"
        assert hass.states.get("scene.key_a") is not None
        # A bare id never sends a delete to another key.
        delete.assert_not_called()

    async def test_remove_by_entity_id_deletes_a_scene_from_scene_create(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        """Core's scene.delete removes a scene scene.create made, which has no
        stored config."""
        await hass.services.async_call(
            "scene", "create", {"scene_id": "rv_dyn", "entities": TV_ON}, blocking=True
        )

        result = await tools.ha_config_remove_scene(scene_id="scene.rv_dyn", wait=True)

        assert result["success"] is True
        assert result["scene_id"] == "scene.rv_dyn"
        assert result["deleted_with"] == "scene.delete"
        assert hass.states.get("scene.rv_dyn") is None
        assert await stored_scenes(hass) == [scene("test_scene", "Test Scene")]

    async def test_remove_by_the_bare_id_of_a_scene_from_scene_create_is_refused(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        """A bare id is a storage key; the refusal names the entity_id form."""
        await hass.services.async_call(
            "scene", "create", {"scene_id": "rv_dyn", "entities": TV_ON}, blocking=True
        )

        body = await rejected_call_body(
            tools.ha_config_remove_scene(scene_id="rv_dyn", wait=False)
        )

        assert body["error"]["code"] == "CONFIG_NOT_FOUND"
        assert body["error"]["suggestions"][0].startswith("Pass 'scene.rv_dyn'")
        assert hass.states.get("scene.rv_dyn") is not None

    @pytest.mark.parametrize("owner", ["hue", "yaml"])
    async def test_remove_by_entity_id_leaves_a_scene_core_will_not_delete(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        owner: str,
    ) -> None:
        """Core's scene.delete refuses a scene scene.create did not make."""
        if owner == "hue":
            tools = await scene_tools(hass, core_client, monkeypatch, [])
            entity_id = integration_scene(hass)
        else:
            tools = await scene_tools(
                hass, core_client, monkeypatch, yaml_only=[_UNREGISTERED]
            )
            entity_id = "scene.test_scene"

        body = await rejected_call_body(
            tools.ha_config_remove_scene(scene_id=entity_id, wait=False)
        )

        assert body["error"]["code"] == "CONFIG_NOT_FOUND"
        # Core's refusal leads.
        assert body["error"]["message"].startswith(
            ("Validation error: The scene", f"Validation error: {entity_id}")
        )
        assert hass.states.get(entity_id) is not None

    @pytest.mark.parametrize("transform", [False, True])
    async def test_hash_bearing_set_names_the_owning_integration(
        self, hass: HomeAssistant, tools: ConfigSceneTools, transform: bool
    ) -> None:
        integration_scene(hass)
        change: dict[str, Any] = (
            {"python_transform": "config['name'] = 'x'"}
            if transform
            else {"config": {"name": "x", "entities": TV_ON}}
        )

        with pytest.raises(ToolError) as exc:
            await tools.ha_config_set_scene(
                scene_id="bedroom_sleepy", config_hash="deadbeef", wait=False, **change
            )

        error = error_of(exc)
        assert error["code"] == "CONFIG_NOT_FOUND"
        assert "hue" in error["message"]

    async def test_set_without_hash_does_not_shadow_an_integration_scene(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        """Writing would add a second, managed scene beside the Hue one."""
        integration_scene(hass)

        with upserts(core_client) as upsert, pytest.raises(ToolError) as exc:
            await tools.ha_config_set_scene(
                scene_id="bedroom_sleepy",
                config={"name": "x", "entities": TV_ON},
                wait=False,
            )

        error = error_of(exc)
        assert error["code"] == "CONFIG_NOT_FOUND"
        assert "hue" in error["message"]
        upsert.assert_not_awaited()

    async def test_set_without_hash_does_not_shadow_an_unregistered_yaml_scene(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No registry entry, but a state: the scene exists all the same."""
        tools = await scene_tools(
            hass, core_client, monkeypatch, yaml_only=[_UNREGISTERED]
        )

        with upserts(core_client) as upsert, pytest.raises(ToolError) as exc:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={"name": "x", "entities": TV_ON},
                wait=False,
            )

        assert error_of(exc)["code"] == "CONFIG_NOT_FOUND"
        upsert.assert_not_awaited()
        assert await stored_scenes(hass) == []


@pytest.mark.parametrize("scene_id", ["", " \t\n "], ids=["empty", "whitespace"])
@pytest.mark.parametrize("tool", ["get", "set", "remove"])
async def test_blank_scene_id_is_refused_before_any_write(
    core_client: HomeAssistantClient,
    tools: ConfigSceneTools,
    tool: str,
    scene_id: str,
) -> None:
    """A blank id used to come back as a misleading RESOURCE_NOT_FOUND."""
    calls = {
        "get": lambda: tools.ha_config_get_scene(scene_id=scene_id),
        "set": lambda: tools.ha_config_set_scene(
            scene_id=scene_id, config={"name": "X", "entities": TV_ON}, wait=False
        ),
        "remove": lambda: tools.ha_config_remove_scene(scene_id=scene_id, wait=False),
    }
    with (
        upserts(core_client) as upsert,
        deletes(core_client) as delete,
        pytest.raises(ToolError) as exc,
    ):
        await calls[tool]()

    error = error_of(exc)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "scene_id must not be empty" in error["message"]
    upsert.assert_not_awaited()
    delete.assert_not_awaited()


class TestVerificationWithoutAnAnswer:
    """The write went through; only checking its result got no answer."""

    @pytest.mark.parametrize(
        ("transform", "verbiage"),
        [
            (False, "Scene saved but verification failed"),
            (True, "Scene updated but verification failed"),
        ],
    )
    async def test_write_reports_success_with_a_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
        transform: bool,
        verbiage: str,
    ) -> None:
        change: dict[str, Any] = (
            {
                "python_transform": "config['name'] = 'Late Show'",
                "config_hash": await config_hash(tools, "test_scene"),
            }
            if transform
            else {"config": {"name": "Late Show", "entities": TV_ON}}
        )

        with patch.object(
            core_client,
            "get_entity_state",
            AsyncMock(side_effect=HomeAssistantConnectionError("forced for test")),
        ):
            result = await tools.ha_config_set_scene(scene_id="test_scene", **change)
        await hass.async_block_till_done()

        assert result["success"] is True
        assert result["scene_id"] == "test_scene"
        assert any(verbiage in w and "forced for test" in w for w in result["warnings"])
        assert [s["name"] for s in await stored_scenes(hass)] == ["Late Show"]

    async def test_remove_reports_success_with_a_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        with patch.object(
            core_client,
            "get_entity_state",
            AsyncMock(side_effect=HomeAssistantConnectionError("forced for test")),
        ):
            result = await tools.ha_config_remove_scene(scene_id="test_scene")

        assert result["success"] is True
        assert result["scene_id"] == "test_scene"
        assert any(
            "removal verification failed" in w and "forced for test" in w
            for w in result["warnings"]
        )
        assert await stored_scenes(hass) == []

    async def test_remove_without_core_search_still_deletes_with_a_warning(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        """Core without ``search`` cannot tell what uses the scene; that is no
        reason to keep it."""
        result = await tools.ha_config_remove_scene(scene_id="test_scene")

        assert "referenced_by" not in result
        assert any("could not check" in w.lower() for w in result["warnings"])
        assert await stored_scenes(hass) == []


class TestSearchSceneId:
    """``ha_search`` reports the storage key ``ha_config_get_scene`` takes."""

    async def _search(
        self, client: HomeAssistantClient, query: str
    ) -> list[dict[str, Any]]:
        result = await SmartSearchTools(client=client).deep_search(
            query=query, search_types=["scene"], limit=10, include_config=True
        )
        scenes: list[dict[str, Any]] = result["scenes"]
        return scenes

    async def test_renamed_scene_reports_its_storage_key(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await scene_tools(
            hass,
            core_client,
            monkeypatch,
            [scene("night_light_led_desk_strip", "LED Desk Strip Night Light")],
        )

        (match,) = await self._search(core_client, "led desk strip")

        assert match["entity_id"] == "scene.led_desk_strip_night_light"
        assert match["scene_id"] == "night_light_led_desk_strip"

    async def test_integration_scene_reports_its_registry_unique_id(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        """No config to read the id from; the registry has it."""
        integration_scene(hass)

        (match,) = await self._search(core_client, "bedroom sleepy")

        assert match["scene_id"] == "hue-uuid-123"

    async def test_unregistered_scene_falls_back_to_its_entity_id_with_a_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await scene_tools(
            hass,
            core_client,
            monkeypatch,
            yaml_only=[{"name": "Orphaned Scene", "entities": TV_ON}],
        )
        caplog.set_level(logging.WARNING, logger="ha_mcp.tools.smart_search")

        (match,) = await self._search(core_client, "orphaned")

        assert match["scene_id"] == "scene.orphaned_scene"
        assert any(
            "fell back to its entity_id" in r.message
            for r in caplog.records
            if r.levelno == logging.WARNING
        )


class TestStorageKeyPrecedence:
    """A bare id that is a storage key opens that scene, even when it is also
    another scene's entity slug."""

    @pytest.fixture
    async def collided(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> ConfigSceneTools:
        """``key_a`` is "First" (``scene.first``); ``key_b`` is "Key A", whose
        entity is ``scene.key_a``."""
        return await scene_tools(
            hass,
            core_client,
            monkeypatch,
            [scene("key_a", "First"), scene("key_b", "Key A")],
        )

    @pytest.mark.parametrize(
        ("scene_id", "key"),
        [("key_a", "key_a"), ("scene.key_a", "key_b"), ("first", "key_a")],
    )
    async def test_get_opens_the_scene_the_id_names(
        self, collided: ConfigSceneTools, scene_id: str, key: str
    ) -> None:
        result = await collided.ha_config_get_scene(scene_id=scene_id)

        assert result["scene_id"] == key
        assert result["config"]["id"] == key

    async def test_the_key_search_returns_opens_its_scene(
        self, core_client: HomeAssistantClient, collided: ConfigSceneTools
    ) -> None:
        result = await SmartSearchTools(client=core_client).deep_search(
            query="first", search_types=["scene"], limit=10, include_config=True
        )
        (match,) = [m for m in result["scenes"] if m["entity_id"] == "scene.first"]

        opened = await collided.ha_config_get_scene(scene_id=match["scene_id"])

        assert opened["config"]["name"] == "First"

    @pytest.mark.parametrize("hashed", [True, False])
    async def test_a_write_by_key_edits_that_scene(
        self, hass: HomeAssistant, collided: ConfigSceneTools, hashed: bool
    ) -> None:
        await collided.ha_config_set_scene(
            scene_id="key_a",
            config={"name": "First", "entities": TV_ON, "icon": "mdi:one"},
            config_hash=await config_hash(collided, "key_a") if hashed else None,
            wait=False,
        )

        icons = {s["id"]: s.get("icon") for s in await stored_scenes(hass)}
        assert icons == {"key_a": "mdi:one", "key_b": None}

    async def test_remove_by_key_removes_that_scene(
        self, hass: HomeAssistant, collided: ConfigSceneTools
    ) -> None:
        result = await collided.ha_config_remove_scene(scene_id="key_a")

        assert [s["id"] for s in await stored_scenes(hass)] == ["key_b"]
        # The removal is checked on the removed scene's entity, not scene.key_a.
        assert hass.states.get("scene.first") is None
        assert hass.states.get("scene.key_a") is not None
        assert not any("scene.key_a" in w for w in result.get("warnings", []))

    @pytest.mark.parametrize("remove", [True, False])
    async def test_an_entity_id_the_registry_cannot_look_up_writes_nothing(
        self, hass: HomeAssistant, collided: ConfigSceneTools, remove: bool
    ) -> None:
        """A failed lookup is no answer: ``scene.key_a`` is not key ``key_a``."""

        call = (
            collided.ha_config_remove_scene(scene_id="scene.key_a", wait=False)
            if remove
            else collided.ha_config_set_scene(
                scene_id="scene.key_a",
                config={"name": "Edited", "entities": TV_ON},
                wait=False,
            )
        )
        with registry_lookup_failing(hass):
            body = await rejected_call_body(call)

        assert "registry unavailable" in body["error"]["message"]
        names = {s["id"]: s["name"] for s in await stored_scenes(hass)}
        assert names == {"key_a": "First", "key_b": "Key A"}

    @pytest.mark.parametrize("wait", [False, True])
    @pytest.mark.parametrize("mode", ["config", "transform"])
    async def test_a_write_the_registry_cannot_look_up_activates_no_other_scene(
        self, hass: HomeAssistant, collided: ConfigSceneTools, mode: str, wait: bool
    ) -> None:
        """After the write a failed lookup is no answer: ``scene.key_a``, the
        entity id key ``key_a`` would have by its key, is key_b's."""
        cat = cr.async_get(hass).async_create(name="Mood", scope="scene").category_id
        before = hass.states.get("scene.key_a")
        assert before is not None
        edit: dict[str, Any] = (
            {
                "python_transform": "config['icon'] = 'mdi:one'",
                "config_hash": await config_hash(collided, "key_a"),
            }
            if mode == "transform"
            else {"config": {"name": "First", "entities": TV_ON}}
        )
        with registry_lookup_failing(hass, "config/entity_registry/list"):
            result = await collided.ha_config_set_scene(
                scene_id="key_a", activate=True, wait=wait, category=cat, **edit
            )
        await hass.async_block_till_done()

        after = hass.states.get("scene.key_a")
        assert after is not None and after.state == before.state
        entry = er.async_get(hass).async_get("scene.key_a")
        assert entry is not None and entry.categories == {}
        assert result["activated"] is False
        # The skip warning alone: no step ran on another object's entity.
        (warning,) = result["warnings"]
        assert "registry unavailable" in warning
        assert "activate" in warning and "category" in warning
        assert ("wait" in warning) is wait

    async def test_a_repeated_remove_by_key_leaves_the_other_scene(
        self, hass: HomeAssistant, collided: ConfigSceneTools
    ) -> None:
        """A bare id is a storage key: once key_a is gone, a repeated remove
        must not reach key_b, whose entity is scene.key_a."""
        await collided.ha_config_remove_scene(scene_id="key_a", wait=False)

        body = await rejected_call_body(
            collided.ha_config_remove_scene(scene_id="key_a", wait=False)
        )

        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert any("'scene.key_a'" in s for s in body["error"]["suggestions"])
        assert [s["id"] for s in await stored_scenes(hass)] == ["key_b"]

    @pytest.mark.parametrize("mode", ["config", "hashed", "transform"])
    async def test_a_recreate_by_a_removed_key_leaves_the_other_scene(
        self, hass: HomeAssistant, collided: ConfigSceneTools, mode: str
    ) -> None:
        """A bare id is a storage key: once key_a is gone, a set by key_a must
        not overwrite key_b, whose entity is scene.key_a."""
        await collided.ha_config_remove_scene(scene_id="key_a", wait=False)
        edit: dict[str, Any] = (
            {"python_transform": "config['icon'] = 'mdi:one'"}
            if mode == "transform"
            else {"config": {"name": "First again", "entities": TV_ON}}
        )
        if mode != "config":
            edit["config_hash"] = await config_hash(collided, "key_a")

        body = await rejected_call_body(
            collided.ha_config_set_scene(scene_id="key_a", wait=False, **edit)
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert any("'scene.key_a'" in s for s in body["error"]["suggestions"])
        assert await stored_scenes(hass) == [scene("key_b", "Key A")]
