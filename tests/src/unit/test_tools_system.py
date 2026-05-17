"""Unit tests for tools_system module.

Regression tests for https://github.com/homeassistant-ai/ha-mcp/issues/612
ha_restart reports failure when a reverse proxy returns 504 during restart.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.tools_system import SystemTools


@contextmanager
def _patch_health_info_baseline():
    """Patch ``_fetch_health_info`` to return a fixed (ws_client, baseline) pair.

    The ws_client is a MagicMock so the section helpers (when not separately
    mocked) won't accidentally hit a real connection; ``disconnect`` is async.
    """
    ws_client = MagicMock()
    ws_client.disconnect = AsyncMock()
    baseline = {"success": True, "health_info": {}}
    with patch.object(
        SystemTools,
        "_fetch_health_info",
        new=AsyncMock(return_value=(ws_client, baseline)),
    ) as p:
        yield p, ws_client


def _make_client_that_fails_on_restart(exception):
    """Create a mock client where check_config succeeds but call_service raises."""
    mock_client = AsyncMock()
    mock_client.check_config.return_value = {"result": "valid"}
    mock_client.call_service.side_effect = exception
    return mock_client


class TestHaRestartErrorHandling:
    """Tests for ha_restart handling of expected errors during restart."""

    @pytest.mark.asyncio
    async def test_504_gateway_timeout_treated_as_success(self):
        """A 504 from a reverse proxy after restart initiated should be success.

        Reproduces issue #612: user behind a reverse proxy gets 504 when HA
        shuts down, but HA actually restarted successfully.
        """
        error = HomeAssistantAPIError("API error: 504 - ", status_code=504)
        client = _make_client_that_fails_on_restart(error)
        tools = SystemTools(client)

        result = await tools.ha_restart(confirm=True)

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_unrelated_error_still_fails(self):
        """Errors unrelated to restart should still report failure via ToolError."""
        error = Exception("Something completely unrelated went wrong")
        client = _make_client_that_fails_on_restart(error)
        tools = SystemTools(client)

        with pytest.raises(ToolError):
            await tools.ha_restart(confirm=True)


def _ws_client_with_issues(issues):
    """Mock ws_client whose send_command('repairs/list_issues') returns ``issues``."""
    ws = AsyncMock()
    ws.send_command.return_value = {
        "success": True,
        "result": {"issues": issues},
    }
    return ws


class TestFetchRepairs:
    """Regression coverage for #1307: dismissed repairs must be filtered by default."""

    @pytest.mark.asyncio
    async def test_default_filters_ignored_repairs(self):
        ws = _ws_client_with_issues(
            [
                {"issue_id": "active", "ignored": False},
                {"issue_id": "dismissed", "ignored": True, "dismissed_version": "2026.4.0"},
            ]
        )

        result = await SystemTools._fetch_repairs(ws)

        assert result["count"] == 1
        assert [i["issue_id"] for i in result["issues"]] == ["active"]
        assert result["dismissed_count"] == 1

    @pytest.mark.asyncio
    async def test_include_dismissed_returns_all(self):
        ws = _ws_client_with_issues(
            [
                {"issue_id": "active", "ignored": False},
                {"issue_id": "dismissed", "ignored": True},
            ]
        )

        result = await SystemTools._fetch_repairs(ws, include_dismissed=True)

        assert result["count"] == 2
        assert "dismissed_count" not in result
        ids = {i["issue_id"] for i in result["issues"]}
        assert ids == {"active", "dismissed"}

    @pytest.mark.asyncio
    async def test_no_dismissed_omits_counter(self):
        """When nothing is filtered, the `dismissed_count` key stays out."""
        ws = _ws_client_with_issues(
            [{"issue_id": "active", "ignored": False}]
        )

        result = await SystemTools._fetch_repairs(ws)

        assert result["count"] == 1
        assert "dismissed_count" not in result

    @pytest.mark.asyncio
    async def test_repair_fields_pass_through_unmodified(self):
        """`_fetch_repairs` returns full payloads — fields like `ignored`,
        `dismissed_version`, `is_fixable`, and the verbose
        `translation_placeholders` all survive (no projection at this layer,
        unlike `ha_get_overview`'s compact view).
        """
        ws = _ws_client_with_issues(
            [
                {
                    "issue_id": "active",
                    "ignored": False,
                    "dismissed_version": None,
                    "is_fixable": True,
                    "severity": "warning",
                    "translation_placeholders": {"foo": "bar"},
                }
            ]
        )

        result = await SystemTools._fetch_repairs(ws, include_dismissed=True)

        entry = result["issues"][0]
        assert entry["ignored"] is False
        assert entry["is_fixable"] is True
        assert entry["translation_placeholders"] == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_success_false_surfaces_error_message(self):
        """When HA responds `success: False`, surface the error message
        instead of silently returning an empty list.
        """
        ws = AsyncMock()
        ws.send_command.return_value = {
            "success": False,
            "error": {"code": "unknown_error", "message": "boom"},
        }

        result = await SystemTools._fetch_repairs(ws)

        assert result["count"] == 0
        assert result["issues"] == []
        assert "boom" in result["error"]

    @pytest.mark.asyncio
    async def test_ws_exception_captured_as_error(self):
        """Exceptions during the WS call are caught and surfaced via `error`."""
        ws = AsyncMock()
        ws.send_command.side_effect = RuntimeError("ws disconnect")

        result = await SystemTools._fetch_repairs(ws)

        assert result["count"] == 0
        assert "ws disconnect" in result["error"]


