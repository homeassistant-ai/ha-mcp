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
   ``ha_get_logs(source="supervisor")`` — contains DEBUG-level records.
   Their presence is positive proof BOTH that the restart took effect
   AND that the override reached the root logger. start.py used to
   hardcode ``basicConfig(level=INFO)``, which made the web-UI setting
   a silent no-op and left #1721's reporter unable to produce debug
   logs.

Why DEBUG records rather than start.py's one-shot boot lines (the
canary / kill-signal arming): Supervisor's ``/addons/<slug>/logs``
serves only a bounded recent window, and at DEBUG verbosity boot lines
scroll out of it within seconds — a first CI run failed exactly that
way. DEBUG records regenerate with every request (including this
poll's own), so the signal is scroll-proof. The boot lines themselves
are asserted against complete docker logs by the testcontainer tests
in tests/addon/test_addon_startup.py, including the negative
(INFO-must-not-arm) case.

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


def _has_debug_record(logs: str) -> bool:
    """True when any line is a DEBUG-level logging record.

    Addon logging uses Python's BASIC_FORMAT (``LEVEL:name:message``),
    so a root logger at DEBUG produces ``DEBUG:``-prefixed lines
    continuously (every MCP request generates several), and a root
    logger at INFO produces none. startswith avoids false-positives on
    INFO messages that merely mention the word.
    """
    return any(ln.startswith("DEBUG:") for ln in logs.splitlines())


async def _fetch_addon_log(slug: str) -> str:
    """Fetch the addon's own recent container log via a fresh client."""
    from haos_runtime import wait_for_addon_mcp_ready

    url = wait_for_addon_mcp_ready(timeout=30.0)
    data = await _call_tool_fresh(
        url,
        "ha_get_logs",
        {"source": "supervisor", "slug": slug, "limit": 2000},
    )
    return data.get("log", "")


async def _await_debug_records_in_addon_log(slug: str) -> str:
    """Poll the addon's own container log until DEBUG records appear.

    Reconnects each round (``wait_for_addon_mcp_ready`` + fresh client),
    so it rides out the restart window. DEBUG-record presence is the
    scroll-proof positive signal that the restart happened AND the
    web-UI level reached the root logger: Supervisor's ``/logs``
    endpoint only serves a bounded recent window, so one-shot boot
    lines (the start.py canary/arming lines) can scroll out under
    DEBUG verbosity — but DEBUG records are re-emitted by every
    request, including this poll's own, so the signal regenerates.
    (Those boot lines are asserted against complete docker logs by
    tests/addon/test_addon_startup.py's testcontainer tests.)
    """
    deadline = time.monotonic() + _RECOVERY_TIMEOUT
    last: object = None
    while time.monotonic() < deadline:
        try:
            logs = await _fetch_addon_log(slug)
            if _has_debug_record(logs):
                return logs
            last = f"no DEBUG records in {len(logs.splitlines())}-line window"
        except _TRANSIENT as err:
            last = err
        await asyncio.sleep(_POLL_INTERVAL)
    raise AssertionError(
        f"Addon log never showed DEBUG-level records within "
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

    # Baseline sanity: at the session's default INFO level the log
    # window must contain zero DEBUG records — otherwise the positive
    # signal below would be meaningless.
    baseline = await _fetch_addon_log(slug)
    assert not _has_debug_record(baseline), (
        "Addon log already contains DEBUG records at the default INFO "
        "level — the DEBUG-record signal cannot discriminate. First "
        "DEBUG line: "
        + next(ln for ln in baseline.splitlines() if ln.startswith("DEBUG:"))
    )

    await _post_log_level(settings_advanced, "DEBUG")
    try:
        await _restart_self(settings_restart)
        logs = await _await_debug_records_in_addon_log(slug)
        LOG.info(
            "DEBUG records verified in addon log after web-UI DEBUG + "
            "restart (%d chars fetched)",
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
