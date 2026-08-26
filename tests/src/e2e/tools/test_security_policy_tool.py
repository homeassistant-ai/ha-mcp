"""
End-to-End tests for the security-policy tool (issue #2148).

This test suite validates:
- Feature flag behavior: ha_manage_security_policy is NOT registered by
  default and appears only when ENABLE_SECURITY_POLICY_TOOL is on
- get returns the full policy document plus its enforcement status
- set replaces the document and bumps its version
- the live approval queue stays unreachable through this tool
- the tool is gated like any other tool: a rule targeting it blocks it

Feature Flag: Set ENABLE_SECURITY_POLICY_TOOL=true to enable.
"""

import logging
import os
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ..utilities.assertions import (
    MCPAssertions,
    extract_error_message,
    parse_mcp_result,
    safe_call_tool,
    tool_error_to_result,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEATURE_FLAG = "ENABLE_SECURITY_POLICY_TOOL"
POLICIES_FLAG = "ENABLE_TOOL_SECURITY_POLICIES"
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
            if original is not None:
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
        async with MCPAssertions(mcp_client_with_policy_tool) as mcp:
            result = await mcp.call_tool_success(TOOL_NAME, {"action": "get"})
        assert set(result["data"]) == {"policy", "policies_enabled", "policies_live"}
        policy = result["data"]["policy"]
        assert "rules" in policy
        assert "version" in policy

    async def test_set_updates_and_bumps_version(self, mcp_client_with_policy_tool):
        async with MCPAssertions(mcp_client_with_policy_tool) as mcp:
            current = await mcp.call_tool_success(TOOL_NAME, {"action": "get"})
            version = current["data"]["policy"]["version"]

            set_result = await mcp.call_tool_success(
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
            assert set_result["data"]["version"] == version + 1
            assert set_result["data"]["rules_changed"] is True

            after = await mcp.call_tool_success(TOOL_NAME, {"action": "get"})
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

    async def test_set_without_rules_is_rejected(self, mcp_client_with_policy_tool):
        """An omitted 'rules' would wipe every gate — it must not be a
        silent success."""
        result = await safe_call_tool(
            mcp_client_with_policy_tool,
            TOOL_NAME,
            {"action": "set", "policy": {"wait_seconds": 30}},
        )
        assert result.get("success") is not True
        assert "'rules' is missing" in extract_error_message(result)


@pytest.fixture
async def self_gated_policy_mcp(ha_container_with_fresh_config, monkeypatch, tmp_path):
    """A server with BOTH the policy engine and the policy tool enabled.

    Function-scoped with its own data dir so the rule this test installs
    (which gates the policy tool itself) can't leak into other modules.
    Mirrors the policy_enabled_mcp fixture in
    tests/src/e2e/policy/test_approval_flow.py.
    """
    from ha_mcp.client.rest_client import HomeAssistantClient
    from ha_mcp.server import HomeAssistantSmartMCPServer
    from ha_mcp.utils.data_paths import get_data_dir
    from tests.test_constants import TEST_TOKEN

    container_info = ha_container_with_fresh_config
    if container_info.get("backend") == "haos_inaddon":
        pytest.skip(
            "Inaddon backend uses the addon's own MCP endpoint; this test "
            "needs an in-process server with the two flags on."
        )

    monkeypatch.setenv(POLICIES_FLAG, "true")
    monkeypatch.setenv(FEATURE_FLAG, "true")
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()

    from ha_mcp import config as ha_mcp_config

    monkeypatch.setattr(ha_mcp_config, "_settings", None)

    ha_client = HomeAssistantClient(
        base_url=container_info["base_url"],
        token=container_info.get("token", TEST_TOKEN),
    )
    server = HomeAssistantSmartMCPServer(client=ha_client)
    assert getattr(server, "approval_queue", None) is not None, (
        f"{POLICIES_FLAG}=true did not register an ApprovalQueue"
    )

    client = Client(server.mcp)
    async with client:
        yield client, server

    await ha_client.close()
    get_data_dir.cache_clear()


async def _expect_approval_required(
    client: Client, args: dict[str, Any]
) -> dict[str, Any]:
    """Call the policy tool and return the USER_APPROVAL_REQUIRED body.

    FastMCP normalizes a middleware-raised ToolError to either a raised
    ToolError or an isError result carrying the JSON body; accept both so
    the test isn't pinned to a transport version (same shape as
    test_approval_flow.py::_expect_blocked).
    """
    try:
        result = await client.call_tool(TOOL_NAME, args)
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    assert body.get("error", {}).get("code") == "USER_APPROVAL_REQUIRED", body
    return body


@pytest.mark.asyncio
async def test_policy_tool_can_gate_itself(self_gated_policy_mcp):
    """The security claim behind shipping this tool: it is gated like any
    other tool, so an operator can require human approval for every policy
    edit by adding a rule that names the tool itself.

    Writing that rule is the LAST ungated call — every later call to the
    tool blocks on approval.
    """
    client, server = self_gated_policy_mcp

    async with MCPAssertions(client) as mcp:
        await mcp.call_tool_success(
            TOOL_NAME,
            {
                "action": "set",
                "policy": {
                    "wait_seconds": 5,
                    "approval_ttl_minutes": 5,
                    "rules": [{"tool_name": TOOL_NAME}],
                    "version": 0,
                },
            },
        )

    # The rule now gates the tool: even a read needs approval.
    # create_error_response spreads the middleware's context fields at the
    # TOP level of the body, not under error.context.
    body = await _expect_approval_required(client, {"action": "get"})
    assert body["matched_rule"]["tool_name"] == TOOL_NAME, body
    pending = server.approval_queue.get(body["token"])
    assert pending is not None and pending.tool_name == TOOL_NAME, body

    # And so does a write that would remove the gate.
    await _expect_approval_required(
        client,
        {"action": "set", "policy": {"rules": [], "version": 1}},
    )
