"""Unit tests for the server-side HACS auto-refresh nudge.

On a startup where the version picture changed, the server asks HACS to
re-fetch the ha_mcp_tools component's repository info so an external install
(add-on / Docker / pip / stdio) surfaces the paired component update instead of
waiting out HACS's ~48h custom-repository cache.

The WebSocket layer is mocked and the marker file is redirected at a tmp dir;
HACS itself is never involved. No test may sleep — the one covering the retry
schedule empties it first.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp import hacs_auto_refresh
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.config import OAUTH_MODE_TOKEN
from ha_mcp.update_check import UpdateInfo

MIRROR, LEGACY = hacs_auto_refresh.CANDIDATE_REPO_FULL_NAMES


def _info(latest="1.1.0", *, update_available=True, current="1.0.0"):
    return UpdateInfo(current=current, latest=latest, update_available=update_available)


def _repo(repo_id, full_name, *, installed=True):
    """One entry of HACS's ``hacs/repositories/list`` result."""
    return {"id": repo_id, "full_name": full_name, "installed": installed}


def _ws(repos=(), error=None):
    """A WS client whose ``hacs/repositories/list`` returns ``repos``, or raises."""
    ws = AsyncMock()
    if error is not None:
        ws.send_command = AsyncMock(side_effect=error)
    else:
        ws.send_command = AsyncMock(
            return_value={"success": True, "result": list(repos)}
        )
    return ws


HA_URL = "http://ha.local:8123"


@contextmanager
def _server(
    ws, *, info=None, version="1.0.0", embedded=False, disabled=False, oauth=False
):
    """Patch the startup gates, the WS client factory and the HACS refresh call."""
    get_ws = AsyncMock(return_value=ws)
    refresh = AsyncMock(return_value={"success": True})
    settings = SimpleNamespace(
        homeassistant_token=OAUTH_MODE_TOKEN if oauth else "a-real-llat",
        homeassistant_url=HA_URL,
    )
    with (
        patch("ha_mcp.hacs_auto_refresh.is_embedded", return_value=embedded),
        patch(
            "ha_mcp.hacs_auto_refresh.is_update_check_disabled", return_value=disabled
        ),
        patch("ha_mcp.hacs_auto_refresh.get_global_settings", return_value=settings),
        patch("ha_mcp.hacs_auto_refresh.get_version", return_value=version),
        patch("ha_mcp.hacs_auto_refresh.get_update_info", return_value=info),
        patch(
            "ha_mcp.tools.hacs_registration.send_hacs_repository_refresh", new=refresh
        ),
        patch("ha_mcp.client.websocket_client.get_websocket_client", new=get_ws),
    ):
        yield SimpleNamespace(get_websocket_client=get_ws, refresh=refresh)


@pytest.fixture
def data_dir(tmp_path):
    """Redirect the marker file at a tmp dir (patched at the module's import site)."""
    with patch("ha_mcp.hacs_auto_refresh.get_data_dir", return_value=tmp_path):
        yield tmp_path


def _written_marker(data_dir, ha_url=HA_URL):
    path = hacs_auto_refresh._marker_path(ha_url)
    return json.loads(path.read_text()) if path.exists() else None


def _refreshed_ids(mocks):
    return [call.args[1] for call in mocks.refresh.await_args_list]


def _marker(version="1.0.0", latest="1.0.0", **extra):
    return {"server_version": version, "latest": latest, **extra}


class TestNudgeDue:
    def test_no_marker_is_due(self):
        assert hacs_auto_refresh._nudge_due("1.0.0", _info(), None) is True

    def test_changed_server_version_is_due(self):
        # The just-updated case: the paired component release is what HACS
        # needs to surface.
        marker = _marker(version="0.9.0", latest="1.1.0")
        assert hacs_auto_refresh._nudge_due("1.0.0", _info(), marker) is True

    def test_same_version_without_an_update_is_not_due(self):
        marker = _marker()
        info = _info(latest="1.0.0", update_available=False)
        assert hacs_auto_refresh._nudge_due("1.0.0", info, marker) is False

    def test_release_appearing_since_the_last_pass_is_due(self):
        marker = _marker()
        info = _info(latest="1.1.0")

        assert hacs_auto_refresh._nudge_due("1.0.0", info, marker) is True

    def test_already_recorded_latest_is_not_due(self):
        marker = _marker(latest="1.1.0")
        info = _info(latest="1.1.0")

        assert hacs_auto_refresh._nudge_due("1.0.0", info, marker) is False

    def test_missing_update_info_on_the_same_version_is_not_due(self):
        marker = _marker(latest=None)
        assert hacs_auto_refresh._nudge_due("1.0.0", None, marker) is False


