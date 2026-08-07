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

import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp import hacs_auto_refresh
from ha_mcp.__main__ import _run_with_shutdown
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
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


@contextmanager
def _server(ws, *, info=None, version="1.0.0", embedded=False, disabled=False):
    """Patch the startup gates, the WS client factory and the HACS refresh call."""
    get_ws = AsyncMock(return_value=ws)
    refresh = AsyncMock(return_value={"success": True})
    with (
        patch("ha_mcp.hacs_auto_refresh.is_embedded", return_value=embedded),
        patch(
            "ha_mcp.hacs_auto_refresh.is_update_check_disabled", return_value=disabled
        ),
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


def _written_marker(data_dir):
    path = data_dir / hacs_auto_refresh.MARKER_FILENAME
    return json.loads(path.read_text()) if path.exists() else None


def _refreshed_ids(mocks):
    return [call.args[1] for call in mocks.refresh.await_args_list]


class TestNudgeDue:
    def test_no_marker_is_due(self):
        assert hacs_auto_refresh._nudge_due("1.0.0", _info(), None) is True

    def test_changed_server_version_is_due(self):
        # The just-updated case: the paired component release is what HACS
        # needs to surface.
        marker = {"server_version": "0.9.0", "latest": "1.1.0"}
        assert hacs_auto_refresh._nudge_due("1.0.0", _info(), marker) is True

    def test_same_version_without_an_update_is_not_due(self):
        marker = {"server_version": "1.0.0", "latest": "1.0.0"}
        info = _info(latest="1.0.0", update_available=False)
        assert hacs_auto_refresh._nudge_due("1.0.0", info, marker) is False

    def test_release_appearing_since_the_last_pass_is_due(self):
        marker = {"server_version": "1.0.0", "latest": "1.0.0"}
        info = _info(latest="1.1.0")

        assert hacs_auto_refresh._nudge_due("1.0.0", info, marker) is True

    def test_already_recorded_latest_is_not_due(self):
        marker = {"server_version": "1.0.0", "latest": "1.1.0"}
        info = _info(latest="1.1.0")

        assert hacs_auto_refresh._nudge_due("1.0.0", info, marker) is False

    def test_missing_update_info_on_the_same_version_is_not_due(self):
        marker = {"server_version": "1.0.0", "latest": None}
        assert hacs_auto_refresh._nudge_due("1.0.0", None, marker) is False


class TestMarkerFile:
    def test_roundtrip(self, data_dir):
        marker = {"server_version": "1.0.0", "latest": "1.1.0", "hacs": "present"}
        hacs_auto_refresh._write_marker(marker)

        assert hacs_auto_refresh._read_marker() == marker

    def test_missing_file_reads_as_none(self, data_dir):
        assert hacs_auto_refresh._read_marker() is None

    def test_corrupt_file_reads_as_none(self, data_dir):
        (data_dir / hacs_auto_refresh.MARKER_FILENAME).write_text("{not json")

        assert hacs_auto_refresh._read_marker() is None

    def test_non_dict_payload_reads_as_none(self, data_dir):
        (data_dir / hacs_auto_refresh.MARKER_FILENAME).write_text('["nope"]')

        assert hacs_auto_refresh._read_marker() is None


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

    async def test_both_installed_candidates_are_refreshed(self, data_dir, caplog):
        ws = _ws([_repo(123, MIRROR), _repo(456, LEGACY), _repo(789, "other/repo")])

        with caplog.at_level(logging.INFO), _server(ws, info=_info()) as mocks:
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

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

    async def test_absent_hacs_is_recorded(self, data_dir):
        ws = _ws(
            error=HomeAssistantCommandError(
                "Command failed: unknown command: hacs/repositories/list",
                "unknown_command",
            )
        )

        with _server(ws, info=_info()) as mocks:
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        mocks.refresh.assert_not_awaited()
        assert _written_marker(data_dir)["hacs"] == "absent"

    async def test_unreachable_hacs_leaves_the_marker_unwritten(self, data_dir):
        # Nothing was determined, so the next startup must retry — which is
        # exactly what an absent marker does. RETRY_DELAYS is emptied so the
        # test cannot sleep through the real ~8 minute schedule.
        ws = _ws(error=HomeAssistantConnectionError("websocket down"))

        with (
            _server(ws, info=_info()) as mocks,
            patch.object(hacs_auto_refresh, "RETRY_DELAYS", ()),
        ):
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        assert ws.send_command.await_count == 1
        mocks.refresh.assert_not_awaited()
        assert _written_marker(data_dir) is None

    async def test_matching_marker_skips_the_websocket(self, data_dir):
        # The stdio hot path: a per-conversation spawn on an unchanged version
        # costs one file read and no WebSocket traffic.
        (data_dir / hacs_auto_refresh.MARKER_FILENAME).write_text(
            json.dumps(
                {"server_version": "1.0.0", "latest": "1.1.0", "hacs": "present"}
            )
        )
        ws = _ws([_repo(123, MIRROR)])

        with _server(ws, info=_info()) as mocks:
            await hacs_auto_refresh.maybe_refresh_hacs_after_update()

        mocks.get_websocket_client.assert_not_awaited()
        mocks.refresh.assert_not_awaited()


class TestStartupWiring:
    async def test_run_with_shutdown_fires_the_nudge(self):
        # The task is created but deliberately kept out of the wait set, so
        # the server's own exit is what ends _run_with_shutdown.
        nudge = AsyncMock(return_value=None)

        async def clean_server():
            return None

        with (
            patch(
                "ha_mcp.hacs_auto_refresh.maybe_refresh_hacs_after_update", new=nudge
            ),
            patch("ha_mcp.__main__._cleanup_resources", new=AsyncMock()),
        ):
            await _run_with_shutdown(clean_server())

        nudge.assert_awaited_once()
