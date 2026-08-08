"""Add-on-lane regression test for the HACS startup nudge scheduling.

The launcher gap this branch fixes was SPECIFIC to the add-on: ``start.py``
calls ``mcp.run()`` directly, so the nudge scheduled from ``__main__``'s
``_run_with_shutdown`` never ran there, and no CI lane noticed. This test
closes that lane: the inaddon tier runs the real dev add-on built from this
PR's source, and a due startup pass emits one unconditional INFO line
*before* any WebSocket work (``hacs_auto_refresh.py``), so the line appears
even though the HAOS VM ships no HACS (where the pass then retries quietly
and ends silently). Finding that line in the add-on's own logs proves the
real add-on launcher entered the server lifespan and started the nudge —
exactly what was silently missing before.

The test self-restarts the add-on first (the ``test_addon_debug_log_level``
pattern) because the line is a one-shot BOOT line: by the time this test
runs, the boot that emitted it sits thousands of request-log lines back,
beyond even the expanded journald search window
(``SUPERVISOR_SEARCH_WINDOW_LINES``). A fresh boot is still due — the HAOS
VM has no HACS, so no earlier pass ever completed and wrote a marker — and
puts the line in the fresh tail where the search window sees it.
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

NUDGE_LOG_SIGNATURE = "HACS auto-refresh: startup pass due"

# Same transient set as test_addon_debug_log_level: the MCP-client polling
# tuple plus httpx transport errors from connections dying mid-restart.
# Bugs (TypeError, KeyError, AssertionError) must propagate.
_TRANSIENT = (*_POLLING_TRANSIENT_ERRORS, httpx.HTTPError)

# The warm-up loop additionally rides out the HTTP-status asserts the
# restart helper makes while the addon is still bouncing.
_RESTORE_TRANSIENT = (*_TRANSIENT, AssertionError)

# Addon restart = container stop + start; CI runners take 5-25s.
_RECOVERY_TIMEOUT = 180.0
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
    """A fresh add-on boot must log the nudge's due-pass line.

    A fresh boot is always due here (no marker: the VM has no HACS, so no
    prior pass ever completed), and the INFO line lands before any WebSocket
    work, so HACS being absent cannot suppress it.
    """
    from haos_runtime import HA_MCP_TEST_SECRET_PATH, wait_for_addon_mcp_ready

    addon_url = ha_container_with_fresh_config.get("addon_mcp_url")
    assert addon_url, "inaddon container_info has no addon_mcp_url"
    base = addon_url.split("/mcp", 1)[0]
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

    LOG.info("Restarting the dev add-on to get a fresh boot in the log tail...")
    try:
        await _restart_self(settings_restart)

        deadline = time.monotonic() + _RECOVERY_TIMEOUT
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
                        "Due-pass line found after restart: %s",
                        payload.get("log", "")[:200],
                    )
                    break
                last = f"no match yet in {payload.get('total_lines', 0)} lines"
            except _TRANSIENT as err:
                last = err
            await asyncio.sleep(_POLL_INTERVAL)

        assert found, (
            f"The dev add-on's fresh boot never logged {NUDGE_LOG_SIGNATURE!r} "
            f"within {_RECOVERY_TIMEOUT}s of the self-restart (last={last!r}) — "
            "the add-on launcher did not schedule the startup nudge. This is "
            "the exact regression the lifespan wiring exists to prevent: "
            "start.py runs mcp.run() directly, so only a server-attached "
            "lifespan reaches the add-on."
        )
    finally:
        # The self-restart dropped the SHARED session mcp_client's connection.
        # Warm it back up so later tests on this worker get a live session.
        warm_deadline = time.monotonic() + _RECOVERY_TIMEOUT
        while True:
            try:
                await mcp_client.call_tool("ha_get_overview", {})
                break
            except _RESTORE_TRANSIENT:
                if time.monotonic() >= warm_deadline:
                    raise
                await asyncio.sleep(_POLL_INTERVAL)

    LOG.info("Add-on launcher scheduled the startup nudge")
