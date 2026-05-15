"""Unit tests for ha_get_state single and bulk state retrieval tool."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_search import register_search_tools


class TestHaGetStates:
    """Test ha_get_state bulk entity state retrieval."""

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
        """Register tools and return the ha_get_state function."""
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_get_state"]

    @pytest.mark.asyncio
    async def test_all_entities_succeed(self, mock_client, get_states_tool):
        """All entities return states keyed by entity_id; no errors in response."""
        mock_client.get_entity_state = AsyncMock(
            side_effect=[
                {
                    "entity_id": "light.kitchen",
                    "state": "on",
                    "attributes": {"brightness": 255},
                },
                {"entity_id": "light.living_room", "state": "off", "attributes": {}},
            ]
        )

        result = await get_states_tool(entity_id=["light.kitchen", "light.living_room"])

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

        result = await get_states_tool(
            entity_id=["light.kitchen", "sensor.nonexistent"]
        )

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

        result = await get_states_tool(entity_id=["sensor.bad1", "sensor.bad2"])

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
        """Empty entity_ids list raises ToolError with validation error."""
        with pytest.raises(ToolError) as exc_info:
            await get_states_tool(entity_id=[])

        data = json.loads(str(exc_info.value))
        assert data["success"] is False
        assert data["error"]["code"] == "VALIDATION_FAILED"
        mock_client.get_entity_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_string_ids_rejected(self, mock_client, get_states_tool):
        """Non-string values in entity_ids raises ToolError with validation error."""
        with pytest.raises(ToolError) as exc_info:
            await get_states_tool(entity_id=["light.ok", 123])

        data = json.loads(str(exc_info.value))
        assert data["success"] is False
        assert data["error"]["code"] == "VALIDATION_FAILED"
        mock_client.get_entity_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_exceeds_max_entities_rejected(self, mock_client, get_states_tool):
        """More than 100 entity IDs raises ToolError with validation error."""
        ids = [f"sensor.test_{i}" for i in range(101)]

        with pytest.raises(ToolError) as exc_info:
            await get_states_tool(entity_id=ids)

        data = json.loads(str(exc_info.value))
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
            entity_id=["light.kitchen", "light.kitchen", "light.kitchen"]
        )

        data = result["data"]
        assert data["success"] is True
        assert data["count"] == 1
        assert "light.kitchen" in data["states"]
        assert mock_client.get_entity_state.call_count == 1

    @pytest.mark.asyncio
    async def test_404_uses_entity_not_found_error(self, mock_client, get_states_tool):
        """404 exceptions produce structured ENTITY_NOT_FOUND error with entity_id in message."""
        mock_client.get_entity_state = AsyncMock(side_effect=Exception("404 Not Found"))

        result = await get_states_tool(entity_id=["sensor.nonexistent"])

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

        result = await get_states_tool(entity_id=["sensor.temp"])

        data = result["data"]
        assert data["success"] is False
        error = data["errors"][0]["error"]
        assert error["code"] == "CONNECTION_FAILED"


class TestHaGetStateSingleEntity:
    """Test ha_get_state single-entity path (isinstance(entity_id, str))."""

    @pytest.fixture
    def mock_mcp(self):
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
        client = MagicMock()
        client.get_entity_state = AsyncMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        return MagicMock()

    @pytest.fixture
    def get_state_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_get_state"]

    @pytest.mark.asyncio
    async def test_single_entity_returns_state(self, mock_client, get_state_tool):
        """Single string entity_id returns state with timezone metadata."""
        mock_client.get_entity_state = AsyncMock(
            return_value={
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"brightness": 255},
            }
        )

        result = await get_state_tool(entity_id="light.kitchen")

        assert result["data"]["entity_id"] == "light.kitchen"
        assert result["data"]["state"] == "on"
        mock_client.get_entity_state.assert_called_once_with("light.kitchen")

    @pytest.mark.asyncio
    async def test_single_entity_not_found_raises_tool_error(self, mock_client, get_state_tool):
        """Single entity that doesn't exist raises ToolError."""
        mock_client.get_entity_state = AsyncMock(
            side_effect=Exception("404 Not Found")
        )

        with pytest.raises(ToolError) as exc_info:
            await get_state_tool(entity_id="sensor.nonexistent")

        error = json.loads(str(exc_info.value))
        assert error["success"] is False

    @pytest.mark.asyncio
    async def test_attribute_keys_no_effect_emits_warning_single(
        self, mock_client, get_state_tool
    ):
        """Single-entity: warn when attribute_keys is set but attributes is excluded from fields.

        Warning lives OUTSIDE the projected entity record (sibling of ``data``)
        so that ``fields=["state"]`` returns a record with only ``state`` and
        no extra warning key mixed into the projected entity-record keyspace.
        """
        mock_client.get_entity_state = AsyncMock(
            return_value={
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"brightness": 255},
            }
        )

        result = await get_state_tool(
            entity_id="light.kitchen",
            fields=["state"],
            attribute_keys=["brightness"],
        )

        # FIELDS PROJECTION contract: fields=["state"] returns ONLY {"state": ...}
        # in the projected record. ``warning`` must NOT leak into the record.
        data = result["data"]
        assert data == {"state": "on"}
        assert "warning" not in data
        # Warning lives at the top-level result, sibling of ``data``/``metadata``.
        assert "warning" in result
        assert "attribute_keys" in result["warning"]

    @pytest.mark.asyncio
    async def test_attribute_keys_no_warning_when_attributes_included(
        self, mock_client, get_state_tool
    ):
        """No warning when attributes IS in fields — attribute_keys applies."""
        mock_client.get_entity_state = AsyncMock(
            return_value={
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"brightness": 255, "color_temp": 3500},
            }
        )

        result = await get_state_tool(
            entity_id="light.kitchen",
            fields=["state", "attributes"],
            attribute_keys=["brightness"],
        )

        data = result["data"]
        assert "warning" not in data
        assert data["attributes"] == {"brightness": 255}

    @pytest.mark.asyncio
    async def test_non_dict_state_with_attribute_keys_no_effect_still_warns(
        self, mock_client, get_state_tool
    ):
        """Warning fires even when get_entity_state returns None (non-dict entity record).

        C2 regression: the isinstance(entity_record, dict) guard must NOT suppress
        the warning — add_timezone_metadata always returns a dict so the write is safe.
        """
        mock_client.get_entity_state = AsyncMock(return_value=None)

        result = await get_state_tool(
            entity_id="light.kitchen",
            fields=["state"],
            attribute_keys=["brightness"],
        )

        # Warning must be present at the top level even when entity_record is None
        assert "warning" in result
        assert "attribute_keys" in result["warning"]


