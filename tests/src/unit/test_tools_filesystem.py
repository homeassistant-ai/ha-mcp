"""Unit tests for tools_filesystem module."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.config import _reset_global_settings
from ha_mcp.tools.tools_filesystem import (
    FEATURE_FLAG,
    MCP_TOOLS_DOMAIN,
    _is_mcp_tools_available,
    is_filesystem_tools_enabled,
)


@pytest.fixture(autouse=True)
def _reset_settings_singleton(tmp_path, monkeypatch):
    """Reset the cached ``Settings`` between tests.

    ``is_filesystem_tools_enabled()`` reads through
    ``get_global_settings()``, which caches the parsed Settings on
    first read. Tests that mutate ``HAMCP_ENABLE_FILESYSTEM_TOOLS``
    via ``patch.dict(os.environ, ...)`` need the cache invalidated
    or every test after the first sees a stale value frozen at
    import time.

    Also isolates ``get_data_dir`` to a tmp dir: ``get_global_settings()``
    reads a real ``feature_flags.json`` under the resolved data dir (e.g.
    ``~/.ha-mcp`` on a dev machine), so without this a locally-toggled beta
    flag silently overrides what these tests expect to see.
    """
    from ha_mcp.utils.data_paths import get_data_dir

    get_data_dir.cache_clear()
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    _reset_global_settings()
    yield
    _reset_global_settings()
    get_data_dir.cache_clear()


@pytest.fixture(autouse=True)
def _default_caps_none():
    """Default the shared capability probe to None so the availability gate
    takes its legacy ``get_services()`` path unless a test opts in.

    ``_assert_mcp_tools_available`` now consults ``get_component_caps`` first;
    with the mock clients these tests use, an unstubbed probe would attempt a
    live WebSocket round-trip. Tests exercising the caps-first path patch
    ``get_component_caps`` explicitly, overriding this default.
    """
    with patch(
        "ha_mcp.tools.tools_filesystem.get_component_caps",
        AsyncMock(return_value=None),
    ):
        yield


class TestFeatureFlag:
    """Test feature flag functionality.

    ``HAMCP_ENABLE_FILESYSTEM_TOOLS`` is a beta sub-flag. The
    master ``ENABLE_BETA_FEATURES`` gate force-sets it False at runtime
    when the master is off, so every enabling test must set both env
    vars to exercise the sub-flag's bool parsing in isolation.
    """

    def test_disabled_by_default(self):
        """Feature should be disabled by default."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove the flag if it exists
            os.environ.pop(FEATURE_FLAG, None)
            assert is_filesystem_tools_enabled() is False

    def test_enabled_with_true(self):
        """Feature should be enabled when set to 'true'."""
        with patch.dict(
            os.environ, {FEATURE_FLAG: "true", "ENABLE_BETA_FEATURES": "true"}
        ):
            assert is_filesystem_tools_enabled() is True

    def test_enabled_with_1(self):
        """Feature should be enabled when set to '1'."""
        with patch.dict(
            os.environ, {FEATURE_FLAG: "1", "ENABLE_BETA_FEATURES": "true"}
        ):
            assert is_filesystem_tools_enabled() is True

    def test_enabled_with_yes(self):
        """Feature should be enabled when set to 'yes'."""
        with patch.dict(
            os.environ, {FEATURE_FLAG: "yes", "ENABLE_BETA_FEATURES": "true"}
        ):
            assert is_filesystem_tools_enabled() is True

    def test_enabled_with_on(self):
        """Feature should be enabled when set to 'on'."""
        with patch.dict(
            os.environ, {FEATURE_FLAG: "on", "ENABLE_BETA_FEATURES": "true"}
        ):
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
        with patch.dict(
            os.environ, {FEATURE_FLAG: "TRUE", "ENABLE_BETA_FEATURES": "true"}
        ):
            assert is_filesystem_tools_enabled() is True
        with patch.dict(
            os.environ, {FEATURE_FLAG: "True", "ENABLE_BETA_FEATURES": "true"}
        ):
            assert is_filesystem_tools_enabled() is True

    def test_master_off_forces_sub_flag_off(self):
        """Master beta gate forces this sub-flag False even when
        the sub-flag env var is true. Lock the behavior so a future
        regression in ``_apply_feature_flag_overrides`` would surface
        in the filesystem-tools tests, not just the config tests.
        """
        with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
            os.environ.pop("ENABLE_BETA_FEATURES", None)
            assert is_filesystem_tools_enabled() is False


