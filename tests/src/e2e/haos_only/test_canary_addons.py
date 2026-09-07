"""Canary E2E for the HAOS test tier (see #1281).

Validates that app (add-on)-aware MCP tools work end-to-end against a real
booted HAOS image with the curated app set installed by ``build_image.py``.
The testcontainer suite cannot run these checks against a real Supervisor
because its partial mock covers only a few direct REST endpoints.

Six concrete assertions:
1. ``ha_get_app`` (default listing) contains every entry from ``ADDONS``
   plus ``GET_HACS_ADDON``, by display name.
2. ``ha_get_app(slug=core_mosquitto)`` returns Supervisor-backed detail.
3. ``ha_get_app(source="available")`` searches the live Supervisor store.
4. The in-app lane submits a harmless duplicate repository write.
5. Beta lanes boot the Supervisor channel/minimum and exact Core version
   resolved from the live beta manifest.
6. HACS is loaded and reachable through its MCP tool in the emitted image.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest
from packaging.version import Version

from ..utilities.assertions import MCPAssertions

LOG = logging.getLogger(__name__)


# Mirrors build_image.py's ADDONS tuple plus GET_HACS_ADDON. It lives outside
# pytest's normal import path, so this list is maintained manually. Missing
# expected entries fail loudly; additional builder-installed apps are not
# detected by this subset assertion.
INSTALLED_ADDON_NAMES = (
    "Mosquitto broker",
    "Node-RED",
    "ESPHome Device Builder",
    "Matter Server",
    "AppDaemon",
    "MQTT IO",
    "Get HACS",
)
BAKED_REPOSITORY_URL = "https://github.com/homeassistant-ai/ha-mcp"


async def test_addons_installed_via_mcp(mcp_client: Any) -> None:
    """`ha_get_app` lists each curated app expected by this canary."""
    async with MCPAssertions(mcp_client) as mcp:
        payload = await mcp.call_tool_success("ha_get_app", {})

    addons = payload.get("addons", [])
    summary = payload.get("summary", {})
    assert summary.get("total_installed") == len(addons)
    assert "filters_applied" not in payload
    assert all(addon.get("installed") is True and "state" in addon for addon in addons)
    installed_names = {addon.get("name") for addon in addons}
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
    in the external, embedded, and stdio HAOS lanes. The testcontainer cannot
    validate either transport against a real Supervisor.
    """
    async with MCPAssertions(mcp_client) as mcp:
        payload = await mcp.call_tool_success("ha_get_app", {"slug": "core_mosquitto"})
    detail = payload.get("addon") or payload.get("data") or payload
    # Confirm the known core slug resolves to Mosquitto.
    assert detail.get("name") == "Mosquitto broker", f"Unexpected app detail: {detail}"


async def test_addon_store_search_via_mcp(mcp_client: Any) -> None:
    """`ha_get_app(source='available')` reaches the real Supervisor store."""

    async with MCPAssertions(mcp_client) as mcp:
        payload = await mcp.call_tool_success(
            "ha_get_app", {"source": "available", "query": "mqtt"}
        )
    matches = payload.get("addons", [])
    assert payload.get("filters_applied") == {
        "repository": None,
        "query": "mqtt",
    }
    assert isinstance(payload.get("repositories"), list)
    assert payload.get("summary", {}).get("total_available") == len(matches)
    assert all("available" in addon for addon in matches)
    assert matches, f"Supervisor store returned no MQTT matches: {payload}"
    assert any(
        "mqtt" in f"{addon.get('name', '')} {addon.get('description', '')}".lower()
        for addon in matches
    ), f"Supervisor store search returned unrelated results: {matches}"


@pytest.mark.inaddon_only
async def test_addon_store_duplicate_write_via_mcp(mcp_client: Any) -> None:
    """The in-app tool can write directly to the real Supervisor store API."""
    async with MCPAssertions(mcp_client) as mcp:
        payload = await mcp.call_tool_success(
            "ha_manage_app",
            {
                "action": "add_repository",
                "repository": BAKED_REPOSITORY_URL,
            },
        )

    assert payload.get("action") == "add_repository"
    assert payload.get("repository") == BAKED_REPOSITORY_URL
    assert "no change needed" in str(payload.get("message", "")).lower()


@pytest.mark.beta_haos_only
async def test_beta_image_versions_match_manifest(ha_client: Any) -> None:
    """Beta lanes attest the versions running inside the booted HAOS VM."""
    expected_channel = os.environ.get("HAOS_EXPECTED_SUPERVISOR_CHANNEL")
    expected_supervisor = os.environ.get("HAOS_EXPECTED_SUPERVISOR_MIN_VERSION")
    expected_core = os.environ.get("HAOS_EXPECTED_CORE_VERSION")
    expected_os = os.environ.get("HAOS_EXPECTED_OS_VERSION")
    assert expected_channel is not None
    assert expected_supervisor is not None
    assert expected_core is not None
    assert expected_os is not None

    os_response = await ha_client.send_websocket_message(
        {"type": "supervisor/api", "endpoint": "/os/info", "method": "GET"}
    )
    assert os_response.get("success"), (
        f"Supervisor integration OS query failed: {os_response}"
    )
    os_info = os_response.get("result", {})
    assert isinstance(os_info, dict), f"Invalid OS info: {os_info!r}"
    assert os_info.get("version") == expected_os, (
        f"Expected HAOS {expected_os!r}, got {os_info.get('version')!r}"
    )

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
    async with MCPAssertions(mcp_client) as mcp:
        await mcp.call_tool_success(
            "ha_get_hacs_info",
            {"action": "search", "installed_only": True, "max_results": 1},
        )
