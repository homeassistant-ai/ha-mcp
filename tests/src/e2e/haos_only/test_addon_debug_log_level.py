"""E2E for the web-UI log level → addon log path (#1721 diagnosis fix).

Runs the FULL user journey against the real dev addon inside HAOS
(inaddon tier only — this is the deployment where the bug lived):

1. POST ``{"log_level": "DEBUG"}`` to the settings advanced API — the
   same endpoint the web Settings UI uses — which persists to
   ``/data/feature_flags.json`` and reports ``restart_required``.
2. Self-restart via the settings ``/restart`` endpoint (empty body →
   target='self'; the handler schedules the bounce in the background so
   the 200 flushes first — same mechanism as test_readonly_mode.py).
3. Poll (fresh streamable-HTTP client per round, via
   ``wait_for_addon_mcp_ready``) until the addon's own container log —
   ``ha_get_logs(source="supervisor")`` — contains the DEBUG canary.
   The canary is emitted through the logging system at DEBUG level, so
   its presence is a positive proof BOTH that the restart took effect
   AND that the override reached the root logger. start.py used to
   hardcode ``basicConfig(level=INFO)``, which made the web-UI setting
   a silent no-op and left #1721's reporter unable to produce debug
   logs. Kill-signal diagnostics arm on the same condition (they
   replaced the removed ``advanced_debug_logging`` addon toggle).

The ``finally`` restores ``log_level=INFO`` (retried set+restart, as
the readonly-mode test does) and re-warms the SHARED session
``mcp_client`` — self-restarts drop its connection for later tests on
this worker (documented in test_supervisor_inaddon.py).
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

from ..utilities.assertions import parse_mcp_result
from ..utilities.wait_helpers import _POLLING_TRANSIENT_ERRORS

LOG = logging.getLogger(__name__)

DEV_ADDON_NAME = "Home Assistant MCP Server (Dev)"

# start.py log lines asserted below. Keep in lockstep with
# homeassistant-addon/start.py (the canary + arming strings).
DEBUG_CANARY = "Debug logging active (log_level applied from settings)"
ARMING_LINE = "arming kill-signal diagnostics"
INSTALLED_LINE = "kill-signal diagnostics installed for"

# Transient errors expected while the addon restarts underneath us:
# the MCP-client polling set from wait_helpers, plus httpx transport
# errors from a freshly-opened connection dying or being refused
# mid-restart. Bugs (TypeError, KeyError) must propagate per
# .gemini/styleguide.md "Exception Handling in Test Polling Loops".
_TRANSIENT = (*_POLLING_TRANSIENT_ERRORS, httpx.HTTPError, AssertionError)

# Addon restart = container stop + start; CI runners take 5-25s, plus
# the log line for the install thread lags boot slightly.
_RECOVERY_TIMEOUT = 180.0
_POLL_INTERVAL = 3.0


async def _call_tool_fresh(addon_url: str, tool: str, args: dict[str, Any]) -> Any:
    """Call an MCP tool over a fresh streamable-HTTP connection.

    The server is stateless, so a new client per call is cheap and —
    unlike a long-lived session — immune to the addon restarting
    between calls.
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
    body = resp.json()
    assert body.get("restart_required") is True, (
        f"settings API response missing restart_required=True: {body}"
    )


async def _restart_self(settings_restart_url: str) -> None:
    """Self-restart via the settings restart endpoint.

    Empty body → target='self'; the handler schedules the bounce in the
    background so this 200 flushes before the process dies (same
    mechanism the readonly-mode e2e uses).
    """
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(settings_restart_url, json={})
    assert resp.status_code == 200, (
        f"self-restart POST returned {resp.status_code}: {resp.text[:300]}"
    )


