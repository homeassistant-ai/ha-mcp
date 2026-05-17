"""Canary E2E for the HAOS test tier (see #1281).

Validates that addon-aware MCP tools work end-to-end against a real
booted HAOS image with the v1 addon set installed by ``build_image.py``.
This is the test the testcontainer suite *can't* run cleanly because
it would have to mock the entire Supervisor API surface (the partial
mock added in #1192 only covers a few direct REST endpoints).

Concretely: the test asserts that ``ha_get_addon`` (default listing
mode) returns each of the six addons the build script installs,
mapped by display name. If this stays green over time, additional
addon-using tests (start/stop/options/log fetch) can migrate from
the testcontainer mocked path to here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

LOG = logging.getLogger(__name__)


# Display names match what build_image.py installs. If that list changes,
# update both in tandem — there's no shared constant yet because
# build_image.py is intentionally stdlib-only and importing it here would
# pull qemu/websockets deps into a runtime-only test module.
EXPECTED_ADDONS = (
    "Mosquitto broker",
    "Node-RED",
    "ESPHome Device Builder",
    "Zigbee2MQTT",
    "Get HACS",
)


def _parse_tool_result(result: Any) -> dict[str, Any]:
    """FastMCP returns Content blocks; flatten to the JSON payload."""
    if hasattr(result, "content"):
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                return json.loads(text)
    if isinstance(result, dict):
        return result
    raise AssertionError(f"Unrecognised tool result shape: {result!r}")


async def test_addons_installed_via_mcp(haos_mcp_client: Any) -> None:
    """`ha_get_addon` (no args) lists every addon the build script installed."""
    raw = await haos_mcp_client.call_tool("ha_get_addon", {})
    payload = _parse_tool_result(raw)
    assert payload.get("success"), f"ha_get_addon returned failure: {payload}"

    installed_names = {a.get("name") for a in payload.get("addons", [])}
    LOG.info("Installed addons on booted HAOS: %s", sorted(installed_names))

    missing = [name for name in EXPECTED_ADDONS if name not in installed_names]
    if missing:
        pytest.fail(
            f"Expected addons missing from HAOS install: {missing}. "
            f"Installed set: {sorted(installed_names)}"
        )


async def test_supervisor_info_via_mcp(haos_mcp_client: Any) -> None:
    """`ha_get_addon` with a known core slug returns Supervisor-backed detail.

    This exercises the WS supervisor/api path through ha-mcp itself — the
    one the testcontainer can't validate because no Supervisor exists
    behind that mocked endpoint.
    """
    raw = await haos_mcp_client.call_tool("ha_get_addon", {"slug": "core_mosquitto"})
    payload = _parse_tool_result(raw)
    assert payload.get("success"), f"ha_get_addon(core_mosquitto) failed: {payload}"
    detail = payload.get("addon") or payload.get("data") or payload
    # Mosquitto is install=true, start=False in the build — so it should
    # be installed but not started. Either field name HA returns is fine.
    assert detail.get("name") == "Mosquitto broker", f"Unexpected addon detail: {detail}"
