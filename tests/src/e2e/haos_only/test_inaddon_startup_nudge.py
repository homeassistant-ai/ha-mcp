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
CI — 50 boots, one due-line). So the test drives the add-on to DEBUG via the
settings API (the ``test_addon_debug_log_level`` flow), records the current
process identity, restarts it, and requires the replacement process's own
startup diagnostics to contain a per-boot line, restoring INFO afterwards.
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

from ..utilities.assertions import parse_mcp_result, safe_call_tool
from ..utilities.wait_helpers import _POLLING_TRANSIENT_ERRORS

LOG = logging.getLogger(__name__)

# Matches BOTH the due (INFO) and not-due (DEBUG) per-boot lines — either
# proves the launcher scheduled the nudge. Keep in lockstep with
# src/ha_mcp/hacs_auto_refresh.py.
NUDGE_LOGGER = "ha_mcp.hacs_auto_refresh"
NUDGE_BOOT_LOG_PHRASES = (
    "HACS auto-refresh: startup pass due",
    "HACS auto-refresh: pass not due",
)

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


async def _get_instance_id(settings_info_url: str) -> str:
    """Read the settings endpoint's per-process identity."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.get(settings_info_url)
    assert resp.status_code == 200, (
        f"GET {settings_info_url} returned {resp.status_code}: {resp.text[:500]}"
    )
    data = resp.json()
    assert isinstance(data, dict), (
        f"GET {settings_info_url} returned non-object JSON: {data!r}"
    )
    instance_id = data.get("instance_id")
    assert isinstance(instance_id, str) and instance_id, (
        f"GET {settings_info_url} returned no process instance_id: {data!r}"
    )
    return instance_id


def _find_nudge_startup_record(startup_logs: object) -> dict[str, Any] | None:
    """Find a real per-boot nudge record in process-local startup logs."""
    if not isinstance(startup_logs, list):
        return None
    for entry in startup_logs:
        if not isinstance(entry, dict) or entry.get("logger") != NUDGE_LOGGER:
            continue
        message = entry.get("message")
        if isinstance(message, str) and message.startswith(NUDGE_BOOT_LOG_PHRASES):
            return entry
    return None


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
    settings_info = f"{base}{HA_MCP_TEST_SECRET_PATH}/api/settings/info"
    settings_advanced = f"{base}{HA_MCP_TEST_SECRET_PATH}/api/settings/advanced"
    settings_restart = f"{base}{HA_MCP_TEST_SECRET_PATH}/api/settings/restart"

    # The settings identity changes only when the add-on process does. Binding
    # this baseline to ha_report_issue's process-local startup records avoids
    # stale matches and rollover races in Supervisor's bounded log window.
    baseline_instance_id = await _get_instance_id(settings_info)
    LOG.info("Recorded pre-restart process %s", baseline_instance_id)

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
                    "ha_report_issue",
                    {
                        "tool_call_count": 1,
                        "fields": ["diagnostic_info", "startup_logs"],
                    },
                )
                if not isinstance(payload, dict) or not payload.get("success"):
                    last = f"unexpected ha_report_issue payload: {payload!r}"
                else:
                    diagnostic_info = payload.get("diagnostic_info")
                    instance = (
                        diagnostic_info.get("instance")
                        if isinstance(diagnostic_info, dict)
                        else None
                    )
                    current_instance_id = (
                        instance.get("instance_id")
                        if isinstance(instance, dict)
                        else None
                    )
                    if current_instance_id == baseline_instance_id:
                        last = (
                            f"still reached pre-restart process {baseline_instance_id}"
                        )
                    elif not isinstance(current_instance_id, str) or not (
                        current_instance_id
                    ):
                        last = f"ha_report_issue omitted process identity: {instance!r}"
                    else:
                        startup_logs = payload.get("startup_logs")
                        record = _find_nudge_startup_record(startup_logs)
                        if record is not None:
                            found = True
                            LOG.info(
                                "Replacement process %s captured nudge startup "
                                "record: %s",
                                current_instance_id,
                                record,
                            )
                            break
                        startup_log_count = (
                            len(startup_logs)
                            if isinstance(startup_logs, list)
                            else f"invalid {type(startup_logs).__name__}"
                        )
                        last = (
                            f"replacement process {current_instance_id} has no "
                            f"nudge record among {startup_log_count} startup logs"
                        )
            except _TRANSIENT as err:
                last = err
            await asyncio.sleep(_POLL_INTERVAL)

        assert found, (
            "A DEBUG-level replacement process did not capture a per-boot "
            f"nudge startup record within {_PROBE_TIMEOUT}s of the self-restart "
            f"(pre-restart instance={baseline_instance_id}, last={last!r}) — "
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
