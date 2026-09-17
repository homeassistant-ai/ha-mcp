"""Scene discovery preserves storage identity and full attribute-value search."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools


@pytest.fixture
def scene_client():
    client = MagicMock(base_url=None, token=None)
    client.get_states = AsyncMock(
        return_value=[
            {"entity_id": "scene.renamed", "attributes": {"friendly_name": "Movie"}},
            {"entity_id": "scene.hue", "attributes": {"friendly_name": "Hue Relax"}},
            {"entity_id": "light.desk", "attributes": {}},
        ]
    )
    client.send_websocket_message = AsyncMock(
        return_value={
            "success": True,
            "result": [
                {
                    "entity_id": "scene.renamed",
                    "unique_id": "17000001",
                    "platform": "homeassistant",
                },
                {
                    "entity_id": "scene.hue",
                    "unique_id": "hue-vendor-id",
                    "platform": "hue",
                },
            ],
        }
    )
    client.get_scene_config = AsyncMock(
        return_value={
            "scene_id": "17000001",
            "config": {
                "id": "17000001",
                "name": "Movie",
                "entities": {
                    "light.desk": {"state": "on", "effect": "Northern Lights"}
                },
            },
        }
    )
    return client


async def test_list_keeps_storage_key_and_hue_is_not_editable(scene_client):
    result = await ConfigSceneTools(scene_client).ha_config_get_scene()
    assert result["success"] is True
    assert result["total"] == 2
    rows = {row["entity_id"]: row for row in result["scenes"]}
    assert rows["scene.renamed"]["scene_id"] == "17000001"
    assert rows["scene.renamed"]["config_available"] is True
    assert rows["scene.hue"]["scene_id"] is None
    assert rows["scene.hue"]["config_available"] is False
    assert rows["scene.hue"]["config_status"] == "integration_managed"
    assert "config" not in rows["scene.renamed"]
    scene_client.get_scene_config.assert_awaited_once()


async def test_storage_id_query_and_list_to_get_handoff(scene_client):
    tools = ConfigSceneTools(scene_client)
    result = await tools.ha_config_get_scene(query="17000001")
    assert len(result["scenes"]) == 1
    scene_id = result["scenes"][0]["scene_id"]
    body = await tools.ha_config_get_scene(scene_id=scene_id)
    assert body["scene_id"] == scene_id
    assert body["config"]["entities"]["light.desk"]["effect"] == "Northern Lights"
    assert body["config_hash"]


async def test_content_search_matches_attribute_values_only_when_requested(
    scene_client,
):
    tools = ConfigSceneTools(scene_client)
    shallow = await tools.ha_config_get_scene(query="northern lights")
    assert shallow["scenes"] == []
    scene_client.get_scene_config.assert_not_awaited()
    deep = await tools.ha_config_get_scene(
        query="northern lights", search_in_config=True
    )
    assert [row["scene_id"] for row in deep["scenes"]] == ["17000001"]
    assert deep["scenes"][0]["match_in_config"] is True
    assert "config" not in deep["scenes"][0]


async def test_pagination_reads_only_returned_page(scene_client):
    tools = ConfigSceneTools(scene_client)
    first = await tools.ha_config_get_scene(limit=1)
    assert first["total"] == 2
    assert first["has_more"] is True
    assert first["next_offset"] == 1
    assert first["scenes"][0]["entity_id"] == "scene.hue"
    scene_client.get_scene_config.assert_not_awaited()
    second = await tools.ha_config_get_scene(limit=1, offset=1)
    assert second["scenes"][0]["scene_id"] == "17000001"
    assert second["has_more"] is False
    scene_client.get_scene_config.assert_awaited_once()


async def test_yaml_config_404_does_not_claim_editable_storage(scene_client):
    scene_client.get_scene_config.side_effect = HomeAssistantAPIError(
        "missing", status_code=404
    )
    result = await ConfigSceneTools(scene_client).ha_config_get_scene(query="Movie")
    row = result["scenes"][0]
    assert row["scene_id"] is None
    assert row["config_available"] is False
    assert row["config_status"] == "not_in_storage"


async def test_config_failure_is_partial_not_no_match(scene_client):
    scene_client.get_scene_config.side_effect = HomeAssistantAPIError(
        "unavailable", status_code=500
    )
    result = await ConfigSceneTools(scene_client).ha_config_get_scene(
        query="northern lights", search_in_config=True
    )
    assert result["partial"] is True
    assert result["config_scan"]["failed"] == 1
    assert "not exhaustive" in result["partial_reason"]


async def test_single_get_does_not_discover_or_probe_component(scene_client):
    result = await ConfigSceneTools(scene_client).ha_config_get_scene("17000001")
    assert result["action"] == "get"
    scene_client.get_states.assert_not_awaited()


@pytest.mark.parametrize("arguments", [{"scene_id": ""}, {"limit": 0}, {"offset": -1}])
async def test_invalid_discovery_parameters_raise_tool_error(scene_client, arguments):
    from ha_mcp._vendor.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await ConfigSceneTools(scene_client).ha_config_get_scene(**arguments)


async def test_component_inventory_pages_without_rest_states(scene_client, monkeypatch):
    from ha_mcp.tools import scene_discovery as discovery
    from ha_mcp.tools.component_api import (
        DEVICE_REGISTRY_CHILD_SEMANTICS,
        ComponentCaps,
    )

    caps = ComponentCaps(
        1,
        "2.1.0",
        frozenset({"search", DEVICE_REGISTRY_CHILD_SEMANTICS}),
        {"max_results": 1},
    )
    monkeypatch.setattr(discovery, "get_component_caps", AsyncMock(return_value=caps))
    ws = MagicMock()
    ws.send_command = AsyncMock(
        side_effect=[
            {
                "result": {
                    "entities": [
                        {"entity_id": "scene.renamed", "friendly_name": "Movie"}
                    ],
                    "entity_has_more": True,
                }
            },
            {
                "result": {
                    "entities": [
                        {"entity_id": "scene.hue", "friendly_name": "Hue Relax"}
                    ],
                    "entity_has_more": False,
                }
            },
        ]
    )
    monkeypatch.setattr(discovery, "get_websocket_client", AsyncMock(return_value=ws))
    result = await ConfigSceneTools(scene_client).ha_config_get_scene()
    assert result["total"] == 2
    scene_client.get_states.assert_not_awaited()
    assert [call.kwargs["offset"] for call in ws.send_command.await_args_list] == [0, 1]
    assert all(call.kwargs["limit"] == 1 for call in ws.send_command.await_args_list)
    assert all(
        call.kwargs["search_types"] == ["entity"]
        for call in ws.send_command.await_args_list
    )


async def test_component_inventory_failure_falls_back_to_rest(
    scene_client, monkeypatch
):
    from ha_mcp.tools import scene_discovery as discovery
    from ha_mcp.tools.component_api import (
        DEVICE_REGISTRY_CHILD_SEMANTICS,
        ComponentCaps,
    )

    caps = ComponentCaps(
        1, "2.1.0", frozenset({"search", DEVICE_REGISTRY_CHILD_SEMANTICS}), {}
    )
    monkeypatch.setattr(discovery, "get_component_caps", AsyncMock(return_value=caps))
    monkeypatch.setattr(
        discovery, "get_websocket_client", AsyncMock(side_effect=OSError("offline"))
    )
    result = await ConfigSceneTools(scene_client).ha_config_get_scene()
    assert result["total"] == 2
    scene_client.get_states.assert_awaited_once()


async def test_component_capability_failure_falls_back_to_rest(
    scene_client, monkeypatch
):
    from ha_mcp.tools import scene_discovery as discovery

    monkeypatch.setattr(
        discovery, "get_component_caps", AsyncMock(side_effect=OSError("offline"))
    )

    result = await ConfigSceneTools(scene_client).ha_config_get_scene()

    assert result["total"] == 2
    scene_client.get_states.assert_awaited_once()


@pytest.mark.parametrize(
    "incomplete_signal",
    [
        {"partial": True, "partial_reason": "snapshot incomplete"},
        {"diagnostics": {"entities": "registry unavailable"}},
    ],
)
async def test_incomplete_component_inventory_falls_back_to_rest(
    scene_client, monkeypatch, incomplete_signal
):
    from ha_mcp.tools import scene_discovery as discovery
    from ha_mcp.tools.component_api import (
        DEVICE_REGISTRY_CHILD_SEMANTICS,
        ComponentCaps,
    )

    caps = ComponentCaps(
        1, "2.1.0", frozenset({"search", DEVICE_REGISTRY_CHILD_SEMANTICS}), {}
    )
    monkeypatch.setattr(discovery, "get_component_caps", AsyncMock(return_value=caps))
    ws = MagicMock()
    ws.send_command = AsyncMock(
        return_value={
            "result": {
                "entities": [{"entity_id": "scene.renamed", "friendly_name": "Movie"}],
                "entity_has_more": False,
                **incomplete_signal,
            }
        }
    )
    monkeypatch.setattr(discovery, "get_websocket_client", AsyncMock(return_value=ws))

    result = await ConfigSceneTools(scene_client).ha_config_get_scene()

    assert result["total"] == 2
    scene_client.get_states.assert_awaited_once()


async def test_malformed_config_is_reported_as_partial(scene_client):
    scene_client.get_scene_config.return_value = {"config": None}

    result = await ConfigSceneTools(scene_client).ha_config_get_scene(
        query="northern lights", search_in_config=True
    )

    assert result["partial"] is True
    assert result["config_scan"]["failed"] == 1
    assert result["total_is_exact"] is False


async def test_content_timeout_cancels_reads_and_reports_incomplete_total(
    scene_client, monkeypatch
):
    from ha_mcp.tools import scene_discovery as discovery

    cancelled = asyncio.Event()

    async def blocked_read(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    scene_client.get_scene_config.side_effect = blocked_read
    monkeypatch.setattr(discovery, "CONFIG_SCAN_TIMEOUT", 0.01)
    result = await ConfigSceneTools(scene_client).ha_config_get_scene(
        query="rainbow", search_in_config=True
    )
    assert result["partial"] is True
    assert result["config_scan"]["not_scanned"] == 1
    assert result["total_is_exact"] is False
    assert cancelled.is_set()


async def test_registry_component_read_preserves_storage_identity(
    scene_client, monkeypatch
):
    from ha_mcp.tools import scene_discovery as discovery

    lookup = AsyncMock(
        return_value={
            "entities": scene_client.send_websocket_message.return_value["result"]
        }
    )
    monkeypatch.setattr(discovery, "resolve_entities_via_component", lookup)
    result = await ConfigSceneTools(scene_client).ha_config_get_scene(query="Movie")
    assert result["scenes"][0]["scene_id"] == "17000001"
    scene_client.send_websocket_message.assert_not_awaited()
    lookup.assert_awaited_once_with(scene_client, ["scene.renamed", "scene.hue"])
