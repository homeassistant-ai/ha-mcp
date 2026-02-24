"""Unit tests for tools_filesystem module."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.tools_filesystem import (
    FEATURE_FLAG,
    MCP_TOOLS_DOMAIN,
    _check_mcp_tools_available,
    is_filesystem_tools_enabled,
)


class TestFeatureFlag:
    """Test feature flag functionality."""

    def test_disabled_by_default(self):
        """Feature should be disabled by default."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove the flag if it exists
            os.environ.pop(FEATURE_FLAG, None)
            assert is_filesystem_tools_enabled() is False

    def test_enabled_with_true(self):
        """Feature should be enabled when set to 'true'."""
        with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
            assert is_filesystem_tools_enabled() is True

    def test_enabled_with_1(self):
        """Feature should be enabled when set to '1'."""
        with patch.dict(os.environ, {FEATURE_FLAG: "1"}):
            assert is_filesystem_tools_enabled() is True

    def test_enabled_with_yes(self):
        """Feature should be enabled when set to 'yes'."""
        with patch.dict(os.environ, {FEATURE_FLAG: "yes"}):
            assert is_filesystem_tools_enabled() is True

    def test_enabled_with_on(self):
        """Feature should be enabled when set to 'on'."""
        with patch.dict(os.environ, {FEATURE_FLAG: "on"}):
            assert is_filesystem_tools_enabled() is True

    def test_disabled_with_false(self):
        """Feature should be disabled when set to 'false'."""
        with patch.dict(os.environ, {FEATURE_FLAG: "false"}):
            assert is_filesystem_tools_enabled() is False

    def test_disabled_with_empty_string(self):
        """Feature should be disabled when set to empty string."""
        with patch.dict(os.environ, {FEATURE_FLAG: ""}):
            assert is_filesystem_tools_enabled() is False

    def test_case_insensitive(self):
        """Feature flag should be case insensitive."""
        with patch.dict(os.environ, {FEATURE_FLAG: "TRUE"}):
            assert is_filesystem_tools_enabled() is True
        with patch.dict(os.environ, {FEATURE_FLAG: "True"}):
            assert is_filesystem_tools_enabled() is True


class TestCheckMcpToolsAvailable:
    """Test _check_mcp_tools_available function."""

    @pytest.mark.asyncio
    async def test_available_when_domain_in_services(self):
        """Returns True when ha_mcp_tools is in the services list."""
        client = AsyncMock()
        client.get_services.return_value = {
            MCP_TOOLS_DOMAIN: {
                "list_files": {},
                "read_file": {},
                "write_file": {},
                "delete_file": {},
            },
            "homeassistant": {"restart": {}},
        }

        is_available, error_msg = await _check_mcp_tools_available(client)

        assert is_available is True
        assert error_msg is None

    @pytest.mark.asyncio
    async def test_not_available_when_domain_missing(self):
        """Returns False when ha_mcp_tools is not in the services list."""
        client = AsyncMock()
        client.get_services.return_value = {
            "homeassistant": {"restart": {}},
            "light": {"turn_on": {}},
        }

        is_available, error_msg = await _check_mcp_tools_available(client)

        assert is_available is False
        assert error_msg is not None
        assert MCP_TOOLS_DOMAIN in error_msg
        assert "ha_install_mcp_tools" in error_msg

    @pytest.mark.asyncio
    async def test_error_on_exception(self):
        """Returns error message when get_services fails."""
        client = AsyncMock()
        client.get_services.side_effect = Exception("Connection failed")

        is_available, error_msg = await _check_mcp_tools_available(client)

        assert is_available is False
        assert error_msg is not None
        assert "Connection failed" in error_msg


