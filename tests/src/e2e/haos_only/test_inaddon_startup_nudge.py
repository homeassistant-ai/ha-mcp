"""Add-on-lane regression test for the HACS startup nudge scheduling.

The launcher gap this branch fixes was SPECIFIC to the add-on: ``start.py``
calls ``mcp.run()`` directly, so the nudge scheduled from ``__main__``'s
``_run_with_shutdown`` never ran there, and no CI lane noticed. This test
closes that lane by proving the real add-on launcher enters the server
lifespan and starts the nudge.

The observable is one of the nudge's own per-boot lines: ``"startup pass
due"`` (INFO, first boot) or ``"pass not due"`` (DEBUG, later boots). Both
prove scheduling; which one fires depends on marker state, which this test
must NOT assume: the add-on's ``/data`` persists across restarts, and once
an early boot lives ~8.5 minutes its HACS-absent pass completes and writes
the marker, making every later boot legitimately not due (observed live in
CI — 50 boots, one due-line). So the test drives the add-on to DEBUG via
the settings API (the ``test_addon_debug_log_level`` flow), restarts it,
and searches the fresh boot's log for the shared ``"HACS auto-refresh:"``
prefix, restoring INFO afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from ..utilities.assertions import MCPAssertions, parse_mcp_result
from ..utilities.wait_helpers import _POLLING_TRANSIENT_ERRORS

LOG = logging.getLogger(__name__)

DEV_ADDON_NAME = "Home Assistant MCP Server (Dev)"

# Matches BOTH the due (INFO) and not-due (DEBUG) per-boot lines — either
# proves the launcher scheduled the nudge. Keep in lockstep with
# src/ha_mcp/hacs_auto_refresh.py.
NUDGE_LOG_SIGNATURE = "HACS auto-refresh:"

# Same transient sets as test_addon_debug_log_level (see its comments).
_TRANSIENT = (*_POLLING_TRANSIENT_ERRORS, httpx.HTTPError)
_RESTORE_TRANSIENT = (*_TRANSIENT, AssertionError)

# Budgets sized to fit inside pytest-timeout's 300 s per-test cap even on
# the all-loops-exhausted path: probe 120 + restore 60 + warm 60 (+ restarts
# at 5-25 s each) stays under it. The probe's happy path is a few seconds.
_PROBE_TIMEOUT = 120.0
_RESTORE_TIMEOUT = 60.0
_WARM_TIMEOUT = 60.0
_POLL_INTERVAL = 3.0


async def _call_tool_fresh(addon_url: str, tool: str, args: dict[str, Any]) -> Any:
    """Call an MCP tool over a fresh streamable-HTTP connection.

    The server is stateless, so a new client per call is cheap and — unlike a
    long-lived session — immune to the addon restarting between calls.
    """
    client = Client(StreamableHttpTransport(url=addon_url))
    async with client:
        raw = await client.call_tool(tool, args)
    return parse_mcp_result(raw)


async def _post_log_level(settings_advanced_url: str, level: str) -> None:
    """Write ``log_level`` through the settings advanced API."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(settings_advanced_url, json={"log_level": level})
    assert resp.status_code == 200, (
        f"POST {{'log_level': {level!r}}} to {settings_advanced_url} returned "
        f"{resp.status_code}: {resp.text[:500]}"
    )


async def _restart_self(settings_restart_url: str) -> None:
    """Self-restart via the settings restart endpoint (empty body → self)."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(settings_restart_url, json={})
    assert resp.status_code == 200, (
        f"self-restart POST returned {resp.status_code}: {resp.text[:300]}"
    )


@pytest.mark.inaddon_only
@pytest.mark.addon_disruptive
async def test_addon_launcher_schedules_the_startup_nudge(
    mcp_client: Any,
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """A DEBUG-level fresh boot must log one of the nudge's per-boot lines."""
    from haos_runtime import HA_MCP_TEST_SECRET_PATH, wait_for_addon_mcp_ready

    addon_url = ha_container_with_fresh_config.get("addon_mcp_url")
    assert addon_url, "inaddon container_info has no addon_mcp_url"
    base = addon_url.split("/mcp", 1)[0]
    settings_advanced = f"{base}{HA_MCP_TEST_SECRET_PATH}/api/settings/advanced"
    settings_restart = f"{base}{HA_MCP_TEST_SECRET_PATH}/api/settings/restart"

    # Resolve the dev addon's Supervisor slug while the shared client is
    # still live (pre-restart). call_tool_success per the test conventions:
    # a failed listing reports itself instead of reading as a missing addon.
    async with MCPAssertions(mcp_client) as mcp:
        data = await mcp.call_tool_success("ha_get_addon", {})
    addons = data.get("addons") or []
    dev_addon = next((a for a in addons if a.get("name") == DEV_ADDON_NAME), None)
    assert dev_addon is not None, (
        f"Dev addon {DEV_ADDON_NAME!r} not in ha_get_addon listing: "
        f"{[a.get('name') for a in addons]}"
    )
    slug = dev_addon["slug"]

    LOG.info("Flipping the dev add-on to DEBUG for a fresh, provable boot...")
    await _post_log_level(settings_advanced, "DEBUG")
    try:
        await _restart_self(settings_restart)

        deadline = time.monotonic() + _PROBE_TIMEOUT
        last: object = None
        found = False
        while time.monotonic() < deadline:
            try:
                url = wait_for_addon_mcp_ready(timeout=30.0)
                payload = await _call_tool_fresh(
                    url,
                    "ha_get_logs",
                    {
                        "source": "supervisor",
                        "slug": slug,
                        "search": NUDGE_LOG_SIGNATURE,
                    },
                )
                if payload.get("success") and payload.get("returned_lines", 0) > 0:
                    found = True
                    LOG.info(
                        "Nudge per-boot line found after restart: %s",
                        payload.get("log", "")[:200],
                    )
                    break
                last = f"no match yet in {payload.get('total_lines', 0)} lines"
            except _TRANSIENT as err:
                last = err
            await asyncio.sleep(_POLL_INTERVAL)

        assert found, (
            f"A DEBUG-level fresh boot never logged {NUDGE_LOG_SIGNATURE!r} "
            f"within {_PROBE_TIMEOUT}s of the self-restart (last={last!r}) — "
            "the add-on launcher did not schedule the startup nudge (neither "
            "the due INFO line nor the not-due DEBUG line appeared). This is "
            "the exact regression the lifespan wiring exists to prevent: "
            "start.py runs mcp.run() directly, so only a server-attached "
            "lifespan reaches the add-on."
        )
    finally:
        # Restore INFO for the rest of the session — retried as a set, like
        # the debug-log-level test (a leaked DEBUG level would flood every
        # later test's addon log).
        restore_deadline = time.monotonic() + _RESTORE_TIMEOUT
        while True:
            try:
                await _post_log_level(settings_advanced, "INFO")
                await _restart_self(settings_restart)
                break
            except _RESTORE_TRANSIENT:
                if time.monotonic() >= restore_deadline:
                    raise
                await asyncio.sleep(_POLL_INTERVAL)
        # The self-restarts dropped the SHARED session mcp_client's
        # connection. Warm it back up so later tests on this worker get a
        # live session.
        warm_deadline = time.monotonic() + _WARM_TIMEOUT
        while True:
            try:
                await mcp_client.call_tool("ha_get_overview", {})
                break
            except _RESTORE_TRANSIENT:
                if time.monotonic() >= warm_deadline:
                    raise
                await asyncio.sleep(_POLL_INTERVAL)

    LOG.info("Add-on launcher scheduled the startup nudge")
