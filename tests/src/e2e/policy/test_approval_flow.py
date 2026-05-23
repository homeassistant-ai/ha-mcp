"""Real e2e test for the tool security policies middleware (#966).

Drives the FULL block → approve → re-call loop against a live
testcontainer HA + ha-mcp server with ``ENABLE_TOOL_SECURITY_POLICIES=true``,
using a function-scoped policy-enabled server fixture distinct from the
session-scoped ``mcp_client`` (which boots without policies).

The /api/policy/* HTTP routes are mounted on the same FastMCP Starlette
app as the MCP endpoint; the in-memory ``mcp_client`` transport bypasses
that app, so we drive the policy handlers (returned by
``build_policy_handlers``) via the same async ``Request`` -> ``JSONResponse``
contract the HTTP routes use. This still exercises the production handler
factory + ``ApprovalQueue`` + persistence path end-to-end — only the
Starlette routing layer is short-circuited. The MCP transport / tool
dispatch / middleware pipeline are exercised exactly as a real client
would see them.

Cannot run on Termux (no Docker for testcontainers); CI-only verification.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_constants import TEST_TOKEN

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.policy.handlers import build_policy_handlers
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.utils.data_paths import get_data_dir

from ..utilities.assertions import parse_mcp_result, tool_error_to_result


async def _expect_blocked(client: Client, args: dict[str, Any]) -> dict[str, Any]:
    """Call ``ha_call_service`` and return the parsed USER_APPROVAL_REQUIRED body.

    FastMCP clients normalize middleware-raised ``ToolError`` to either
    a raised ``ToolError`` (older transport behavior) or a result with
    ``isError=True`` carrying the JSON body in ``content[0].text`` (newer
    transport). Accept both so the test isn't pinned to a specific
    FastMCP version.
    """
    try:
        result = await client.call_tool("ha_call_service", args)
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    assert body.get("error", {}).get("code") == "USER_APPROVAL_REQUIRED", body
    return body


def _make_request(body: dict[str, Any] | None = None) -> MagicMock:
    """Build a minimal Starlette ``Request`` mock for direct handler calls.

    The /api/policy/* handlers only need ``await request.json()``; mock just
    that surface rather than wiring a full ASGI scope.
    """
    request = MagicMock()
    request.json = AsyncMock(return_value=body or {})
    return request


@pytest.fixture
async def policy_enabled_mcp(ha_container_with_fresh_config, monkeypatch, tmp_path):
    """Spin up a fresh policy-enabled MCP server bound to the testcontainer HA.

    Function-scoped so each test gets a clean ``ApprovalQueue`` and an
    isolated ``tool_policy.json`` (no cross-test bleed via the lru-cached
    ``get_data_dir``). The session-scoped ``mcp_server`` / ``mcp_client``
    fixtures boot without ``ENABLE_TOOL_SECURITY_POLICIES`` so they can't
    be reused here.

    Yields ``(client, server, policy_handlers)``:
      * ``client`` — in-memory ``fastmcp.Client`` bound to the policy-enabled MCP
      * ``server`` — the underlying ``HomeAssistantSmartMCPServer`` (exposes
        ``approval_queue``)
      * ``policy_handlers`` — dict of policy_get_config / policy_put_config /
        policy_post_approve / etc. closures, equivalent to what the HTTP
        routes mount.
    """
    container_info = ha_container_with_fresh_config
    if container_info.get("backend") == "haos_inaddon":
        pytest.skip(
            "Inaddon backend uses the addon's own MCP endpoint; this test "
            "needs an in-process server with ENABLE_TOOL_SECURITY_POLICIES=true."
        )

    monkeypatch.setenv("ENABLE_TOOL_SECURITY_POLICIES", "true")
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()

    # Reset cached settings so the new server picks up the env var.
    import ha_mcp.config

    monkeypatch.setattr(ha_mcp.config, "_settings", None)

    base_url = container_info["base_url"]
    token = container_info.get("token", TEST_TOKEN)
    ha_client = HomeAssistantClient(base_url=base_url, token=token)

    server = HomeAssistantSmartMCPServer(client=ha_client)
    assert getattr(server, "approval_queue", None) is not None, (
        "ENABLE_TOOL_SECURITY_POLICIES=true did not register an ApprovalQueue; "
        "verify _apply_tool_security_policies ran successfully."
    )

    handlers = build_policy_handlers(
        data_dir=tmp_path,
        queue=server.approval_queue,
    )

    client = Client(server.mcp)
    async with client:
        yield client, server, handlers

    await ha_client.close()
    get_data_dir.cache_clear()


@pytest.mark.asyncio
async def test_blocked_call_then_approve_then_recall(policy_enabled_mcp):
    """Block, approve, re-call succeeds; mutated args re-block (#966).

    Exercises the full middleware loop through the real MCP transport:
    1. ``PUT /api/policy/config`` enables a rule on ``ha_call_service``
       when ``args.domain == "light"``.
    2. First ``ha_call_service`` raises ``ToolError`` carrying
       ``USER_APPROVAL_REQUIRED`` + an approval token.
    3. ``POST /api/policy/approve`` consumes the token.
    4. Re-call with SAME args succeeds (queue lookup hits approved entry).
    5. Re-call with DIFFERENT args re-blocks (strict args-hash binding;
       approval does not blanket-permit future calls).
    """
    client, server, handlers = policy_enabled_mcp

    # 1. Install a rule that gates light service calls.
    current_resp = await handlers["policy_get_config"](_make_request())
    current = json.loads(current_resp.body)
    new_policy = {
        "enabled": True,
        "wait_seconds": 5,
        "approval_ttl_minutes": 5,
        "rules": [
            {
                "tool_name": "ha_call_service",
                "when": [{"path": "args.domain", "op": "eq", "value": "light"}],
                "remember_minutes": 0,
            }
        ],
        "version": current["version"],
    }
    put_resp = await handlers["policy_put_config"](_make_request(new_policy))
    assert put_resp.status_code == 200, put_resp.body

    # 2. First call: middleware gates → USER_APPROVAL_REQUIRED.
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.bed_light"}
    await _expect_blocked(client, args)
    pending = server.approval_queue.list_pending()
    assert len(pending) == 1, f"expected exactly one pending entry, got {pending!r}"
    token = pending[0].token

    # 3. Approve via the same handler the HTTP route would call.
    approve_resp = await handlers["policy_post_approve"](
        _make_request({"token": token})
    )
    assert approve_resp.status_code == 200, approve_resp.body

    # 4. Re-call with SAME args: middleware sees approved entry → proceeds.
    result = await client.call_tool("ha_call_service", args)
    assert not result.is_error, result

    # 5. Re-call with DIFFERENT args: strict args-hash binding → new gate.
    other_args = {
        "domain": "light",
        "service": "turn_off",
        "entity_id": "light.bed_light",
    }
    await _expect_blocked(client, other_args)
