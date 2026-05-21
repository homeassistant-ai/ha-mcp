"""Unit tests for fields= projection in ha_list_services (issue #1199)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_services import register_services_tools


class TestHaListServicesFieldsProjection:
    """Tool-level tests for fields= projection in ha_list_services."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_tool(func):
            self.registered_tools[func.__name__] = func
            return func

        mcp.add_tool = capture_tool
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        # get_services returns an empty dict (no services), _process_services handles it
        client.get_services = AsyncMock(return_value={})
        # send_websocket_message used by _get_service_translations
        client.send_websocket_message = AsyncMock(return_value={"success": True, "result": {"resources": {}}})
        return client

    @pytest.fixture
    def list_services_tool(self, mock_mcp, mock_client):
        register_services_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_list_services"]

    @pytest.mark.asyncio
    async def test_fields_none_returns_full_response(self, list_services_tool):
        result = await list_services_tool()
        assert "success" in result
        assert "services" in result

    @pytest.mark.asyncio
    async def test_fields_single_key_projects_correctly(self, list_services_tool):
        result = await list_services_tool(fields=["services"])
        assert "services" in result
        assert "success" in result
        assert "domains" not in result

    @pytest.mark.asyncio
    async def test_fields_success_always_retained(self, list_services_tool):
        result = await list_services_tool(fields=["services"])
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_malformed_fields_raises_tool_error(self, list_services_tool):
        with pytest.raises(ToolError):
            await list_services_tool(fields=123)

    @pytest.mark.asyncio
    async def test_bad_json_fields_raises_tool_error(self, list_services_tool):
        with pytest.raises(ToolError):
            await list_services_tool(fields='["')


class TestHaListServicesServiceFieldsProjection:
    """Unit tests for service_fields= per-record projection in ha_list_services.

    service_fields= projects each service record (name, description, domain,
    service, ...) down to the requested keys.  The typo-guard fires when
    all projected records are empty dicts.
    """

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def capture_tool(func):
            self.registered_tools[func.__name__] = func
            return func

        mcp.add_tool = capture_tool
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        # Two light services so we can verify projection on multiple records.
        client.get_services = AsyncMock(return_value={
            "light": {
                "services": {
                    "turn_on": {"description": "Turn on a light"},
                    "turn_off": {"description": "Turn off a light"},
                }
            }
        })
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"resources": {}}}
        )
        return client

    @pytest.fixture
    def list_services_tool(self, mock_mcp, mock_client):
        register_services_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_list_services"]

    @pytest.mark.asyncio
    async def test_service_fields_projects_each_record(self, list_services_tool):
        """service_fields=['name'] returns records with only that key."""
        result = await list_services_tool(service_fields=["name"])
        for svc in result["services"].values():
            assert set(svc.keys()) == {"name"}

    @pytest.mark.asyncio
    async def test_service_fields_multiple_keys(self, list_services_tool):
        """service_fields=['name','domain'] returns both keys per record."""
        result = await list_services_tool(service_fields=["name", "domain"])
        for svc in result["services"].values():
            assert set(svc.keys()) == {"name", "domain"}

    @pytest.mark.asyncio
    async def test_service_fields_unknown_key_emits_warning(self, list_services_tool):
        """service_fields with only unknown keys emits a diagnostic in warnings[].

        Pins the typo-footgun guard: service_fields=["nmae"] (typo for "name")
        silently produced {k: {} for k in services} before this fix.
        """
        result = await list_services_tool(service_fields=["nmae"])
        # Every projected service record is empty
        for svc in result["services"].values():
            assert svc == {}
        # Diagnostic warning present and names the wrong field
        assert "warnings" in result, (
            "Expected warnings key when all projected service records are empty"
        )
        assert any("nmae" in w for w in result["warnings"]), (
            f"Expected typo field name in warning, got: {result['warnings']}"
        )

    @pytest.mark.asyncio
    async def test_service_fields_does_not_affect_outer_response_keys(self, list_services_tool):
        """service_fields only projects inside services{}; top-level keys unchanged."""
        result = await list_services_tool(service_fields=["name"])
        assert "success" in result
        assert "services" in result
