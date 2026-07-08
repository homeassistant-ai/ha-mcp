"""
End-to-End tests for developer-mode tools (issue #1775).

This test suite validates:
- Feature flag behavior: ha_dev_* tools are NOT registered by default and
  appear only when HAMCP_ENABLE_DEV_MODE is on
- ha_dev_manage_settings list / set / reset against a real server
- ha_dev_manage_server info and its validation / deployment-mode errors

Feature Flag: Set HAMCP_ENABLE_DEV_MODE=true to enable.

ha_dev_manage_server's update_source action is exercised only through its
validation error paths here — submitting a real options flow would reinstall
the in-process server that other tests in the session depend on.
"""

import json
import logging
import os

import pytest

from ..utilities.assertions import extract_error_message, safe_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEATURE_FLAG = "HAMCP_ENABLE_DEV_MODE"
DEV_TOOL_NAMES = {"ha_dev_manage_server", "ha_dev_manage_settings"}


@pytest.fixture(scope="module")
def dev_mode_enabled(ha_container_with_fresh_config, tmp_path_factory):
    """Enable dev mode + isolate the data dir for the test module.

    The settings tool persists to ``feature_flags.json`` under
    ``get_data_dir()``; pointing ``HA_MCP_CONFIG_DIR`` at a module tmp
    dir keeps those writes away from the developer's real data dir.
    """
    from ha_mcp.utils.data_paths import get_data_dir

    old_flag = os.environ.get(FEATURE_FLAG)
    old_dir = os.environ.get("HA_MCP_CONFIG_DIR")
    data_dir = tmp_path_factory.mktemp("dev-mode-data")
    os.environ[FEATURE_FLAG] = "true"
    os.environ["HA_MCP_CONFIG_DIR"] = str(data_dir)
    get_data_dir.cache_clear()
    import ha_mcp.config

    ha_mcp.config._settings = None
    logger.info("Dev mode feature flag enabled (data dir: %s)", data_dir)
    yield data_dir
    if old_flag is not None:
        os.environ[FEATURE_FLAG] = old_flag
    else:
        os.environ.pop(FEATURE_FLAG, None)
    if old_dir is not None:
        os.environ["HA_MCP_CONFIG_DIR"] = old_dir
    else:
        os.environ.pop("HA_MCP_CONFIG_DIR", None)
    get_data_dir.cache_clear()
    ha_mcp.config._settings = None


@pytest.fixture(scope="module")
async def _dev_mode_server(dev_mode_enabled, ha_container_with_fresh_config):
    """Create a single MCP server with dev mode enabled for the module."""
    from ha_mcp.client.rest_client import HomeAssistantClient
    from ha_mcp.server import HomeAssistantSmartMCPServer
    from tests.test_constants import TEST_TOKEN

    base_url = ha_container_with_fresh_config["base_url"]
    client = HomeAssistantClient(base_url=base_url, token=TEST_TOKEN)
    server = HomeAssistantSmartMCPServer(client=client)
    yield server


@pytest.fixture
async def mcp_client_with_dev_mode(_dev_mode_server):
    """Create MCP client connected to the dev-mode-enabled server."""
    from fastmcp import Client

    mcp_client = Client(_dev_mode_server.mcp)
    async with mcp_client:
        yield mcp_client


class TestDevModeAvailability:
    """The tools must be invisible by default and present when enabled."""

    async def test_dev_tools_hidden_by_default(self, ha_container_with_fresh_config):
        """Verify the ha_dev_* tools are NOT registered when the flag is off."""
        original = os.environ.pop(FEATURE_FLAG, None)
        try:
            import ha_mcp.config

            ha_mcp.config._settings = None

            from fastmcp import Client

            from ha_mcp.server import HomeAssistantSmartMCPServer

            server = HomeAssistantSmartMCPServer(
                client=None,
                server_name="test-dev-mode-disabled",
            )
            client = Client(server.mcp)
            async with client:
                tool_names = {t.name for t in await client.list_tools()}
                present = DEV_TOOL_NAMES & tool_names
                assert not present, (
                    f"Dev tools should NOT be registered when {FEATURE_FLAG} "
                    f"is off, but found: {present}"
                )
        finally:
            if original:
                os.environ[FEATURE_FLAG] = original
            import ha_mcp.config

            ha_mcp.config._settings = None

    async def test_dev_tools_registered_when_enabled(self, mcp_client_with_dev_mode):
        tool_names = {t.name for t in await mcp_client_with_dev_mode.list_tools()}
        missing = DEV_TOOL_NAMES - tool_names
        assert not missing, f"Dev tools missing with flag on: {missing}"


