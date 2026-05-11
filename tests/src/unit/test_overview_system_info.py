"""Unit tests for ha_get_overview system_info builder."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_search import register_search_tools


class TestHaGetOverviewSystemInfo:
    """Test system_info field assembly in ha_get_overview at detail_level='full'."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures registered tool functions."""
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
        """Create a mock Home Assistant client with default-empty config."""
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={})
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        """Create a mock smart_tools that returns a minimal success result."""
        smart = MagicMock()
        smart.get_system_overview = AsyncMock(return_value={"success": True})
        return smart

    @pytest.fixture
    def overview_tool(self, mock_mcp, mock_client, mock_smart_tools):
        """Register search tools and return the ha_get_overview function."""
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_get_overview"]

    @pytest.mark.asyncio
    async def test_allowlist_external_dirs_missing_key_yields_none(
        self, mock_client, overview_tool
    ):
        """When HA config omits the key entirely, the field is None — not [].

        Distinguishes 'HA didn't expose the key' from 'HA reported an empty
        allowlist' for security-sensitive agent reasoning. Locks in the contract
        so a future refactor cannot silently switch the default back to [].
        """
        mock_client.get_config = AsyncMock(return_value={})

        result = await overview_tool(detail_level="full")

        system_info = result["system_info"]
        assert "allowlist_external_dirs" in system_info
        assert system_info["allowlist_external_dirs"] is None

    @pytest.mark.asyncio
    async def test_allowlist_external_dirs_passes_through_list_value(
        self, mock_client, overview_tool
    ):
        """When HA config exposes the key, the list value passes through unchanged."""
        mock_client.get_config = AsyncMock(
            return_value={"allowlist_external_dirs": ["/media", "/share"]}
        )

        result = await overview_tool(detail_level="full")

        assert result["system_info"]["allowlist_external_dirs"] == [
            "/media",
            "/share",
        ]

    @pytest.mark.asyncio
    async def test_allowlist_external_dirs_omitted_at_minimal_detail_level(
        self, mock_client, overview_tool
    ):
        """The field must not appear in system_info when detail_level != 'full'."""
        mock_client.get_config = AsyncMock(
            return_value={"allowlist_external_dirs": ["/media"]}
        )

        result = await overview_tool(detail_level="minimal")

        assert "allowlist_external_dirs" not in result["system_info"]
        """The field must not appear in system_info when detail_level != 'full'."""
        mock_client.get_config = AsyncMock(
            return_value={"allowlist_external_dirs": ["/media"]}
        )

        result = await overview_tool(detail_level="minimal")

        assert "allowlist_external_dirs" not in result["system_info"]


class TestHaGetOverviewFieldsProjection:
    """fields= projects the response to the requested top-level keys.

    Pins the contract from issue #1199: callers that only need one section
    (e.g. system_info) can request it via fields= and receive a response
    that omits all other top-level keys, reducing token usage by up to 94%.
    """

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

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
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(
            return_value={"version": "2026.5.0", "location_name": "Home"}
        )
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        smart = MagicMock()
        smart.get_system_overview = AsyncMock(
            return_value={
                "success": True,
                "domains": {"light": {"count": 3}},
                "entity_summary": [],
                "total_entities": 3,
            }
        )
        return smart

    @pytest.fixture
    def overview_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_get_overview"]

    @pytest.mark.asyncio
    async def test_fields_none_returns_full_response(self, overview_tool):
        """fields=None (default) returns the full response — no projection."""
        result = await overview_tool()
        assert "success" in result
        assert "system_info" in result
        assert "domains" in result

    @pytest.mark.asyncio
    async def test_fields_single_key_projects_correctly(self, overview_tool):
        """fields=["system_info"] keeps only system_info (+ success always)."""
        result = await overview_tool(fields=["system_info"])
        assert result["success"] is True
        assert "system_info" in result
        assert result["system_info"]["version"] == "2026.5.0"
        # All other top-level keys must be absent.
        for key in ("domains", "entity_summary", "total_entities", "repair_count"):
            assert key not in result, f"unexpected key {key!r} survived projection"

    @pytest.mark.asyncio
    async def test_fields_multiple_keys(self, overview_tool):
        """fields=["system_info", "domains"] keeps exactly those two (+ success)."""
        result = await overview_tool(fields=["system_info", "domains"])
        assert "system_info" in result
        assert "domains" in result
        assert "entity_summary" not in result

    @pytest.mark.asyncio
    async def test_fields_success_always_included(self, overview_tool):
        """success is always present even when the caller omits it from fields."""
        result = await overview_tool(fields=["domains"])
        assert "success" in result

    @pytest.mark.asyncio
    async def test_fields_unknown_key_silently_absent(self, overview_tool):
        """Requesting a non-existent key silently produces no entry — no error."""
        result = await overview_tool(fields=["nonexistent_key"])
        assert result["success"] is True
        assert "nonexistent_key" not in result