class TestIsMcpToolsAvailable:
    """Test _is_mcp_tools_available function."""

    @pytest.mark.asyncio
    async def test_available_when_domain_in_services_list_format(self):
        """Returns True when ha_mcp_tools is in the services list (HA REST API format)."""
        client = AsyncMock()
        # HA /api/services returns a list of {"domain": str, "services": {...}}
        client.get_services.return_value = [
            {"domain": "homeassistant", "services": {"restart": {}}},
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {
                    "list_files": {},
                    "read_file": {},
                    "write_file": {},
                    "delete_file": {},
                },
            },
        ]

        assert await _is_mcp_tools_available(client) is True

    @pytest.mark.asyncio
    async def test_not_available_when_domain_missing_list_format(self):
        """Returns False when ha_mcp_tools is not in the services list."""
        client = AsyncMock()
        client.get_services.return_value = [
            {"domain": "homeassistant", "services": {"restart": {}}},
            {"domain": "light", "services": {"turn_on": {}}},
        ]

        assert await _is_mcp_tools_available(client) is False

    @pytest.mark.asyncio
    async def test_propagates_exception_on_api_failure(self):
        """API errors propagate — callers handle them via exception_to_structured_error."""
        client = AsyncMock()
        client.get_services.side_effect = Exception("Connection failed")

        with pytest.raises(Exception, match="Connection failed"):
            await _is_mcp_tools_available(client)


class TestAssertMcpToolsAvailableCapsFirst:
    """``_assert_mcp_tools_available`` consults the shared caps probe first and
    only falls back to the ``get_services()`` existence probe when caps is None
    (issue #1813 P5 item 1c)."""

    @staticmethod
    def _caps(version: str = "1.2.0"):
        from ha_mcp.tools.component_api import ComponentCaps

        return ComponentCaps(
            schema_version=1,
            component_version=version,
            capabilities=frozenset({"search"}),
            limits={},
        )

    @pytest.mark.asyncio
    async def test_caps_hit_skips_get_services(self):
        """A caps-present component obviously exists — the get_services probe
        is skipped entirely."""
        from ha_mcp.tools.tools_filesystem import _assert_mcp_tools_available

        client = AsyncMock()
        with patch(
            "ha_mcp.tools.tools_filesystem.get_component_caps",
            AsyncMock(return_value=self._caps("1.2.0")),
        ):
            await _assert_mcp_tools_available(client)  # must not raise

        client.get_services.assert_not_called()

    @pytest.mark.asyncio
    async def test_caps_hit_enforces_min_version(self):
        """The version gate still fires on the caps path: a caps-present
        component below MIN_COMPONENT_VERSION raises the actionable error."""
        from ha_mcp.tools.tools_filesystem import _assert_mcp_tools_available

        client = AsyncMock()
        with (
            patch(
                "ha_mcp.tools.tools_filesystem.get_component_caps",
                AsyncMock(return_value=self._caps("0.10.0")),
            ),
            pytest.raises(ToolError) as exc,
        ):
            await _assert_mcp_tools_available(client)

        data = json.loads(str(exc.value))
        assert data["success"] is False
        assert "too old" in data["error"]["message"].lower()
        client.get_services.assert_not_called()

    @pytest.mark.asyncio
    async def test_caps_miss_falls_back_to_get_services_present(self):
        """caps None → the legacy get_services() probe runs; domain present → ok."""
        from ha_mcp.tools.tools_filesystem import _assert_mcp_tools_available

        client = AsyncMock()
        client.get_services.return_value = [
            {"domain": MCP_TOOLS_DOMAIN, "services": {"list_files": {}}}
        ]
        with patch(
            "ha_mcp.tools.tools_filesystem.get_component_caps",
            AsyncMock(return_value=None),
        ):
            await _assert_mcp_tools_available(client)  # must not raise

        client.get_services.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_caps_miss_falls_back_to_get_services_absent(self):
        """caps None + domain absent → COMPONENT_NOT_INSTALLED via the legacy path."""
        from ha_mcp.tools.tools_filesystem import _assert_mcp_tools_available

        client = AsyncMock()
        client.get_services.return_value = [{"domain": "homeassistant", "services": {}}]
        with (
            patch(
                "ha_mcp.tools.tools_filesystem.get_component_caps",
                AsyncMock(return_value=None),
            ),
            pytest.raises(ToolError),
        ):
            await _assert_mcp_tools_available(client)

        client.get_services.assert_awaited_once()


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
        """Tools should be registered via mcp.add_tool when feature flag is enabled.

        ``register_tool_methods`` (in helpers.py) discovers @tool-decorated
        methods on the FilesystemTools instance and calls ``mcp.add_tool(...)``
        for each. Earlier versions of this test mocked ``mcp.tool`` (the
        legacy decorator path) and never inspected the call list, so the
        guarded ``if registered_func:`` branches in production code could
        silently skip without anyone noticing. Now we assert mcp.add_tool
        fired the expected number of times (one per @tool method).
        """
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = MagicMock()

        # Master beta gate force-sets the filesystem-tools sub-flag
        # False when ``ENABLE_BETA_FEATURES`` is unset, so set both
        # together — otherwise ``register_filesystem_tools`` early-
        # returns without registering anything.
        with patch.dict(
            os.environ,
            {FEATURE_FLAG: "true", "ENABLE_BETA_FEATURES": "true"},
        ):
            register_filesystem_tools(mcp, client)

        # FilesystemTools exposes 4 @tool methods: list_files, read_file,
        # write_file, delete_file. Assert all four registered.
        assert mcp.add_tool.call_count == 4, (
            f"Expected 4 add_tool calls (one per @tool method on FilesystemTools), "
            f"got {mcp.add_tool.call_count}"
        )

        # Tool functions are passed positionally — extract them and verify
        # the names match the @tool(name=...) decorator values.
        registered_names = {
            call.args[0].__name__ for call in mcp.add_tool.call_args_list if call.args
        }
        expected_methods = {
            "ha_list_files",
            "ha_read_file",
            "ha_write_file",
            "ha_delete_file",
        }
        assert registered_names == expected_methods, (
            f"Expected methods {expected_methods}, got {registered_names}"
        )