class TestDevManageSettings:
    async def test_list_returns_settings_matrix(self, mcp_client_with_dev_mode):
        result = await safe_call_tool(
            mcp_client_with_dev_mode, "ha_dev_manage_settings", {"action": "list"}
        )
        assert result.get("success") is True
        rows = {r["setting"]: r for r in result["data"]["settings"]}
        assert rows["enable_dev_mode"]["value"] is True
        assert rows["enable_dev_mode"]["origin"] == "env"
        assert "log_level" in rows

    async def test_set_and_reset_roundtrip(
        self, mcp_client_with_dev_mode, dev_mode_enabled
    ):
        data_dir = dev_mode_enabled
        set_result = await safe_call_tool(
            mcp_client_with_dev_mode,
            "ha_dev_manage_settings",
            {"action": "set", "setting": "fuzzy_threshold", "value": 61},
        )
        assert set_result.get("success") is True
        assert set_result["data"]["restart_required"] is True
        persisted = json.loads((data_dir / "feature_flags.json").read_text())
        assert persisted["fuzzy_threshold"] == 61

        list_result = await safe_call_tool(
            mcp_client_with_dev_mode, "ha_dev_manage_settings", {"action": "list"}
        )
        rows = {r["setting"]: r for r in list_result["data"]["settings"]}
        assert rows["fuzzy_threshold"]["value"] == 61
        assert rows["fuzzy_threshold"]["origin"] == "file"

        reset_result = await safe_call_tool(
            mcp_client_with_dev_mode,
            "ha_dev_manage_settings",
            {"action": "reset", "setting": "fuzzy_threshold"},
        )
        assert reset_result["data"]["removed_override"] is True
        persisted = json.loads((data_dir / "feature_flags.json").read_text())
        assert "fuzzy_threshold" not in persisted

    async def test_set_rejects_unknown_setting(self, mcp_client_with_dev_mode):
        result = await safe_call_tool(
            mcp_client_with_dev_mode,
            "ha_dev_manage_settings",
            {"action": "set", "setting": "not_a_setting", "value": 1},
        )
        assert result.get("success") is not True
        assert "unknown setting" in extract_error_message(result).lower()

    async def test_set_rejects_env_pinned_setting(self, mcp_client_with_dev_mode):
        """enable_dev_mode itself is env-pinned in this fixture setup."""
        result = await safe_call_tool(
            mcp_client_with_dev_mode,
            "ha_dev_manage_settings",
            {"action": "set", "setting": "enable_dev_mode", "value": False},
        )
        assert result.get("success") is not True
        assert "locked by env" in extract_error_message(result).lower()


class TestDevManageServer:
    async def test_info_reports_deployment(self, mcp_client_with_dev_mode):
        result = await safe_call_tool(
            mcp_client_with_dev_mode, "ha_dev_manage_server", {"action": "info"}
        )
        assert result.get("success") is True
        data = result["data"]
        assert data["deployment_mode"] == "standalone"
        assert isinstance(data["server_version"], str) and data["server_version"]
        assert "component_server_entry" in data

    async def test_update_source_requires_params(self, mcp_client_with_dev_mode):
        result = await safe_call_tool(
            mcp_client_with_dev_mode,
            "ha_dev_manage_server",
            {"action": "update_source"},
        )
        assert result.get("success") is not True
        assert "channel" in extract_error_message(result).lower()

    async def test_update_source_rejects_bad_channel(self, mcp_client_with_dev_mode):
        result = await safe_call_tool(
            mcp_client_with_dev_mode,
            "ha_dev_manage_server",
            {"action": "update_source", "channel": "nightly"},
        )
        assert result.get("success") is not True

    async def test_restart_unavailable_standalone(self, mcp_client_with_dev_mode):
        result = await safe_call_tool(
            mcp_client_with_dev_mode, "ha_dev_manage_server", {"action": "restart"}
        )
        assert result.get("success") is not True
        assert "standalone" in extract_error_message(result).lower()
