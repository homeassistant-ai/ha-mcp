"""E2E for the web-UI log level → addon log path (#1721 diagnosis fix).

Runs the FULL user journey against the real dev addon inside HAOS
(inaddon tier only — this is the deployment where the bug lived):

1. POST ``{"log_level": "DEBUG"}`` to the settings advanced API — the
   same endpoint the web Settings UI uses — which persists to
   ``/data/feature_flags.json`` and reports ``restart_required``.
2. Restart the addon via ``hassio.addon_restart``. This is a
   SELF-restart (the tool call's own server dies mid-request), so the
   severed response is tolerated by design.
3. Once the addon is back, assert the addon's own container log (via
   ``ha_get_logs(source="supervisor")``) contains the DEBUG canary and
   the kill-signal diagnostics arming lines.

The canary line is emitted through the logging system at DEBUG level,
so its presence proves the override was applied to the root logger —
start.py used to hardcode ``basicConfig(level=INFO)``, which made the
web-UI setting a silent no-op and left #1721's reporter unable to
produce debug logs. Kill-signal diagnostics arm on the same condition
(they replaced the removed ``advanced_debug_logging`` addon toggle).

Every MCP call opens a FRESH streamable-HTTP client (stateless server)
so the test never depends on a pre-restart connection surviving its own
server's restart. The test restores ``log_level`` to INFO (second
restart) so the rest of the session runs at normal verbosity.
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
# errors from the freshly-opened streamable-HTTP connection dying or
# being refused mid-restart. Bugs (TypeError, KeyError, AssertionError)
# must propagate per .gemini/styleguide.md "Exception Handling in Test
# Polling Loops".
_EXPECTED_RESTART_ERRORS = (*_POLLING_TRANSIENT_ERRORS, httpx.HTTPError)

# start.py log lines asserted below. Keep in lockstep with
# homeassistant-addon/start.py (the canary + arming strings).
DEBUG_CANARY = "Debug logging active (log_level applied from settings)"
ARMING_LINE = "arming kill-signal diagnostics"
INSTALLED_LINE = "kill-signal diagnostics installed for"

# Addon restart = container stop + start; CI runners take 5-25s.
_RESTART_TIMEOUT = 120.0
_POLL_INTERVAL = 1.0


async def _call_tool(addon_url: str, tool: str, args: dict[str, Any]) -> Any:
    """Call an MCP tool over a fresh streamable-HTTP connection.

    The server is stateless, so a new client per call is cheap and —
    unlike a long-lived session — immune to the addon restarting
    between calls.
    """
    client = Client(StreamableHttpTransport(url=addon_url))
    async with client:
        raw = await client.call_tool(tool, args)
    return parse_mcp_result(raw)


async def _resolve_dev_addon_slug(addon_url: str) -> str:
    """Resolve the dev addon's Supervisor slug by display name."""
    data = await _call_tool(addon_url, "ha_get_addon", {})
    addons = data.get("addons") or []
    dev_addon = next((a for a in addons if a.get("name") == DEV_ADDON_NAME), None)
    assert dev_addon is not None, (
        f"Dev addon {DEV_ADDON_NAME!r} not in ha_get_addon listing: "
        f"{[a.get('name') for a in addons]}"
    )
    return dev_addon["slug"]


async def _post_log_level(settings_url: str, level: str) -> None:
    """Write ``log_level`` through the settings advanced API."""
    async with httpx.AsyncClient() as http:
        resp = await http.post(settings_url, json={"log_level": level}, timeout=15)
    assert resp.status_code == 200, (
        f"POST {{'log_level': {level!r}}} to {settings_url} returned "
        f"{resp.status_code}: {resp.text[:500]}"
    )
    body = resp.json()
    assert body.get("restart_required") is True, (
        f"settings API response missing restart_required=True: {body}"
    )