def test_candidate_names_match_the_component_constants():
    # CANDIDATE_REPO_FULL_NAMES duplicates the component's constants (the
    # server package cannot import the component at runtime); this pins the
    # two sources together so a rename cannot silently strand the server
    # refresh on stale repository names. The component package pulls
    # homeassistant on import, so stub it first (same as test_hacs_nudge).
    from ._embedded_stubs import install

    install()

    from custom_components.ha_mcp_tools.const import (
        HACS_LEGACY_REPO_FULL_NAME,
        HACS_MIRROR_REPO_FULL_NAME,
    )

    assert hacs_auto_refresh.CANDIDATE_REPO_FULL_NAMES == (
        HACS_MIRROR_REPO_FULL_NAME,
        HACS_LEGACY_REPO_FULL_NAME,
    )


class TestMarkerFile:
    def test_roundtrip(self, data_dir):
        marker = {"server_version": "1.0.0", "latest": "1.1.0", "hacs": "present"}
        hacs_auto_refresh._write_marker(HA_URL, marker)

        assert hacs_auto_refresh._read_marker(HA_URL) == marker

    def test_missing_file_reads_as_none(self, data_dir):
        assert hacs_auto_refresh._read_marker(HA_URL) is None

    def test_corrupt_file_reads_as_none(self, data_dir):
        hacs_auto_refresh._marker_path(HA_URL).write_text("{not json")

        assert hacs_auto_refresh._read_marker(HA_URL) is None

    def test_non_dict_payload_reads_as_none(self, data_dir):
        hacs_auto_refresh._marker_path(HA_URL).write_text('["nope"]')

        assert hacs_auto_refresh._read_marker(HA_URL) is None

    def test_markers_are_scoped_per_ha_target(self, data_dir):
        # Two server configs sharing one data dir but targeting different
        # instances keep independent markers — neither suppresses the other's
        # nudge nor invalidates the other's marker on alternating spawns.
        hacs_auto_refresh._write_marker(HA_URL, {"server_version": "1.0.0"})
        other = "http://other-ha:8123"

        assert hacs_auto_refresh._read_marker(other) is None
        hacs_auto_refresh._write_marker(other, {"server_version": "2.0.0"})
        assert hacs_auto_refresh._read_marker(HA_URL) == {"server_version": "1.0.0"}
        assert hacs_auto_refresh._read_marker(other) == {"server_version": "2.0.0"}

    def test_trailing_slash_is_the_same_target(self, data_dir):
        hacs_auto_refresh._write_marker(HA_URL, {"server_version": "1.0.0"})

        assert hacs_auto_refresh._read_marker(HA_URL + "/") == {
            "server_version": "1.0.0"
        }


