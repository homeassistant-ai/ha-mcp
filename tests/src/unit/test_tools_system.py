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

        with _patch_health_info_baseline() as (_, ws_client), patch.object(
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
    @pytest.mark.parametrize(
        "raising_section",
        ["repairs", "zha_network", "zwave_network"],
    )
    async def test_one_section_raising_does_not_block_siblings(
        self, raising_section
    ):
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

        with _patch_health_info_baseline(), patch.object(
            SystemTools, "_fetch_repairs", new=mocks["repairs"]
        ), patch.object(
            SystemTools, "_fetch_zha_network", new=mocks["zha_network"]
        ), patch.object(
            SystemTools, "_fetch_zwave_network", new=mocks["zwave_network"]
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

        with _patch_health_info_baseline(), patch.object(
            SystemTools, "_fetch_repairs", new=AsyncMock(side_effect=waiting_helper)
        ), patch.object(
            SystemTools,
            "_fetch_zha_network",
            new=AsyncMock(side_effect=signalling_helper),
        ):
            result = await tools.ha_get_system_health(
                include="repairs,zha_network"
            )

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

        with _patch_health_info_baseline(), patch.object(
            SystemTools, "_fetch_repairs", new=mock_repairs
        ), patch.object(
            SystemTools, "_fetch_zha_network", new=mock_zha
        ), pytest.raises(asyncio.CancelledError):
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

        with _patch_health_info_baseline(), patch.object(
            SystemTools, "_fetch_repairs", new=mock_repairs
        ), patch.object(
            SystemTools, "_fetch_zha_network", new=mock_zha
        ), pytest.raises(ToolError) as excinfo:
            await tools.ha_get_system_health(include="repairs,zha_network")
        assert excinfo.value is tool_err

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
        with _patch_health_info_baseline(), patch(
            "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
            new=AsyncMock(return_value=diag_payload),
        ) as mock_fetch:
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
        with _patch_health_info_baseline(), patch(
            "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
            new=AsyncMock(return_value={"data": {}}),
        ) as mock_fetch:
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
        with _patch_health_info_baseline(), patch.object(
            SystemTools, "_fetch_repairs", new=mock_repairs
        ), patch(
            "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
            new=AsyncMock(return_value={"data": {"x": 1}}),
        ) as mock_diag:
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
        with _patch_health_info_baseline(), patch(
            "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
            new=AsyncMock(return_value={"data": {}}),
        ) as mock_fetch:
            await tools.ha_get_system_health(
                include="diagnostics",
                config_entry_id="entry_abc",
                diagnostics_fields="home_assistant, issues",
                diagnostics_truncate_at_bytes="20000",
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
        with _patch_health_info_baseline(), patch(
            "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
            new=AsyncMock(return_value={"data": {}}),
        ) as mock_fetch:
            await tools.ha_get_system_health(
                include="diagnostics",
                config_entry_id="entry_abc",
                diagnostics_data_path="data.devices",
                diagnostics_data_offset="10",
                diagnostics_data_limit="5",
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
        with _patch_health_info_baseline(), patch(
            "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
            new=AsyncMock(
                return_value={
                    "config_entry_id": None,
                    "error": "config_entry_id is required for diagnostics fetch.",
                }
            ),
        ) as mock_fetch:
            result = await tools.ha_get_system_health(include="diagnostics")
        # Positional config_entry_id is None, not "".
        assert mock_fetch.await_args.args[1] is None
        assert result["diagnostics"]["config_entry_id"] is None

    @pytest.mark.asyncio
    async def test_diagnostics_omitted_from_include_skips_helper(self):
        """include='repairs' (no diagnostics) must not invoke the diagnostics helper."""
        client = MagicMock()
        tools = SystemTools(client)
        with _patch_health_info_baseline(), patch.object(
            SystemTools,
            "_fetch_repairs",
            new=AsyncMock(return_value={"issues": [], "count": 0}),
        ), patch(
            "ha_mcp.tools.tools_system.fetch_integration_diagnostics",
            new=AsyncMock(),
        ) as mock_fetch:
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
        with _patch_health_info_baseline(), patch.object(
            SystemTools,
            "_fetch_repairs",
            new=AsyncMock(return_value={"issues": [], "count": 0}),
        ):
            result = await tools.ha_get_system_health(
                include="repairs",
                config_entry_id="entry_abc",
                device_id="dev_xyz",
            )
        assert "warnings" in result
        assert any(
            "config_entry_id" in w or "device_id" in w
            for w in result["warnings"]
        )

    @pytest.mark.asyncio
    async def test_orphaned_data_offset_alone_surfaces_warning(self):
        """Pure ``diagnostics_data_offset > 0`` (no other diagnostics args)
        without 'diagnostics' in include triggers the orphan-args warning —
        guards the ``data_offset_int > 0`` term in the predicate."""
        client = MagicMock()
        tools = SystemTools(client)
        with _patch_health_info_baseline(), patch.object(
            SystemTools,
            "_fetch_repairs",
            new=AsyncMock(return_value={"issues": [], "count": 0}),
        ):
            result = await tools.ha_get_system_health(
                include="repairs",
                diagnostics_data_offset=5,
            )
        assert "warnings" in result
        assert any(
            "diagnostics_data_offset" in w for w in result["warnings"]
        )

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

    @pytest.mark.asyncio
    async def test_data_limit_zero_rejected_with_validation_error(self):
        """``diagnostics_data_limit=0`` violates the ``min_value=1`` guard on
        the coerce_int_param call — surfaces as a structured validation
        error rather than slipping through as a no-op pagination window.
        Coercion was moved outside the includes branch deliberately (see
        ``tools_system.py`` comment); a regression putting it back would
        skip validation for no-include callers."""
        client = MagicMock()
        tools = SystemTools(client)
        with _patch_health_info_baseline(), pytest.raises(ToolError) as excinfo:
            await tools.ha_get_system_health(
                include="diagnostics",
                config_entry_id="entry_abc",
                diagnostics_data_limit=0,
            )
        msg = str(excinfo.value)
        assert "diagnostics_data_limit" in msg
        assert "min" in msg.lower() or "must be" in msg.lower()
