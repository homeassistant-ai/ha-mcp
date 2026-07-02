"""Unit tests for tools_system module.

Regression tests for https://github.com/homeassistant-ai/ha-mcp/issues/612
ha_restart reports failure when a reverse proxy returns 504 during restart.
"""

import asyncio
import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
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


def _make_client_that_fails_on_check_config(exception):
    """Create a mock client where check_config itself raises.

    Used to exercise the pre-dispatch path: the failure happens before the
    restart service is ever called, so ``restart_initiated`` stays False.
    """
    mock_client = AsyncMock()
    mock_client.check_config.side_effect = exception
    return mock_client


class TestGetSystemHealthHaMcpUpdate:
    """ha_get_system_health surfaces the MCP server's own update status."""

    @pytest.mark.asyncio
    async def test_ha_mcp_update_present_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from ha_mcp import update_check

        monkeypatch.setattr(
            update_check,
            "get_update_field",
            AsyncMock(
                return_value={
                    "current": "7.8.0",
                    "latest": "7.9.0",
                    "update_available": True,
                }
            ),
        )
        with _patch_health_info_baseline():
            result = await SystemTools(AsyncMock()).ha_get_system_health()
        assert result["ha_mcp_update"] == {
            "current": "7.8.0",
            "latest": "7.9.0",
            "update_available": True,
        }

    @pytest.mark.asyncio
    async def test_ha_mcp_update_absent_when_not_applicable(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from ha_mcp import update_check

        monkeypatch.setattr(
            update_check, "get_update_field", AsyncMock(return_value=None)
        )
        with _patch_health_info_baseline():
            result = await SystemTools(AsyncMock()).ha_get_system_health()
        assert "ha_mcp_update" not in result


class TestHaRestartErrorHandling:
    """Tests for ha_restart handling of expected errors during restart."""

    @pytest.mark.asyncio
    async def test_504_gateway_timeout_treated_as_success(self):
        """A 504 from a reverse proxy after restart initiated should be success.

        Reproduces issue #612: user behind a reverse proxy gets 504 when HA
        shuts down, but HA actually restarted successfully.

        Also pins the post-#1332 warnings-list contract on the
        restart-initiated-but-connection-dropped branch
        (``tools_system.py`` ~L208-214).
        """
        error = HomeAssistantAPIError("API error: 504 - ", status_code=504)
        client = _make_client_that_fails_on_restart(error)
        tools = SystemTools(client)

        result = await tools.ha_restart(confirm=True)

        assert result["success"] is True
        warnings = result.get("warnings")
        assert isinstance(warnings, list) and warnings, (
            f"Expected non-empty warnings list, got: {result!r}"
        )
        assert any("Wait 1-5 minutes" in w for w in warnings), (
            f"Expected wait-for-restart warning content; got: {warnings!r}"
        )

    @pytest.mark.parametrize(
        ("error", "pattern"),
        [
            (
                HomeAssistantAPIError("API error: 502 - ", status_code=502),
                "502",
            ),
            (
                HomeAssistantAPIError("API error: 503 - ", status_code=503),
                "503",
            ),
            (
                HomeAssistantConnectionError("Proxy returned: Bad Gateway"),
                "gateway",
            ),
            (
                HomeAssistantConnectionError("Upstream Service Unavailable"),
                "unavailable",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_proxy_5xx_during_restart_treated_as_success(self, error, pattern):
        """Reverse-proxy 502/503/'gateway'/'unavailable' responses after the
        restart was initiated should be treated as the expected connection drop,
        not a failure.

        Reproduces issue #1666: behind nginx/Traefik/Cloudflare the proxy often
        returns a 502/503 (or a 'Bad Gateway'/'Service Unavailable' body) while
        HA is shutting down, even though the restart succeeded. Extends the #612
        (504) fix to the broader set of known-good patterns in
        ``tools_system.py`` (~L199-211).

        Each case isolates a single newly added pattern so a future narrowing of
        the match list fails loudly here.
        """
        client = _make_client_that_fails_on_restart(error)
        tools = SystemTools(client)

        result = await tools.ha_restart(confirm=True)

        assert result["success"] is True, (
            f"Proxy '{pattern}' error should be treated as the expected restart "
            f"connection drop; got: {result!r}"
        )
        warnings = result.get("warnings")
        assert isinstance(warnings, list) and warnings, (
            f"Expected non-empty warnings list, got: {result!r}"
        )
        assert any("Wait 1-5 minutes" in w for w in warnings), (
            f"Expected wait-for-restart warning content; got: {warnings!r}"
        )

    @pytest.mark.asyncio
    async def test_proxy_error_before_restart_dispatch_still_fails(self):
        """A matching proxy error raised *before* the restart is dispatched must
        still fail loudly.

        The known-good branch is gated on ``restart_initiated and any(pattern ...)``.
        The other tests all run with ``restart_initiated`` already True (the
        failure comes from ``call_service``); this pins the ``restart_initiated
        is False`` half of that guard. Here ``check_config`` raises a
        ``Bad Gateway`` (which would otherwise match the ``gateway`` pattern),
        but since the restart was never sent it must raise a ``ToolError`` rather
        than be reported as a successful restart. Guards against a future change
        that weakens the guard and lets a pre-restart proxy error masquerade as
        success.
        """
        error = HomeAssistantConnectionError("Bad Gateway")
        client = _make_client_that_fails_on_check_config(error)
        tools = SystemTools(client)

        with pytest.raises(ToolError):
            await tools.ha_restart(confirm=True)

    @pytest.mark.asyncio
    async def test_unrelated_error_still_fails(self):
        """Errors unrelated to restart should still report failure via ToolError."""
        error = Exception("Something completely unrelated went wrong")
        client = _make_client_that_fails_on_restart(error)
        tools = SystemTools(client)

        with pytest.raises(ToolError):
            await tools.ha_restart(confirm=True)

    @pytest.mark.asyncio
    async def test_success_path_returns_connection_lost_warning(self):
        """Successful restart (no exception from call_service) surfaces a top-level
        warnings list with the "connection will be lost" note. Pins the
        post-#1332 contract on ``tools_system.py`` ~L188-194 (the first return
        in ha_restart, previously emitting singular ``warning``)."""
        client = AsyncMock()
        client.check_config.return_value = {"result": "valid"}
        client.call_service.return_value = None  # restart fires, no error
        tools = SystemTools(client)

        result = await tools.ha_restart(confirm=True)

        assert result["success"] is True
        warnings = result.get("warnings")
        assert isinstance(warnings, list) and warnings, (
            f"Expected non-empty warnings list, got: {result!r}"
        )
        assert any("Connection will be lost" in w for w in warnings), (
            f"Expected connection-lost warning content; got: {warnings!r}"
        )


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
                {
                    "issue_id": "dismissed",
                    "ignored": True,
                    "dismissed_version": "2026.4.0",
                },
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
        ws = _ws_client_with_issues([{"issue_id": "active", "ignored": False}])

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


class TestFetchZwaveNetwork:
    """``_fetch_zwave_network`` resolves the zwave_js config entry then fetches
    network status. Regression guard for the config_entries/get command name —
    the helper was previously only mocked whole, so a slash-vs-underscore typo
    in the command went undetected (it errored as 'integration not available'
    for every install)."""

    def _zwave_ws_client(self):
        """ws_client whose send_command dispatches on the canonical commands."""
        ws = MagicMock()

        async def _send(command, **kwargs):
            if command == "config_entries/get":
                return {
                    "success": True,
                    "result": [{"domain": "zwave_js", "entry_id": "zw_entry"}],
                }
            if command == "zwave_js/network_status":
                return {
                    "success": True,
                    "result": {"controller": {"nodes": [{"node_id": 1}]}},
                }
            return {"success": False, "error": {"message": "Unknown command."}}

        ws.send_command = AsyncMock(side_effect=_send)
        return ws

    @pytest.mark.asyncio
    async def test_uses_canonical_config_entries_command(self):
        """The entry lookup must use 'config_entries/get' (underscore). With the
        wrong slash name the mock returns Unknown-command and the section would
        report 'integration not found' despite zwave_js being present."""
        ws = self._zwave_ws_client()
        result = await SystemTools._fetch_zwave_network(ws)

        sent_commands = [call.args[0] for call in ws.send_command.call_args_list]
        assert "config_entries/get" in sent_commands
        assert "config/entries/get" not in sent_commands
        # Proves the entry was actually resolved (only possible with the right
        # command name) — not the "integration not found" error path.
        assert "error" not in result
        assert result["count"] == 1
        assert result["nodes"][0]["node_id"] == 1


class TestFetchThreadNetwork:
    """``_fetch_thread_network`` calls ``otbr/info`` and summarizes each border
    router (channel, extended_pan_id, border_agent_id) keyed by extended
    address — mirrors the Z-Wave/ZHA never-raise section convention."""

    def _thread_ws_client(self, info_resp):
        """ws_client whose send_command('otbr/info') returns ``info_resp``."""
        ws = MagicMock()

        async def _send(command, **kwargs):
            if command == "otbr/info":
                return info_resp
            return {"success": False, "error": {"message": "Unknown command."}}

        ws.send_command = AsyncMock(side_effect=_send)
        return ws

    @pytest.mark.asyncio
    async def test_summarizes_border_routers(self):
        """A loaded OTBR maps to one border-router summary with the three
        documented fields, keyed by its extended address."""
        ws = self._thread_ws_client(
            {
                "success": True,
                "result": {
                    "f00dcafef00dcafe": {
                        "active_dataset_tlvs": "0e08...",
                        "border_agent_id": "deadbeefdeadbeef",
                        "channel": 15,
                        "extended_address": "f00dcafef00dcafe",
                        "extended_pan_id": "abcdef0123456789",
                        "url": "http://core-silabs-multiprotocol:8081",
                    }
                },
            }
        )
        result = await SystemTools._fetch_thread_network(ws)

        sent = [call.args[0] for call in ws.send_command.call_args_list]
        assert "otbr/info" in sent
        assert "error" not in result
        assert result["count"] == 1
        br = result["border_routers"][0]
        assert br["extended_address"] == "f00dcafef00dcafe"
        assert br["channel"] == 15
        assert br["extended_pan_id"] == "abcdef0123456789"
        assert br["border_agent_id"] == "deadbeefdeadbeef"

    @pytest.mark.asyncio
    async def test_not_loaded_surfaces_error(self):
        """``otbr/info`` answers success=false (code not_loaded) when no OTBR is
        configured; the section surfaces the message as an error sub-dict."""
        ws = self._thread_ws_client(
            {
                "success": False,
                "error": {"code": "not_loaded", "message": "No OTBR API loaded"},
            }
        )
        result = await SystemTools._fetch_thread_network(ws)

        assert result["count"] == 0
        assert result["border_routers"] == []
        assert "No OTBR API loaded" in result["error"]

    @pytest.mark.asyncio
    async def test_ws_exception_captured_as_error(self):
        """Exceptions during the WS call are caught and surfaced via ``error``."""
        ws = AsyncMock()
        ws.send_command.side_effect = RuntimeError("ws disconnect")

        result = await SystemTools._fetch_thread_network(ws)

        assert result["count"] == 0
        assert "ws disconnect" in result["error"]

    @pytest.mark.asyncio
    async def test_fatal_cancellation_propagates_not_embedded(self):
        """A CancelledError must unwind, not demote to a section error — guards
        the ``_reraise_if_fatal`` gate."""
        ws = AsyncMock()
        ws.send_command.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await SystemTools._fetch_thread_network(ws)


class TestFetchMatterNetwork:
    """``_fetch_matter_network`` resolves the matter config entry via
    ``config_entries/get`` and returns a lightweight presence summary."""

    def _matter_ws_client(self, entries_resp):
        """ws_client whose send_command('config_entries/get') returns
        ``entries_resp``."""
        ws = MagicMock()

        async def _send(command, **kwargs):
            if command == "config_entries/get":
                return entries_resp
            return {"success": False, "error": {"message": "Unknown command."}}

        ws.send_command = AsyncMock(side_effect=_send)
        return ws

    @pytest.mark.asyncio
    async def test_resolves_matter_entry(self):
        """The matter entry is selected by domain and reduced to
        {config_entry_id, state, title}. Pins the canonical (underscore)
        command name like the zwave helper test."""
        ws = self._matter_ws_client(
            {
                "success": True,
                "result": [
                    {
                        "domain": "hue",
                        "entry_id": "hue1",
                        "state": "loaded",
                        "title": "Hue",
                    },
                    {
                        "domain": "matter",
                        "entry_id": "m1",
                        "state": "loaded",
                        "title": "Matter Server",
                    },
                ],
            }
        )
        result = await SystemTools._fetch_matter_network(ws)

        sent = [call.args[0] for call in ws.send_command.call_args_list]
        assert "config_entries/get" in sent
        assert "config/entries/get" not in sent
        assert result == {
            "config_entry_id": "m1",
            "state": "loaded",
            "title": "Matter Server",
        }

    @pytest.mark.asyncio
    async def test_no_matter_entry_returns_not_found(self):
        """No matter entry → the documented not-found error sub-dict."""
        ws = self._matter_ws_client(
            {"success": True, "result": [{"domain": "hue", "entry_id": "hue1"}]}
        )
        result = await SystemTools._fetch_matter_network(ws)

        assert result == {"error": "Matter integration not found"}

    @pytest.mark.asyncio
    async def test_ws_exception_captured_as_error(self):
        """Exceptions during the WS call are caught and surfaced via ``error``."""
        ws = AsyncMock()
        ws.send_command.side_effect = RuntimeError("ws disconnect")

        result = await SystemTools._fetch_matter_network(ws)

        assert "ws disconnect" in result["error"]

    @pytest.mark.asyncio
    async def test_fatal_cancellation_propagates_not_embedded(self):
        """A CancelledError must unwind, not demote to a section error."""
        ws = AsyncMock()
        ws.send_command.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await SystemTools._fetch_matter_network(ws)


class TestGetSystemHealthThreadMatterWiring:
    """``ha_get_system_health`` wires the thread_network / matter_network
    include sections into the concurrent gather and the WS-unavailable branch,
    mirroring the zwave_network plumbing."""

    @pytest.mark.asyncio
    async def test_both_sections_populated_when_requested(self):
        client = MagicMock()
        tools = SystemTools(client)
        mock_thread = AsyncMock(return_value={"border_routers": [], "count": 0})
        mock_matter = AsyncMock(
            return_value={"config_entry_id": "m1", "state": "loaded", "title": "M"}
        )

        with (
            _patch_health_info_baseline() as (_, ws_client),
            patch.object(SystemTools, "_fetch_thread_network", new=mock_thread),
            patch.object(SystemTools, "_fetch_matter_network", new=mock_matter),
        ):
            result = await tools.ha_get_system_health(
                include="thread_network,matter_network"
            )

        assert result["thread_network"] == {"border_routers": [], "count": 0}
        assert result["matter_network"]["config_entry_id"] == "m1"
        mock_thread.assert_awaited_once()
        mock_matter.assert_awaited_once()
        ws_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unrequested_sections_not_called(self):
        client = MagicMock()
        tools = SystemTools(client)
        mock_thread = AsyncMock()
        mock_matter = AsyncMock()

        with (
            _patch_health_info_baseline(),
            patch.object(
                SystemTools,
                "_fetch_repairs",
                new=AsyncMock(return_value={"issues": [], "count": 0}),
            ),
            patch.object(SystemTools, "_fetch_thread_network", new=mock_thread),
            patch.object(SystemTools, "_fetch_matter_network", new=mock_matter),
        ):
            result = await tools.ha_get_system_health(include="repairs")

        assert "thread_network" not in result
        assert "matter_network" not in result
        mock_thread.assert_not_awaited()
        mock_matter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sections_reported_unavailable_when_ws_fails(self):
        """With the health WS baseline down but a REST section also requested,
        thread_network/matter_network carry the {error} sub-dict shape and a
        summary warning rather than crashing the tool."""
        client = MagicMock()
        client.check_config = AsyncMock(return_value={"result": "valid"})
        tools = SystemTools(client)

        with patch.object(
            SystemTools,
            "_fetch_health_info",
            new=AsyncMock(side_effect=ToolError("system_health WebSocket down")),
        ):
            result = await tools.ha_get_system_health(
                include="thread_network,matter_network,config_check"
            )

        assert result["success"] is True
        assert result["baseline_available"] is False
        assert "error" in result["thread_network"]
        assert "error" in result["matter_network"]
        assert any(
            "thread_network" in w or "matter_network" in w
            for w in result.get("warnings", [])
        )


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

        with (
            _patch_health_info_baseline() as (_, ws_client),
            patch.object(SystemTools, "_fetch_repairs", new=mock_repairs),
            patch.object(SystemTools, "_fetch_zha_network", new=mock_zha),
            patch.object(SystemTools, "_fetch_zwave_network", new=mock_zwave),
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
        # ``finally``-clause cleanup of the WS client must still fire on the
        # gather path — guards against a regression that skips disconnect.
        ws_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unrequested_sections_not_called(self):
        """Only requested sections fire; the other helpers stay un-awaited."""
        client = MagicMock()
        tools = SystemTools(client)
        mock_repairs = AsyncMock(return_value={"issues": [], "count": 0})
        mock_zha = AsyncMock()
        mock_zwave = AsyncMock()

        with (
            _patch_health_info_baseline(),
            patch.object(SystemTools, "_fetch_repairs", new=mock_repairs),
            patch.object(SystemTools, "_fetch_zha_network", new=mock_zha),
            patch.object(SystemTools, "_fetch_zwave_network", new=mock_zwave),
        ):
            result = await tools.ha_get_system_health(include="repairs")

        assert "repairs" in result
        assert "zha_network" not in result
        assert "zwave_network" not in result
        mock_repairs.assert_awaited_once()
        mock_zha.assert_not_awaited()
        mock_zwave.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raising_section",
        ["repairs", "zha_network", "zwave_network"],
    )
    async def test_one_section_raising_does_not_block_siblings(self, raising_section):
        """A raising helper attributes its failure to its section; others still populate.

        The helpers themselves are written never to raise (each wraps its WS
        call in try/except). This test exercises the defensive
        ``return_exceptions=True`` path on ``gather`` against an
        unexpected-exception regression in any one helper.

        Parametrized over which helper raises so a future zip/mis-attribution
        regression gets caught regardless of position in the sections list.
        """
        client = MagicMock()
        tools = SystemTools(client)

        section_to_helper = {
            "repairs": "_fetch_repairs",
            "zha_network": "_fetch_zha_network",
            "zwave_network": "_fetch_zwave_network",
        }
        section_success_values = {
            "repairs": {"issues": [], "count": 0},
            "zha_network": {"devices": [{"name": "B"}]},
            "zwave_network": {"nodes": [{"id": 1}]},
        }

        mocks = {
            section: AsyncMock(
                side_effect=RuntimeError(f"{section} blew up")
                if section == raising_section
                else None,
                return_value=None
                if section == raising_section
                else section_success_values[section],
            )
            for section in section_to_helper
        }

        with (
            _patch_health_info_baseline(),
            patch.object(SystemTools, "_fetch_repairs", new=mocks["repairs"]),
            patch.object(SystemTools, "_fetch_zha_network", new=mocks["zha_network"]),
            patch.object(
                SystemTools, "_fetch_zwave_network", new=mocks["zwave_network"]
            ),
        ):
            result = await tools.ha_get_system_health(
                include="repairs,zha_network,zwave_network"
            )

        # Raising section attributed by name — confirms zip alignment between
        # the sections list (insertion order) and the gather result list.
        assert "error" in result[raising_section]
        assert "RuntimeError" in result[raising_section]["error"]
        assert f"{raising_section} blew up" in result[raising_section]["error"]
        # Sibling sections unaffected and carry their success payload.
        for sibling in section_to_helper:
            if sibling == raising_section:
                continue
            assert result[sibling] == section_success_values[sibling]
        # All three were attempted — proves the gather didn't short-circuit.
        for section in section_to_helper:
            mocks[section].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_helpers_actually_run_concurrently(self):
        """Two helpers can be in-flight simultaneously under the gather path
        — guards against a regression that reverts to serial ``await``.

        Helper A waits on an ``asyncio.Event`` that helper B sets. If gather
        ran the helpers serially in registration order, helper A would
        deadlock waiting for an event that helper B (queued behind it) has
        no chance to set, and the test would time out. The parallel path
        completes both helpers in well under the timeout."""
        client = MagicMock()
        tools = SystemTools(client)

        signal = asyncio.Event()

        async def waiting_helper(*_args, **_kwargs):
            # Helper A: blocks until helper B signals; cap at 2s so a
            # serial-regression fails fast rather than timing out the suite.
            await asyncio.wait_for(signal.wait(), timeout=2.0)
            return {"issues": [], "count": 0}

        async def signalling_helper(*_args, **_kwargs):
            # Helper B: trips the signal so helper A can complete.
            signal.set()
            return {"devices": [{"name": "B"}]}

        with (
            _patch_health_info_baseline(),
            patch.object(
                SystemTools, "_fetch_repairs", new=AsyncMock(side_effect=waiting_helper)
            ),
            patch.object(
                SystemTools,
                "_fetch_zha_network",
                new=AsyncMock(side_effect=signalling_helper),
            ),
        ):
            result = await tools.ha_get_system_health(include="repairs,zha_network")

        assert result["repairs"] == {"issues": [], "count": 0}
        assert result["zha_network"] == {"devices": [{"name": "B"}]}

    @pytest.mark.asyncio
    async def test_cancelled_error_propagated_not_demoted(self):
        """``asyncio.CancelledError`` from a helper must propagate out of
        ``ha_get_system_health`` rather than land as ``{"error": "CancelledError: …"}``.

        ``asyncio.gather(return_exceptions=True)`` returns ``CancelledError``
        as a result element instead of re-raising, so the pre-pass must
        explicitly re-raise it. Without this, a cancelled request would
        return ``success=True`` and the runtime wouldn't unwind."""
        client = MagicMock()
        tools = SystemTools(client)
        mock_repairs = AsyncMock(side_effect=asyncio.CancelledError())
        mock_zha = AsyncMock(return_value={"devices": [{"name": "B"}]})

        with (
            _patch_health_info_baseline(),
            patch.object(SystemTools, "_fetch_repairs", new=mock_repairs),
            patch.object(SystemTools, "_fetch_zha_network", new=mock_zha),
            pytest.raises(asyncio.CancelledError),
        ):
            await tools.ha_get_system_health(include="repairs,zha_network")

    @pytest.mark.asyncio
    async def test_tool_error_from_helper_re_raised_not_demoted(self):
        """A ``ToolError`` raised inside a helper must propagate so the MCP
        ``isError=true`` contract holds — not get silently demoted to
        ``result[section] = {"error": "ToolError: …"}`` with ``success=True``."""
        client = MagicMock()
        tools = SystemTools(client)
        tool_err = ToolError("simulated helper-side tool error")
        mock_repairs = AsyncMock(side_effect=tool_err)
        mock_zha = AsyncMock(return_value={"devices": [{"name": "B"}]})

        with (
            _patch_health_info_baseline(),
            patch.object(SystemTools, "_fetch_repairs", new=mock_repairs),
            patch.object(SystemTools, "_fetch_zha_network", new=mock_zha),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_get_system_health(include="repairs,zha_network")
        assert excinfo.value is tool_err

    @pytest.mark.asyncio
    async def test_zha_full_routes_to_full_flag(self):
        """``include="zha_network_full"`` calls ``_fetch_zha_network`` with full=True."""
        client = MagicMock()
        tools = SystemTools(client)
        mock_zha = AsyncMock(return_value={"devices": []})

        with (
            _patch_health_info_baseline(),
            patch.object(SystemTools, "_fetch_zha_network", new=mock_zha),
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

        with (
            _patch_health_info_baseline(),
            patch.object(SystemTools, "_fetch_repairs", new=mock_repairs),
            patch.object(SystemTools, "_fetch_zha_network", new=mock_zha),
            patch.object(SystemTools, "_fetch_zwave_network", new=mock_zwave),
        ):
            result = await tools.ha_get_system_health()

        assert result["success"] is True
        assert "repairs" not in result
        assert "zha_network" not in result
        assert "zwave_network" not in result
        mock_repairs.assert_not_awaited()
        mock_zha.assert_not_awaited()
        mock_zwave.assert_not_awaited()


class TestGetSystemHealthDiagnostics:
    """Wire-up tests for ha_get_system_health's include='diagnostics' branch."""

    @pytest.mark.asyncio
    async def test_diagnostics_without_config_entry_id_returns_error_subdict(self):
        client = MagicMock()
        tools = SystemTools(client)
        with _patch_health_info_baseline():
            result = await tools.ha_get_system_health(include="diagnostics")
        assert "diagnostics" in result
        assert "error" in result["diagnostics"]
        assert "config_entry_id is required" in result["diagnostics"]["error"]
        assert "ha_get_integration" in result["diagnostics"]["error"]

    @pytest.mark.asyncio
    async def test_diagnostics_with_config_entry_id_calls_helper(self):
        client = MagicMock()
        tools = SystemTools(client)
        diag_payload = {
            "config_entry_id": "entry_abc",
            "data": {"home_assistant": {"version": "2026.5.0"}},
        }
        with (
            _patch_health_info_baseline(),
            patch(
                "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
                new=AsyncMock(return_value=diag_payload),
            ) as mock_fetch,
        ):
            result = await tools.ha_get_system_health(
                include="diagnostics", config_entry_id="entry_abc"
            )
        assert result["diagnostics"] == diag_payload
        mock_fetch.assert_awaited_once_with(
            client,
            "entry_abc",
            None,
            fields=None,
            truncate_at_bytes=None,
            data_path=None,
            data_offset=0,
            data_limit=None,
        )

    @pytest.mark.asyncio
    async def test_diagnostics_with_device_id_forwarded_to_helper(self):
        client = MagicMock()
        tools = SystemTools(client)
        with (
            _patch_health_info_baseline(),
            patch(
                "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
                new=AsyncMock(return_value={"data": {}}),
            ) as mock_fetch,
        ):
            await tools.ha_get_system_health(
                include="diagnostics",
                config_entry_id="entry_abc",
                device_id="dev_xyz",
            )
        mock_fetch.assert_awaited_once_with(
            client,
            "entry_abc",
            "dev_xyz",
            fields=None,
            truncate_at_bytes=None,
            data_path=None,
            data_offset=0,
            data_limit=None,
        )

    @pytest.mark.asyncio
    async def test_diagnostics_combined_with_repairs(self):
        """include='repairs,diagnostics' should populate both sections."""
        client = MagicMock()
        tools = SystemTools(client)
        mock_repairs = AsyncMock(return_value={"issues": [], "count": 0})
        with (
            _patch_health_info_baseline(),
            patch.object(SystemTools, "_fetch_repairs", new=mock_repairs),
            patch(
                "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
                new=AsyncMock(return_value={"data": {"x": 1}}),
            ) as mock_diag,
        ):
            result = await tools.ha_get_system_health(
                include="repairs,diagnostics", config_entry_id="entry_abc"
            )
        assert "repairs" in result
        assert "diagnostics" in result
        assert result["diagnostics"]["data"] == {"x": 1}
        # Both branches actually fired — guards against a silent no-op
        # in either fetch.
        mock_repairs.assert_awaited_once()
        mock_diag.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_diagnostics_fields_and_truncate_forwarded_to_helper(self):
        """Tool surfaces ``diagnostics_fields`` + ``diagnostics_truncate_at_bytes``."""
        client = MagicMock()
        tools = SystemTools(client)
        with (
            _patch_health_info_baseline(),
            patch(
                "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
                new=AsyncMock(return_value={"data": {}}),
            ) as mock_fetch,
        ):
            await tools.ha_get_system_health(
                include="diagnostics",
                config_entry_id="entry_abc",
                diagnostics_fields="home_assistant, issues",
                diagnostics_truncate_at_bytes=20000,
            )
        mock_fetch.assert_awaited_once_with(
            client,
            "entry_abc",
            None,
            fields=["home_assistant", "issues"],
            truncate_at_bytes=20000,
            data_path=None,
            data_offset=0,
            data_limit=None,
        )

    @pytest.mark.asyncio
    async def test_diagnostics_data_path_and_pagination_forwarded_to_helper(self):
        """Tool surfaces ``diagnostics_data_path`` + offset/limit through to
        the helper."""
        client = MagicMock()
        tools = SystemTools(client)
        with (
            _patch_health_info_baseline(),
            patch(
                "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
                new=AsyncMock(return_value={"data": {}}),
            ) as mock_fetch,
        ):
            await tools.ha_get_system_health(
                include="diagnostics",
                config_entry_id="entry_abc",
                diagnostics_data_path="data.devices",
                diagnostics_data_offset=10,
                diagnostics_data_limit=5,
            )
        mock_fetch.assert_awaited_once_with(
            client,
            "entry_abc",
            None,
            fields=None,
            truncate_at_bytes=None,
            data_path="data.devices",
            data_offset=10,
            data_limit=5,
        )

    @pytest.mark.asyncio
    async def test_missing_config_entry_id_forwarded_as_none_not_empty_string(
        self,
    ):
        """``include=diagnostics`` without ``config_entry_id`` forwards ``None``
        to the helper (not ``""``) so the echo field reflects the actual input
        rather than a coerced placeholder."""
        client = MagicMock()
        tools = SystemTools(client)
        with (
            _patch_health_info_baseline(),
            patch(
                "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
                new=AsyncMock(
                    return_value={
                        "config_entry_id": None,
                        "error": "config_entry_id is required for diagnostics fetch.",
                    }
                ),
            ) as mock_fetch,
        ):
            result = await tools.ha_get_system_health(include="diagnostics")
        # Positional config_entry_id is None, not "".
        assert mock_fetch.await_args.args[1] is None
        assert result["diagnostics"]["config_entry_id"] is None

    @pytest.mark.asyncio
    async def test_diagnostics_omitted_from_include_skips_helper(self):
        """include='repairs' (no diagnostics) must not invoke the diagnostics helper."""
        client = MagicMock()
        tools = SystemTools(client)
        with (
            _patch_health_info_baseline(),
            patch.object(
                SystemTools,
                "_fetch_repairs",
                new=AsyncMock(return_value={"issues": [], "count": 0}),
            ),
            patch(
                "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
                new=AsyncMock(),
            ) as mock_fetch,
        ):
            result = await tools.ha_get_system_health(
                include="repairs", config_entry_id="entry_abc"
            )
        mock_fetch.assert_not_awaited()
        assert "diagnostics" not in result

    @pytest.mark.asyncio
    async def test_orphaned_ids_surface_parity_warning(self):
        """config_entry_id/device_id without 'diagnostics' in include surface a
        warnings entry (parity with ha_get_integration's `include_diagnostics=False
        + device_id` warning)."""
        client = MagicMock()
        tools = SystemTools(client)
        with (
            _patch_health_info_baseline(),
            patch.object(
                SystemTools,
                "_fetch_repairs",
                new=AsyncMock(return_value={"issues": [], "count": 0}),
            ),
        ):
            result = await tools.ha_get_system_health(
                include="repairs",
                config_entry_id="entry_abc",
                device_id="dev_xyz",
            )
        assert "warnings" in result
        assert any(
            "config_entry_id" in w or "device_id" in w for w in result["warnings"]
        )

    @pytest.mark.asyncio
    async def test_orphaned_data_offset_alone_surfaces_warning(self):
        """Pure ``diagnostics_data_offset > 0`` (no other diagnostics args)
        without 'diagnostics' in include triggers the orphan-args warning —
        guards the ``data_offset_int > 0`` term in the predicate."""
        client = MagicMock()
        tools = SystemTools(client)
        with (
            _patch_health_info_baseline(),
            patch.object(
                SystemTools,
                "_fetch_repairs",
                new=AsyncMock(return_value={"issues": [], "count": 0}),
            ),
        ):
            result = await tools.ha_get_system_health(
                include="repairs",
                diagnostics_data_offset=5,
            )
        assert "warnings" in result
        assert any("diagnostics_data_offset" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_data_path_non_string_rejected_with_validation_error(self):
        """Non-string ``diagnostics_data_path`` (dict / list / int) surfaces
        as ``VALIDATION_INVALID_PARAMETER`` instead of leaking ``INTERNAL_ERROR``
        from the resolver's ``.strip()`` call downstream. Pins the
        ``isinstance(str)`` type-guard at the ``ha_get_system_health`` layer."""
        client = MagicMock()
        tools = SystemTools(client)
        with _patch_health_info_baseline(), pytest.raises(ToolError) as excinfo:
            await tools.ha_get_system_health(
                include="diagnostics",
                config_entry_id="entry_abc",
                diagnostics_data_path={"not": "a string"},  # type: ignore[arg-type]
            )
        err_payload = json.loads(str(excinfo.value))
        assert err_payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "diagnostics_data_path" in err_payload["error"]["message"]


class TestGetSystemHealthConfigCheck:
    """Wire-up tests for ha_get_system_health's include='config_check' branch —
    the folded-in replacement for the removed standalone ha_check_config tool."""

    @pytest.mark.asyncio
    async def test_config_check_populates_section_when_valid(self):
        """include='config_check' calls client.check_config() (REST) and embeds
        the result/is_valid/errors sub-dict the old ha_check_config returned."""
        client = MagicMock()
        client.check_config = AsyncMock(return_value={"result": "valid"})
        tools = SystemTools(client)
        with _patch_health_info_baseline():
            result = await tools.ha_get_system_health(include="config_check")
        assert "config_check" in result
        assert result["config_check"]["is_valid"] is True
        assert result["config_check"]["result"] == "valid"
        assert result["config_check"]["errors"] == []
        client.check_config.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_config_check_surfaces_invalid_with_errors(self):
        client = MagicMock()
        client.check_config = AsyncMock(
            return_value={"result": "invalid", "errors": ["bad yaml at line 3"]}
        )
        tools = SystemTools(client)
        with _patch_health_info_baseline():
            result = await tools.ha_get_system_health(include="config_check")
        assert result["config_check"]["is_valid"] is False
        assert result["config_check"]["result"] == "invalid"
        assert result["config_check"]["errors"] == ["bad yaml at line 3"]

    @pytest.mark.asyncio
    async def test_config_check_absent_when_not_requested(self):
        """No include → config_check section absent and check_config not called
        (parity with test_no_sections_when_include_omitted)."""
        client = MagicMock()
        client.check_config = AsyncMock(return_value={"result": "valid"})
        tools = SystemTools(client)
        with _patch_health_info_baseline():
            result = await tools.ha_get_system_health()
        client.check_config.assert_not_awaited()
        assert "config_check" not in result

    @pytest.mark.asyncio
    async def test_config_check_backend_failure_embeds_error_not_raises(self):
        """A check_config backend failure surfaces as an embedded error sub-dict;
        the parent tool still succeeds (the _fetch_* never-raise convention)."""
        client = MagicMock()
        client.check_config = AsyncMock(side_effect=RuntimeError("boom"))
        tools = SystemTools(client)
        with _patch_health_info_baseline():
            result = await tools.ha_get_system_health(include="config_check")
        assert result.get("success") is True
        assert "error" in result["config_check"]
        assert "boom" in result["config_check"]["error"]
        # Pin the safe baseline contract: on failure config must read as
        # NOT valid so a caller never mistakes a transient error for "config OK".
        assert result["config_check"]["is_valid"] is False
        assert result["config_check"]["result"] == "unknown"
        assert result["config_check"]["errors"] == []

    @pytest.mark.asyncio
    async def test_config_check_non_dict_check_config_result_embeds_error(self):
        """client.check_config() returning None (non-dict) is caught and embedded
        as an error, not raised (defensive boundary the old tool also had)."""
        client = MagicMock()
        client.check_config = AsyncMock(return_value=None)
        tools = SystemTools(client)
        with _patch_health_info_baseline():
            result = await tools.ha_get_system_health(include="config_check")
        assert result.get("success") is True
        assert "error" in result["config_check"]
        assert result["config_check"]["is_valid"] is False

    @pytest.mark.asyncio
    async def test_config_check_returns_even_when_health_ws_unavailable(self):
        """If the system_health WebSocket baseline fails, config_check (pure REST)
        must STILL return — it must not be sunk by the WS dependency. This is the
        graceful-degradation guarantee that keeps config_check as robust as the
        REST-only ha_check_config tool it replaced."""
        client = MagicMock()
        client.check_config = AsyncMock(return_value={"result": "valid"})
        tools = SystemTools(client)
        with patch.object(
            SystemTools,
            "_fetch_health_info",
            new=AsyncMock(side_effect=ToolError("system_health WebSocket down")),
        ):
            result = await tools.ha_get_system_health(include="config_check")
        assert result["success"] is True
        assert result["config_check"]["is_valid"] is True
        assert result["config_check"]["result"] == "valid"
        client.check_config.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ws_backed_section_reported_unavailable_when_health_fails(self):
        """When the health WS baseline fails but a REST section is also
        requested, a requested WS-backed section (repairs) is reported with a
        machine-readable error sub-dict (and a summary warning) rather than
        crashing the tool, while the REST config_check still returns."""
        client = MagicMock()
        client.check_config = AsyncMock(return_value={"result": "valid"})
        tools = SystemTools(client)
        with patch.object(
            SystemTools,
            "_fetch_health_info",
            new=AsyncMock(side_effect=ToolError("system_health WebSocket down")),
        ):
            result = await tools.ha_get_system_health(include="repairs,config_check")
        assert result["success"] is True
        assert result["baseline_available"] is False
        assert result["config_check"]["is_valid"] is True
        # Dropped WS section carries the same {error} shape it would if the
        # baseline were up and the fetch itself failed — not a vanished key.
        assert "error" in result["repairs"]
        assert any("repairs" in w for w in result.get("warnings", []))

    @pytest.mark.asyncio
    async def test_themes_section_reported_unavailable_when_ws_fails(self):
        """include='themes' with WS baseline failure embeds error in themes."""
        client = MagicMock()
        client.check_config = AsyncMock(return_value={"result": "valid"})
        tools = SystemTools(client)
        with patch.object(
            SystemTools,
            "_fetch_health_info",
            new=AsyncMock(side_effect=ToolError("system_health WebSocket down")),
        ):
            result = await tools.ha_get_system_health(include="themes,config_check")
        assert result["success"] is True
        assert "error" in result["themes"]
        assert "WebSocket" in result["themes"]["error"]

    @pytest.mark.asyncio
    async def test_bare_call_raises_when_health_ws_unavailable(self):
        """A bare ha_get_system_health() (the health baseline IS the deliverable)
        must still RAISE on a WS-baseline failure — degradation only applies when
        a REST-only section was requested. Guards against regressing the tool's
        primary mode into a misleading success."""
        client = MagicMock()
        tools = SystemTools(client)
        with (
            patch.object(
                SystemTools,
                "_fetch_health_info",
                new=AsyncMock(side_effect=ToolError("system_health WebSocket down")),
            ),
            pytest.raises(ToolError),
        ):
            await tools.ha_get_system_health()

    @pytest.mark.asyncio
    async def test_ws_only_request_raises_when_health_ws_unavailable(self):
        """include='repairs' (WS-only, no REST section) must RAISE on a
        WS-baseline failure rather than degrade to success — the requested data
        genuinely cannot be served without the WebSocket."""
        client = MagicMock()
        tools = SystemTools(client)
        with (
            patch.object(
                SystemTools,
                "_fetch_health_info",
                new=AsyncMock(side_effect=ToolError("system_health WebSocket down")),
            ),
            pytest.raises(ToolError),
        ):
            await tools.ha_get_system_health(include="repairs")

    @pytest.mark.asyncio
    async def test_diagnostics_returns_when_health_ws_unavailable(self):
        """diagnostics is REST-based, so it too survives a WS-baseline failure —
        the degradation guarantee is not specific to config_check."""
        client = MagicMock()
        tools = SystemTools(client)
        with (
            patch.object(
                SystemTools,
                "_fetch_health_info",
                new=AsyncMock(side_effect=ToolError("system_health WebSocket down")),
            ),
            patch(
                "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
                new=AsyncMock(return_value={"data": {"ok": True}}),
            ) as mock_diag,
        ):
            result = await tools.ha_get_system_health(
                include="diagnostics", config_entry_id="entry_abc"
            )
        assert result["success"] is True
        assert result["baseline_available"] is False
        assert result["diagnostics"] == {"data": {"ok": True}}
        mock_diag.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_config_check_combined_with_repairs(self):
        """include='repairs,config_check' populates both — proves the standalone
        ``if`` is not mutually exclusive with the ws-gather sections."""
        client = MagicMock()
        client.check_config = AsyncMock(return_value={"result": "valid"})
        tools = SystemTools(client)
        with (
            _patch_health_info_baseline(),
            patch.object(
                SystemTools,
                "_fetch_repairs",
                new=AsyncMock(return_value={"issues": [], "count": 0}),
            ),
        ):
            result = await tools.ha_get_system_health(include="repairs,config_check")
        assert "repairs" in result
        assert "config_check" in result
        assert result["config_check"]["is_valid"] is True


def _make_dead_entities_client(
    states,
    *,
    registry=None,
    entries=None,
    registry_resp=None,
    entries_resp=None,
    states_exc=None,
    registry_exc=None,
    entries_exc=None,
):
    """Build a mock client for ``_fetch_dead_entities``.

    ``get_states`` returns ``states`` (or raises ``states_exc``).
    ``send_websocket_message`` dispatches on the message ``type`` and returns
    the HA envelope for the registry / config-entries lists. Pass
    ``registry_resp`` / ``entries_resp`` to inject a raw (e.g. failure)
    envelope, or ``registry_exc`` / ``entries_exc`` to make the WS call
    raise (covering the fatal-propagation paths).
    """
    client = MagicMock()
    if states_exc is not None:
        client.get_states = AsyncMock(side_effect=states_exc)
    else:
        client.get_states = AsyncMock(return_value=states)

    async def _ws(message):
        msg_type = message.get("type")
        if msg_type == "config/entity_registry/list":
            if registry_exc is not None:
                raise registry_exc
            if registry_resp is not None:
                return registry_resp
            return {"success": True, "result": registry or []}
        if msg_type == "config_entries/get":
            if entries_exc is not None:
                raise entries_exc
            if entries_resp is not None:
                return entries_resp
            return {"success": True, "result": entries or []}
        return {"success": False, "error": f"unexpected ws message: {msg_type}"}

    client.send_websocket_message = AsyncMock(side_effect=_ws)
    return client


def _state(entity_id, state, *, restored=None):
    """Build a minimal state-machine object."""
    attrs = {}
    if restored is not None:
        attrs["restored"] = restored
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


class TestFetchDeadEntities:
    """``_fetch_dead_entities`` diffs the registry against states + config
    entries and classifies orphaned/stale registry entries (issue #1578)."""

    @pytest.mark.asyncio
    async def test_config_entry_orphan_surfaces(self):
        """A registry entry whose config_entry_id is not in the live entries set
        is a definitive orphan, regardless of whether it still has a state."""
        client = _make_dead_entities_client(
            states=[_state("sensor.ghost", "unavailable", restored=True)],
            registry=[
                {
                    "entity_id": "sensor.ghost",
                    "platform": "pi_hole",
                    "config_entry_id": "gone_entry",
                    "disabled_by": None,
                }
            ],
            entries=[{"entry_id": "live_entry", "domain": "hue"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        orphans = result["config_entry_orphans"]["items"]
        assert len(orphans) == 1
        assert orphans[0]["entity_id"] == "sensor.ghost"
        assert orphans[0]["config_entry_id"] == "gone_entry"
        assert orphans[0]["has_state"] is True
        # An orphan must NOT be double-counted under stale_restored.
        assert result["stale_restored"]["items"] == []
        assert result["config_entries_checked"] is True
        assert result["summary"]["candidate_total"] == 1

    @pytest.mark.asyncio
    async def test_stale_restored_surfaces(self):
        """unavailable + restored, with the owning config entry still alive,
        classifies as stale_restored (not orphan)."""
        client = _make_dead_entities_client(
            states=[_state("sensor.stale", "unavailable", restored=True)],
            registry=[
                {
                    "entity_id": "sensor.stale",
                    "platform": "mobile_app",
                    "config_entry_id": "live_entry",
                    "disabled_by": None,
                }
            ],
            entries=[{"entry_id": "live_entry", "domain": "mobile_app"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert result["config_entry_orphans"]["items"] == []
        stale = result["stale_restored"]["items"]
        assert len(stale) == 1
        assert stale[0]["entity_id"] == "sensor.stale"

    @pytest.mark.asyncio
    async def test_unknown_state_never_flagged(self):
        """An 'unknown'-state entity (alive, just no current value — e.g. a
        disaster-alert sensor) must never be flagged. This is the core
        false-positive guard from issue #1578."""
        client = _make_dead_entities_client(
            states=[_state("sensor.disaster_alert", "unknown")],
            registry=[
                {
                    "entity_id": "sensor.disaster_alert",
                    "platform": "weather",
                    "config_entry_id": "live_entry",
                    "disabled_by": None,
                }
            ],
            entries=[{"entry_id": "live_entry", "domain": "weather"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert result["config_entry_orphans"]["items"] == []
        assert result["stale_restored"]["items"] == []
        assert result["summary"]["candidate_total"] == 0

    @pytest.mark.asyncio
    async def test_bare_unavailable_without_restored_not_flagged(self):
        """A loaded integration reporting a device merely offline right now
        (unavailable WITHOUT restored) is alive — must not be flagged."""
        client = _make_dead_entities_client(
            states=[_state("light.offline_now", "unavailable")],
            registry=[
                {
                    "entity_id": "light.offline_now",
                    "platform": "hue",
                    "config_entry_id": "live_entry",
                    "disabled_by": None,
                }
            ],
            entries=[{"entry_id": "live_entry", "domain": "hue"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert result["stale_restored"]["items"] == []
        assert result["config_entry_orphans"]["items"] == []

    @pytest.mark.asyncio
    async def test_non_dict_attributes_does_not_crash(self):
        """A malformed state whose `attributes` is not a dict must be skipped,
        not raise AttributeError on `.get()` — guards the isinstance check."""
        client = _make_dead_entities_client(
            states=[
                {
                    "entity_id": "sensor.weird",
                    "state": "unavailable",
                    "attributes": "not-a-dict",
                }
            ],
            registry=[
                {
                    "entity_id": "sensor.weird",
                    "platform": "hue",
                    "config_entry_id": "live_entry",
                    "disabled_by": None,
                }
            ],
            entries=[{"entry_id": "live_entry", "domain": "hue"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert "error" not in result
        assert result["stale_restored"]["items"] == []

    @pytest.mark.asyncio
    async def test_disabled_entity_with_live_entry_not_flagged(self):
        """An intentionally-disabled entry whose config entry still exists is a
        user choice, not dead — excluded from both tiers."""
        client = _make_dead_entities_client(
            states=[],
            registry=[
                {
                    "entity_id": "sensor.disabled",
                    "platform": "hue",
                    "config_entry_id": "live_entry",
                    "disabled_by": "user",
                }
            ],
            entries=[{"entry_id": "live_entry", "domain": "hue"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert result["config_entry_orphans"]["items"] == []
        assert result["stale_restored"]["items"] == []

    @pytest.mark.asyncio
    async def test_disabled_orphan_still_surfaces_with_disabled_by(self):
        """A disabled entry whose config entry is ALSO gone is still dead cruft —
        surfaces as an orphan with disabled_by preserved for client context."""
        client = _make_dead_entities_client(
            states=[],
            registry=[
                {
                    "entity_id": "sensor.disabled_orphan",
                    "platform": "removed_integration",
                    "config_entry_id": "gone_entry",
                    "disabled_by": "integration",
                }
            ],
            entries=[{"entry_id": "live_entry", "domain": "hue"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        orphans = result["config_entry_orphans"]["items"]
        assert len(orphans) == 1
        assert orphans[0]["disabled_by"] == "integration"
        assert orphans[0]["has_state"] is False

    @pytest.mark.asyncio
    async def test_config_entries_unavailable_degrades_to_stale_only(self):
        """If config/entries/get fails, the definitive orphan tier is skipped but
        stale_restored is still computed — graceful degradation, not a hard
        failure."""
        client = _make_dead_entities_client(
            states=[_state("sensor.stale", "unavailable", restored=True)],
            registry=[
                {
                    "entity_id": "sensor.stale",
                    "platform": "mobile_app",
                    "config_entry_id": "some_entry",
                    "disabled_by": None,
                }
            ],
            entries_resp={"success": False, "error": "ws down"},
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert result["config_entries_checked"] is False
        assert result["config_entry_orphans"]["items"] == []
        assert len(result["stale_restored"]["items"]) == 1
        # The section returns warnings under the ``_warnings`` sentinel; the
        # aggregator (``ha_get_system_health``) bubbles them to top-level
        # ``result["warnings"]``. Direct-call tests assert the sentinel; the
        # bubbling contract is covered by an integration test below.
        assert any("config_entry_orphans" in w for w in result["_warnings"])
        # A genuine fetch failure (success=false) preserves the envelope
        # error string rather than substituting a fixed "no entries" message.
        assert any("ws down" in w for w in result["_warnings"])
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_registry_unavailable_is_section_error(self):
        """The registry is the foundational source — if it can't be fetched, the
        section returns an embedded error (never raises)."""
        client = _make_dead_entities_client(
            states=[],
            registry_resp={"success": False, "error": "ws down"},
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert "error" in result
        assert "registry" in result["error"]

    @pytest.mark.asyncio
    async def test_states_unavailable_is_section_error(self):
        """A states-fetch failure surfaces as an embedded section error, not an
        unhandled raise."""
        client = _make_dead_entities_client(
            states=None,
            states_exc=RuntimeError("states boom"),
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert "error" in result
        assert "states" in result["error"]

    @pytest.mark.asyncio
    async def test_fatal_cancellation_propagates_not_embedded(self):
        """A CancelledError from a source fetch must unwind, not be demoted to a
        section error string — guards the _reraise_if_fatal pre-pass."""
        client = _make_dead_entities_client(
            states=None,
            states_exc=asyncio.CancelledError(),
        )
        with pytest.raises(asyncio.CancelledError):
            await SystemTools(client)._fetch_dead_entities()

    @pytest.mark.asyncio
    async def test_tool_error_propagates_not_embedded(self):
        """A ToolError from a source fetch must propagate (MCP isError
        contract), not be demoted to a section error string by the outer
        except — guards the `except ToolError: raise` chain order."""
        client = _make_dead_entities_client(
            states=None,
            states_exc=ToolError("boom"),
        )
        with pytest.raises(ToolError):
            await SystemTools(client)._fetch_dead_entities()

    @pytest.mark.asyncio
    async def test_orphan_bucket_truncates_at_limit(self):
        """The orphan bucket caps + flags truncation independently of the stale
        bucket (the two tiers populate via different code paths)."""
        registry = [
            {
                "entity_id": f"sensor.orphan_{i}",
                "platform": "removed",
                "config_entry_id": "gone_entry",
                "disabled_by": None,
            }
            for i in range(60)
        ]
        client = _make_dead_entities_client(
            states=[],
            registry=registry,
            entries=[{"entry_id": "live_entry", "domain": "hue"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        bucket = result["config_entry_orphans"]
        assert bucket["count"] == 50
        assert bucket["total_count"] == 60
        assert bucket["truncated"] is True
        # The actionable cleanup guidance must (a) be present, (b) name
        # both cap and total in the documented ``"Showing X of Y"`` form
        # so a client can render the truncation context, and (c) carry
        # the "remove ... batches" instruction so the user knows the
        # cleanup is iterative — a wrong-template copy-paste from the
        # ZHA truncation hint would fail this.
        assert "hint" in bucket
        assert "50 of 60" in bucket["hint"]
        assert "remove" in bucket["hint"].lower()

    @pytest.mark.asyncio
    async def test_entity_without_config_entry_id_classifies_as_stale(self):
        """A restored-unavailable entry with config_entry_id=None (e.g. a
        YAML/helper entity its provider no longer supplies) is not an orphan
        (no config entry to be missing) but still surfaces as stale_restored,
        carrying config_entry_id: None."""
        client = _make_dead_entities_client(
            states=[_state("sensor.no_cfg", "unavailable", restored=True)],
            registry=[
                {
                    "entity_id": "sensor.no_cfg",
                    "platform": "template",
                    "config_entry_id": None,
                    "disabled_by": None,
                }
            ],
            entries=[{"entry_id": "live_entry", "domain": "hue"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert result["config_entry_orphans"]["items"] == []
        stale = result["stale_restored"]["items"]
        assert len(stale) == 1
        assert stale[0]["entity_id"] == "sensor.no_cfg"
        assert stale[0]["config_entry_id"] is None

    @pytest.mark.asyncio
    async def test_empty_sources_return_zero_counts(self):
        """All sources empty-but-successful → baseline structure with zeros, no
        error, no crash on len(). Empty config-entries degrades the orphan tier
        (cannot distinguish a removed integration from an empty fetch), and the
        guidance note is omitted when there is nothing to act on."""
        client = _make_dead_entities_client(states=[], registry=[], entries=[])
        result = await SystemTools(client)._fetch_dead_entities()

        assert "error" not in result
        assert result["config_entry_orphans"]["count"] == 0
        assert result["stale_restored"]["count"] == 0
        assert result["summary"] == {"candidate_total": 0, "registry_total": 0}
        assert result["config_entries_checked"] is False
        assert "note" not in result

    @pytest.mark.asyncio
    async def test_empty_config_entries_skips_orphan_tier(self):
        """An empty-but-successful config_entries/get must NOT flag every
        cfg-linked registry entry as an orphan — empty is treated like a failed
        fetch (it is indistinguishable from a backend hiccup), so the orphan
        tier is skipped rather than producing a mass false positive."""
        client = _make_dead_entities_client(
            states=[],
            registry=[
                {
                    "entity_id": "sensor.has_cfg",
                    "platform": "hue",
                    "config_entry_id": "some_entry",
                    "disabled_by": None,
                }
            ],
            entries=[],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert result["config_entry_orphans"]["items"] == []
        assert result["config_entries_checked"] is False
        # Direct-call asserts the ``_warnings`` sentinel; integration test
        # below covers the bubble to top-level ``result["warnings"]``.
        assert any("config_entry_orphans" in w for w in result["_warnings"])
        # Real empty list — distinct from a fetch failure, which would
        # carry the underlying error string. Asserts the two paths produce
        # distinct messages (the "empty list" vs "failed" branching).
        assert any("empty list" in w for w in result["_warnings"])

    @pytest.mark.asyncio
    async def test_note_present_when_candidates_found(self):
        """The guidance note is attached when there is at least one candidate."""
        client = _make_dead_entities_client(
            states=[_state("sensor.stale", "unavailable", restored=True)],
            registry=[
                {
                    "entity_id": "sensor.stale",
                    "platform": "mobile_app",
                    "config_entry_id": "live_entry",
                    "disabled_by": None,
                }
            ],
            entries=[{"entry_id": "live_entry", "domain": "mobile_app"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        assert result["summary"]["candidate_total"] == 1
        assert "ha_remove_entity" in result["note"]

    @pytest.mark.asyncio
    async def test_entities_sharing_config_entry_classified_independently(self):
        """Set-membership is per-entity: two entries on a live entry are not
        orphans, a third on a gone entry is — no first-match-wins bug."""
        client = _make_dead_entities_client(
            states=[],
            registry=[
                {
                    "entity_id": "light.a",
                    "platform": "hue",
                    "config_entry_id": "live_entry",
                    "disabled_by": None,
                },
                {
                    "entity_id": "light.b",
                    "platform": "hue",
                    "config_entry_id": "live_entry",
                    "disabled_by": None,
                },
                {
                    "entity_id": "light.c",
                    "platform": "removed",
                    "config_entry_id": "gone_entry",
                    "disabled_by": None,
                },
            ],
            entries=[{"entry_id": "live_entry", "domain": "hue"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        orphans = result["config_entry_orphans"]["items"]
        assert len(orphans) == 1
        assert orphans[0]["entity_id"] == "light.c"

    @pytest.mark.asyncio
    async def test_stale_bucket_truncates_at_limit(self):
        """The stale bucket caps its item list and reports truncation + totals so
        large installs stay token-friendly."""
        registry = [
            {
                "entity_id": f"sensor.stale_{i}",
                "platform": "mobile_app",
                "config_entry_id": "live_entry",
                "disabled_by": None,
            }
            for i in range(60)
        ]
        states = [
            _state(f"sensor.stale_{i}", "unavailable", restored=True) for i in range(60)
        ]
        client = _make_dead_entities_client(
            states=states,
            registry=registry,
            entries=[{"entry_id": "live_entry", "domain": "mobile_app"}],
        )
        result = await SystemTools(client)._fetch_dead_entities()

        bucket = result["stale_restored"]
        assert bucket["count"] == 50
        assert bucket["total_count"] == 60
        assert bucket["truncated"] is True
        # Same template assertions as the orphan bucket so a copy-paste
        # error swapping the two buckets' hints is caught.
        assert "hint" in bucket
        assert "50 of 60" in bucket["hint"]
        assert "remove" in bucket["hint"].lower()
        assert result["summary"]["candidate_total"] == 60

    @pytest.mark.asyncio
    async def test_uses_canonical_ws_command_names(self):
        """Pin the exact HA WebSocket command strings. A wrong name (e.g.
        'config/entries/get' with a slash instead of 'config_entries/get')
        returns 'Unknown command' against a real HA and silently degrades the
        orphan tier — but a mock keyed to the wrong name would still pass. This
        asserts the real names are sent so a typo is caught without live HA."""
        client = _make_dead_entities_client(
            states=[],
            registry=[],
            entries=[],
        )
        await SystemTools(client)._fetch_dead_entities()

        sent_types = {
            call.args[0].get("type")
            for call in client.send_websocket_message.call_args_list
        }
        assert "config/entity_registry/list" in sent_types
        assert "config_entries/get" in sent_types

    @pytest.mark.asyncio
    async def test_dead_entities_via_tool_routes_to_helper(self):
        """include='dead_entities' routes to the helper and embeds its result."""
        client = MagicMock()
        tools = SystemTools(client)
        payload = {"summary": {"candidate_total": 0, "registry_total": 0}}
        with (
            _patch_health_info_baseline(),
            patch.object(
                SystemTools,
                "_fetch_dead_entities",
                new=AsyncMock(return_value=payload),
            ) as mock_dead,
        ):
            result = await tools.ha_get_system_health(include="dead_entities")
        assert result["dead_entities"] == payload
        mock_dead.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dead_entities_returns_when_health_ws_unavailable(self):
        """dead_entities is REST-based, so it survives a WS-baseline failure —
        mirrors the config_check / diagnostics degradation guarantee."""
        client = MagicMock()
        tools = SystemTools(client)
        payload = {"summary": {"candidate_total": 0, "registry_total": 0}}
        with (
            patch.object(
                SystemTools,
                "_fetch_health_info",
                new=AsyncMock(side_effect=ToolError("system_health WebSocket down")),
            ),
            patch.object(
                SystemTools,
                "_fetch_dead_entities",
                new=AsyncMock(return_value=payload),
            ),
        ):
            result = await tools.ha_get_system_health(include="dead_entities")
        assert result["success"] is True
        assert result["baseline_available"] is False
        assert result["dead_entities"] == payload


class TestWsResultList:
    """Edge cases for ``SystemTools._ws_result_list``: the unwrap shape is a
    tuple ``(list | None, error_str | None)``, preserving the underlying cause
    so the caller can attribute a failure rather than substitute a fixed
    "unavailable" message that hides whether it was auth, command, or
    malformed envelope. The recoverable-Exception branch, the non-dict
    response, and the success-but-result-not-a-list branches were previously
    only exercised through ``_fetch_dead_entities``; pinning them directly
    documents the contract and guards against a refactor regressing it."""

    def test_recoverable_exception_returns_cause_string(self):
        """A bare ``Exception`` from ``gather`` is recoverable (per
        ``_reraise_if_fatal`` policy): returns ``(None, cause)`` with the
        exception class name + message preserved."""
        data, err = SystemTools._ws_result_list(RuntimeError("ws boom"))
        assert data is None
        assert err is not None
        assert "RuntimeError" in err
        assert "ws boom" in err

    def test_non_dict_response_reports_unexpected_type(self):
        """A non-dict envelope (e.g. a list slipped through) is a protocol
        violation worth surfacing in the cause string, not a silent
        ``None`` substitution."""
        data, err = SystemTools._ws_result_list(["unexpected", "shape"])
        assert data is None
        assert err is not None
        assert "list" in err

    def test_success_false_envelope_preserves_error_message(self):
        """An HA error envelope (``{"success": False, "error": {...}}``)
        carries the actual cause (auth, command name, validation) — the
        caller surfaces it instead of a fixed "unavailable" substitute."""
        resp = {
            "success": False,
            "error": {"code": "unknown_command", "message": "Unknown command."},
        }
        data, err = SystemTools._ws_result_list(resp)
        assert data is None
        assert err == "Unknown command."

    def test_success_false_envelope_code_only_falls_back_to_code(self):
        """When the envelope error dict has only ``code`` (no ``message``),
        the code is still preserved as the cause — better than substituting
        a generic "unknown error" and losing the discriminator."""
        data, err = SystemTools._ws_result_list(
            {"success": False, "error": {"code": "auth_failed"}}
        )
        assert data is None
        assert err == "auth_failed"

    def test_success_false_envelope_unknown_shape_uses_str_repr(self):
        """A dict-error envelope with neither ``message`` nor ``code`` still
        surfaces something attributable to the client — degrades to
        ``str(err)`` so the discriminating field reaches the cause string."""
        data, err = SystemTools._ws_result_list(
            {"success": False, "error": {"detail": "weird"}}
        )
        assert data is None
        assert err is not None
        assert "weird" in err

    def test_success_false_string_error_preserved(self):
        """Some envelopes report ``error`` as a plain string; that path must
        also survive (the envelope-dict branch can't claim it)."""
        data, err = SystemTools._ws_result_list({"success": False, "error": "ws down"})
        assert data is None
        assert err == "ws down"

    def test_success_false_no_error_field_defaults_to_unknown(self):
        """A malformed envelope without ``error`` shouldn't crash; degrade
        to a sentinel string so the caller can still attribute the failure."""
        data, err = SystemTools._ws_result_list({"success": False})
        assert data is None
        assert err == "unknown error"

    def test_success_but_result_not_a_list_reports_wrong_shape(self):
        """``success=True`` with a non-list ``result`` (e.g. a dict from a
        sibling command shape) is reportable as a wrong-shape failure."""
        data, err = SystemTools._ws_result_list(
            {"success": True, "result": {"unexpected": "dict"}}
        )
        assert data is None
        assert err is not None
        assert "dict" in err

    def test_success_with_list_result_returns_data_and_no_error(self):
        """Happy path: list returned as data, error slot is ``None``."""
        data, err = SystemTools._ws_result_list(
            {"success": True, "result": [{"id": 1}, {"id": 2}]}
        )
        assert data == [{"id": 1}, {"id": 2}]
        assert err is None

    def test_cancelled_error_unwinds_not_returned(self):
        """Per ``_reraise_if_fatal``: cancellation must propagate; the
        function never returns ``(None, ...)`` for it."""
        with pytest.raises(asyncio.CancelledError):
            SystemTools._ws_result_list(asyncio.CancelledError())

    def test_home_assistant_connection_error_unwinds_not_returned(self):
        """Transport-dead errors propagate via ``_reraise_if_fatal`` (the
        #1624 policy folded into this PR) — once the HA connection is dead
        the remaining section fetches will fail anyway, so a propagated
        root cause beats N per-section embedded errors."""
        with pytest.raises(HomeAssistantConnectionError):
            SystemTools._ws_result_list(HomeAssistantConnectionError("transport dead"))


class TestRegistryFailurePreservesCause:
    """The ``registry`` branch in ``_fetch_dead_entities`` previously
    substituted a fixed ``"config/entity_registry/list unavailable"`` string
    and discarded ``resp.get("error")``. The new contract preserves the
    envelope cause so a client can distinguish auth vs command-error vs
    malformed envelope."""

    @pytest.mark.asyncio
    async def test_registry_envelope_error_preserved_in_section_error(self):
        client = _make_dead_entities_client(
            states=[],
            registry_resp={
                "success": False,
                "error": {"code": "auth_failed", "message": "Auth required."},
            },
        )
        result = await SystemTools(client)._fetch_dead_entities()
        assert "error" in result
        # The original envelope message is preserved (rather than masked as
        # "unavailable"), so the cause class is attributable from the dict.
        assert "Auth required." in result["error"]

    @pytest.mark.asyncio
    async def test_registry_exception_class_preserved_in_section_error(self):
        client = _make_dead_entities_client(
            states=[],
            registry_resp=None,
            registry_exc=RuntimeError("transport hiccup"),
        )
        result = await SystemTools(client)._fetch_dead_entities()
        assert "error" in result
        assert "RuntimeError" in result["error"]
        assert "transport hiccup" in result["error"]


class TestConfigEntriesFailureVsEmpty:
    """The config-entries branch previously folded the genuine-fetch-failure
    case and the truly-empty-list case under the same "returned no entries"
    message. The new contract distinguishes them so a backend failure isn't
    reported as "no entries"."""

    @pytest.mark.asyncio
    async def test_failure_message_carries_underlying_cause(self):
        client = _make_dead_entities_client(
            states=[],
            registry=[],
            entries_resp={
                "success": False,
                "error": {"code": "unknown_command", "message": "Bad."},
            },
        )
        result = await SystemTools(client)._fetch_dead_entities()
        # The bubbled warning surfaces the cause string rather than the
        # generic "no entries" message — i.e. distinguishable from the
        # empty-list path below.
        assert "_warnings" in result
        msg = next(iter(result["_warnings"]))
        assert "failed" in msg
        assert "Bad." in msg

    @pytest.mark.asyncio
    async def test_empty_list_message_names_the_empty_state_not_a_failure(self):
        client = _make_dead_entities_client(states=[], registry=[], entries=[])
        result = await SystemTools(client)._fetch_dead_entities()
        assert "_warnings" in result
        msg = next(iter(result["_warnings"]))
        # Distinguishable from the failure path: this branch names the
        # actual state ("empty list"/"no integrations configured") rather
        # than reporting a fetch failure with a cause string.
        assert "empty list" in msg
        assert "failed" not in msg


class TestWarningsBubbleToTopLevel:
    """Section-emitted warnings must surface under the top-level
    ``result["warnings"]`` (the documented contract location), not on the
    section dict's reserved-term-colliding ``warnings`` key. The section
    helper uses a ``_warnings`` sentinel that the aggregator pops + appends
    to ``result["warnings"]``."""

    @pytest.mark.asyncio
    async def test_dead_entities_warnings_bubble_to_result_warnings(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value={"success": True})
        tools = SystemTools(client)
        section_payload = {
            "summary": {"candidate_total": 0, "registry_total": 0},
            "_warnings": ["config_entries/get failed (boom); tier skipped."],
        }
        with (
            _patch_health_info_baseline(),
            patch.object(
                SystemTools,
                "_fetch_dead_entities",
                new=AsyncMock(return_value=section_payload),
            ),
        ):
            result = await tools.ha_get_system_health(include="dead_entities")
        # The sentinel is stripped from the section dict so a client never
        # sees the internal key.
        assert "_warnings" not in result["dead_entities"]
        # And the message lands under the documented contract location.
        assert any("config_entries/get failed" in w for w in result.get("warnings", []))

    @pytest.mark.asyncio
    async def test_dead_entities_without_warnings_does_not_create_warnings_key(self):
        """No warnings ⇒ no spurious top-level ``warnings`` key (avoids
        adding noise to the aggregator output)."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value={"success": True})
        tools = SystemTools(client)
        section_payload = {"summary": {"candidate_total": 0, "registry_total": 0}}
        with (
            _patch_health_info_baseline(),
            patch.object(
                SystemTools,
                "_fetch_dead_entities",
                new=AsyncMock(return_value=section_payload),
            ),
        ):
            result = await tools.ha_get_system_health(include="dead_entities")
        # No-warnings happy path: no spurious top-level key. (The
        # aggregator may still add unrelated warnings for orphan args etc.
        # — we assert specifically that no dead-entities-bubbled message
        # surfaced rather than checking the key's absence outright.)
        assert all("config_entries/get" not in w for w in result.get("warnings", []))


class TestHomeAssistantConnectionErrorPropagation:
    """#1624 policy folded into this PR: a dead HA transport propagates as
    ``isError=true`` (via ``_reraise_if_fatal``) rather than being demoted to
    N per-section embedded errors. Applies to ``_fetch_dead_entities``, every
    sibling section helper, and the ws ``sections`` gather pre-pass — the
    cross-section consistency was the whole point of the #1624 decision."""

    @pytest.mark.asyncio
    async def test_connection_error_from_states_propagates(self):
        """A ``HomeAssistantConnectionError`` from the REST states fetch
        unwinds out of ``_fetch_dead_entities`` rather than being demoted
        to a section error string."""
        client = _make_dead_entities_client(
            states=None,
            states_exc=HomeAssistantConnectionError("transport dead"),
        )
        with pytest.raises(HomeAssistantConnectionError):
            await SystemTools(client)._fetch_dead_entities()

    @pytest.mark.asyncio
    async def test_connection_error_from_registry_propagates(self):
        """Connection failure on the WS registry fetch unwinds (via
        ``_ws_result_list``'s fatal pre-pass) rather than landing as a
        section error string."""
        client = _make_dead_entities_client(
            states=[],
            registry_resp=None,
            registry_exc=HomeAssistantConnectionError("ws gone"),
        )
        with pytest.raises(HomeAssistantConnectionError):
            await SystemTools(client)._fetch_dead_entities()

    @pytest.mark.parametrize(
        "helper_name",
        [
            "_fetch_repairs",
            "_fetch_zha_network",
            "_fetch_zwave_network",
            "_fetch_themes",
        ],
    )
    @pytest.mark.asyncio
    async def test_connection_error_from_ws_section_helper_propagates(
        self, helper_name
    ):
        """Each ws-backed sibling section helper's ``except Exception`` chain
        routes through ``_reraise_if_fatal`` as its first line, so a
        connection error from one section unwinds the request rather than
        embedding N per-section errors. Parametrized across every helper
        so a regression that removes the call from one (and only one) is
        caught — the cross-section consistency #1624 demanded."""
        ws_client = MagicMock()
        ws_client.send_command = AsyncMock(
            side_effect=HomeAssistantConnectionError("ws gone")
        )
        helper = getattr(SystemTools, helper_name)
        kwargs: dict[str, Any] = {}
        if helper_name == "_fetch_zha_network":
            kwargs["full"] = False
        with pytest.raises(HomeAssistantConnectionError):
            await helper(ws_client, **kwargs)

    @pytest.mark.asyncio
    async def test_connection_error_from_config_check_propagates(self):
        """``_fetch_config_check`` is REST-based (calls
        ``self._client.check_config()``) and runs inline rather than through
        the gather pre-pass, so its propagation can't piggyback on the
        ws-section parametrize above. Pin it independently."""
        client = MagicMock()
        client.check_config = AsyncMock(
            side_effect=HomeAssistantConnectionError("rest dead")
        )
        with pytest.raises(HomeAssistantConnectionError):
            await SystemTools(client)._fetch_config_check()

    @pytest.mark.asyncio
    async def test_aggregator_pre_pass_propagates_connection_error_as_tool_error(
        self,
    ):
        """The ws ``sections`` gather pre-pass uses the same
        ``_reraise_if_fatal`` policy: a HomeAssistantConnectionError from
        any section result element unwinds the gather and is then wrapped
        by ``ha_get_system_health``'s outer ``except Exception``
        (``exception_to_structured_error``) into a ``ToolError`` with the
        ``CONNECTION_FAILED`` code — the MCP ``isError=true`` contract for
        a dead transport. Without ``_reraise_if_fatal``, the connection
        error would have been demoted to one section's ``error`` string,
        and the tool would have returned ``success=True`` with the dead
        transport silently embedded — the exact #1624 anti-pattern."""
        client = MagicMock()
        tools = SystemTools(client)
        with (
            _patch_health_info_baseline(),
            patch.object(
                SystemTools,
                "_fetch_repairs",
                new=AsyncMock(side_effect=HomeAssistantConnectionError("ws gone")),
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_get_system_health(include="repairs")
        # The structured error preserves the cause string + the
        # CONNECTION_FAILED code, so a client sees the real root cause
        # rather than a per-section embedded ``{"error": "..."}``.
        msg = str(excinfo.value)
        assert "CONNECTION_FAILED" in msg
        assert "ws gone" in msg
