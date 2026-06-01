"""Unit tests for tools_system module.

Regression tests for https://github.com/homeassistant-ai/ha-mcp/issues/612
ha_restart reports failure when a reverse proxy returns 504 during restart.
"""

import asyncio
import json
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