class TestRegisterFilesystemTools:
    """Test register_filesystem_tools function."""

    def test_does_not_register_when_disabled(self):
        """Tools should not be registered when feature flag is disabled."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = MagicMock()

        with patch.dict(os.environ, {FEATURE_FLAG: "false"}):
            register_filesystem_tools(mcp, client)

        # mcp.tool should not have been called
        mcp.tool.assert_not_called()

    def test_registers_tools_when_enabled(self):
        """Tools should be registered when feature flag is enabled."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = MagicMock()

        # Mock the tool decorator to return the function unchanged
        def mock_tool(**kwargs):
            def decorator(func):
                return func
            return decorator

        mcp.tool = mock_tool

        with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
            register_filesystem_tools(mcp, client)

        # Since we're using a mock decorator, we just verify no exceptions occurred
        # The actual registration happens via the @mcp.tool decorator


class TestHaListFilesTool:
    """Test ha_list_files tool behavior."""

    @pytest.mark.asyncio
    async def test_returns_error_when_mcp_tools_not_installed(self):
        """Should return error when ha_mcp_tools is not installed."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = {"homeassistant": {}}
        client.get_config.return_value = {"time_zone": "UTC"}

        # Capture the registered function
        registered_func = None

        def capture_tool(**kwargs):
            def decorator(func):
                nonlocal registered_func
                if "ha_list_files" in func.__name__:
                    registered_func = func
                return func
            return decorator

        mcp.tool = capture_tool

        with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
            register_filesystem_tools(mcp, client)

        # Call the captured function
        if registered_func:
            # Need to unwrap from log_tool_usage decorator
            inner_func = registered_func.__wrapped__ if hasattr(registered_func, '__wrapped__') else registered_func
            result = await inner_func(path="www/")

            assert result["data"]["success"] is False
            assert "MCP_TOOLS_NOT_INSTALLED" in result["data"]["error_code"]

    @pytest.mark.asyncio
    async def test_calls_service_when_mcp_tools_installed(self):
        """Should call the service when ha_mcp_tools is installed."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = {MCP_TOOLS_DOMAIN: {"list_files": {}}}
        client.get_config.return_value = {"time_zone": "UTC"}
        client.call_service.return_value = {
            "success": True,
            "path": "www/",
            "files": [{"name": "test.css", "size": 100}],
            "count": 1,
        }

        registered_func = None

        def capture_tool(**kwargs):
            def decorator(func):
                nonlocal registered_func
                if "ha_list_files" in func.__name__:
                    registered_func = func
                return func
            return decorator

        mcp.tool = capture_tool

        with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
            register_filesystem_tools(mcp, client)

        if registered_func:
            inner_func = registered_func.__wrapped__ if hasattr(registered_func, '__wrapped__') else registered_func
            result = await inner_func(path="www/", pattern="*.css")

            client.call_service.assert_called_once_with(
                MCP_TOOLS_DOMAIN,
                "list_files",
                {"path": "www/", "pattern": "*.css"},
                return_response=True,
            )
            assert result["data"]["success"] is True


