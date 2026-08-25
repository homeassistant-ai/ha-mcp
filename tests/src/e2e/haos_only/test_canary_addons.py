"""Canary E2E for the HAOS test tier (see #1281).

Validates that app (add-on)-aware MCP tools work end-to-end against a real
booted HAOS image with the curated app set installed by ``build_image.py``.
The testcontainer suite cannot run these checks against a real Supervisor
because its partial mock covers only a few direct REST endpoints.

Five concrete assertions:
1. ``ha_get_app`` (default listing) returns every app the build script
   installs, by display name.
2. ``ha_get_app(slug=core_mosquitto)`` returns Supervisor-backed detail.
3. ``ha_get_app(source="available")`` searches the live Supervisor store.
4. Beta lanes boot the Supervisor channel/minimum and exact Core version
   resolved from the live beta manifest.
5. HACS is loaded and reachable through its MCP tool in the emitted image.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest
from packaging.version import Version

from ..utilities.assertions import parse_mcp_result

LOG = logging.getLogger(__name__)


# Mirrors build_image.py's ADDONS tuple plus GET_HACS_ADDON. Keep this
# expected app set in sync with the image builder. It is not shared because
# that script lives outside pytest's normal import path; drift fails loudly
# through the missing-name assertion below.
INSTALLED_ADDON_NAMES = (
    "Mosquitto broker",
    "Node-RED",
    "ESPHome Device Builder",
    "Matter Server",
    "AppDaemon",
    "MQTT IO",
    "Get HACS",
)


async def test_addons_installed_via_mcp(mcp_client: Any) -> None:
    """`ha_get_app` (no args) lists every app the build script installed."""
    raw = await mcp_client.call_tool("ha_get_app", {})
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_app returned failure: {payload}"

    installed_names = {a.get("name") for a in payload.get("addons", [])}
    LOG.info("Installed apps on booted HAOS: %s", sorted(installed_names))

    missing = [name for name in INSTALLED_ADDON_NAMES if name not in installed_names]
    if missing:
        pytest.fail(
            f"Expected apps missing from HAOS install: {missing}. "
            f"Installed set: {sorted(installed_names)}"
        )


async def test_supervisor_info_via_mcp(mcp_client: Any) -> None:
    """`ha_get_app` with a known core slug returns Supervisor-backed detail.

    This exercises direct REST in the in-app lane and Core's WebSocket proxy
    in the external and embedded HAOS lanes. The testcontainer cannot validate
    either transport against a real Supervisor.
    """
    raw = await mcp_client.call_tool("ha_get_app", {"slug": "core_mosquitto"})
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_app(core_mosquitto) failed: {payload}"
    detail = payload.get("addon") or payload.get("data") or payload
    # Mosquitto is install=true, start=False in the build — so it should
    # be installed but not started. Either field name HA returns is fine.
    assert detail.get("name") == "Mosquitto broker", f"Unexpected app detail: {detail}"


async def test_addon_store_search_via_mcp(mcp_client: Any) -> None:
    """`ha_get_app(source='available')` reaches the real Supervisor store."""

    raw = await mcp_client.call_tool(
        "ha_get_app", {"source": "available", "query": "mqtt"}
    )
    payload = parse_mcp_result(raw)

    assert payload.get("success"), f"ha_get_app store search failed: {payload}"
    matches = payload.get("addons", [])
    assert matches, f"Supervisor store returned no MQTT matches: {payload}"
    assert any(
        "mqtt" in f"{addon.get('name', '')} {addon.get('description', '')}".lower()
        for addon in matches
    ), f"Supervisor store search returned unrelated results: {matches}"


async def test_beta_image_versions_match_manifest(ha_client: Any) -> None:
    """Beta lanes attest the versions running inside the booted HAOS VM."""
    expected_channel = os.environ.get("HAOS_EXPECTED_SUPERVISOR_CHANNEL")
    expected_supervisor = os.environ.get("HAOS_EXPECTED_SUPERVISOR_MIN_VERSION")
    expected_core = os.environ.get("HAOS_EXPECTED_CORE_VERSION")
    expectations = (expected_channel, expected_supervisor, expected_core)
    if not any(expectations):
        pytest.skip("stable HAOS lane has no beta version contract")
    assert expected_channel is not None
    assert expected_supervisor is not None
    assert expected_core is not None

    supervisor_response = await ha_client.send_websocket_message(
        {
            "type": "supervisor/api",
            "endpoint": "/supervisor/info",
            "method": "GET",
        }
    )
    assert supervisor_response.get("success"), (
        f"Supervisor integration version query failed: {supervisor_response}"
    )
    supervisor_info = supervisor_response.get("result", {})
    assert isinstance(supervisor_info, dict), (
        f"Supervisor returned invalid info: {supervisor_info!r}"
    )
    actual_supervisor = supervisor_info.get("version")
    actual_channel = supervisor_info.get("channel")
    assert isinstance(actual_supervisor, str), (
        f"Supervisor returned no running version: {supervisor_info}"
    )
    assert actual_channel == expected_channel, (
        f"Expected Supervisor channel {expected_channel!r}, got {actual_channel!r}"
    )
    assert Version(actual_supervisor) >= Version(expected_supervisor), (
        f"Expected Supervisor >= {expected_supervisor}, got {actual_supervisor}"
    )

    core_config = await ha_client.get_config()
    actual_core = core_config.get("version")
    assert actual_core == expected_core, (
        f"Expected Core {expected_core!r}, got {actual_core!r}"
    )


async def test_hacs_available_in_emitted_image(mcp_client: Any) -> None:
    """The emitted qcow2 boots with HACS loaded and reachable through MCP.

    The post-shutdown seed state also contains HACS files and a config entry,
    so this validates final runtime availability rather than isolating which
    image-build step supplied the integration.
    """
    raw = await mcp_client.call_tool(
        "ha_get_hacs_info",
        {"action": "search", "installed_only": True, "max_results": 1},
    )
    payload = parse_mcp_result(raw)
    # ha_get_hacs_info returns {"success": True, "data": {...}, "metadata":
    # {...}} — ``success`` at the top level above the timezone wrapper. The
    # legacy nested {"data": {"success": ...}} shape remains accepted.
    inner = payload.get("data", payload) if isinstance(payload, dict) else {}
    assert payload.get("success") or inner.get("success"), (
        f"HACS integration not reachable via ha_get_hacs_info: {payload}"
    )
