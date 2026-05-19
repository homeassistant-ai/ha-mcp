"""Real-Supervisor parallel of ``test_supervisor_mock.py`` for the inaddon tier.

This module mirrors the test classes from ``test_supervisor_mock.py`` but
runs against the real Supervisor process inside the HAOS-inaddon CI tier
(see #1349 item 6). The mock module stays put as the ``external_only``
coverage path (testcontainer + external-HAOS); this module covers the
same Supervisor wire contract against the real Supervisor that ships
inside HAOS, with assertions adapted for live responses
(shape-not-content).

**Two tests are intentionally NOT migrated** from ``test_supervisor_mock.py``:

* ``TestBugReportAddonLogs::test_returns_empty_when_token_missing`` — verifies
  the in-process ``_fetch_addon_logs()`` defensive guard returns an empty
  string when ``SUPERVISOR_TOKEN`` is unset. In inaddon mode the test
  process is the harness, but the function reads ``SUPERVISOR_TOKEN``
  inside the addon container's own process (where it IS set). We cannot
  ``monkeypatch.delenv`` the addon-process env without restarting the
  addon. Coverage stays in the mock-tier (external_only) suite.
* ``TestMockResilience::test_unauthorized_supervisor_call_surfaces_as_tool_error`` —
  same family of problem. Forcing a bad ``SUPERVISOR_TOKEN`` requires
  mutating the addon container's env, which would either need test-only
  env overrides in production code (rejected) or an addon restart cycle
  per assertion. The 401 wire-contract coverage stays in the mock-tier
  suite; this module covers the 403 path against a real
  insufficient-role token (the SSH addon's ``hassio_role: default``).

See ``docs/superpowers/specs/2026-05-18-1349-closeout-design.md`` — the
"Token-missing + bad-token paths" section under spec-review fixes for
the design rationale.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time

import httpx
import pytest
from haos_runtime import (
    HA_MCP_DEV_ADDON_SLUG,
    SSH_ADDON_SLUG,
    SSH_DEBUG_HOST_PORT,
)

from ha_mcp._version import get_supervisor_base_url, is_running_in_addon
from ha_mcp.tools.tools_bug_report import _fetch_addon_logs

from ...utilities.assertions import MCPAssertions, safe_call_tool

pytestmark = [pytest.mark.inaddon_only]

logger = logging.getLogger(__name__)

# Real Supervisor system-service slugs. Kept as a tuple (not a frozenset)
# so the parametrize id ordering is stable across runs.
SYSTEM_SERVICES: tuple[str, ...] = (
    "audio",
    "cli",
    "core",
    "dns",
    "host",
    "multicast",
    "observer",
    "supervisor",
)

# Journald-style timestamp pattern emitted by Supervisor's log endpoints
# (e.g. ``2026-05-18T12:34:56.789012+00:00`` or shorter ISO variants).
# Used as a sentinel that we got real log content, not an empty stub.
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _wait_for_tcp_port(
    port: int, host: str = "127.0.0.1", timeout: float = 90.0
) -> None:
    """Poll ``host:port`` until a TCP connect succeeds or ``timeout`` elapses.

    Used by the SSH-addon restart test to wait for the addon's listener to
    come back up post-restart. The SSH addon binds inside HAOS on port
    22222; ``SSH_DEBUG_HOST_PORT`` is the host-side hostfwd that points at
    it (see ``haos_runtime.boot_haos_qemu``).
    """
    deadline = time.monotonic() + timeout
    last_err: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError as e:
            last_err = e
            time.sleep(1.0)
    raise TimeoutError(
        f"{host}:{port} did not become reachable within {timeout}s "
        f"(last error: {last_err!r})"
    )


def _wait_for_tcp_port_closed(
    port: int, host: str = "127.0.0.1", timeout: float = 30.0
) -> None:
    """Poll until ``host:port`` REFUSES connections (or ``timeout`` elapses).

    Pre-restart probe for the SSH-addon restart test — without this,
    ``_wait_for_tcp_port`` returns instantly because the SSH addon's
    listener is still up (Supervisor hasn't actually stopped the
    container yet). Waiting for the port to close first proves the
    restart kicked in; then the post-close ``_wait_for_tcp_port`` call
    proves the new listener actually started.

    Note: slirp NAT may keep the host-side hostfwd bound briefly after
    the guest listener drops, so this can falsely report "open" for
    a beat. A 30s window is enough for the addon container to fully
    stop on any realistic CI runner.
    """
    deadline = time.monotonic() + timeout
    last_open_at: float | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                last_open_at = time.monotonic()
                time.sleep(0.5)
        except OSError:
            return
    raise TimeoutError(
        f"{host}:{port} never closed within {timeout}s; "
        f"last open at t+{(last_open_at or 0) - (deadline - timeout):.1f}s. "
        f"The Supervisor restart request may have been silently ignored."
    )


@pytest.mark.system
class TestGetLogsSystemServiceReal:
    """ha_get_logs source='system_service' against real Supervisor."""

    @pytest.mark.parametrize("service", SYSTEM_SERVICES)
    async def test_each_system_service(self, mcp_client, service: str) -> None:
        """Each real Supervisor-managed service returns parseable journald output."""
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success(
                "ha_get_logs",
                {"source": "system_service", "slug": service},
            )

        assert result.get("success") is True, (
            f"Expected success=True for service={service!r}, got result={result!r}"
        )
        assert result["source"] == "system_service"
        assert result["slug"] == service
        log_text = result.get("log", "")
        assert isinstance(log_text, str) and log_text, (
            f"Expected non-empty log string for service={service!r}, "
            f"got log={log_text!r}"
        )
        assert result.get("total_lines", 0) >= 1, (
            f"Expected total_lines >= 1 for service={service!r}, "
            f"got total_lines={result.get('total_lines')!r}"
        )


@pytest.mark.system
class TestGetLogsSupervisorReal:
    """ha_get_logs source='supervisor' — real addon container logs."""

    async def test_dev_addon_logs(self, mcp_client) -> None:
        """source='supervisor' hits the real /addons/<slug>/logs and parses it.

        Targets the dev addon (``HA_MCP_DEV_ADDON_SLUG``) — guaranteed to
        be installed and running by the bake (otherwise the mcp_client
        fixture would never have come up).
        """
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success(
                "ha_get_logs",
                {"source": "supervisor", "slug": HA_MCP_DEV_ADDON_SLUG},
            )

        assert result.get("success") is True
        assert result["source"] == "supervisor"
        assert result["slug"] == HA_MCP_DEV_ADDON_SLUG
        log_text = result.get("log", "")
        assert isinstance(log_text, str) and log_text, (
            f"Expected non-empty log string for {HA_MCP_DEV_ADDON_SLUG}, "
            f"got log={log_text!r}"
        )
        assert _TIMESTAMP_RE.search(log_text), (
            f"Expected a journald-style ISO timestamp in addon log output; "
            f"got log (first 500 chars)={log_text[:500]!r}"
        )


@pytest.mark.system
class TestBugReportAddonLogsReal:
    """tools_bug_report._fetch_addon_logs — direct httpx to /addons/self/logs.

    See module docstring for why ``test_returns_empty_when_token_missing``
    is NOT migrated.
    """

    async def test_fetches_self_logs(self) -> None:
        """Real ``/addons/self/logs`` returns non-empty journald-style text.

        Called inside the addon container (where the test process == the
        MCP server process inaddon-side); the function reads its own
        ``SUPERVISOR_TOKEN`` env and hits the real Supervisor socket.
        """
        text = await _fetch_addon_logs()
        assert isinstance(text, str) and text, (
            f"Expected non-empty log text from _fetch_addon_logs, got {text!r}"
        )
        assert _TIMESTAMP_RE.search(text), (
            f"Expected a journald-style ISO timestamp in self-log output; "
            f"got text (first 500 chars)={text[:500]!r}"
        )


@pytest.mark.system
class TestFixtureWiringReal:
    """Sanity checks that the addon process actually has Supervisor env wired up."""

    async def test_is_running_in_addon(self) -> None:
        """The addon process has SUPERVISOR_TOKEN set → addon-mode branch active."""
        assert is_running_in_addon() is True

    async def test_supervisor_base_url(self) -> None:
        """No SUPERVISOR_BASE_URL override inside the addon → production hostname."""
        assert get_supervisor_base_url() == "http://supervisor"


@pytest.mark.system
class TestSettingsUiRestartReal:
    """settings_ui restart wire-contract against real Supervisor.

    Targets the SSH addon (``SSH_ADDON_SLUG``) rather than ``self`` —
    restarting ``self`` (the dev addon serving the MCP session) would
    kill the test process. The SSH addon is bake-installed for exactly
    this purpose; restarting it is harmless because the test harness
    only uses SSH for the docker-exec helper, which the restart-cycle
    tests don't exercise.
    """

    async def test_restart_request_succeeds(self) -> None:
        """POST /addons/<ssh>/restart succeeds and the SSH listener comes back."""
        url = f"http://supervisor/addons/{SSH_ADDON_SLUG}/restart"
        # Reuse the addon's own SUPERVISOR_TOKEN — same auth the production
        # code path uses for /addons/self/restart.
        token = os.environ["SUPERVISOR_TOKEN"]
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 200, (
            f"Expected 200 from Supervisor /addons/{SSH_ADDON_SLUG}/restart, "
            f"got {resp.status_code}; body={resp.text!r}"
        )

        # Two-phase wait. ``_wait_for_tcp_port`` alone would return
        # instantly because the SSH addon's listener was up before the
        # restart POST and Supervisor returns 200 once the request is
        # accepted — not once the container actually went down. Without
        # the close-then-open sequence, a Supervisor-side bug that
        # silently ignored the restart would still pass this test.
        _wait_for_tcp_port_closed(SSH_DEBUG_HOST_PORT, timeout=30.0)
        # 90s covers cache-cold Docker restarts on slow CI runners.
        _wait_for_tcp_port(SSH_DEBUG_HOST_PORT, timeout=90.0)

    async def test_restart_request_rejects_bad_token(self) -> None:
        """A bogus Bearer token gets a real 401 from Supervisor."""
        url = f"http://supervisor/addons/{SSH_ADDON_SLUG}/restart"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url, headers={"Authorization": "Bearer wrong-token-on-purpose"}
            )
        assert resp.status_code == 401, (
            f"Expected 401 from Supervisor with bad token, got {resp.status_code}; "
            f"body={resp.text!r}"
        )


@pytest.mark.system
class TestMockResilienceReal:
    """Stresses the real Supervisor socket for concurrent + edge-case behavior.

    See module docstring for why
    ``test_unauthorized_supervisor_call_surfaces_as_tool_error`` is NOT
    migrated; the 403-path test below is the inaddon parallel for the
    insufficient-role wire-contract.
    """

    async def test_concurrent_log_fetches(self, mcp_client) -> None:
        """Five+ parallel ha_get_logs calls against real Supervisor all succeed.

        Catches event-loop / socket-reuse bugs that serial-call tests
        would miss. Uses the full SYSTEM_SERVICES tuple (8 services) so
        the parallelism is meaningfully above the ``5+`` floor.
        """
        async with MCPAssertions(mcp_client) as mcp:
            results = await asyncio.gather(
                *(
                    mcp.call_tool_success(
                        "ha_get_logs",
                        {"source": "system_service", "slug": svc},
                    )
                    for svc in SYSTEM_SERVICES
                )
            )
        assert {r["slug"] for r in results} == set(SYSTEM_SERVICES)
        assert all(r.get("success") is True for r in results), (
            f"Expected success=True on every concurrent fetch, got: "
            f"{[(r.get('slug'), r.get('success')) for r in results]!r}"
        )
        # Catch a single-shared-buffer regression: every per-service log
        # should be distinct content. Compare first 200 chars (the
        # journald-prefix-plus-some-detail surface) — multiple identical
        # prefixes would indicate Supervisor or the tool is returning a
        # shared response object across calls.
        log_prefixes = {r.get("log", "")[:200] for r in results}
        assert len(log_prefixes) > 1, (
            f"All {len(results)} concurrent log fetches returned identical "
            f"content (first-200-char fingerprint). This looks like a "
            f"shared-buffer regression in the supervisor proxy or the tool. "
            f"Fingerprint: {next(iter(log_prefixes))[:100]!r}"
        )

    async def test_addon_logs_limit_truncation(self, mcp_client) -> None:
        """limit=1 returns at most one line — real-socket tail-N coverage."""
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success(
                "ha_get_logs",
                {
                    "source": "supervisor",
                    "slug": HA_MCP_DEV_ADDON_SLUG,
                    "limit": 1,
                },
            )

        assert result.get("success") is True
        returned = result.get("returned_lines")
        assert isinstance(returned, int) and returned <= 1, (
            f"Expected returned_lines <= 1 with limit=1, got returned_lines="
            f"{returned!r}; full result={result!r}"
        )

    async def test_insufficient_role_supervisor_call_surfaces_403(
        self, mcp_client
    ) -> None:
        """Calling /addons/<ssh>/options requires admin role → real 403 path.

        The dev addon's token has ``hassio_role: manager``, which is
        sufficient for log fetches but NOT for cross-addon options
        writes. Supervisor returns 403 on
        ``POST /addons/<other-addon>/options`` for any caller below
        admin. This exercises the 403 branch in ``ha_manage_addon``'s
        Supervisor wrapper, which surfaces a structured
        AUTH_INVALID_TOKEN error to the caller (today's classifier
        maps both 401 and 403 to that code).
        """
        # Target the SSH addon's options endpoint with an empty options dict —
        # the role check fires before payload validation, so an empty dict is
        # enough to surface the 403 path.
        result = await safe_call_tool(
            mcp_client,
            "ha_manage_addon",
            {"slug": SSH_ADDON_SLUG, "options": {}},
        )
        assert result.get("success") is False, (
            f"Expected ha_manage_addon to fail for cross-addon options write "
            f"as non-admin caller; got success result={result!r}"
        )
        error = result.get("error", {})
        if isinstance(error, dict):
            code = error.get("code")
        else:
            code = None
        assert code == "AUTH_INVALID_TOKEN", (
            f"Expected AUTH_INVALID_TOKEN error code from 403 path, got "
            f"code={code!r}; full error={error!r}"
        )

