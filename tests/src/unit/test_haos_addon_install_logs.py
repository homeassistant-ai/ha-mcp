"""A failed add-on install logs Supervisor's log tail, which holds the detail
behind Supervisor's "Check Supervisor logs for details" answer."""

from __future__ import annotations

import http.client
import logging
import urllib.error
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from tests.haos_image_build.build_image import (
    HAWebSocket,
    OAuthCredentials,
    WSCommandError,
    _install_addon_with_retry,
)


def _failing_ws() -> Any:
    ws = HAWebSocket(
        "http://127.0.0.1:18123",
        OAuthCredentials(access_token="access", refresh_token="refresh"),
    )
    ws.supervisor_api = Mock(  # type: ignore[method-assign]
        side_effect=WSCommandError(
            "Check Supervisor logs for details", code="home_assistant_error"
        )
    )
    ws.reconnect = Mock()  # type: ignore[method-assign]
    return ws


def _log_response(text: str) -> Any:
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = text.encode()
    return resp


def test_each_failed_attempt_logs_the_supervisor_log_tail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ws = _failing_ws()

    with (
        patch("tests.haos_image_build.build_image.time.sleep"),
        patch(
            "tests.haos_image_build.supervisor_logs.urllib.request.urlopen",
            return_value=_log_response("docker build failed: apt timeout"),
        ) as urlopen,
        pytest.raises(WSCommandError, match="Check Supervisor logs"),
    ):
        _install_addon_with_retry(ws, "local_ha_mcp_dev", timeout=1.0)

    request = urlopen.call_args.args[0]
    assert request.full_url == (
        "http://127.0.0.1:18123/api/hassio/supervisor/logs?lines=200"
    )
    assert request.get_header("Authorization") == "Bearer access"
    tails = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(tails) == 2
    assert "apt timeout" in tails[0].getMessage()


@pytest.mark.parametrize(
    "failure",
    [urllib.error.URLError("connection refused"), http.client.IncompleteRead(b"")],
    ids=["connection", "truncated-body"],
)
def test_an_unreadable_log_tail_does_not_replace_the_install_error(
    caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    ws = _failing_ws()

    with (
        patch("tests.haos_image_build.build_image.time.sleep"),
        patch(
            "tests.haos_image_build.supervisor_logs.urllib.request.urlopen",
            side_effect=failure,
        ),
        pytest.raises(WSCommandError, match="Check Supervisor logs"),
    ):
        _install_addon_with_retry(ws, "local_ha_mcp_dev", timeout=1.0, attempts=1)

    assert "Could not fetch the Supervisor log tail" in caplog.text
