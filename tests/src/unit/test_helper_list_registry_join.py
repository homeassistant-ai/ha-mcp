"""
Unit tests for ha_config_list_helpers entity-registry enrichment (issue #1794).

``ha_config_list_helpers`` builds its records from the ``{helper_type}/list``
storage-collection WebSocket response, which carries the immutable ``id``
(unique_id) and creation-time ``name``. After a UI rename those go stale — the
current ``entity_id`` and display name live in the entity registry. These tests
pin that the tool joins the entity registry so a renamed helper surfaces its
current ``entity_id`` and ``name`` (keeping the storage ``id`` and the original
name for reference), and that the join degrades gracefully.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_client():
    """Mock client whose WS handler is assembled per-test from canned responses."""
    return MagicMock()


@pytest.fixture
def register_tools(mock_client):
    """Register helper config tools and return the captured tool callables."""
    from ha_mcp.tools.tools_config_helpers import register_config_helper_tools

    registered_tools: dict[str, Any] = {}

    def capture_add_tool(method):
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered_tools[name] = method

    mock_mcp = MagicMock()
    mock_mcp.add_tool = capture_add_tool
    register_config_helper_tools(mock_mcp, mock_client)
    return registered_tools


def _ws_handler(list_items: list[dict], registry: list[dict] | Exception):
    """Build a send_websocket_message side_effect for list + registry responses."""

    async def handler(msg: dict) -> dict:
        msg_type = msg.get("type", "")
        if msg_type == "config/entity_registry/list":
            if isinstance(registry, Exception):
                raise registry
            return {"success": True, "result": registry}
        if msg_type.endswith("/list"):
            return {"success": True, "result": list_items}
        return {"success": True, "result": {}}

    return handler


class TestListHelpersRegistryJoin:
    async def test_renamed_helper_surfaces_current_entity_id_and_name(
        self, register_tools, mock_client
    ):
        """A helper renamed in the UI (entity_id + name changed, unique_id kept)
        must surface the current entity_id and display name, not the stale
        storage values."""
        list_items = [
            {"id": "dark_enough", "name": "Dark Enough", "icon": "mdi:brightness-6"}
        ]
        registry = [
            {
                "entity_id": "input_boolean.dark_enough_mode",
                "unique_id": "dark_enough",
                "platform": "input_boolean",
                "name": "Dark Enough Mode",
                "original_name": "Dark Enough",
            }
        ]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(list_items, registry)
        )

        result = await register_tools["ha_config_list_helpers"](
            helper_type="input_boolean"
        )

        assert result["success"] is True
        helper = result["helpers"][0]
        # Storage id preserved (still the key HA's collection uses).
        assert helper["id"] == "dark_enough"
        # Current values surfaced from the registry.
        assert helper["entity_id"] == "input_boolean.dark_enough_mode"
        assert helper["name"] == "Dark Enough Mode"
        # Original creation-time name kept, labeled.
        assert helper["original_name"] == "Dark Enough"

    async def test_registry_name_null_falls_back_to_original_name(
        self, register_tools, mock_client
    ):
        """When the registry entry has no custom name (name is null), the current
        name falls back to original_name and entity_id is still surfaced."""
        list_items = [{"id": "guest_mode", "name": "Guest Mode"}]
        registry = [
            {
                "entity_id": "input_boolean.guest_mode",
                "unique_id": "guest_mode",
                "platform": "input_boolean",
                "name": None,
                "original_name": "Guest Mode",
            }
        ]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(list_items, registry)
        )

        result = await register_tools["ha_config_list_helpers"](
            helper_type="input_boolean"
        )

        helper = result["helpers"][0]
        assert helper["entity_id"] == "input_boolean.guest_mode"
        assert helper["name"] == "Guest Mode"

    async def test_no_registry_match_keeps_storage_values(
        self, register_tools, mock_client
    ):
        """A helper with no matching registry entry (e.g. a tag, which has no
        entity) keeps its storage id/name and gains no entity_id."""
        list_items = [{"id": "abc123", "name": "My Tag"}]
        registry: list[dict] = []
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(list_items, registry)
        )

        result = await register_tools["ha_config_list_helpers"](helper_type="tag")

        helper = result["helpers"][0]
        assert helper["id"] == "abc123"
        assert helper["name"] == "My Tag"
        assert "entity_id" not in helper

    async def test_platform_mismatch_is_not_joined(self, register_tools, mock_client):
        """A registry entry sharing the unique_id but on a different platform must
        not be joined (guards against cross-platform unique_id collision)."""
        list_items = [{"id": "shared", "name": "Storage Name"}]
        registry = [
            {
                "entity_id": "sensor.shared_thing",
                "unique_id": "shared",
                "platform": "sensor",
                "name": "Some Sensor",
                "original_name": "Some Sensor",
            }
        ]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(list_items, registry)
        )

        result = await register_tools["ha_config_list_helpers"](
            helper_type="input_boolean"
        )

        helper = result["helpers"][0]
        assert helper["name"] == "Storage Name"
        assert "entity_id" not in helper

    async def test_registry_fetch_failure_degrades_open_with_warning(
        self, register_tools, mock_client
    ):
        """If the entity_registry/list call fails, the tool still returns the
        helper list (as before the join) and flags the degradation."""
        list_items = [{"id": "dark_enough", "name": "Dark Enough"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(list_items, TimeoutError("ws boom"))
        )

        result = await register_tools["ha_config_list_helpers"](
            helper_type="input_boolean"
        )

        assert result["success"] is True
        assert result["count"] == 1
        helper = result["helpers"][0]
        assert helper["id"] == "dark_enough"
        # No enrichment, but the list still works.
        assert "entity_id" not in helper
        assert any("registry" in w.lower() for w in result.get("warnings", []))

    async def test_success_without_result_key_degrades_open_with_warning(
        self, register_tools, mock_client
    ):
        """A registry response that reports success but omits ``result`` is a
        malformed read, not an empty registry: the tool must flag it rather than
        silently returning un-enriched records."""

        async def handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/entity_registry/list":
                return {"success": True}  # no "result" key
            if msg_type.endswith("/list"):
                return {"success": True, "result": [{"id": "x", "name": "X"}]}
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=handler)

        result = await register_tools["ha_config_list_helpers"](
            helper_type="input_boolean"
        )

        helper = result["helpers"][0]
        assert helper == {"id": "x", "name": "X"}
        assert "entity_id" not in helper
        assert any("registry" in w.lower() for w in result.get("warnings", []))

    async def test_registry_unsuccessful_response_degrades_open_with_warning(
        self, register_tools, mock_client
    ):
        """The registry read never raises in production — the client returns
        ``{"success": False, ...}`` on failure rather than throwing. That
        unsuccessful response is a malformed read: the tool must flag it and
        return the un-enriched list (the branch production actually takes)."""

        async def handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/entity_registry/list":
                return {"success": False, "error": "registry unavailable"}
            if msg_type.endswith("/list"):
                return {"success": True, "result": [{"id": "x", "name": "X"}]}
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=handler)

        result = await register_tools["ha_config_list_helpers"](
            helper_type="input_boolean"
        )

        helper = result["helpers"][0]
        assert helper == {"id": "x", "name": "X"}
        assert "entity_id" not in helper
        assert any("registry" in w.lower() for w in result.get("warnings", []))

    async def test_registry_unexpected_shape_degrades_open_without_raising(
        self, register_tools, mock_client
    ):
        """An unexpected record shape (here a non-hashable ``id`` that breaks the
        registry lookup) must degrade open. Enrichment is cosmetic, so a raise
        from the join must never turn a list call into a failure — the broad
        guard converts it to the un-enriched list plus a warning."""
        list_items = [{"id": ["not", "hashable"], "name": "X"}]
        registry = [
            {
                "entity_id": "input_boolean.x",
                "unique_id": "x",
                "platform": "input_boolean",
                "name": "X",
            }
        ]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(list_items, registry)
        )

        result = await register_tools["ha_config_list_helpers"](
            helper_type="input_boolean"
        )

        assert result["success"] is True
        assert any("registry" in w.lower() for w in result.get("warnings", []))

    async def test_person_dict_shaped_list_is_flattened_and_enriched(
        self, register_tools, mock_client
    ):
        """person/list returns {"storage": [...], "config": [...]} rather than a
        flat list; the tool must flatten it (so count and shape are right) and
        still enrich each record from the entity registry."""

        async def handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "person.mirko",
                            "unique_id": "mirko",
                            "platform": "person",
                            "name": "Mirko Renamed",
                            "original_name": "Mirko",
                        },
                        {
                            "entity_id": "person.guest",
                            "unique_id": "guest",
                            "platform": "person",
                            "name": None,
                            "original_name": "Guest",
                        },
                    ],
                }
            if msg_type == "person/list":
                # Both storage- and config-entry-backed persons must be merged.
                return {
                    "success": True,
                    "result": {
                        "storage": [{"id": "mirko", "name": "Mirko"}],
                        "config": [{"id": "guest", "name": "Guest"}],
                    },
                }
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=handler)

        result = await register_tools["ha_config_list_helpers"](helper_type="person")

        # Flattened across storage + config: count is the person count, not
        # len({"storage", "config"}) == 2 by coincidence here — assert the ids.
        assert result["count"] == 2
        assert isinstance(result["helpers"], list)
        by_id = {h["id"]: h for h in result["helpers"]}
        assert by_id.keys() == {"mirko", "guest"}
        assert by_id["mirko"]["entity_id"] == "person.mirko"
        assert by_id["mirko"]["name"] == "Mirko Renamed"
        assert by_id["mirko"]["original_name"] == "Mirko"
        # config-entry person is enriched too (registry name null -> original_name).
        assert by_id["guest"]["entity_id"] == "person.guest"
        assert by_id["guest"]["name"] == "Guest"
