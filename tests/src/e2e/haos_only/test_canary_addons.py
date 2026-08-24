"""Canary E2E for the HAOS test tier (see #1281).

Validates that addon-aware MCP tools work end-to-end against a real
booted HAOS image with the v1 addon set installed by ``build_image.py``.
This is the test the testcontainer suite *can't* run cleanly because
it would have to mock the entire Supervisor API surface (the partial
mock added in #1192 only covers a few direct REST endpoints).

Four concrete assertions:
1. ``ha_get_app`` (default listing) returns every addon the build
   script installs, by display name.
2. ``ha_get_app(slug=core_mosquitto)`` returns Supervisor-backed
   detail for a known core slug.
3. ``ha_get_app(source="available")`` searches the live Supervisor store.
4. HACS bootstrap actually completed — the "Get HACS" addon installs
   HACS into ``/config/custom_components/hacs/``, and HACS registers the
   ``hacs`` integration on first HA Core start; if either step
   silently failed, the addon would still be present but HACS wouldn't
   be loaded.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from ..utilities.assertions import parse_mcp_result

LOG = logging.getLogger(__name__)


# Mirrors build_image.py's ADDONS list — keep both in sync when the
# addon set changes. Not a shared constant because the build script
# lives outside the pytest rootdir's import paths; the duplication is
# small and the failure mode of drift is loud (this test fails fast on
# the missing-name list).
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
    """`ha_get_app` (no args) lists every addon the build script installed."""
    raw = await mcp_client.call_tool("ha_get_app", {})
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_app returned failure: {payload}"

    installed_names = {a.get("name") for a in payload.get("addons", [])}
    LOG.info("Installed addons on booted HAOS: %s", sorted(installed_names))

    missing = [name for name in INSTALLED_ADDON_NAMES if name not in installed_names]
    if missing:
        pytest.fail(
            f"Expected addons missing from HAOS install: {missing}. "
            f"Installed set: {sorted(installed_names)}"
        )


async def test_supervisor_info_via_mcp(mcp_client: Any) -> None:
    """`ha_get_app` with a known core slug returns Supervisor-backed detail.

    This exercises the Supervisor path through ha-mcp itself — direct REST in
    the in-addon lane and Core's WebSocket proxy in the external lane. The
    testcontainer cannot validate either against a real Supervisor.
    """
    raw = await mcp_client.call_tool("ha_get_app", {"slug": "core_mosquitto"})
    payload = parse_mcp_result(raw)
    assert payload.get("success"), f"ha_get_app(core_mosquitto) failed: {payload}"
    detail = payload.get("addon") or payload.get("data") or payload
    # Mosquitto is install=true, start=False in the build — so it should
    # be installed but not started. Either field name HA returns is fine.
    assert detail.get("name") == "Mosquitto broker", (
        f"Unexpected addon detail: {detail}"
    )


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


async def test_hacs_bootstrap_completed(mcp_client: Any) -> None:
    """HACS bootstrap reached its product, not just installed the addon.

    The "Get HACS" addon's purpose is to drop HACS into
    /config/custom_components/hacs/ on first run; HA then loads the
    integration. If the addon installs but the bootstrap step silently
    fails mid-build (network glitch, addon image change), the previous
    test still passes but HACS itself is missing. Probe a HACS-specific
    tool to confirm the integration is reachable.
    """
    raw = await mcp_client.call_tool(
        "ha_get_hacs_info",
        {"action": "search", "installed_only": True, "max_results": 1},
    )
    payload = parse_mcp_result(raw)
    # ha_get_hacs_info returns {"success": True, "data": {...}, "metadata":
    # {...}} — ``success`` at the TOP level above the timezone wrapper (the
    # PR #2124 envelope). The legacy nested {"data": {"success": ...}} shape
    # is accepted too so this canary spans the transition. If HACS isn't
    # loaded, the tool raises ToolError (parse_mcp_result surfaces it as
    # {"success": False, "error": ...}), which fails both checks.
    inner = payload.get("data", payload) if isinstance(payload, dict) else {}
    assert payload.get("success") or inner.get("success"), (
        f"HACS integration not reachable via ha_get_hacs_info — "
        f"bootstrap from 'Get HACS' addon may have silently failed. "
        f"Response: {payload}"
    )