class TestMaybeRefreshHacsAfterUpdate:
    async def test_embedded_server_is_a_no_op(self, data_dir):
        # The component's own hacs_nudge covers the embedded server; running
        # both would double HACS's GitHub fetches.
        with (
            _server(_ws(), info=_info(), embedded=True) as mocks,
            patch("ha_mcp.hacs_auto_refresh._read_marker") as read_marker,
        ):
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        read_marker.assert_not_called()
        mocks.get_websocket_client.assert_not_awaited()

    async def test_disabled_update_check_is_a_no_op(self, data_dir):
        with (
            _server(_ws(), info=_info(), disabled=True) as mocks,
            patch("ha_mcp.hacs_auto_refresh._read_marker") as read_marker,
        ):
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        read_marker.assert_not_called()
        mocks.get_websocket_client.assert_not_awaited()

    async def test_oauth_mode_is_a_no_op(self, data_dir):
        # Per-user OAuth mode holds no server-level HA credential; the nudge
        # would only burn its retry schedule on auth failures.
        with (
            _server(_ws(), info=_info(), oauth=True) as mocks,
            patch("ha_mcp.hacs_auto_refresh._read_marker") as read_marker,
        ):
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        read_marker.assert_not_called()
        mocks.get_websocket_client.assert_not_awaited()

    async def test_both_installed_candidates_are_refreshed(self, data_dir, caplog):
        ws = _ws([_repo(123, MIRROR), _repo(456, LEGACY), _repo(789, "other/repo")])

        with caplog.at_level(logging.INFO), _server(ws, info=_info()) as mocks:
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        # The due-pass line logs BEFORE any WS work — the observable the
        # HAOS add-on lane greps for, so it is contract, not decoration.
        assert "HACS auto-refresh: startup pass due" in caplog.text
        # Both entries the component can be tracked under, and nothing else.
        assert _refreshed_ids(mocks) == ["123", "456"]
        assert _written_marker(data_dir) == {
            "server_version": "1.0.0",
            "latest": "1.1.0",
            "hacs": "present",
        }
        # The one info line is the operator-visible signal for this feature.
        assert f"{MIRROR}, {LEGACY}" in caplog.text

    async def test_uninstalled_candidate_is_skipped(self, data_dir):
        # Migration limbo: the mirror record is added but not downloaded, so
        # only the legacy entry has an update entity to light up.
        ws = _ws([_repo(123, MIRROR, installed=False), _repo(456, LEGACY)])

        with _server(ws, info=_info()) as mocks:
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        assert _refreshed_ids(mocks) == ["456"]

    async def test_no_installed_candidate_still_records_the_pass(self, data_dir):
        ws = _ws([_repo(789, "other/repo")])

        with _server(ws, info=_info()) as mocks:
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        mocks.refresh.assert_not_awaited()
        # "nothing to refresh" is a completed pass, so the next startup on this
        # same version does not re-ask HACS.
        assert _written_marker(data_dir)["hacs"] == "present"

    async def test_one_failing_refresh_does_not_skip_the_other(self, data_dir):
        ws = _ws([_repo(123, MIRROR), _repo(456, LEGACY)])

        with _server(ws, info=_info()) as mocks:
            mocks.refresh.side_effect = [
                HomeAssistantCommandError("Command failed: repository not found"),
                {"success": True},
            ]
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        assert _refreshed_ids(mocks) == ["123", "456"]
        assert _written_marker(data_dir)["hacs"] == "present"

    async def test_absent_hacs_is_recorded_only_after_retries_exhaust(self, data_dir):
        # unknown_command is also what a still-booting HA returns before HACS
        # registers its WS handlers, so absence must survive the whole retry
        # schedule before it is recorded. RETRY_DELAYS is trimmed (not
        # emptied) so the test proves the re-attempt without sleeping long.
        ws = _ws(
            error=HomeAssistantCommandError(
                "Command failed: unknown command: hacs/repositories/list",
                "unknown_command",
            )
        )

        with (
            _server(ws, info=_info()) as mocks,
            patch.object(hacs_auto_refresh, "RETRY_DELAYS", (0.001,)),
        ):
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        # Immediate attempt + one retry: absence was NOT accepted first try.
        assert ws.send_command.await_count == 2
        mocks.refresh.assert_not_awaited()
        assert _written_marker(data_dir)["hacs"] == "absent"

    async def test_all_refreshes_failing_leaves_the_marker_unwritten(self, data_dir):
        # list succeeded but every installed candidate's refresh failed — an
        # undetermined pass must not write the marker, or the retry schedule
        # and the next startup's pass are both cancelled.
        ws = _ws([_repo(123, MIRROR), _repo(456, LEGACY)])

        with (
            _server(ws, info=_info()) as mocks,
            patch.object(hacs_auto_refresh, "RETRY_DELAYS", ()),
        ):
            mocks.refresh.side_effect = HomeAssistantCommandTimeout("no answer")
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        assert mocks.refresh.await_count == 2
        assert _written_marker(data_dir) is None

    async def test_unreachable_hacs_leaves_the_marker_unwritten(self, data_dir, caplog):
        # Nothing was determined, so the next startup must retry — which is
        # exactly what an absent marker does. RETRY_DELAYS is emptied so the
        # test cannot sleep through the real ~8 minute schedule.
        ws = _ws(error=HomeAssistantConnectionError("websocket down"))

        with (
            caplog.at_level(logging.INFO),
            _server(ws, info=_info()) as mocks,
            patch.object(hacs_auto_refresh, "RETRY_DELAYS", ()),
        ):
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        assert ws.send_command.await_count == 1
        mocks.refresh.assert_not_awaited()
        assert _written_marker(data_dir) is None
        # The due-pass line still logged although every WS attempt failed —
        # the strongest form of "before any WebSocket work", and the property
        # the HAOS add-on lane depends on (its VM has no HACS to answer).
        assert "HACS auto-refresh: startup pass due" in caplog.text

    async def test_websocket_closure_is_retried(self, data_dir):
        # send_command re-raises raw websocket exceptions on a mid-send
        # closure (ambiguous-write contract), so they arrive here unwrapped —
        # they must consume a retry, not escape to the outer advisory catch.
        from ha_mcp._vendor import websockets

        ws = _ws(error=websockets.exceptions.WebSocketException("closed"))

        with (
            _server(ws, info=_info()) as mocks,
            patch.object(hacs_auto_refresh, "RETRY_DELAYS", (0.001,)),
        ):
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        assert ws.send_command.await_count == 2
        mocks.refresh.assert_not_awaited()
        assert _written_marker(data_dir) is None

    async def test_matching_marker_skips_the_websocket(self, data_dir, caplog):
        # The stdio hot path: a per-conversation spawn on an unchanged version
        # costs one file read, no WebSocket traffic, and no log noise.
        hacs_auto_refresh._marker_path(HA_URL).write_text(
            json.dumps(_marker(latest="1.1.0", hacs="present"))
        )
        ws = _ws([_repo(123, MIRROR)])

        with caplog.at_level(logging.INFO), _server(ws, info=_info()) as mocks:
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        mocks.get_websocket_client.assert_not_awaited()
        mocks.refresh.assert_not_awaited()
        assert "startup pass due" not in caplog.text