class TestHaGetStateAttributeKeysWarningBulk:
    """Bulk-path warning when attribute_keys is set but 'attributes' is not in fields=."""

    @pytest.fixture
    def mock_mcp(self):
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
        client = MagicMock()
        client.get_entity_state = AsyncMock(
            return_value={
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"brightness": 255},
            }
        )
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        return client

    @pytest.fixture
    def get_states_tool(self, mock_mcp, mock_client):
        register_search_tools(mock_mcp, mock_client, smart_tools=MagicMock())
        return self.registered_tools["ha_get_state"]

    @pytest.mark.asyncio
    async def test_bulk_attribute_keys_no_effect_emits_warning(
        self, get_states_tool
    ):
        """Bulk-path: warn once at the top level when attribute_keys is silently ignored."""
        result = await get_states_tool(
            entity_id=["light.kitchen"],
            fields=["state"],
            attribute_keys=["brightness"],
        )

        data = result["data"]
        assert "warning" in data
        assert "attribute_keys" in data["warning"]
        assert data["states"]["light.kitchen"] == {"state": "on"}

    @pytest.mark.asyncio
    async def test_bulk_no_warning_when_attributes_in_fields(self, mock_client, get_states_tool):
        """When attributes IS in fields, attribute_keys applies and no warning is emitted."""
        mock_client.get_entity_state = AsyncMock(
            return_value={
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"brightness": 200, "color_temp": 3500},
            }
        )
        result = await get_states_tool(
            entity_id=["light.kitchen"],
            fields=["state", "attributes"],
            attribute_keys=["brightness"],
        )
        data = result["data"]
        assert "warning" not in data
        assert data["states"]["light.kitchen"]["attributes"] == {"brightness": 200}


class TestHaGetStateFieldsValidation:
    """Tests for malformed fields= and attribute_keys= parameter validation."""

    @pytest.fixture
    def mock_mcp(self):
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
        client = MagicMock()
        client.get_entity_state = AsyncMock(
            return_value={
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"brightness": 255},
            }
        )
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        return client

    @pytest.fixture
    def ha_get_state(self, mock_mcp, mock_client):
        register_search_tools(mock_mcp, mock_client, smart_tools=MagicMock())
        return self.registered_tools["ha_get_state"]

    @pytest.mark.asyncio
    async def test_bad_fields_integer_raises_tool_error(self, ha_get_state):
        """fields=123 raises ToolError with VALIDATION_FAILED and parameter='fields'.

        Pins the parameter attribution so a regression swapping the two raise
        sites (``fields`` vs ``attribute_keys``) can't silently pass — the
        symmetric mirror test for ``attribute_keys`` asserts the same hint.
        """
        with pytest.raises(ToolError) as exc_info:
            await ha_get_state(entity_id="light.kitchen", fields=123)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_FAILED"
        # parameter is surfaced at the top level of the error response
        assert error.get("parameter") == "fields"

    @pytest.mark.asyncio
    async def test_bad_json_fields_raises_tool_error(self, ha_get_state):
        """fields='[\"' (malformed JSON) raises ToolError."""
        with pytest.raises(ToolError):
            await ha_get_state(entity_id="light.kitchen", fields='["')

    @pytest.mark.asyncio
    async def test_bad_attribute_keys_raises_with_correct_param(self, ha_get_state):
        """attribute_keys=123 raises ToolError with parameter='attribute_keys' in the error."""
        with pytest.raises(ToolError) as exc_info:
            await ha_get_state(entity_id="light.kitchen", attribute_keys=123)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_FAILED"
        # parameter is surfaced at the top level of the error response
        assert error.get("parameter") == "attribute_keys"
