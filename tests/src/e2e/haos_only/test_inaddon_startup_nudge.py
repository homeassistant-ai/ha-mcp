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

from ..utilities.assertions import MCPAssertions, parse_mcp_result, safe_call_tool
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

# Real-world HAOS budgets (a 60 s warm-up starved in CI: two back-to-back
# container restarts, and DEBUG-level logging makes every log fetch heavy).
# The per-test pytest.mark.timeout below is sized to the phase sum plus
# restart margin, following the other HAOS long-runners' precedent.
_PROBE_TIMEOUT = 180.0
_RESTORE_TIMEOUT = 120.0
_WARM_TIMEOUT = 180.0
_POLL_INTERVAL = 3.0
_TEST_TIMEOUT_S = 600


async def _call_tool_fresh(addon_url: str, tool: str, args: dict[str, Any]) -> Any:
    """Call an MCP tool over a fresh streamable-HTTP connection.

    The server is stateless, so a new client per call is cheap and — unlike a
    long-lived session — immune to the addon restarting between calls.
    asyncio.wait_for bounds the whole exchange: a half-open connection to a
    bouncing addon otherwise parks the await indefinitely — the event loop
    sat idle at selector.select past the 600 s pytest-timeout with none of
    this test's own deadlines ever re-evaluated (round-4 CI failure).
    TimeoutError is in the polling transient set, so a bound trip is retried.
    """

    async def _exchange() -> Any:
        client = Client(StreamableHttpTransport(url=addon_url))
        async with client:
            return await client.call_tool(tool, args)

    raw = await asyncio.wait_for(_exchange(), timeout=30)
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


async def _restore_info_level(settings_advanced: str, settings_restart: str) -> None:
    """Restore log_level=INFO and bounce, retried as a set (leaked DEBUG
    would flood every later test's addon log)."""
    deadline = time.monotonic() + _RESTORE_TIMEOUT
    while True:
        try:
            await _post_log_level(settings_advanced, "INFO")
            await _restart_self(settings_restart)
            return
        except _RESTORE_TRANSIENT:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(_POLL_INTERVAL)


async def _warm_shared_client(mcp_client: Any) -> None:
    """Warm the SHARED session client back up after the self-restarts.

    Any completed round-trip — success or ToolError — proves the session
    is usable again; only transport-level transients keep the loop going.
    """
    deadline = time.monotonic() + _WARM_TIMEOUT
    last: object = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            # safe_call_tool: a returned dict — success OR ToolError shape —
            # is a completed round-trip, which is all warm-up needs. (Payload
            # inspection here proved harmful: it kept the loop spinning on a
            # healthy session — two prior CI failures.) wait_for bounds the
            # exchange, capped to the remaining budget so the last iteration
            # cannot overshoot the deadline.
            await asyncio.wait_for(
                safe_call_tool(mcp_client, "ha_get_overview", {}),
                timeout=min(30.0, remaining),
            )
            return
        except _RESTORE_TRANSIENT as err:
            # Transient while the addon bounces underneath us; the loop
            # condition bounds the retries.
            last = err
        await asyncio.sleep(min(_POLL_INTERVAL, max(deadline - time.monotonic(), 0)))
    raise AssertionError(
        "Shared mcp_client never warmed back up after the restore "
        f"restart within {_WARM_TIMEOUT}s (last={last!r})"
    )


@pytest.mark.inaddon_only
@pytest.mark.addon_disruptive
@pytest.mark.timeout(_TEST_TIMEOUT_S)
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
                log_text = payload.get("log", "")
                # Post-filter for the two REAL per-boot phrases: at DEBUG the
                # addon logs this probe's own tool calls, whose search
                # argument contains the bare signature — matching only the
                # signature would let the probe confirm itself.
                if payload.get("success") and (
                    "startup pass due" in log_text or "pass not due" in log_text
                ):
                    found = True
                    LOG.info(
                        "Nudge per-boot line found after restart: %s",
                        log_text[:200],
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
        await _restore_info_level(settings_advanced, settings_restart)
        await _warm_shared_client(mcp_client)

    LOG.info("Add-on launcher scheduled the startup nudge")
