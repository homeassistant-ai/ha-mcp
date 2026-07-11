"""Real e2e tests for the strict mandatory best-practices gate (#1779).

Boots a fresh in-process ha-mcp server with ``ENABLE_STRICT_MANDATORY_BPS``
set explicitly (the shared session server pins it OFF in conftest) against
the testcontainer HA, and verifies the full contract:

- with strict effective, a keyless ``ha_config_set_automation`` write is
  BLOCKED with the structured BPS_ACKNOWLEDGMENT_REQUIRED error and no
  automation is created;
- ``ha_get_skill_guide`` Tier-3 best-practices content carries the
  acknowledgment key line (the only caller-facing surface that does);
- the SAME write, now carrying ``BestPracticeKey=<the key>``, succeeds;
- with strict disabled, a keyless write succeeds (baseline).

Requires Docker (testcontainers) and the skills-vendor submodule (the gate
fails open without it); runs in CI.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_constants import TEST_TOKEN

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.strict_bps import STRICT_BPS_ACK_KEY
from ha_mcp.utils.data_paths import get_data_dir
from ha_mcp.utils.skill_loader import get_skills_dir

from ..utilities.assertions import (
    parse_mcp_result,
    safe_call_tool,
    tool_error_to_result,
)
from ..utilities.entity_finders import find_test_light_entity

_BEST_PRACTICES_SKILL = "home-assistant-best-practices"
_AUTOMATION_PATTERNS_REF = "references/automation-patterns.md"


async def _build_strict_server(container_info, monkeypatch, tmp_path, *, strict: bool):
    if container_info.get("backend") == "haos_inaddon":
        pytest.skip(
            "Inaddon backend uses the addon's own MCP endpoint; this test "
            "needs an in-process server it can configure via env."
        )

    monkeypatch.setenv("ENABLE_STRICT_MANDATORY_BPS", "true" if strict else "false")
    # Parent master switch defaults ON, but pin it explicitly so the gate's
    # effective state is deterministic regardless of ambient env.
    monkeypatch.setenv("ENABLE_MANDATORY_BPS", "true")
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()

    import ha_mcp.config

    monkeypatch.setattr(ha_mcp.config, "_settings", None)

    base_url = container_info["base_url"]
    token = container_info.get("token", TEST_TOKEN)
    ha_client = HomeAssistantClient(base_url=base_url, token=token)
    server = HomeAssistantSmartMCPServer(client=ha_client)
    return server, ha_client


@pytest.fixture
async def strict_bps_mcp(ha_container_with_fresh_config, monkeypatch, tmp_path):
    """In-process server with strict best-practices mode enabled."""
    if get_skills_dir() is None:
        pytest.skip(
            "skills-vendor submodule absent — the strict-BPS gate fails open, "
            "so there is nothing to assert. Run `git submodule update --init`."
        )
    server, ha_client = await _build_strict_server(
        ha_container_with_fresh_config, monkeypatch, tmp_path, strict=True
    )
    client = Client(server.mcp)
    async with client:
        yield client, server
    await ha_client.close()
    get_data_dir.cache_clear()


@pytest.fixture
async def strict_disabled_mcp(ha_container_with_fresh_config, monkeypatch, tmp_path):
    """In-process server with strict mode explicitly disabled (baseline)."""
    server, ha_client = await _build_strict_server(
        ha_container_with_fresh_config, monkeypatch, tmp_path, strict=False
    )
    client = Client(server.mcp)
    async with client:
        yield client, server
    await ha_client.close()
    get_data_dir.cache_clear()


# Minimal plausible arguments per gated tool for the blocked-path loop —
# the gate raises BEFORE tool-argument validation, so these only need to be
# schema-shaped enough for the client to send them.
_GATED_TOOL_MINIMAL_ARGS: dict[str, dict[str, Any]] = {
    "ha_config_set_automation": {"config": {"alias": "x"}},
    "ha_config_set_script": {"script_id": "x", "config": {"sequence": []}},
    "ha_config_set_scene": {"scene_id": "x", "config": {"entities": {}}},
    "ha_config_set_helper": {"helper_type": "input_boolean", "name": "x"},
    "ha_config_set_dashboard": {"url_path": "x", "config": {"views": []}},
    "ha_config_set_yaml": {"yaml_path": "automations.yaml", "content": "[]"},
}


def _automation_config(name: str, light: str) -> dict[str, Any]:
    return {
        "alias": name,
        "trigger": [{"platform": "time", "at": "07:00:00"}],
        "action": [{"service": "light.turn_on", "target": {"entity_id": light}}],
    }


async def _expect_bps_blocked(
    client: Client, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Call ``tool`` and return the parsed BPS_ACKNOWLEDGMENT_REQUIRED body.

    Accepts both the raised-ToolError and isError-result transports.
    """
    try:
        result = await client.call_tool(tool, args)
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    assert body.get("error", {}).get("code") == "BPS_ACKNOWLEDGMENT_REQUIRED", body
    return body


@pytest.mark.asyncio
async def test_keyless_write_blocked_with_structured_error(strict_bps_mcp):
    client, _server = strict_bps_mcp
    light = await find_test_light_entity(client)
    config = _automation_config("Strict BPS Keyless E2E", light)

    body = await _expect_bps_blocked(
        client, "ha_config_set_automation", {"config": config}
    )
    # The block error must guide recovery without leaking the key.
    assert STRICT_BPS_ACK_KEY not in str(body)
    assert body.get("strict_mandatory_bps") is True
    suggestion = body["error"].get("suggestion", "")
    assert "ha_get_skill_guide" in suggestion
    assert _AUTOMATION_PATTERNS_REF in suggestion


@pytest.mark.asyncio
async def test_skill_guide_publishes_ack_key(strict_bps_mcp):
    client, _server = strict_bps_mcp
    result = await client.call_tool(
        "ha_get_skill_guide",
        {"skill": _BEST_PRACTICES_SKILL, "file": _AUTOMATION_PATTERNS_REF},
    )
    body = parse_mcp_result(result)
    assert body.get("success") is True, body
    assert STRICT_BPS_ACK_KEY in body.get("content", ""), (
        "strict mode ON: the Tier-3 best-practices content must publish the "
        "acknowledgment key"
    )


@pytest.mark.asyncio
async def test_write_with_key_succeeds(strict_bps_mcp):
    client, _server = strict_bps_mcp
    light = await find_test_light_entity(client)
    config = _automation_config("Strict BPS WithKey E2E", light)

    result = await safe_call_tool(
        client,
        "ha_config_set_automation",
        {"config": config, "BestPracticeKey": STRICT_BPS_ACK_KEY},
    )
    assert result.get("success"), f"keyed write should succeed: {result}"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(_GATED_TOOL_MINIMAL_ARGS))
async def test_every_gated_tool_blocked_keyless(strict_bps_mcp, tool_name):
    """The gate covers all six tools through the real registered surface —
    not just the automation tool the happy-path tests exercise. The block
    fires before tool-argument validation, so nothing is created."""
    client, _server = strict_bps_mcp
    await _expect_bps_blocked(client, tool_name, _GATED_TOOL_MINIMAL_ARGS[tool_name])


@pytest.mark.asyncio
async def test_keyless_write_succeeds_when_strict_disabled(strict_disabled_mcp):
    client, _server = strict_disabled_mcp
    light = await find_test_light_entity(client)
    config = _automation_config("Strict BPS Disabled E2E", light)

    result = await safe_call_tool(
        client, "ha_config_set_automation", {"config": config}
    )
    assert result.get("success"), (
        f"keyless write should succeed when strict off: {result}"
    )
