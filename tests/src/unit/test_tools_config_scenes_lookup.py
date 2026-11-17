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
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._tool_errors import error_body, error_of
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

        with registered_after_first_list(
            hass, "scene.led_desk_strip_night_light", "night_light_led_desk_strip"
        ):
            result = await _activate(tools, "night_light_led_desk_strip")

        assert result["entity_id"] == "scene.led_desk_strip_night_light"
        assert result["activated"] is True
        assert len(sent_of_type(sent, "config/entity_registry/list")) == 2

    async def test_activation_by_key_of_an_unregistered_scene_falls_back(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No registry entry after the recheck: ``scene.<key>``, and a debug
        line the later warnings can be traced to."""
        tools = await scene_tools(
            hass, core_client, monkeypatch, yaml_only=[_UNREGISTERED]
        )
        caplog.set_level(logging.DEBUG, logger="ha_mcp.tools.tools_config_scenes")

        result = await _activate(tools, "test_scene")

        assert result["entity_id"] == "scene.test_scene"
        assert result["activated"] is True
        assert any("test_scene" in m for m in _exhausted_logs(caplog))

    async def test_first_lookup_hit_logs_no_exhausted_retry(
        self, tools: ConfigSceneTools, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="ha_mcp.tools.tools_config_scenes")

        result = await _activate(tools, "test_scene")

        assert result["entity_id"] == "scene.test_scene"
        assert _exhausted_logs(caplog) == []

    async def test_activation_ignores_another_domains_entry_with_that_unique_id(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        entity_registry: er.EntityRegistry,
    ) -> None:
        tools = await scene_tools(
            hass, core_client, monkeypatch, yaml_only=[_UNREGISTERED]
        )
        entity_registry.async_get_or_create(
            "automation", "automation", "test_scene", suggested_object_id="collision"
        )

        result = await _activate(tools, "test_scene")

        assert result["entity_id"] == "scene.test_scene"
        assert result["activated"] is True

    async def test_activation_without_a_registry_answer_falls_back_at_once(
        self, core_client: HomeAssistantClient, tools: ConfigSceneTools
    ) -> None:
        """A dead connection will not answer a retry either."""
        with patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("registry offline")),
        ) as websocket:
            result = await _activate(tools, "test_scene")

        assert result["entity_id"] == "scene.test_scene"
        assert result["activated"] is True
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

        result = await tools.ha_config_remove_scene(scene_id="led_desk_strip")

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

    async def test_unregistered_scene_falls_back_to_its_slug_with_a_warning(
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

        assert match["scene_id"] == "orphaned_scene"
        assert any(
            "fell back to entity-id slug" in r.message
            for r in caplog.records
            if r.levelno == logging.WARNING
        )
