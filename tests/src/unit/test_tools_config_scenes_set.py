"""``ha_config_set_scene`` writing scenes through Core's config API.

The scenes live in scenes.yaml in the test's own config directory; a scene's
entity_id comes from its name, while its storage key is the ``id`` the config
API is keyed by.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.scene_checks import category_problems
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._tool_errors import error_of
from ._tools_config_scenes_core import (
    TV_ON,
    category,
    config_hash,
    config_reads,
    scene,
    scene_tools,
    sent_of_type,
    set_refused,
    stored_scene,
    stored_scenes,
    upserts,
)

TV_OFF = {"input_boolean.tv": {"state": "off"}}


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


async def _transform(
    tools: ConfigSceneTools,
    expression: str,
    scene_id: str = "test_scene",
    *,
    sent: AsyncMock | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read the scene's hash, as a caller does, then transform it; ``sent``
    forgets the read."""
    hash_value = await config_hash(tools, scene_id)
    if sent is not None:
        sent.reset_mock()
    return await tools.ha_config_set_scene(
        scene_id=scene_id,
        python_transform=expression,
        config_hash=hash_value,
        **{"wait": False, **kwargs},
    )


class TestPostWriteEntity:
    """A scene's entity_id comes from its name, not from its storage key."""

    @pytest.mark.parametrize("transform", [False, True])
    @pytest.mark.parametrize("wait,with_category", [(True, False), (False, True)])
    async def test_wait_and_category_reach_the_name_derived_entity(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        entity_registry: er.EntityRegistry,
        sent: AsyncMock,
        transform: bool,
        wait: bool,
        with_category: bool,
    ) -> None:
        tools = await scene_tools(
            hass, core_client, monkeypatch, [scene("test_scene", "Friendly Name")]
        )
        category_id = category(hass, "Lighting") if with_category else None
        if transform:
            result = await _transform(
                tools,
                "config['entities']['input_boolean.tv']['state'] = 'off'",
                sent=sent,
                wait=wait,
                category=category_id,
            )
        else:
            result = await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={"name": "Friendly Name", "entities": TV_OFF},
                wait=wait,
                category=category_id,
            )
        await hass.async_block_till_done()

        assert result["success"] is True
        assert not any("not yet queryable" in w for w in result.get("warnings", []))
        assert sent_of_type(sent, "config/entity_registry/list")
        if category_id:
            entry = entity_registry.async_get("scene.friendly_name")
            assert entry is not None
            assert entry.categories == {"scene": category_id}

    @pytest.mark.parametrize("transform", [False, True])
    async def test_no_registry_lookup_without_wait_or_category(
        self,
        hass: HomeAssistant,
        tools: ConfigSceneTools,
        sent: AsyncMock,
        transform: bool,
    ) -> None:
        """A bulk write (no wait, no category) skips the post-write lookup."""
        if transform:
            result = await _transform(
                tools,
                "config['entities']['input_boolean.tv']['state'] = 'off'",
                sent=sent,
            )
        else:
            result = await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={"name": "Test Scene", "entities": TV_OFF},
                wait=False,
            )
        await hass.async_block_till_done()

        assert result["success"] is True
        assert sent_of_type(sent, "config/entity_registry/list") == []
        assert "references_unchecked" in result["validation"]


