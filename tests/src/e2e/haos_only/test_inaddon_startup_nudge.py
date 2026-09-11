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
startup diagnostics to contain a per-boot line. Cleanup restores INFO and
waits for that replacement process before the shared session is created.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from ..utilities.addon_restart import TRANSIENT_ADDON_ERRORS as _TRANSIENT
from ..utilities.addon_restart import call_tool_fresh as _call_tool_fresh
from ..utilities.addon_restart import get_instance_id as _get_instance_id
from ..utilities.addon_restart import post_log_level as _post_log_level
from ..utilities.addon_restart import restart_self as _restart_self
from ..utilities.addon_restart import restore_info_level

LOG = logging.getLogger(__name__)

# Matches BOTH the due (INFO) and not-due (DEBUG) per-boot lines — either
# proves the launcher scheduled the nudge. Keep in lockstep with
# src/ha_mcp/hacs_auto_refresh.py.
NUDGE_LOGGER = "ha_mcp.hacs_auto_refresh"
NUDGE_BOOT_LOG_PHRASES = (
    "HACS auto-refresh: startup pass due",
    "HACS auto-refresh: pass not due",
)

# Real-world HAOS budgets (a 60 s warm-up starved in CI: two back-to-back
# container restarts, and DEBUG-level logging makes every log fetch heavy).
# The per-test pytest.mark.timeout below is sized to the phase sum plus
# restart margin, following the other HAOS long-runners' precedent.
_PROBE_TIMEOUT = 180.0
_RESTORE_TIMEOUT = 120.0
_READY_TIMEOUT = 180.0
_POLL_INTERVAL = 3.0
_TEST_TIMEOUT_S = 600


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


@pytest.mark.inaddon_only
@pytest.mark.addon_disruptive
@pytest.mark.timeout(_TEST_TIMEOUT_S)
async def test_addon_launcher_schedules_the_startup_nudge(
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
        await restore_info_level(
            settings_advanced,
            settings_restart,
            settings_info,
            addon_url,
            restore_timeout=_RESTORE_TIMEOUT,
            ready_timeout=_READY_TIMEOUT,
            poll_interval=_POLL_INTERVAL,
        )

    LOG.info("Add-on launcher scheduled the startup nudge")
