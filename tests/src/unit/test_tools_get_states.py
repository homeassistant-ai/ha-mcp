"""Unit tests for ha_get_states bulk state retrieval tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_search import register_search_tools


class TestHaGetStates:
    """Test ha_get_states bulk entity state retrieval."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        self.registered_tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func
            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.get_entity_state = AsyncMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        """Create a mock smart_tools instance."""
        return MagicMock()

    @pytest.fixture
    def get_states_tool(self, mock_mcp, mock_client, mock_smart_tools):
        """Register tools and return the ha_get_states function."""
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_get_states"]

    @pytest.mark.asyncio
    async def test_all_entities_succeed(self, mock_client, get_states_tool):
        """All entities return states keyed by entity_id; no errors in response."""
        mock_client.get_entity_state = AsyncMock(
            side_effect=[
                {"entity_id": "light.kitchen", "state": "on", "attributes": {"brightness": 255}},
                {"entity_id": "light.living_room", "state": "off", "attributes": {}},
            ]
        )

        result = await get_states_tool(entity_ids=["light.kitchen", "light.living_room"])

        data = result["data"]
        assert data["success"] is True
        assert data["count"] == 2
        assert len(data["states"]) == 2
        assert "light.kitchen" in data["states"]
        assert data["states"]["light.kitchen"]["state"] == "on"
        assert "light.living_room" in data["states"]
        assert data["states"]["light.living_room"]["state"] == "off"
        assert "errors" not in data
        assert "error_count" not in data
        assert "partial" not in data
        assert mock_client.get_entity_state.call_count == 2

    @pytest.mark.asyncio
    async def test_partial_failure(self, mock_client, get_states_tool):
        """One entity succeeds, one 404s; success is True, both results and errors present."""
        mock_client.get_entity_state = AsyncMock(
            side_effect=[
                {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
                Exception("404 Not Found"),
            ]
        )

        result = await get_states_tool(entity_ids=["light.kitchen", "sensor.nonexistent"])

        data = result["data"]
        assert data["success"] is True
        assert data["count"] == 1
        assert data["error_count"] == 1
        assert "light.kitchen" in data["states"]
        assert data["states"]["light.kitchen"]["state"] == "on"
        assert len(data["errors"]) == 1
        assert data["errors"][0]["entity_id"] == "sensor.nonexistent"
        assert data["errors"][0]["error"]["code"] == "ENTITY_NOT_FOUND"
        assert data["partial"] is True
        assert "suggestions" in data

    @pytest.mark.asyncio
    async def test_all_fail(self, mock_client, get_states_tool):
        """All entities fail; success is False, states empty, errors populated."""
        mock_client.get_entity_state = AsyncMock(
            side_effect=[
                Exception("404 Not Found"),
                Exception("Connection refused"),
            ]
        )

        result = await get_states_tool(entity_ids=["sensor.bad1", "sensor.bad2"])

        data = result["data"]
        assert data["success"] is False
        assert data["count"] == 0
        assert data["error_count"] == 2
        assert len(data["states"]) == 0
        assert len(data["errors"]) == 2
        assert data["errors"][0]["entity_id"] == "sensor.bad1"
        assert data["errors"][1]["entity_id"] == "sensor.bad2"
        assert "partial" not in data
        assert "suggestions" in data

    @pytest.mark.asyncio
    async def test_empty_list_rejected(self, mock_client, get_states_tool):
        """Empty entity_ids list returns structured validation error."""
        result = await get_states_tool(entity_ids=[])

        data = result["data"]
        assert data["success"] is False
        assert data["error"]["code"] == "VALIDATION_FAILED"
        assert data["parameter"] == "entity_ids"
        mock_client.get_entity_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_string_ids_rejected(self, mock_client, get_states_tool):
        """Non-string values in entity_ids returns structured validation error."""
        result = await get_states_tool(entity_ids=["light.ok", 123])

        data = result["data"]
        assert data["success"] is False
        assert data["error"]["code"] == "VALIDATION_FAILED"
        mock_client.get_entity_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_exceeds_max_entities_rejected(self, mock_client, get_states_tool):
        """More than 100 entity IDs returns structured validation error."""
        ids = [f"sensor.test_{i}" for i in range(101)]

        result = await get_states_tool(entity_ids=ids)

        data = result["data"]
        assert data["success"] is False
        assert data["error"]["code"] == "VALIDATION_FAILED"
        assert "101" in data["error"]["message"]
        mock_client.get_entity_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_ids_deduplicated(self, mock_client, get_states_tool):
        """Duplicate entity IDs are deduplicated; client called once per unique ID."""
        mock_client.get_entity_state = AsyncMock(
            return_value={"entity_id": "light.kitchen", "state": "on", "attributes": {}}
        )

        result = await get_states_tool(
            entity_ids=["light.kitchen", "light.kitchen", "light.kitchen"]
        )

        data = result["data"]
        assert data["success"] is True
        assert data["count"] == 1
        assert "light.kitchen" in data["states"]
        assert mock_client.get_entity_state.call_count == 1

    @pytest.mark.asyncio
    async def test_404_uses_entity_not_found_error(self, mock_client, get_states_tool):
        """404 exceptions produce structured ENTITY_NOT_FOUND error with entity_id in message."""
        mock_client.get_entity_state = AsyncMock(
            side_effect=Exception("404 Not Found")
        )

        result = await get_states_tool(entity_ids=["sensor.nonexistent"])

        data = result["data"]
        error = data["errors"][0]["error"]
        assert error["code"] == "ENTITY_NOT_FOUND"
        assert "sensor.nonexistent" in error["message"]
        assert "suggestions" in data
        assert any("ha_search_entities" in s for s in data["suggestions"])

    @pytest.mark.asyncio
    async def test_non_404_uses_structured_error(self, mock_client, get_states_tool):
        """Non-404 exceptions use exception_to_structured_error with CONNECTION_FAILED code."""
        mock_client.get_entity_state = AsyncMock(
            side_effect=Exception("Connection refused")
        )

        result = await get_states_tool(entity_ids=["sensor.temp"])

        data = result["data"]
        assert data["success"] is False
        error = data["errors"][0]["error"]
        assert error["code"] == "CONNECTION_FAILED"