class TestGetSystemHealthGather:
    """``ha_get_system_health`` runs optional sections concurrently via ``asyncio.gather``.

    Issue #1331 — replaces the prior sequential ``await self._fetch_repairs(...)``
    chain to halve wall-clock when multiple slow sections are requested.
    """

    @pytest.mark.asyncio
    async def test_all_three_sections_populated_when_all_requested(self):
        """``include="repairs,zha_network,zwave_network"`` populates all three from concurrent gather."""
        client = MagicMock()
        tools = SystemTools(client)
        mock_repairs = AsyncMock(return_value={"issues": [], "count": 0})
        mock_zha = AsyncMock(return_value={"devices": [{"name": "A"}]})
        mock_zwave = AsyncMock(return_value={"nodes": []})

        with _patch_health_info_baseline(), patch.object(
            SystemTools, "_fetch_repairs", new=mock_repairs
        ), patch.object(
            SystemTools, "_fetch_zha_network", new=mock_zha
        ), patch.object(
            SystemTools, "_fetch_zwave_network", new=mock_zwave
        ):
            result = await tools.ha_get_system_health(
                include="repairs,zha_network,zwave_network"
            )

        assert result["repairs"] == {"issues": [], "count": 0}
        assert result["zha_network"] == {"devices": [{"name": "A"}]}
        assert result["zwave_network"] == {"nodes": []}
        # Each helper fired exactly once — guards against a regression that
        # silently double-runs or skips a section under gather.
        mock_repairs.assert_awaited_once()
        mock_zha.assert_awaited_once()
        mock_zwave.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unrequested_sections_not_called(self):
        """Only requested sections fire; the other helpers stay un-awaited."""
        client = MagicMock()
        tools = SystemTools(client)
        mock_repairs = AsyncMock(return_value={"issues": [], "count": 0})
        mock_zha = AsyncMock()
        mock_zwave = AsyncMock()

        with _patch_health_info_baseline(), patch.object(
            SystemTools, "_fetch_repairs", new=mock_repairs
        ), patch.object(
            SystemTools, "_fetch_zha_network", new=mock_zha
        ), patch.object(
            SystemTools, "_fetch_zwave_network", new=mock_zwave
        ):
            result = await tools.ha_get_system_health(include="repairs")

        assert "repairs" in result
        assert "zha_network" not in result
        assert "zwave_network" not in result
        mock_repairs.assert_awaited_once()
        mock_zha.assert_not_awaited()
        mock_zwave.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_section_raising_does_not_block_siblings(self):
        """A raising helper attributes its failure to its section; others still populate.

        The helpers themselves are written never to raise (each wraps its WS
        call in try/except). This test exercises the defensive
        ``return_exceptions=True`` path on ``gather`` against an
        unexpected-exception regression in any one helper.
        """
        client = MagicMock()
        tools = SystemTools(client)
        mock_repairs = AsyncMock(side_effect=RuntimeError("repairs blew up"))
        mock_zha = AsyncMock(return_value={"devices": [{"name": "B"}]})
        mock_zwave = AsyncMock(return_value={"nodes": [{"id": 1}]})

        with _patch_health_info_baseline(), patch.object(
            SystemTools, "_fetch_repairs", new=mock_repairs
        ), patch.object(
            SystemTools, "_fetch_zha_network", new=mock_zha
        ), patch.object(
            SystemTools, "_fetch_zwave_network", new=mock_zwave
        ):
            result = await tools.ha_get_system_health(
                include="repairs,zha_network,zwave_network"
            )

        # Raising section attributed by name
        assert "error" in result["repairs"]
        assert "RuntimeError" in result["repairs"]["error"]
        assert "repairs blew up" in result["repairs"]["error"]
        # Sibling sections unaffected
        assert result["zha_network"] == {"devices": [{"name": "B"}]}
        assert result["zwave_network"] == {"nodes": [{"id": 1}]}
        # All three were attempted — proves the gather didn't short-circuit
        mock_repairs.assert_awaited_once()
        mock_zha.assert_awaited_once()
        mock_zwave.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_zha_full_routes_to_full_flag(self):
        """``include="zha_network_full"`` calls ``_fetch_zha_network`` with full=True."""
        client = MagicMock()
        tools = SystemTools(client)
        mock_zha = AsyncMock(return_value={"devices": []})

        with _patch_health_info_baseline(), patch.object(
            SystemTools, "_fetch_zha_network", new=mock_zha
        ):
            await tools.ha_get_system_health(include="zha_network_full")

        mock_zha.assert_awaited_once()
        # Helper called with ws_client + full=True
        assert mock_zha.await_args.kwargs == {"full": True}

    @pytest.mark.asyncio
    async def test_no_sections_when_include_omitted(self):
        """No ``include`` → no gather call, only baseline health_info returned."""
        client = MagicMock()
        tools = SystemTools(client)
        mock_repairs = AsyncMock()
        mock_zha = AsyncMock()
        mock_zwave = AsyncMock()

        with _patch_health_info_baseline(), patch.object(
            SystemTools, "_fetch_repairs", new=mock_repairs
        ), patch.object(
            SystemTools, "_fetch_zha_network", new=mock_zha
        ), patch.object(
            SystemTools, "_fetch_zwave_network", new=mock_zwave
        ):
            result = await tools.ha_get_system_health()

        assert result["success"] is True
        assert "repairs" not in result
        assert "zha_network" not in result
        assert "zwave_network" not in result
        mock_repairs.assert_not_awaited()
        mock_zha.assert_not_awaited()
        mock_zwave.assert_not_awaited()