class TestHaListFilesTool:
    """Test ha_list_files tool behavior."""

    @pytest.mark.asyncio
    async def test_raises_tool_error_when_mcp_tools_not_installed(self):
        """Should raise ToolError when ha_mcp_tools is not installed."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = [{"domain": "homeassistant", "services": {}}]
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
            inner_func = (
                registered_func.__wrapped__
                if hasattr(registered_func, "__wrapped__")
                else registered_func
            )
            with pytest.raises(ToolError) as exc_info:
                await inner_func(path="www/")
            # The error is the #1853 install recipe: generic HACS tools plus
            # the File & YAML entry, never the removed installer tool.
            error_data = json.loads(str(exc_info.value))
            assert error_data["error"]["code"] == "COMPONENT_NOT_INSTALLED"
            suggestions = " ".join(error_data["error"]["suggestions"])
            assert "homeassistant-ai/ha-mcp-integration" in suggestions
            assert "HA-MCP File & YAML Tools" in suggestions
            assert "ha_install_mcp_tools" not in suggestions

    @pytest.mark.asyncio
    async def test_calls_service_when_mcp_tools_installed(self):
        """Should call the service when ha_mcp_tools is installed."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = [
            {"domain": MCP_TOOLS_DOMAIN, "services": {"list_files": {}}}
        ]
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
            inner_func = (
                registered_func.__wrapped__
                if hasattr(registered_func, "__wrapped__")
                else registered_func
            )
            result = await inner_func(path="www/", pattern="*.css")

            client.call_service.assert_called_once_with(
                MCP_TOOLS_DOMAIN,
                "list_files",
                {"path": "www/", "pattern": "*.css"},
                return_response=True,
            )
            assert result["success"] is True


class TestHaReadFileTool:
    """Test ha_read_file tool behavior."""

    @pytest.mark.asyncio
    async def test_raises_tool_error_when_mcp_tools_not_installed(self):
        """Should raise ToolError when ha_mcp_tools is not installed."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = [{"domain": "homeassistant", "services": {}}]
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
            inner_func = (
                registered_func.__wrapped__
                if hasattr(registered_func, "__wrapped__")
                else registered_func
            )
            with pytest.raises(ToolError):
                await inner_func(path="configuration.yaml")


class TestHaWriteFileTool:
    """Test ha_write_file tool behavior."""

    @pytest.mark.asyncio
    async def test_raises_tool_error_when_mcp_tools_not_installed(self):
        """Should raise ToolError when ha_mcp_tools is not installed."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = [{"domain": "homeassistant", "services": {}}]
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
            inner_func = (
                registered_func.__wrapped__
                if hasattr(registered_func, "__wrapped__")
                else registered_func
            )
            with pytest.raises(ToolError):
                await inner_func(path="www/test.css", content=".test { color: red; }")

    @pytest.mark.asyncio
    async def test_calls_service_with_all_params(self):
        """Should pass all parameters to the service."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = [
            {"domain": MCP_TOOLS_DOMAIN, "services": {"write_file": {}}}
        ]
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
            inner_func = (
                registered_func.__wrapped__
                if hasattr(registered_func, "__wrapped__")
                else registered_func
            )
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
            assert result["success"] is True


class TestHaDeleteFileTool:
    """Test ha_delete_file tool behavior."""

    @pytest.mark.asyncio
    async def test_requires_confirmation(self):
        """Should require confirm=True to delete."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = [
            {"domain": MCP_TOOLS_DOMAIN, "services": {"delete_file": {}}}
        ]
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
            inner_func = (
                registered_func.__wrapped__
                if hasattr(registered_func, "__wrapped__")
                else registered_func
            )
            with pytest.raises(ToolError) as exc_info:
                await inner_func(path="www/test.css", confirm=False)

            error_data = json.loads(str(exc_info.value))
            assert "not confirmed" in error_data["error"]["message"].lower()
            # Service should not have been called
            client.call_service.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_when_confirmed(self):
        """Should call service when confirm=True."""
        from ha_mcp.tools.tools_filesystem import register_filesystem_tools

        mcp = MagicMock()
        client = AsyncMock()
        client.get_services.return_value = [
            {"domain": MCP_TOOLS_DOMAIN, "services": {"delete_file": {}}}
        ]
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
            inner_func = (
                registered_func.__wrapped__
                if hasattr(registered_func, "__wrapped__")
                else registered_func
            )
            result = await inner_func(path="www/test.css", confirm=True)

            client.call_service.assert_called_once_with(
                MCP_TOOLS_DOMAIN,
                "delete_file",
                {"path": "www/test.css"},
                return_response=True,
            )
            assert result["success"] is True