class TestLifespanWiring:
    async def test_lifespan_schedules_and_cancels_the_nudge(self):
        # Entering the lifespan starts the task; exiting cancels it, so a
        # nudge parked in a retry sleep cannot stall shutdown.
        started = asyncio.Event()
        cancelled = False

        async def never_finishes():
            nonlocal cancelled
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled = True
                raise

        with patch(
            "ha_mcp.hacs_auto_refresh.maybe_refresh_hacs_after_update",
            new=never_finishes,
        ):
            async with hacs_auto_refresh.hacs_refresh_lifespan(object()):
                # The lifespan hands the task to the loop; wait on the flag
                # the fake sets rather than sleeping.
                await started.wait()

        assert cancelled, "exiting the lifespan must cancel the pending nudge"

    async def test_server_attaches_the_lifespan(self):
        # The built server's FastMCP instance carries the lifespan — the pin
        # that catches a future constructor change dropping it, which is the
        # sibling of the add-on gap this wiring replaced.
        from ha_mcp.server import HomeAssistantSmartMCPServer

        with (
            patch("ha_mcp.server.get_global_settings") as get_settings,
            patch("ha_mcp.server.HomeAssistantSmartMCPServer._initialize_server"),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer._build_skills_instructions",
                return_value=None,
            ),
        ):
            settings = get_settings.return_value
            settings.mcp_server_name = "test"
            settings.mcp_server_version = "0.0.1"
            settings.read_only_mode = False
            server = HomeAssistantSmartMCPServer()

        assert server.mcp._lifespan is hacs_auto_refresh.hacs_refresh_lifespan
