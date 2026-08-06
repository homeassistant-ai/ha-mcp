"""
End-to-End tests for the security-policy tool (issue #2148).

This test suite validates:
- Feature flag behavior: ha_manage_security_policy is NOT registered by
  default and appears only when ENABLE_SECURITY_POLICY_TOOL is on
- get returns the full policy document plus its enforcement status
- set replaces the document and bumps its version
- the live approval queue stays unreachable through this tool

Feature Flag: Set ENABLE_SECURITY_POLICY_TOOL=true to enable.
"""

import logging
import os

import pytest

from ..utilities.assertions import extract_error_message, safe_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEATURE_FLAG = "ENABLE_SECURITY_POLICY_TOOL"
TOOL_NAME = "ha_manage_security_policy"


def _reset_settings_state():
    """Drop the cached Settings singleton + data-dir memo.

    Both are read-once caches; every env mutation in this module must be
    followed by this reset or the server under test sees stale values.
    """
    from ha_mcp.config import reset_global_settings
    from ha_mcp.utils.data_paths import get_data_dir

    get_data_dir.cache_clear()
    reset_global_settings()


@pytest.fixture(scope="module")
def security_policy_tool_enabled(ha_container_with_fresh_config, tmp_path_factory):
    """Enable the policy tool + isolate the data dir for the test module.

    The tool persists to ``tool_policy.json`` under ``get_data_dir()``;
    pointing ``HA_MCP_CONFIG_DIR`` at a module tmp dir keeps those writes
    away from the developer's real data dir.
    """
    old_flag = os.environ.get(FEATURE_FLAG)
    old_dir = os.environ.get("HA_MCP_CONFIG_DIR")
    data_dir = tmp_path_factory.mktemp("security-policy-data")
    os.environ[FEATURE_FLAG] = "true"
    os.environ["HA_MCP_CONFIG_DIR"] = str(data_dir)
    _reset_settings_state()
    logger.info("Security policy tool enabled (data dir: %s)", data_dir)
    yield data_dir
    if old_flag is not None:
        os.environ[FEATURE_FLAG] = old_flag
    else:
        os.environ.pop(FEATURE_FLAG, None)
    if old_dir is not None:
        os.environ["HA_MCP_CONFIG_DIR"] = old_dir
    else:
        os.environ.pop("HA_MCP_CONFIG_DIR", None)
    _reset_settings_state()


@pytest.fixture(scope="module")
async def _security_policy_server(
    security_policy_tool_enabled, ha_container_with_fresh_config
):
    """Create a single MCP server with the policy tool enabled."""
    from ha_mcp.client.rest_client import HomeAssistantClient
    from ha_mcp.server import HomeAssistantSmartMCPServer
    from tests.test_constants import TEST_TOKEN

    base_url = ha_container_with_fresh_config["base_url"]
    client = HomeAssistantClient(base_url=base_url, token=TEST_TOKEN)
    server = HomeAssistantSmartMCPServer(client=client)
    yield server


@pytest.fixture
async def mcp_client_with_policy_tool(_security_policy_server):
    """Create MCP client connected to the policy-tool-enabled server."""
    from fastmcp import Client

    mcp_client = Client(_security_policy_server.mcp)
    async with mcp_client:
        yield mcp_client


class TestSecurityPolicyToolAvailability:
    """The tool must be invisible by default and present when enabled."""

    async def test_tool_hidden_by_default(self, ha_container_with_fresh_config):
        """Verify ha_manage_security_policy is NOT registered when the flag is off."""
        original = os.environ.pop(FEATURE_FLAG, None)
        try:
            _reset_settings_state()

            from fastmcp import Client

            from ha_mcp.server import HomeAssistantSmartMCPServer

            server = HomeAssistantSmartMCPServer(
                client=None,
                server_name="test-security-policy-disabled",
            )
            client = Client(server.mcp)
            async with client:
                tool_names = {t.name for t in await client.list_tools()}
                assert TOOL_NAME not in tool_names, (
                    f"{TOOL_NAME} should NOT be registered when {FEATURE_FLAG} is off"
                )
        finally:
            if original:
                os.environ[FEATURE_FLAG] = original
            _reset_settings_state()

    async def test_tool_registered_when_enabled(self, mcp_client_with_policy_tool):
        tool_names = {t.name for t in await mcp_client_with_policy_tool.list_tools()}
        assert TOOL_NAME in tool_names, f"{TOOL_NAME} missing with flag on"

    async def test_only_get_and_set_are_exposed(self, mcp_client_with_policy_tool):
        """The approval queue must not be reachable through this tool."""
        tools = {t.name: t for t in await mcp_client_with_policy_tool.list_tools()}
        schema = tools[TOOL_NAME].inputSchema
        actions = schema["properties"]["action"]["enum"]
        assert set(actions) == {"get", "set"}


class TestSecurityPolicyToolActions:
    async def test_get_returns_policy_document(self, mcp_client_with_policy_tool):
        result = await safe_call_tool(
            mcp_client_with_policy_tool, TOOL_NAME, {"action": "get"}
        )
        assert result.get("success") is True
        assert set(result["data"]) == {"policy", "policies_enabled", "policies_live"}
        policy = result["data"]["policy"]
        assert "rules" in policy
        assert "version" in policy

    async def test_set_updates_and_bumps_version(self, mcp_client_with_policy_tool):
        current = await safe_call_tool(
            mcp_client_with_policy_tool, TOOL_NAME, {"action": "get"}
        )
        version = current["data"]["policy"]["version"]

        set_result = await safe_call_tool(
            mcp_client_with_policy_tool,
            TOOL_NAME,
            {
                "action": "set",
                "policy": {
                    "wait_seconds": 45,
                    "approval_ttl_minutes": 5,
                    "rules": [{"tool_name": "ha_call_service"}],
                    "version": version,
                },
            },
        )
        assert set_result.get("success") is True
        assert set_result["data"]["version"] == version + 1
        assert set_result["data"]["rules_changed"] is True

        after = await safe_call_tool(
            mcp_client_with_policy_tool, TOOL_NAME, {"action": "get"}
        )
        policy = after["data"]["policy"]
        assert policy["version"] == version + 1
        assert policy["wait_seconds"] == 45
        assert policy["rules"][0]["tool_name"] == "ha_call_service"

    async def test_set_rejects_stale_version(self, mcp_client_with_policy_tool):
        result = await safe_call_tool(
            mcp_client_with_policy_tool,
            TOOL_NAME,
            {
                "action": "set",
                "policy": {"rules": []},
                "expected_version": -1,
            },
        )
        assert result.get("success") is not True
        assert "version mismatch" in extract_error_message(result)
