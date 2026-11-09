"""Real e2e tests for the redact_secrets toggle (issue 2157).

Boots a fresh in-process ha-mcp server with ``REDACT_SECRETS=true`` against
the testcontainer HA (function-scoped — the session-scoped ``mcp_client``
boots without the flag) and verifies the deployable contract:

- reads keep working with the flag on: a plain read opens no options flow, so
  it withholds every option value of an entry that has one, and
  ``include_schema`` returns options with no password markers unredacted (no
  false redaction);
- a config-entry flow write carrying a redaction sentinel is rejected with
  a structured validation error — with the flag ON and with it OFF (the
  round-trip guard is deliberately unconditional).

- a stored integration secret stays out of the options form reads: Core's
  own ``forecast_solar`` options form marks its API key secret, so a change
  in how Core serializes that marker fails here (#3002).

Add-on ``format: password`` options need a Supervisor add-on, which the test
container does not ship; that transformation is covered by the unit suite
(``tests/src/unit/test_redaction.py``).

Requires Docker (testcontainers); runs in CI.
"""

from __future__ import annotations

import json
import uuid

import pytest
from test_constants import TEST_TOKEN

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.redaction import REDACTED_SET, _clear_known_secret_values, is_sentinel
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.utils.data_paths import get_data_dir

from ..utilities.assertions import MCPAssertions, parse_mcp_result, safe_call_tool
from ..utilities.forecast_solar import create_forecast_solar_entry


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
async def test_list_read_works_and_leaves_flowless_options_alone(redacting_mcp):
    """With the flag on, the list read succeeds and an entry without an options
    flow keeps its options unredacted (no false redaction)."""
    client, _server = redacting_mcp
    result = await client.call_tool(
        "ha_get_integration", {"include_options": True, "limit": 10}
    )
    body = parse_mcp_result(result)
    assert body.get("success") is True, body
    entries = body.get("entries") or []
    assert entries, body
    for entry in entries:
        if not entry.get("supports_options"):
            _assert_no_false_redaction(entry.get("options"))


@pytest.mark.asyncio
async def test_options_are_withheld_until_the_options_form_is_asked_for(
    redacting_mcp,
):
    """A plain read opens no options flow (#2987), so it cannot tell the
    password fields apart and returns no option value; include_schema reads
    the form and returns options with no password markers as stored."""
    client, _server = redacting_mcp
    created = parse_mcp_result(
        await client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "tod",
                "name": "redact e2e tod",
                "config": {
                    "name": "redact e2e tod",
                    "after_time": "08:00:00",
                    "before_time": "17:00:00",
                },
            },
        )
    )
    entry_id = created["entry_id"]
    try:
        plain = parse_mcp_result(
            await client.call_tool("ha_get_integration", {"entry_id": entry_id})
        )
        assert plain.get("success") is True, plain
        warnings = plain.get("warnings") or []
        if "options" in plain["entry"]:
            # The component served the options; every value is withheld.
            assert all(is_sentinel(v) for v in plain["entry"]["options"].values())
            assert any("redact_secrets" in w for w in warnings), plain
        else:
            # Without the component nothing was read.
            assert any("ha_mcp_tools" in w for w in warnings), plain

        described = parse_mcp_result(
            await client.call_tool(
                "ha_get_integration", {"entry_id": entry_id, "include_schema": True}
            )
        )
        assert described["entry"]["options"]["after_time"] == "08:00:00", described
    finally:
        await safe_call_tool(
            client,
            "ha_remove_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )


@pytest.mark.asyncio
async def test_stored_integration_secret_stays_out_of_options_form_reads(
    redacting_mcp, ha_client
):
    """An agent opening an integration's options form must not get the stored
    API key back. Core marks the field secret in its own serialization (since
    2026.11 a ``secret`` flag on a plain string field), so this fails when
    Core changes that marker and ha-mcp no longer recognises it (#3002)."""
    client, _server = redacting_mcp
    api_key = uuid.uuid4().hex[:16]
    # Core's options form for forecast_solar carries a secret field and needs
    # no device; the shared helper waits out the forecast fetch setup runs.
    flow_done = await create_forecast_solar_entry(ha_client)
    assert flow_done.get("type") == "create_entry", flow_done
    entry_id = flow_done["result"]["entry_id"]
    async with MCPAssertions(client) as mcp:
        flow_id = None
        try:
            await mcp.call_tool_success(
                "ha_set_integration",
                {"entry_id": entry_id, "config": {"api_key": api_key}},
            )
            # A server that never saw the value has nothing to scrub it with;
            # only recognising the field keeps it out.
            _clear_known_secret_values()

            form = await mcp.call_tool_success(
                "ha_set_integration", {"entry_id": entry_id}
            )
            flow_id = form["flow_id"]
            assert api_key not in json.dumps(form), form
            field = next(f for f in form["fields"] if f["name"] == "api_key")
            assert field["current"] == REDACTED_SET, field

            described = await mcp.call_tool_success(
                "ha_get_integration", {"entry_id": entry_id, "include_schema": True}
            )
            assert api_key not in json.dumps(described), described
        finally:
            if flow_id:
                await safe_call_tool(
                    client,
                    "ha_set_integration",
                    {"entry_id": entry_id, "flow_id": flow_id, "abort": True},
                )
            await safe_call_tool(
                client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )


async def _expect_sentinel_rejected(client: Client) -> None:
    """Create a flow helper with a sentinel-valued field; expect rejection."""
    body = await safe_call_tool(
        client,
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