class TestHaReadFileTool:
    """Test ha_read_file tool behavior."""

    @pytest.mark.asyncio
    async def test_returns_error_when_mcp_tools_not_installed(self):
        """Should return error when ha_mcp_tools is not installed."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = {"homeassistant": {}}
        client.get_config.return_value = {"time_zone": "UTC"}

        registered_func = None

        def capture_tool(**kwargs):
            def decorator(func):
                nonlocal registered_func
                if "ha_read_file" in func.__name__:
                    registered_func = func
                return func
            return decorator

        mcp.tool = capture_tool

        with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
            register_filesystem_tools(mcp, client)

        if registered_func:
            inner_func = registered_func.__wrapped__ if hasattr(registered_func, '__wrapped__') else registered_func
            result = await inner_func(path="configuration.yaml")

            assert result["data"]["success"] is False
            assert "MCP_TOOLS_NOT_INSTALLED" in result["data"]["error_code"]


class TestHaWriteFileTool:
    """Test ha_write_file tool behavior."""

    @pytest.mark.asyncio
    async def test_returns_error_when_mcp_tools_not_installed(self):
        """Should return error when ha_mcp_tools is not installed."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = {"homeassistant": {}}
        client.get_config.return_value = {"time_zone": "UTC"}

        registered_func = None

        def capture_tool(**kwargs):
            def decorator(func):
                nonlocal registered_func
                if "ha_write_file" in func.__name__:
                    registered_func = func
                return func
            return decorator

        mcp.tool = capture_tool

        with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
            register_filesystem_tools(mcp, client)

        if registered_func:
            inner_func = registered_func.__wrapped__ if hasattr(registered_func, '__wrapped__') else registered_func
            result = await inner_func(path="www/test.css", content=".test { color: red; }")

            assert result["data"]["success"] is False
            assert "MCP_TOOLS_NOT_INSTALLED" in result["data"]["error_code"]

    @pytest.mark.asyncio
    async def test_calls_service_with_all_params(self):
        """Should pass all parameters to the service."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = {MCP_TOOLS_DOMAIN: {"write_file": {}}}
        client.get_config.return_value = {"time_zone": "UTC"}
        client.call_service.return_value = {
            "success": True,
            "path": "www/test.css",
            "size": 50,
            "created": True,
        }

        registered_func = None

        def capture_tool(**kwargs):
            def decorator(func):
                nonlocal registered_func
                if "ha_write_file" in func.__name__:
                    registered_func = func
                return func
            return decorator

        mcp.tool = capture_tool

        with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
            register_filesystem_tools(mcp, client)

        if registered_func:
            inner_func = registered_func.__wrapped__ if hasattr(registered_func, '__wrapped__') else registered_func
            result = await inner_func(
                path="www/test.css",
                content=".test { color: red; }",
                overwrite=True,
                create_dirs=False,
            )

            client.call_service.assert_called_once_with(
                MCP_TOOLS_DOMAIN,
                "write_file",
                {
                    "path": "www/test.css",
                    "content": ".test { color: red; }",
                    "overwrite": True,
                    "create_dirs": False,
                },
                return_response=True,
            )
            assert result["data"]["success"] is True


class TestHaDeleteFileTool:
    """Test ha_delete_file tool behavior."""

    @pytest.mark.asyncio
    async def test_requires_confirmation(self):
        """Should require confirm=True to delete."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = {MCP_TOOLS_DOMAIN: {"delete_file": {}}}
        client.get_config.return_value = {"time_zone": "UTC"}

        registered_func = None

        def capture_tool(**kwargs):
            def decorator(func):
                nonlocal registered_func
                if "ha_delete_file" in func.__name__:
                    registered_func = func
                return func
            return decorator

        mcp.tool = capture_tool

        with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
            register_filesystem_tools(mcp, client)

        if registered_func:
            inner_func = registered_func.__wrapped__ if hasattr(registered_func, '__wrapped__') else registered_func
            result = await inner_func(path="www/test.css", confirm=False)

            assert result["data"]["success"] is False
            assert "not confirmed" in result["data"]["error"].lower()
            # Service should not have been called
            client.call_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_when_confirmed(self):
        """Should call service when confirm=True."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = {MCP_TOOLS_DOMAIN: {"delete_file": {}}}
        client.get_config.return_value = {"time_zone": "UTC"}
        client.call_service.return_value = {
            "success": True,
            "path": "www/test.css",
            "deleted_size": 50,
            "message": "File deleted successfully",
        }

        registered_func = None

        def capture_tool(**kwargs):
            def decorator(func):
                nonlocal registered_func
                if "ha_delete_file" in func.__name__:
                    registered_func = func
                return func
            return decorator

        mcp.tool = capture_tool

        with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
            register_filesystem_tools(mcp, client)

        if registered_func:
            inner_func = registered_func.__wrapped__ if hasattr(registered_func, '__wrapped__') else registered_func
            result = await inner_func(path="www/test.css", confirm=True)

            client.call_service.assert_called_once_with(
                MCP_TOOLS_DOMAIN,
                "delete_file",
                {"path": "www/test.css"},
                return_response=True,
            )
            assert result["data"]["success"] is True
