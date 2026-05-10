"""E2E tests for the direct-Supervisor httpx call sites against a mock sidecar.

Closes the coverage gap from issue #1129: prior to this, the three call sites
that hit ``http://supervisor`` directly (logs via rest_client, bug-report addon
log fetch, settings_ui addon self-restart) had only mock-based unit tests. Now
they exercise the real socket path against a stdlib ``http.server`` sidecar
served from ``tests/src/e2e/utilities/supervisor_mock.py``.

Out of scope (intentional): the WS-proxy ``supervisor/api`` path used by
``tools_addons.py``. See #1129 for why that's deferred.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from ha_mcp._version import get_supervisor_base_url, is_running_in_addon
from ha_mcp.tools.tools_bug_report import _fetch_addon_logs

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.supervisor_mock import (
    MOCK_INSUFFICIENT_ROLE_TOKEN,
    MOCK_SUPERVISOR_TOKEN,
    SYSTEM_SERVICES,
)

logger = logging.getLogger(__name__)


@pytest.mark.system
class TestGetLogsSupervisor:
    """ha_get_logs source='supervisor' — addon container logs."""

    async def test_addon_logs_returns_mocked_text(self, mcp_client, supervisor_mock):
        """source='supervisor' with a slug hits /addons/<slug>/logs and parses it."""
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success(
                "ha_get_logs",
                {"source": "supervisor", "slug": "core_mosquitto", "limit": 100},
            )

        assert result["source"] == "supervisor"
        assert result["slug"] == "core_mosquitto"
        assert "core_mosquitto" in result["log"]
        assert result["total_lines"] == 2
        assert result["returned_lines"] == 2

    async def test_addon_logs_search_filter(self, mcp_client, supervisor_mock):
        """search filter narrows lines."""
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success(
                "ha_get_logs",
                {
                    "source": "supervisor",
                    "slug": "core_mosquitto",
                    "search": "line 1",
                },
            )

        assert result["returned_lines"] == 1
        assert "line 1" in result["log"]
        assert result["filters_applied"] == {"search": "line 1"}


@pytest.mark.system
class TestGetLogsSystemService:
    """ha_get_logs source='system_service' — Supervisor-managed services."""

    @pytest.mark.parametrize("service", sorted(SYSTEM_SERVICES))
    async def test_each_system_service(self, mcp_client, supervisor_mock, service: str):
        """All seven Supervisor-managed services are reachable."""
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success(
                "ha_get_logs",
                {"source": "system_service", "slug": service},
            )

        assert result["source"] == "system_service"
        assert result["slug"] == service
        assert f"[{service}]" in result["log"]
        assert result["total_lines"] == 3

    async def test_unknown_service_rejected_by_caller_validation(
        self, mcp_client, supervisor_mock
    ):
        """The tool validates the slug against SYSTEM_SERVICE_SLUGS before dispatch.

        Provides regression coverage that an unknown service never reaches the
        mock — caller-side validation short-circuits.
        """
        result = await safe_call_tool(
            mcp_client,
            "ha_get_logs",
            {"source": "system_service", "slug": "not_a_real_service"},
        )
        assert result.get("success") is False


@pytest.mark.system
class TestBugReportAddonLogs:
    """tools_bug_report._fetch_addon_logs — direct httpx to /addons/self/logs."""

    async def test_fetches_self_logs(self, supervisor_mock):
        text = await _fetch_addon_logs()
        assert "[addon:self]" in text
        assert "mock log line 1" in text

    async def test_returns_empty_when_token_missing(self, supervisor_mock, monkeypatch):
        """No SUPERVISOR_TOKEN → defensive guard returns empty string, no request."""
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        text = await _fetch_addon_logs()
        assert text == ""


@pytest.mark.system
class TestSettingsUiRestart:
    """settings_ui._restart_addon — direct httpx POST to /addons/self/restart.

    Tested via a raw httpx call rather than constructing a starlette Request,
    because the request shape is irrelevant to the Supervisor wire contract;
    the wire contract is what this PR adds coverage for. The handler's branch
    logic (success / token-missing / connection drop) is already covered by
    unit tests in tests/src/unit/test_settings_ui.py.
    """

    async def test_restart_request_succeeds(self, supervisor_mock):
        url = f"{supervisor_mock}/addons/self/restart"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {MOCK_SUPERVISOR_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"result": "ok", "data": {}}

    async def test_restart_request_rejects_bad_token(self, supervisor_mock):
        url = f"{supervisor_mock}/addons/self/restart"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url, headers={"Authorization": "Bearer wrong-token"}
            )
        assert resp.status_code == 401


@pytest.mark.system
class TestFixtureWiring:
    """Sanity checks that the fixture flips the production gates correctly.

    These are fast, deterministic, and prove that subsequent tests aren't
    silently exercising the wrong branch (e.g. falling back to the HA Core
    proxy when they should be hitting the mock).
    """

    async def test_is_running_in_addon_returns_true(self, supervisor_mock):
        """SUPERVISOR_TOKEN set → addon-mode branch is taken."""
        assert is_running_in_addon() is True

    async def test_base_url_resolves_to_mock(self, supervisor_mock):
        """The new helper picks up the override env var."""
        assert get_supervisor_base_url() == supervisor_mock
        assert get_supervisor_base_url().startswith("http://127.0.0.1:")

    async def test_default_when_override_unset(self, supervisor_mock, monkeypatch):
        """Removing the override falls back to the production hostname."""
        monkeypatch.delenv("SUPERVISOR_BASE_URL", raising=False)
        assert get_supervisor_base_url() == "http://supervisor"


@pytest.mark.system
class TestMockResilience:
    """Stresses the mock to catch obvious wiring bugs (event-loop conflicts,
    socket reuse issues, header mishandling) that trivial happy-path tests
    would miss.
    """

    async def test_concurrent_log_fetches(self, mcp_client, supervisor_mock):
        """Five parallel ha_get_logs calls all succeed.

        The mock and the MCP server share the test event loop; if either
        serialises requests incorrectly this surfaces as a hang or an error.
        """
        async with MCPAssertions(mcp_client) as mcp:
            results = await asyncio.gather(
                *(
                    mcp.call_tool_success(
                        "ha_get_logs",
                        {"source": "system_service", "slug": svc},
                    )
                    for svc in sorted(SYSTEM_SERVICES)
                )
            )
        assert {r["slug"] for r in results} == SYSTEM_SERVICES
        assert all(r["total_lines"] == 3 for r in results)

    async def test_addon_logs_limit_truncation(self, mcp_client, supervisor_mock):
        """limit=1 returns the last line only — proves the tool's tail-N
        truncation works against real wire bytes, not just mock dicts.
        """
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success(
                "ha_get_logs",
                {"source": "supervisor", "slug": "core_zigbee", "limit": 1},
            )
        assert result["returned_lines"] == 1
        assert result["total_lines"] == 2
        # The mock returns "line 1\nline 2\n" — tail-1 should give us line 2.
        assert "line 2" in result["log"]
        assert "line 1" not in result["log"]

    async def test_unauthorized_supervisor_call_surfaces_as_tool_error(
        self, mcp_client, supervisor_mock, monkeypatch
    ):
        """Wrong token → 401 → AUTH_INVALID_TOKEN with SUPERVISOR_TOKEN hint.

        Exercises the auth-failure path through the full ha_get_logs →
        _supervisor_logs_get → mock chain. The explicit
        ``except HomeAssistantAuthError`` clause in
        ``_get_system_service_log`` (added alongside this PR) routes the
        401 through ``exception_to_structured_error`` so callers get a
        structured ``code`` + remediation suggestions instead of the raw
        FastMCP wrap they used to get.
        """
        monkeypatch.setenv("SUPERVISOR_TOKEN", "wrong-token-on-purpose")
        result = await safe_call_tool(
            mcp_client,
            "ha_get_logs",
            {"source": "system_service", "slug": "core"},
        )
        assert result.get("success") is False
        error = result.get("error", {})
        assert error.get("code") == "AUTH_INVALID_TOKEN", (
            f"Expected AUTH_INVALID_TOKEN, got code={error.get('code')!r}, "
            f"full error={error!r}"
        )
        suggestions = error.get("suggestions", [])
        assert any("SUPERVISOR_TOKEN" in s for s in suggestions), (
            f"Expected SUPERVISOR_TOKEN remediation suggestion, "
            f"got suggestions={suggestions!r}"
        )

    async def test_insufficient_role_supervisor_call_surfaces_403(
        self, mcp_client, supervisor_mock, monkeypatch
    ):
        """Valid token but addon hassio_role too low → 403 → structured tool error.

        Covers the role-mismatch branch in ``_get_system_service_log`` added
        alongside the #1116 fix (the addon's ``hassio_role`` bump from
        ``default`` → ``manager`` was the matching production change). Without
        this E2E the 403-handling path has no real-socket coverage.

        Asserts both:
          - the error code is AUTH_INVALID_TOKEN (today _classify_api_status
            maps both 401 and 403 to this — distinguishing them would need a
            new ErrorCode), and
          - the role-specific suggestion (``hassio_role must be 'manager'``)
            from the 403 branch reaches the caller.
        The second assertion is what proves the 403 branch fired specifically,
        not the 401 branch (which has a different suggestion set).
        """
        monkeypatch.setenv("SUPERVISOR_TOKEN", MOCK_INSUFFICIENT_ROLE_TOKEN)
        result = await safe_call_tool(
            mcp_client,
            "ha_get_logs",
            {"source": "system_service", "slug": "core"},
        )
        assert result.get("success") is False
        error = result.get("error", {})
        assert error.get("code") == "AUTH_INVALID_TOKEN", (
            f"Expected AUTH_INVALID_TOKEN, got code={error.get('code')!r}, "
            f"full error={error!r}"
        )
        suggestions = error.get("suggestions", [])
        assert any("hassio_role" in s for s in suggestions), (
            f"Expected a hassio_role suggestion (proves 403 branch fired), "
            f"got suggestions={suggestions!r}"
        )
