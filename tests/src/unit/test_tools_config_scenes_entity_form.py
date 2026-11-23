"""``scene.<id>`` on real Core: an entity_id, never the storage key ``<id>``.

A scene's entity_id comes from its name, so ``scene.<id>`` and the scene
stored under key ``<id>`` can be two scenes, or the first can be no entity.
"""

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
)
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._tool_errors import error_of, rejected_call_body
from ._tools_config_scenes_core import (
    TV_ON,
    scene,
    scene_tools,
    set_refused,
    stored_scenes,
)


class TestEntityIdForm:
    """``scene.<id>`` names an entity: with no such entity it never opens the
    scene stored under key ``<id>``, whose entity is another one."""

    @pytest.fixture
    async def orphaned_key(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> ConfigSceneTools:
        """``key_a`` is "First" (``scene.first``); no registered ``scene.key_a``."""
        return await scene_tools(
            hass, core_client, monkeypatch, [scene("key_a", "First")]
        )

    @pytest.mark.parametrize("action", ["get", "set", "set_hashed", "remove"])
    async def test_it_does_not_reach_another_scene_by_its_key(
        self, hass: HomeAssistant, orphaned_key: ConfigSceneTools, action: str
    ) -> None:
        before = await stored_scenes(hass)
        call = {
            "get": lambda: orphaned_key.ha_config_get_scene(scene_id="scene.key_a"),
            "set": lambda: orphaned_key.ha_config_set_scene(
                scene_id="scene.key_a",
                config={"name": "Brand New", "entities": TV_ON},
                wait=False,
            ),
            "set_hashed": lambda: orphaned_key.ha_config_set_scene(
                scene_id="scene.key_a",
                config={"entities": TV_ON},
                config_hash="any",
                wait=False,
            ),
            "remove": lambda: orphaned_key.ha_config_remove_scene(
                scene_id="scene.key_a", wait=False
            ),
        }[action]
        with pytest.raises(ToolError) as exc:
            await call()

        error = error_of(exc)
        assert error["code"] == "VALIDATION_FAILED"
        assert any("'key_a'" in s for s in error["suggestions"])
        assert any("entity_id" in s for s in error["suggestions"])
        assert await stored_scenes(hass) == before

    async def test_a_backup_snapshot_reads_it_as_absent(
        self, core_client: HomeAssistantClient, orphaned_key: ConfigSceneTools
    ) -> None:
        """A backup snapshot reads a 404 from this read as no prior config."""
        with pytest.raises(HomeAssistantAPIError) as exc:
            await core_client.get_scene_config("scene.key_a")

        assert exc.value.status_code == 404

    @pytest.mark.parametrize(
        "config",
        [
            {"name": "RV Movie Night", "entities": TV_ON},
            {"entities": TV_ON},
        ],
    )
    async def test_a_create_by_entity_id_takes_the_bare_key(
        self, hass: HomeAssistant, orphaned_key: ConfigSceneTools, config: dict
    ) -> None:
        """Core names the new entity after the scene's name, so the entity_id
        form of a scene that does not exist yet is never a create."""
        before = await stored_scenes(hass)
        body = await set_refused(orphaned_key, scene_id="scene.rv_movie", config=config)

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert any("'rv_movie'" in s for s in body["error"]["suggestions"])
        assert await stored_scenes(hass) == before

    async def test_an_entity_form_create_is_refused_with_the_rest(
        self, hass: HomeAssistant, orphaned_key: ConfigSceneTools
    ) -> None:
        before = await stored_scenes(hass)
        body = await set_refused(
            orphaned_key,
            scene_id="scene.rv_movie",
            config={"name": "RV Movie Night", "platform": "homeassistant"},
        )

        assert [p["code"] for p in body["problems"]] == ["VALIDATION_FAILED"] * 2
        assert await stored_scenes(hass) == before

    async def test_a_scene_created_by_key_refuses_the_entity_form_of_the_key(
        self, hass: HomeAssistant, orphaned_key: ConfigSceneTools
    ) -> None:
        await orphaned_key.ha_config_set_scene(
            scene_id="rv_movie",
            config={"name": "RV Movie Night", "entities": TV_ON},
            wait=True,
        )

        with pytest.raises(ToolError) as exc:
            await orphaned_key.ha_config_get_scene(scene_id="scene.rv_movie")

        assert error_of(exc)["code"] == "VALIDATION_FAILED"
        found = await orphaned_key.ha_config_get_scene(scene_id="rv_movie")
        assert found["config"]["name"] == "RV Movie Night"

    async def test_search_gives_an_unregistered_scene_neither_key_nor_config(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        orphaned_key: ConfigSceneTools,
    ) -> None:
        """A scene from ``scene.create`` has no registry entry and no stored
        config; its slug ``key_a`` is "First"'s storage key."""
        await hass.services.async_call(
            "scene", "create", {"scene_id": "key_a", "entities": TV_ON}, blocking=True
        )
        result = await SmartSearchTools(client=core_client).deep_search(
            query="key_a", search_types=["scene"], limit=10, include_config=True
        )
        (match,) = [m for m in result["scenes"] if m["entity_id"] == "scene.key_a"]

        assert match["scene_id"] == "scene.key_a"
        assert match["config"] is None
        with pytest.raises(ToolError):
            await orphaned_key.ha_config_get_scene(scene_id=match["scene_id"])

    async def test_remove_deletes_a_scene_create_scene_named_after_another_key(
        self, hass: HomeAssistant, orphaned_key: ConfigSceneTools
    ) -> None:
        """scene.key_a from scene.create is no stored scene; key_a stays."""
        await hass.services.async_call(
            "scene", "create", {"scene_id": "key_a", "entities": TV_ON}, blocking=True
        )
        before = await stored_scenes(hass)

        result = await orphaned_key.ha_config_remove_scene(
            scene_id="scene.key_a", wait=True
        )

        assert result["success"] is True
        assert hass.states.get("scene.key_a") is None
        assert await stored_scenes(hass) == before

    async def test_remove_of_a_yaml_scene_named_after_another_key_names_no_key(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """scene.key_a is an id-less YAML scene, so no registered entity, while
        key_a holds "First": Core's refusal leads, and the bare key, which
        would delete "First", is not suggested."""
        tools = await scene_tools(
            hass,
            core_client,
            monkeypatch,
            [scene("key_a", "First")],
            yaml_only=[{"name": "Key A", "entities": TV_ON}],
        )
        before = await stored_scenes(hass)

        body = await rejected_call_body(
            tools.ha_config_remove_scene(scene_id="scene.key_a", wait=False)
        )

        assert body["error"]["code"] == "CONFIG_NOT_FOUND"
        assert body["error"]["message"].startswith(
            "Validation error: The scene scene.key_a is not"
        )
        assert not any("'key_a'" in s for s in body["error"]["suggestions"])
        assert hass.states.get("scene.key_a") is not None
        assert await stored_scenes(hass) == before

    @pytest.mark.parametrize("search_in_config", [False, True])
    async def test_discovery_gives_an_unregistered_scene_neither_key_nor_config(
        self,
        hass: HomeAssistant,
        orphaned_key: ConfigSceneTools,
        search_in_config: bool,
    ) -> None:
        await hass.services.async_call(
            "scene", "create", {"scene_id": "key_a", "entities": TV_ON}, blocking=True
        )
        result = await orphaned_key.ha_config_get_scene(
            query="key_a", search_in_config=search_in_config
        )
        (row,) = [r for r in result["scenes"] if r["entity_id"] == "scene.key_a"]

        assert (row["scene_id"], row["config_status"]) == (None, "not_in_storage")