async def _restart_self_and_wait(slug: str, addon_url: str) -> None:
    """Restart the addon under test and wait until it serves MCP again.

    The restart call's HTTP response dies with the addon container, so
    transport errors from it are expected and swallowed. To guard
    against a silently-failed restart (e.g. a tool-level validation
    error also lands in the except — that exact false-negative shipped
    in this test's first version, when wrong ha_call_service args drew
    a VALIDATION_FAILED ToolError that was logged as "expected"), the
    helper then REQUIRES observing the addon go down before polling for
    recovery.
    """
    try:
        await _call_tool(
            addon_url,
            "ha_call_service",
            {
                "domain": "hassio",
                "service": "addon_restart",
                "data": {"addon": slug},
            },
        )
    except _EXPECTED_RESTART_ERRORS as e:
        LOG.info("Self-restart severed the in-flight call (expected): %r", e)

    # Proof the restart actually fired: the HTTP endpoint must go DOWN
    # within the window. If it never does, the restart silently failed
    # and polling for recovery would false-pass against the old process.
    went_down = False
    deadline = time.monotonic() + _RESTART_TIMEOUT
    async with httpx.AsyncClient() as http:
        while time.monotonic() < deadline:
            try:
                await http.get(addon_url, timeout=2)
                await asyncio.sleep(0.2)
            except httpx.HTTPError:
                went_down = True
                break
    if not went_down:
        pytest.fail(
            f"Addon never went down within {_RESTART_TIMEOUT}s of the "
            "hassio.addon_restart call — the self-restart did not fire."
        )

    deadline = time.monotonic() + _RESTART_TIMEOUT
    async with httpx.AsyncClient() as http:
        while time.monotonic() < deadline:
            try:
                await http.get(addon_url, timeout=3)
                break  # any HTTP response means uvicorn is listening again
            except httpx.HTTPError:
                await asyncio.sleep(_POLL_INTERVAL)
        else:
            pytest.fail(
                f"Addon did not come back within {_RESTART_TIMEOUT}s of restart"
            )

    # Uvicorn listening != tools registered; poll a real MCP call too.
    deadline = time.monotonic() + _RESTART_TIMEOUT
    while time.monotonic() < deadline:
        try:
            await _call_tool(addon_url, "ha_get_addon", {})
            return
        except _EXPECTED_RESTART_ERRORS as e:
            LOG.debug("MCP not ready yet after restart: %r", e)
            await asyncio.sleep(_POLL_INTERVAL)
    pytest.fail(f"MCP calls did not recover within {_RESTART_TIMEOUT}s of restart")


@pytest.mark.inaddon_only
async def test_web_ui_debug_log_level_reaches_addon_log(
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Web-UI log_level=DEBUG must produce DEBUG output in the addon log."""
    addon_url = ha_container_with_fresh_config.get("addon_mcp_url")
    assert addon_url, "inaddon container_info has no addon_mcp_url"
    settings_url = f"{addon_url.rstrip('/')}/api/settings/advanced"

    slug = await _resolve_dev_addon_slug(addon_url)

    await _post_log_level(settings_url, "DEBUG")
    try:
        await _restart_self_and_wait(slug, addon_url)

        data = await _call_tool(
            addon_url,
            "ha_get_logs",
            {"source": "supervisor", "slug": slug, "limit": 400},
        )
        logs = data.get("log", "")

        assert DEBUG_CANARY in logs, (
            "DEBUG canary missing from addon log after setting "
            "log_level=DEBUG + restart — the web-UI log level was NOT "
            "applied to the root logger (the #1721-era hardcoded-INFO "
            "bug is back). Log tail:\n" + logs[-2000:]
        )
        assert ARMING_LINE in logs, (
            "Kill-signal diagnostics arming line missing — DEBUG level "
            "no longer arms diagnostics. Log tail:\n" + logs[-2000:]
        )
        assert INSTALLED_LINE in logs, (
            "Kill-signal diagnostics install-confirmation missing — "
            "scheduled install did not complete. Log tail:\n" + logs[-2000:]
        )
        LOG.info("DEBUG canary + kill-signal diagnostics verified in addon log")
    finally:
        # Restore normal verbosity for the rest of the session.
        await _post_log_level(settings_url, "INFO")
        await _restart_self_and_wait(slug, addon_url)