class TestConfigValidation:
    """Malformed requests are refused before anything is written."""

    @pytest.mark.parametrize(
        ("kwargs", "message_part"),
        [
            (
                {"config": {"entities": TV_ON}, "python_transform": "config.clear()"},
                "both",
            ),
            ({}, "either config or python_transform"),
            ({"python_transform": "config['name'] = 'x'"}, "config_hash"),
            ({"config": "{not valid json"}, "config"),
            ({"config": "[1, 2, 3]"}, "object"),
            ({"capture_entities": ["input_boolean.tv"]}, "requires config"),
            (
                {
                    "capture_entities": ["input_boolean.tv"],
                    "python_transform": "config['name'] = 'x'",
                    "config_hash": "0" * 16,
                },
                "requires config",
            ),
        ],
        ids=[
            "config-and-transform",
            "neither",
            "transform-without-hash",
            "invalid-json",
            "json-array",
            "capture-without-config",
            "capture-with-transform",
        ],
    )
    async def test_malformed_request_is_refused_unwritten(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
        kwargs: dict[str, Any],
        message_part: str,
    ) -> None:
        before = await stored_scenes(hass)
        with upserts(core_client) as upsert:
            body = await set_refused(tools, **kwargs)

        assert body["success"] is False
        assert message_part in body["error"]["message"].lower()
        upsert.assert_not_awaited()
        assert await stored_scenes(hass) == before

    @pytest.mark.parametrize(
        ("config", "core_says"),
        [
            ({"name": "No entities"}, "required key not provided"),
            (
                {
                    "name": "Wrong shape",
                    "entities": [{"entity_id": "input_boolean.tv"}],
                },
                "expected dict",
            ),
        ],
        ids=["missing-entities", "entities-list"],
    )
    async def test_cores_refusal_of_the_entities_leads_unwritten(
        self,
        hass: HomeAssistant,
        tools: ConfigSceneTools,
        config: dict[str, Any],
        core_says: str,
    ) -> None:
        """The entities shape is Core's to check; its own text reaches the
        caller instead of a copy of the rule."""
        before = await stored_scenes(hass)
        body = await set_refused(tools, scene_id="bad_scene", config=config)

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert core_says in body["error"]["message"]
        assert await stored_scenes(hass) == before

    async def test_cores_refusal_keeps_the_scene_id_and_shape_hint(
        self, tools: ConfigSceneTools
    ) -> None:
        """Core refuses an entity key that is no entity_id."""
        body = await set_refused(
            tools,
            scene_id="bad_scene",
            config={"name": "Bad Scene", "entities": {"kitchen": {"state": "on"}}},
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert body["scene_id"] == "bad_scene"
        assert any("entities" in s.lower() for s in body["error"]["suggestions"])

    @pytest.mark.parametrize("mode", ["config", "transform"])
    async def test_a_platform_stanza_is_refused_unwritten(
        self, hass: HomeAssistant, tools: ConfigSceneTools, mode: str
    ) -> None:
        """Core's config API stores a body with ``platform`` as a scene platform,
        not as this scene: ``states: []`` passes and no entity appears."""
        before = await stored_scenes(hass)
        stanza = {"platform": "homeassistant", "states": []}
        if mode == "config":
            kwargs: dict[str, Any] = {"config": {"name": "Test Scene", **stanza}}
        else:
            kwargs = {
                "python_transform": f"config.update({stanza!r})",
                "config_hash": await config_hash(tools, "test_scene"),
            }
        body = await set_refused(tools, **kwargs)

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "platform" in body["error"]["message"]
        assert await stored_scenes(hass) == before


class TestConfigWrite:
    async def test_new_scene_is_created_without_a_precheck_read(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        """A scene in neither the registry nor the state machine is a create:
        no config read first, and no ``operation`` field, which Core's answer
        cannot tell apart from an update."""
        new = {"name": "Brand New", "entities": TV_ON}
        with config_reads(core_client) as reads:
            result = await tools.ha_config_set_scene(
                scene_id="brand_new", config=dict(new), wait=False
            )

        assert result["success"] is True
        assert result["scene_id"] == "brand_new"
        assert "operation" not in result
        reads.assert_not_awaited()
        assert {"id": "brand_new", **new} in await stored_scenes(hass)

    async def test_slug_writes_the_renamed_scenes_storage_key(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``led_desk_strip`` is the entity_id slug of the scene stored as
        ``night_light_led_desk_strip``; the write updates that scene."""
        tools = await scene_tools(
            hass,
            core_client,
            monkeypatch,
            [scene("night_light_led_desk_strip", "LED Desk Strip")],
        )

        result = await tools.ha_config_set_scene(
            scene_id="led_desk_strip",
            config={"name": "LED Desk Strip", "entities": TV_OFF},
            wait=False,
        )

        assert result["scene_id"] == "night_light_led_desk_strip"
        assert await stored_scenes(hass) == [
            {
                "id": "night_light_led_desk_strip",
                "name": "LED Desk Strip",
                "entities": TV_OFF,
            }
        ]

    async def test_stale_hash_is_a_conflict_carrying_the_fresh_hash(
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
        fresh = await config_hash(tools, "led_desk_strip")

        with upserts(core_client) as upsert:
            body = await set_refused(
                tools,
                scene_id="led_desk_strip",
                config={"name": "New Name", "entities": TV_OFF},
                config_hash="stale-hash-value",
                wait=False,
            )

        assert "modified" in body["error"]["message"].lower()
        assert body["current_config_hash"] == fresh
        assert body["scene_id"] == "night_light_led_desk_strip"
        upsert.assert_not_awaited()

    async def test_an_empty_hash_is_checked_not_skipped(
        self, core_client: HomeAssistantClient, tools: ConfigSceneTools
    ) -> None:
        with upserts(core_client) as upsert:
            body = await set_refused(
                tools, config={"name": "New", "entities": TV_OFF}, config_hash=""
            )

        assert "modified" in body["error"]["message"].lower()
        upsert.assert_not_awaited()

    async def test_matching_hash_replaces_the_scene(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        result = await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={"name": "New Name", "entities": TV_OFF},
            config_hash=await config_hash(tools, "test_scene"),
            wait=False,
        )

        assert result["success"] is True
        assert await stored_scenes(hass) == [
            {"id": "test_scene", "name": "New Name", "entities": TV_OFF}
        ]

    async def test_renamed_scene_without_name_keeps_the_callers_name(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A config without ``name`` defaults it from the id the caller passed,
        not from the storage key the write goes to (#1935)."""
        tools = await scene_tools(
            hass, core_client, monkeypatch, [scene("storage_key", "Renamed Scene")]
        )

        await tools.ha_config_set_scene(
            scene_id="renamed_scene",
            config={"entities": TV_OFF},
            config_hash=await config_hash(tools, "renamed_scene"),
            wait=False,
        )

        assert await stored_scenes(hass) == [
            {"id": "storage_key", "name": "renamed_scene", "entities": TV_OFF}
        ]

    async def test_another_id_in_the_config_is_refused_unwritten(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        """Core stores the body under its ``id``: the scene would move to a new
        key while the tool reports the one it was called with."""
        before = await stored_scenes(hass)
        with upserts(core_client) as upsert:
            body = await set_refused(
                tools,
                config={"id": "other_key", "name": "Test Scene", "entities": TV_OFF},
            )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert body["attempted_id"] == "other_key"
        upsert.assert_not_awaited()
        assert await stored_scenes(hass) == before

    async def test_a_none_id_in_the_config_is_dropped(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        """Core refuses ``id: None``; it means the same as leaving ``id`` out."""
        await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={"id": None, "name": "Test Scene", "entities": TV_OFF},
            wait=False,
        )

        assert await stored_scene(hass, "test_scene") == {
            "id": "test_scene",
            "name": "Test Scene",
            "entities": TV_OFF,
        }

    async def test_a_renamed_scenes_own_id_in_the_config_is_written(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A config read back from ``ha_config_get_scene`` carries the storage
        key, not the slug the caller names the scene by."""
        tools = await scene_tools(
            hass, core_client, monkeypatch, [scene("storage_key", "Renamed Scene")]
        )
        new = {"id": "storage_key", "name": "Renamed Scene", "entities": TV_OFF}

        await tools.ha_config_set_scene(
            scene_id="renamed_scene", config=dict(new), wait=False
        )

        assert await stored_scenes(hass) == [new]


class TestResolvesOnce:
    """Each call resolves the storage key once and hands it on."""

    @pytest.mark.parametrize("transform", [False, True])
    async def test_write_resolves_the_storage_key_once(
        self,
        hass: HomeAssistant,
        tools: ConfigSceneTools,
        sent: AsyncMock,
        transform: bool,
    ) -> None:
        if transform:
            result = await _transform(
                tools,
                "config['entities']['input_boolean.tv']['state'] = 'off'",
                sent=sent,
            )
        else:
            result = await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={"name": "Test Scene", "entities": TV_OFF},
                wait=False,
            )
        await hass.async_block_till_done()

        assert result["scene_id"] == "test_scene"
        assert len(sent_of_type(sent, "config/entity_registry/get")) == 1


class TestPythonTransform:
    async def test_transform_edits_the_stored_scene(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        result = await _transform(
            tools, "config['entities']['input_boolean.tv']['state'] = 'off'"
        )

        assert result["success"] is True
        assert result["action"] == "python_transform"
        assert result["config_hash"] == await config_hash(tools, "test_scene")
        assert await stored_scenes(hass) == [
            {"id": "test_scene", "name": "Test Scene", "entities": TV_OFF}
        ]

    async def test_transform_of_a_renamed_scene_writes_its_storage_key(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tools = await scene_tools(
            hass, core_client, monkeypatch, [scene("storage_key", "Renamed Scene")]
        )

        result = await _transform(
            tools,
            "config['entities']['input_boolean.tv']['state'] = 'off'",
            scene_id="renamed_scene",
        )

        assert result["scene_id"] == "storage_key"
        assert await stored_scenes(hass) == [
            {"id": "storage_key", "name": "Renamed Scene", "entities": TV_OFF}
        ]

    async def test_stale_hash_is_a_conflict(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        body = await set_refused(
            tools,
            python_transform="config['entities']['input_boolean.tv']['state'] = 'off'",
            config_hash="stale-hash-that-doesnt-match",
        )

        assert "modified" in body["error"]["message"].lower()
        assert await stored_scenes(hass) == [scene("test_scene", "Test Scene")]

    @pytest.mark.parametrize(
        ("expression", "message_part"),
        [
            ("config = None", "none"),
            ("import os; os.system('echo pwned')", "import"),
            ("del config['entities']['light.nonexistent']", ""),
        ],
        ids=[
            "config-none",
            "import",
            "runtime-error",
        ],
    )
    async def test_failing_transform_is_a_validation_error_unwritten(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
        expression: str,
        message_part: str,
    ) -> None:
        hash_value = await config_hash(tools, "test_scene")
        with upserts(core_client) as upsert:
            body = await set_refused(
                tools, python_transform=expression, config_hash=hash_value, wait=False
            )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert message_part in body["error"]["message"].lower()
        upsert.assert_not_awaited()

    async def test_backslash_escape_gets_a_specific_hint(
        self, tools: ConfigSceneTools
    ) -> None:
        """A JSON-style ``\\"`` outside a string literal is a syntax error with
        its own hint."""
        body = await set_refused(
            tools,
            python_transform='config["entities"]["input_boolean.tv"] = \\"v\\"',
            config_hash=await config_hash(tools, "test_scene"),
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert any("backslash" in s.lower() for s in body["error"]["suggestions"])

    async def test_changing_the_id_is_refused(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        """A changed ``id`` would move the scene to another storage key."""
        hash_value = await config_hash(tools, "test_scene")
        with upserts(core_client) as upsert:
            body = await set_refused(
                tools,
                python_transform="config['id'] = 'completely_different_id'",
                config_hash=hash_value,
            )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert body["attempted_id"] == "completely_different_id"
        upsert.assert_not_awaited()

    @pytest.mark.parametrize(
        "expression", ["del config['id']", "config['id'] = None"], ids=["del", "none"]
    )
    async def test_dropping_the_id_updates_the_same_scene(
        self, hass: HomeAssistant, tools: ConfigSceneTools, expression: str
    ) -> None:
        """``id`` is optional in a scene config; dropping it is no rename."""
        result = await _transform(tools, expression)

        assert result["success"] is True
        assert [s["id"] for s in await stored_scenes(hass)] == ["test_scene"]


class TestTransformMetadata:
    """Metadata of an entity the transform removed goes with it, as with a
    full replacement."""

    @pytest.fixture
    async def tools(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> ConfigSceneTools:
        entities = {**TV_ON, "input_boolean.radio": {"state": "on"}}
        metadata = {key: {"entity_only": True} for key in entities}
        stored = scene("test_scene", "Test Scene", metadata=metadata)
        stored["entities"] = entities
        return await scene_tools(hass, core_client, monkeypatch, [stored])

    async def test_orphan_metadata_is_pruned_and_logged(
        self,
        hass: HomeAssistant,
        tools: ConfigSceneTools,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger="ha_mcp.tools.tools_config_scenes")

        await _transform(tools, "del config['entities']['input_boolean.radio']")

        (stored,) = await stored_scenes(hass)
        assert stored["entities"] == TV_ON
        assert stored["metadata"] == {"input_boolean.tv": {"entity_only": True}}
        assert any(
            "1 orphan" in r.message and "input_boolean.radio" in r.message
            for r in caplog.records
        )

    async def test_cores_refusal_of_dropped_entities_leads_unwritten(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        """Pruning metadata against entities the transform dropped must not
        turn Core's refusal into an internal error."""
        before = await stored_scenes(hass)
        hash_value = await config_hash(tools, "test_scene")
        body = await set_refused(
            tools,
            python_transform="del config['entities']",
            config_hash=hash_value,
            wait=False,
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert "required key not provided" in body["error"]["message"]
        assert await stored_scenes(hass) == before

    async def test_metadata_is_kept_when_entities_stay(
        self, hass: HomeAssistant, tools: ConfigSceneTools
    ) -> None:
        await _transform(
            tools, "config['entities']['input_boolean.tv']['state'] = 'off'"
        )

        (stored,) = await stored_scenes(hass)
        assert stored["metadata"] == {
            "input_boolean.tv": {"entity_only": True},
            "input_boolean.radio": {"entity_only": True},
        }


class TestCategoryValidation:
    """A category id must exist in the scene scope before anything is written."""

    async def test_category_of_another_scope_is_refused_unwritten(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        other_scope = category(hass, "Morning", scope="automation")
        with upserts(core_client) as upsert:
            error = (
                await set_refused(
                    tools,
                    config={"name": "X", "entities": TV_ON},
                    category=other_scope,
                    wait=False,
                )
            )["error"]

        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert other_scope in error["message"]
        assert "ha_config_set_category" in " ".join(error["suggestions"])
        upsert.assert_not_awaited()

    async def test_transform_with_unknown_category_is_refused_unwritten(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        category(hass, "Lighting")
        hash_value = await config_hash(tools, "test_scene")
        with upserts(core_client) as upsert:
            error = (
                await set_refused(
                    tools,
                    python_transform="config['name'] = 'x'",
                    config_hash=hash_value,
                    category="phantom_id",
                    wait=False,
                )
            )["error"]

        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "phantom_id" in error["message"]
        upsert.assert_not_awaited()

    async def test_unknown_category_inside_the_config_is_refused_unwritten(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
    ) -> None:
        """A top-level ``category`` in the config is checked like the parameter."""
        with upserts(core_client) as upsert:
            error = (
                await set_refused(
                    tools,
                    config={
                        "name": "X",
                        "category": "phantom_via_config_dict",
                        "entities": TV_ON,
                    },
                    wait=False,
                )
            )["error"]

        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "phantom_via_config_dict" in error["message"]
        upsert.assert_not_awaited()

    @pytest.mark.parametrize("transform", [False, True])
    async def test_an_unknown_category_is_refused_with_the_rest(
        self,
        core_client: HomeAssistantClient,
        tools: ConfigSceneTools,
        transform: bool,
    ) -> None:
        change = (
            {
                "python_transform": "config['id'] = 'other'",
                "config_hash": await config_hash(tools, "test_scene"),
            }
            if transform
            else {"config": {"id": "other", "entities": TV_ON}}
        )
        with upserts(core_client) as upsert:
            body = await set_refused(
                tools, scene_id="test_scene", category="phantom_id", **change
            )

        assert [p["code"] for p in body["problems"]] == [
            "VALIDATION_FAILED",
            "VALIDATION_INVALID_PARAMETER",
        ]
        upsert.assert_not_awaited()

    async def test_an_unreadable_category_registry_refuses_the_call_alone(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A transport failure is no input problem: it is not folded into the
        collected refusals, so the caller can tell to retry."""
        with (
            patch.object(
                core_client,
                "send_websocket_message",
                AsyncMock(side_effect=HomeAssistantConnectionError("down")),
            ),
            pytest.raises(ToolError) as exc,
        ):
            await category_problems(core_client, "lighting")

        assert error_of(exc)["code"].startswith("CONNECTION")

    async def test_no_category_lookup_without_a_category(
        self, hass: HomeAssistant, tools: ConfigSceneTools, sent: AsyncMock
    ) -> None:
        await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={"name": "X", "entities": TV_ON},
            wait=False,
        )
        await hass.async_block_till_done()

        assert sent_of_type(sent, "config/category_registry/list") == []
