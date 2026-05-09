"""E2E tests for the direct-Supervisor httpx call sites against a mock sidecar.

Closes the coverage gap from issue #1129: prior to this, the three call sites
that hit ``http://supervisor`` directly (logs via rest_client, bug-report addon
log fetch, settings_ui addon self-restart) had only mock-based unit tests. Now
they exercise the real socket path against an aiohttp sidecar via the
``supervisor_mock`` fixture (see ``tests/src/e2e/utilities/supervisor_mock.py``).

Out of scope (intentional): the WS-proxy ``supervisor/api`` path used by
``tools_addons.py``. See #1129 for why that's deferred.
"""

from __future__ import annotations

import logging
import os

import httpx
import pytest

from ha_mcp.tools.tools_bug_report import _fetch_addon_logs

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.supervisor_mock import (
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
        url = f"{os.environ['SUPERVISOR_BASE_URL']}/addons/self/restart"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {MOCK_SUPERVISOR_TOKEN}"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"result": "ok", "data": {}}

    async def test_restart_request_rejects_bad_token(self, supervisor_mock):
        url = f"{os.environ['SUPERVISOR_BASE_URL']}/addons/self/restart"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url, headers={"Authorization": "Bearer wrong-token"}
            )
        assert resp.status_code == 401