async def _await_lines_in_addon_log(slug: str, required: tuple[str, ...]) -> str:
    """Poll the addon's own container log until all ``required`` lines appear.

    Reconnects each round (``wait_for_addon_mcp_ready`` + fresh client),
    so it rides out the restart window. Returning only when every line
    is present is the positive both-proofs signal: the canary can only
    exist after a boot that applied DEBUG, and the install-confirmation
    lags boot by the install thread's poll cadence.
    """
    from haos_runtime import wait_for_addon_mcp_ready

    deadline = time.monotonic() + _RECOVERY_TIMEOUT
    last: object = None
    while time.monotonic() < deadline:
        try:
            url = wait_for_addon_mcp_ready(timeout=30.0)
            data = await _call_tool_fresh(
                url,
                "ha_get_logs",
                {"source": "supervisor", "slug": slug, "limit": 2000},
            )
            logs = data.get("log", "")
            missing = [line for line in required if line not in logs]
            if not missing:
                return logs
            last = f"missing: {missing}"
        except _TRANSIENT as err:
            last = err
        await asyncio.sleep(_POLL_INTERVAL)
    pytest.fail(
        f"Addon log never showed all of {required} within "
        f"{_RECOVERY_TIMEOUT}s of the DEBUG restart (last={last!r}). "
        "Either the self-restart did not fire, or the web-UI log level "
        "was not applied to the root logger (the #1721-era "
        "hardcoded-INFO bug)."
    )


@pytest.mark.inaddon_only
async def test_web_ui_debug_log_level_reaches_addon_log(
    mcp_client: Any,
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Web-UI log_level=DEBUG must produce DEBUG output in the addon log."""
    from haos_runtime import HA_MCP_TEST_SECRET_PATH

    addon_url = ha_container_with_fresh_config.get("addon_mcp_url")
    assert addon_url, "inaddon container_info has no addon_mcp_url"
    # Settings routes mount at the secret-path root (see
    # test_readonly_mode.py's identical derivation).
    base = addon_url.split("/mcp", 1)[0]
    settings_advanced = f"{base}{HA_MCP_TEST_SECRET_PATH}/api/settings/advanced"
    settings_restart = f"{base}{HA_MCP_TEST_SECRET_PATH}/api/settings/restart"

    # Resolve the dev addon's Supervisor slug while the shared client is
    # still live (pre-restart).
    data = parse_mcp_result(await mcp_client.call_tool("ha_get_addon", {}))
    addons = data.get("addons") or []
    dev_addon = next((a for a in addons if a.get("name") == DEV_ADDON_NAME), None)
    assert dev_addon is not None, (
        f"Dev addon {DEV_ADDON_NAME!r} not in ha_get_addon listing: "
        f"{[a.get('name') for a in addons]}"
    )
    slug = dev_addon["slug"]

    await _post_log_level(settings_advanced, "DEBUG")
    try:
        await _restart_self(settings_restart)
        logs = await _await_lines_in_addon_log(
            slug, (DEBUG_CANARY, ARMING_LINE, INSTALLED_LINE)
        )
        LOG.info(
            "DEBUG canary + kill-signal diagnostics verified in addon log "
            "(%d chars fetched)",
            len(logs),
        )
    finally:
        # Restore INFO for the rest of the session — retried as a set,
        # like the readonly-mode restore (a leaked DEBUG level would
        # flood every later test's addon log).
        restore_deadline = time.monotonic() + _RECOVERY_TIMEOUT
        while True:
            try:
                await _post_log_level(settings_advanced, "INFO")
                await _restart_self(settings_restart)
                break
            except _TRANSIENT:
                if time.monotonic() >= restore_deadline:
                    raise
                await asyncio.sleep(_POLL_INTERVAL)
        # The self-restarts dropped the SHARED session mcp_client's
        # connection. Warm it back up so later tests on this worker get
        # a live session (read tool is enough; retry while the addon
        # finishes booting).
        warm_deadline = time.monotonic() + _RECOVERY_TIMEOUT
        while True:
            try:
                await mcp_client.call_tool("ha_get_overview", {})
                break
            except _TRANSIENT:
                if time.monotonic() >= warm_deadline:
                    raise
                await asyncio.sleep(_POLL_INTERVAL)
