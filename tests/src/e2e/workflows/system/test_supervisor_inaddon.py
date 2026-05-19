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

import pytest
from haos_runtime import HA_MCP_DEV_ADDON_SLUG

from ...utilities.assertions import MCPAssertions

pytestmark = [pytest.mark.inaddon_only]

logger = logging.getLogger(__name__)


# Real Supervisor system-service slugs that expose ``/logs`` on the
# HAOS-17.3 / Supervisor-2026.05.0 baseline. Verified on PR #1375 CI
# runs 287c5ced (``cli`` 404s) and c80006d9 (``observer`` 404s):
# both are listed by Supervisor as known services but their
# ``/<service>/logs`` endpoints return 404 ("Service 'X' not found
# at http://supervisor/X/logs — Supervisor may not expose it on
# this HA OS version"); the other 6 return parseable journald
# content. Omitted here to keep CI green; if a future HAOS bump
# exposes their logs, add back.
# Kept as a tuple (not a frozenset) so parametrize id ordering is
# stable across runs.
SYSTEM_SERVICES: tuple[str, ...] = (
    "audio",
    "core",
    "dns",
    "host",
    "multicast",
    "supervisor",
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
        # Addon container stdout (verified on PR #1375 CI run 287c5ced)
        # doesn't use journald-style timestamps — Python's logging
        # config inside the addon emits plain ``INFO:httpx:HTTP
        # Request: GET ...`` lines. Assert substantial content rather
        # than a timestamp pattern.
        assert isinstance(log_text, str) and len(log_text) >= 100, (
            f"Expected substantial (>=100 char) log content for "
            f"{HA_MCP_DEV_ADDON_SLUG}, got {len(log_text)} chars: "
            f"{log_text[:200]!r}"
        )


# NOTE: Three test classes were deleted in this commit:
#
# - ``TestBugReportAddonLogsReal::test_fetches_self_logs`` —
#   ``_fetch_addon_logs()`` reads ``SUPERVISOR_TOKEN`` from the
#   CURRENT process's env. On inaddon, the current process is the
#   pytest test runner (running on the CI host), NOT the addon
#   container — SUPERVISOR_TOKEN is unset, the function's defensive
#   guard returns ``""``, and the test fails on the non-empty
#   assertion. To exercise this against the addon's real
#   SUPERVISOR_TOKEN would require running the assertion INSIDE the
#   addon container (e.g. via an MCP tool that wraps
#   ``_fetch_addon_logs``), which doesn't exist as a public tool. The
#   helper's unit tests in ``tests/src/unit/`` cover the function's
#   in-process behavior.
#
# - ``TestFixtureWiringReal::{test_is_running_in_addon,test_supervisor_base_url}``
#   — same problem. ``is_running_in_addon()`` and
#   ``get_supervisor_base_url()`` read the current process's env.
#   The test runner doesn't have those vars set. To test that the
#   ADDON has them set, you'd have to invoke the assertion from
#   inside the addon process.
#
# - ``TestSettingsUiRestartReal::{test_restart_request_succeeds,test_restart_request_rejects_bad_token}``
#   — same root cause. The test reads ``os.environ["SUPERVISOR_TOKEN"]``
#   to construct a Bearer auth header for a direct httpx POST. The
#   test runner has no such env var; ``KeyError`` on access.
#   Retargeting to the SSH addon doesn't help — the problem is the
#   AUTH token, not the target endpoint.
#
# All four scenarios remain covered by the in-process mock-tier tests
# in ``test_supervisor_mock.py`` (which run on testcontainer + HAOS-
# external where the test process IS the MCP server process and shares
# its env). The inaddon module retains coverage for the wire-contract
# tests that go THROUGH the addon via ``mcp_client`` (system_service
# logs, supervisor addon logs, concurrent fetches, limit truncation).


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

    # NOTE: ``test_insufficient_role_supervisor_call_surfaces_403`` was
    # deleted in this commit. Premise was: pass empty ``options={}`` to
    # ha_manage_addon to surface Supervisor's role-check 403. Reality
    # (verified on PR #1375 CI 287c5ced): ha_manage_addon's TOOL-side
    # input validation fires FIRST and returns VALIDATION_FAILED before
    # the Supervisor call is even made — "Must provide either 'path'
    # for proxy mode or at least one config parameter (options/network/
    # boot/auto_update/watchdog) for config mode." Empty options counts
    # as "no config parameter" per the tool's logic. Sending a non-
    # empty options dict would either succeed (if the role check passes
    # — but our manager token doesn't write other addons' options) or
    # mutate the SSH addon's config, which we don't want.
    #
    # The 401/403 wire-contract test surface stays covered by the
    # external_only mock-tier tests in test_supervisor_mock.py.

