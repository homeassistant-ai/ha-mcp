"""Scene discovery on real Core keeps storage identity and searches stored bodies.

Core holds two scenes: ``scene.movie`` from ``scenes.yaml`` (storage key
``17000001``, unrelated to its name-derived entity_id) and ``scene.hue``, a
Hue-owned registry entry with no editable config. Where the storage scene is
defined in ``configuration.yaml`` instead, Core's config API answers 404.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr

from custom_components.ha_mcp_tools.websocket_api import constants
from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.tools import scene_discovery
from ha_mcp.tools.helpers import register_tool_methods
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools

from ._config_dir_yaml import setup_scenes

_MOVIE = {
    "id": "17000001",
    "name": "Movie",
    "entities": {"light.desk": {"state": "on", "effect": "Northern Lights"}},
}


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Core's scene config API reads ``scenes.yaml`` here."""
    return hass_tmp_config_dir


def _hue_scene(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> None:
    entity_registry.async_get_or_create(
        "scene", "hue", "hue-vendor-id", suggested_object_id="hue"
    )
    hass.states.async_set("scene.hue", "unknown", {"friendly_name": "Hue Relax"})


@pytest.fixture
async def stored(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> None:
    """``Movie`` in ``scenes.yaml`` beside the Hue scene."""
    await setup_scenes(hass, [_MOVIE])
    _hue_scene(hass, entity_registry)


@pytest.fixture
async def yaml_only(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> None:
    """``Movie`` in ``configuration.yaml``: Core runs it, its config API cannot read it."""
    await setup_scenes(hass, [], yaml_only=[_MOVIE])
    _hue_scene(hass, entity_registry)


def _spy(client: HomeAssistantClient, method: str) -> Any:
    return patch.object(client, method, wraps=getattr(client, method))


@pytest.mark.usefixtures("stored")
async def test_list_keeps_storage_key_and_hue_is_not_editable(
    core_client: HomeAssistantClient,
) -> None:
    with _spy(core_client, "get_scene_config") as read:
        result = await ConfigSceneTools(core_client).ha_config_get_scene()

    assert result["success"] is True
    assert result["total"] == 2
    rows = {row["entity_id"]: row for row in result["scenes"]}
    assert rows["scene.movie"]["scene_id"] == "17000001"
    assert rows["scene.movie"]["config_available"] is True
    assert "config" not in rows["scene.movie"]
    assert rows["scene.hue"]["scene_id"] is None
    assert rows["scene.hue"]["config_available"] is False
    assert rows["scene.hue"]["config_status"] == "integration_managed"
    # Only the storage scene's body is read; the Hue scene has none.
    read.assert_awaited_once()


@pytest.mark.usefixtures("stored")
async def test_storage_id_query_hands_off_to_get(
    core_client: HomeAssistantClient,
) -> None:
    tools = ConfigSceneTools(core_client)
    result = await tools.ha_config_get_scene(query="17000001")
    assert len(result["scenes"]) == 1
    scene_id = result["scenes"][0]["scene_id"]

    body = await tools.ha_config_get_scene(scene_id=scene_id)

    assert body["scene_id"] == scene_id
    assert body["config"]["entities"]["light.desk"]["effect"] == "Northern Lights"
    assert body["config_hash"]


@pytest.mark.usefixtures("stored")
async def test_attribute_values_are_searched_only_when_requested(
    core_client: HomeAssistantClient,
) -> None:
    tools = ConfigSceneTools(core_client)
    with _spy(core_client, "get_scene_config") as read:
        shallow = await tools.ha_config_get_scene(query="northern lights")
        assert shallow["scenes"] == []
        read.assert_not_awaited()

        deep = await tools.ha_config_get_scene(
            query="northern lights", search_in_config=True
        )

    assert [row["scene_id"] for row in deep["scenes"]] == ["17000001"]
    assert deep["scenes"][0]["match_in_config"] is True
    assert "config" not in deep["scenes"][0]


@pytest.mark.usefixtures("stored")
async def test_pagination_reads_only_the_returned_page(
    core_client: HomeAssistantClient,
) -> None:
    tools = ConfigSceneTools(core_client)
    with _spy(core_client, "get_scene_config") as read:
        first = await tools.ha_config_get_scene(limit=1)
        assert first["total"] == 2
        assert first["has_more"] is True
        assert first["next_offset"] == 1
        assert first["scenes"][0]["entity_id"] == "scene.hue"
        read.assert_not_awaited()

        second = await tools.ha_config_get_scene(limit=1, offset=1)

    assert second["scenes"][0]["scene_id"] == "17000001"
    assert second["has_more"] is False
    read.assert_awaited_once()


@pytest.mark.usefixtures("yaml_only")
async def test_yaml_scene_is_not_claimed_as_editable_storage(
    core_client: HomeAssistantClient,
) -> None:
    result = await ConfigSceneTools(core_client).ha_config_get_scene(query="Movie")

    row = result["scenes"][0]
    assert row["entity_id"] == "scene.movie"
    assert row["scene_id"] is None
    assert row["config_available"] is False
    assert row["config_status"] == "not_in_storage"


@pytest.mark.usefixtures("stored")
async def test_unreadable_config_is_partial_not_no_match(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core answers 500 when ``scenes.yaml`` no longer parses."""
    path = Path(hass.config.path("scenes.yaml"))
    await hass.async_add_executor_job(path.write_text, "- id: [unclosed\n")

    result = await ConfigSceneTools(core_client).ha_config_get_scene(
        query="northern lights", search_in_config=True
    )

    assert result["partial"] is True
    assert result["config_scan"]["failed"] == 1
    assert "not exhaustive" in result["partial_reason"]


@pytest.mark.usefixtures("yaml_only")
@pytest.mark.parametrize("query", ["Movie", "northern lights"])
async def test_content_search_over_a_yaml_scene_reports_an_incomplete_total(
    core_client: HomeAssistantClient, query: str
) -> None:
    """The YAML scene's body cannot be read, so a content miss is not a miss."""
    result = await ConfigSceneTools(core_client).ha_config_get_scene(
        query=query, search_in_config=True
    )

    assert result["partial"] is True
    assert result["total_is_exact"] is False
    assert result["config_scan"]["not_in_storage"] == 1
    assert result["config_scan"]["failed"] == 0
    assert "not exhaustive" in result["partial_reason"]
    if query == "Movie":
        assert result["scenes"][0]["config_status"] == "not_in_storage"
        assert result["scenes"][0]["scene_id"] is None
    else:
        assert result["scenes"] == []
        assert result["total"] == 0


@pytest.mark.usefixtures("stored")
async def test_single_get_does_not_list_scenes(
    core_client: HomeAssistantClient,
) -> None:
    with _spy(core_client, "get_states") as states:
        result = await ConfigSceneTools(core_client).ha_config_get_scene("17000001")

    assert result["action"] == "get"
    states.assert_not_awaited()


@pytest.mark.usefixtures("stored")
@pytest.mark.parametrize(
    "arguments", [{"scene_id": ""}, {"limit": 0}, {"limit": 101}, {"offset": -1}]
)
async def test_invalid_discovery_parameters_are_refused_before_reading(
    core_client: HomeAssistantClient, arguments: dict[str, str | int]
) -> None:
    mcp = FastMCP("scene-discovery-validation")
    register_tool_methods(mcp, ConfigSceneTools(core_client))
    with (
        _spy(core_client, "get_states") as states,
        _spy(core_client, "get_scene_config") as read,
    ):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "ha_config_get_scene", arguments, raise_on_error=False
            )

    assert result.is_error
    states.assert_not_awaited()
    read.assert_not_awaited()


@pytest.mark.usefixtures("stored")
async def test_component_inventory_pages_without_rest_states(
    component_core_client: HomeAssistantClient,
) -> None:
    """The component's advertised page size is honoured page by page."""
    ws = await get_websocket_client(
        url=component_core_client.base_url,
        token=component_core_client.token,
        verify_ssl=component_core_client.verify_ssl,
    )
    with (
        patch.dict(constants.LIMITS, {"max_results": 1}),
        _spy(component_core_client, "get_states") as states,
        patch.object(ws, "send_command", wraps=ws.send_command) as sent,
    ):
        result = await ConfigSceneTools(component_core_client).ha_config_get_scene()

    assert result["total"] == 2
    assert {row["entity_id"] for row in result["scenes"]} == {
        "scene.hue",
        "scene.movie",
    }
    states.assert_not_awaited()
    searches = [
        call.kwargs
        for call in sent.await_args_list
        if call.args == ("ha_mcp_tools/search",)
    ]
    assert [search["offset"] for search in searches] == [0, 1]
    assert all(search["limit"] == 1 for search in searches)
    assert all(search["search_types"] == ["entity"] for search in searches)


@pytest.mark.usefixtures("stored")
async def test_component_inventory_without_a_socket_falls_back_to_rest(
    component_core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        scene_discovery,
        "get_websocket_client",
        AsyncMock(side_effect=OSError("offline")),
    )
    with _spy(component_core_client, "get_states") as states:
        result = await ConfigSceneTools(component_core_client).ha_config_get_scene()

    assert result["total"] == 2
    states.assert_awaited_once()


@pytest.mark.usefixtures("stored")
async def test_without_the_component_the_inventory_comes_from_rest(
    core_client: HomeAssistantClient,
) -> None:
    with _spy(core_client, "get_states") as states:
        result = await ConfigSceneTools(core_client).ha_config_get_scene()

    assert result["total"] == 2
    states.assert_awaited_once()


@pytest.mark.usefixtures("stored")
async def test_partial_component_inventory_falls_back_to_rest(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    """A registry the component cannot read makes its search partial; the
    tool must not take that page as the whole inventory."""
    floor = floor_registry.async_create("Ground")
    area = area_registry.async_create("Living", floor_id=floor.floor_id)
    entity_registry.async_update_entity("scene.movie", area_id=area.id)

    with (
        patch.object(
            fr.FloorRegistry,
            "async_get_floor",
            side_effect=RuntimeError("floor registry unavailable"),
        ),
        _spy(component_core_client, "get_states") as states,
    ):
        result = await ConfigSceneTools(component_core_client).ha_config_get_scene()

    assert result["total"] == 2
    states.assert_awaited_once()


@pytest.mark.usefixtures("stored")
async def test_content_scan_deadline_cancels_reads_and_reports_incomplete_total(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads still in flight at the deadline are cancelled (Core's plugin fails
    a test that leaves a task behind) and counted as not scanned."""
    monkeypatch.setattr(scene_discovery, "CONFIG_SCAN_TIMEOUT", 0)

    with _spy(core_client, "get_scene_config") as read:
        result = await ConfigSceneTools(core_client).ha_config_get_scene(
            query="rainbow", search_in_config=True
        )

    read.assert_awaited_once()
    assert result["partial"] is True
    assert result["config_scan"]["not_scanned"] == 1
    assert result["total_is_exact"] is False


@pytest.mark.usefixtures("stored")
async def test_registry_rows_come_from_the_component(
    component_core_client: HomeAssistantClient,
) -> None:
    """With the component, the scene rows are looked up by entity_id instead of
    listing Core's whole entity registry, and keep the storage key."""
    lookup = AsyncMock(wraps=scene_discovery.resolve_entities_via_component)
    with (
        patch.object(scene_discovery, "resolve_entities_via_component", lookup),
        _spy(component_core_client, "send_websocket_message") as legacy,
    ):
        result = await ConfigSceneTools(component_core_client).ha_config_get_scene(
            query="Movie"
        )

    assert result["scenes"][0]["scene_id"] == "17000001"
    legacy.assert_not_awaited()
    lookup.assert_awaited_once_with(component_core_client, ["scene.hue", "scene.movie"])
