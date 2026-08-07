"""Real e2e tests for the redact_secrets toggle (issue 2157).

Boots a fresh in-process ha-mcp server with ``REDACT_SECRETS=true`` against
the testcontainer HA (function-scoped — the session-scoped ``mcp_client``
boots without the flag) and verifies the deployable contract:

- reads keep working with the flag on, and options with no password markers
  pass through unredacted (no false redaction);
- a config-entry flow write carrying a redaction sentinel is rejected with
  a structured validation error — with the flag ON and with it OFF (the
  round-trip guard is deliberately unconditional).

The password-bearing read surfaces themselves (add-on ``format: password``
options, integration password selectors) need a Supervisor add-on or an
integration with a password options flow, which the test container does not
ship; those transformations are covered by the unit suite
(``tests/src/unit/test_redaction.py``).

Requires Docker (testcontainers); runs in CI.
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_constants import TEST_TOKEN

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.redaction import REDACTED_SET, is_sentinel
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.utils.data_paths import get_data_dir

from ..utilities.assertions import parse_mcp_result, tool_error_to_result


async def _build_redacting_server(container_info, monkeypatch, tmp_path):
    if container_info.get("backend") == "haos_inaddon":
        pytest.skip(
            "Inaddon backend uses the addon's own MCP endpoint; this test "
            "needs an in-process server with REDACT_SECRETS=true."
        )

    monkeypatch.setenv("REDACT_SECRETS", "true")
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()

    # Reset cached settings so the new server picks up the env vars.
    import ha_mcp.config

    monkeypatch.setattr(ha_mcp.config, "_settings", None)

    base_url = container_info["base_url"]
    token = container_info.get("token", TEST_TOKEN)
    ha_client = HomeAssistantClient(base_url=base_url, token=token)
    server = HomeAssistantSmartMCPServer(client=ha_client)
    return server, ha_client


@pytest.fixture
async def redacting_mcp(ha_container_with_fresh_config, monkeypatch, tmp_path):
    """In-process server with REDACT_SECRETS=true."""
    server, ha_client = await _build_redacting_server(
        ha_container_with_fresh_config, monkeypatch, tmp_path
    )
    client = Client(server.mcp)
    async with client:
        yield client, server
    await ha_client.close()
    get_data_dir.cache_clear()


def _assert_no_false_redaction(options) -> None:
    """No option value on the secret-free test container may be a sentinel."""
    if not isinstance(options, dict):
        return
    for value in options.values():
        assert not is_sentinel(value), options


@pytest.mark.asyncio
async def test_reads_work_and_secret_free_options_pass_through(redacting_mcp):
    """With the flag on, list + detail reads succeed and options without
    password markers are returned unredacted (no false redaction)."""
    client, _server = redacting_mcp
    result = await client.call_tool(
        "ha_get_integration", {"include_options": True, "limit": 10}
    )
    body = parse_mcp_result(result)
    assert body.get("success") is True, body
    entries = body.get("entries") or []
    assert entries, body
    for entry in entries:
        _assert_no_false_redaction(entry.get("options"))

    detail = await client.call_tool(
        "ha_get_integration", {"entry_id": entries[0]["entry_id"]}
    )
    detail_body = parse_mcp_result(detail)
    assert detail_body.get("success") is True, detail_body
    _assert_no_false_redaction(detail_body.get("entry", {}).get("options"))


async def _expect_sentinel_rejected(client: Client) -> None:
    """Create a flow helper with a sentinel-valued field; expect rejection."""
    try:
        result = await client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "group",
                "config": {
                    "name": "redact e2e group",
                    "group_type": "light",
                    "entities": [REDACTED_SET],
                },
            },
        )
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    error = body.get("error") or {}
    assert error.get("code") == "VALIDATION_INVALID_PARAMETER", body
    assert "placeholder" in error.get("message", ""), body


@pytest.mark.asyncio
async def test_sentinel_flow_write_rejected_with_flag_on(redacting_mcp):
    client, _server = redacting_mcp
    await _expect_sentinel_rejected(client)


@pytest.mark.asyncio
async def test_sentinel_flow_write_rejected_with_flag_off(mcp_client):
    """The round-trip guard is unconditional: the session server (flag off)
    must reject sentinel-valued flow writes too."""
    await _expect_sentinel_rejected(mcp_client)
