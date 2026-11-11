"""The container signals that silence ``ha-mcp-web``'s default-path warning.

Every other default-path warning test pins the container check, so the
actual detection mechanism — the ``/.dockerenv`` / ``/run/.containerenv``
marker files and the ``SUPERVISOR_TOKEN`` env var that suppress the
default-path warning on containerized / add-on deployments — is never
exercised there. A wrong marker path or env-var name would pass that suite
green. These tests start on a LAN bind with the default path and pin the
real signals.
"""

from __future__ import annotations

import logging
import os

import pytest

from ._main_web import start_main_web

_MARKERS = ("/.dockerenv", "/run/.containerenv")


def _warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    marker: str | None,
    token: str | None,
) -> bool:
    """Whether a LAN-bound default-path start warns, with only ``marker`` present."""
    real_exists = os.path.exists
    monkeypatch.setattr(
        "os.path.exists",
        lambda path: path == marker if path in _MARKERS else real_exists(path),
    )
    with caplog.at_level(logging.WARNING, logger="ha_mcp.__main__"):
        start_main_web(
            monkeypatch,
            MCP_HOST="0.0.0.0",
            MCP_SECRET_PATH=None,
            SUPERVISOR_TOKEN=token,
        )
    return any("default MCP_SECRET_PATH" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("marker", _MARKERS)
def test_container_marker_file_silences_the_warning(
    marker: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert not _warns(monkeypatch, caplog, marker=marker, token=None)


def test_ha_addon_supervisor_token_silences_the_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert not _warns(monkeypatch, caplog, marker=None, token="abc123")


def test_no_markers_no_token_is_not_container(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert _warns(monkeypatch, caplog, marker=None, token=None)


def test_empty_supervisor_token_is_not_container(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty ``SUPERVISOR_TOKEN`` must not count as containerized."""
    assert _warns(monkeypatch, caplog, marker=None, token="")
