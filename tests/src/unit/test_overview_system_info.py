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


class TestHaGetOverviewSettingsUrl:
    """Pin the stdio sidecar URL surfacing in ha_get_overview (issue #863).

    The ``settings_url`` field is the ONLY path the LLM ever sees the
    URL through — a regression that silently emits an empty string (or
    inverts the conditional) would route users to a broken bookmark
    without test failure. Mirrors the system_info test scaffolding so
    the file's pacing stays consistent.
    """

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
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={})
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        smart = MagicMock()
        smart.get_system_overview = AsyncMock(return_value={"success": True})
        return smart

    @pytest.fixture
    def overview_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_get_overview"]

    @pytest.mark.asyncio
    async def test_settings_url_surfaced_when_sidecar_running(
        self, overview_tool, monkeypatch
    ):
        """Sidecar URL file present → field appears verbatim in the result."""
        url = "http://127.0.0.1:8099/private_abc/settings"
        monkeypatch.setattr(
            "ha_mcp.stdio_settings_sidecar.read_sidecar_url",
            lambda: url,
        )
        result = await overview_tool(detail_level="minimal")
        assert result.get("settings_url") == url

    @pytest.mark.asyncio
    async def test_settings_url_omitted_when_no_sidecar(
        self, overview_tool, monkeypatch
    ):
        """No URL file → ``settings_url`` MUST NOT be in the result.

        Strict ``not in`` rather than ``== None`` so a future regression
        that emits ``settings_url=""`` or ``settings_url=None`` to every
        overview call is caught.
        """
        monkeypatch.setattr(
            "ha_mcp.stdio_settings_sidecar.read_sidecar_url",
            lambda: None,
        )
        result = await overview_tool(detail_level="minimal")
        assert "settings_url" not in result
