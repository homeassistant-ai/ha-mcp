"""Running vs installed server version in ``ha_dev_manage_server(info)`` (#2766).

``server_version`` used to come from the package metadata on disk, so after a
reinstall the running process had not loaded, ``info`` reported the new version
while the old code kept serving.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import ErrorLogPage
from ha_mcp.tools import server_version
from ha_mcp.tools.tools_bug_report import register_bug_report_tools
from ha_mcp.tools.tools_dev import DevTools

from .test_tools_bug_report import (  # noqa: F401
    _no_supervisor,
    _stub_component_caps,
    mock_mcp,
)
from .test_tools_dev import _mock_client


async def _info(monkeypatch, *, installed, embedded=False):
    monkeypatch.setattr(server_version, "__version__", "8.7.0")
    monkeypatch.setattr(server_version, "detect_installed_version", lambda: installed)
    monkeypatch.setattr(server_version, "is_embedded", lambda: embedded)
    return await DevTools(_mock_client()).ha_dev_manage_server(action="info")


@pytest.mark.parametrize(
    ("embedded", "restart"), [(True, "Home Assistant"), (False, "the server")]
)
async def test_info_reports_the_running_version_after_an_unloaded_reinstall(
    monkeypatch, embedded, restart
):
    result = await _info(monkeypatch, installed="8.7.0.dev3077", embedded=embedded)

    data = result["data"]
    assert data["server_version"] == "8.7.0"
    assert data["is_dev_build"] is False
    assert data["installed_version"] == "8.7.0.dev3077"
    assert data["version_mismatch"] is True
    [warning] = result["warnings"]
    assert "8.7.0.dev3077 is installed" in warning
    assert f"restart {restart}" in warning


async def test_info_quiet_when_the_running_version_is_installed(monkeypatch):
    result = await _info(monkeypatch, installed="8.7.0")

    assert result["data"]["version_mismatch"] is False
    assert "warnings" not in result


async def test_info_says_when_it_could_not_compare(monkeypatch):
    """A failed probe must neither fail ``info`` nor read as "versions match"."""

    def _broken() -> str:
        raise RuntimeError("metadata read failed")

    monkeypatch.setattr(server_version, "get_version", _broken)
    result = await DevTools(_mock_client()).ha_dev_manage_server(action="info")

    assert result["data"]["installed_version"] is None
    assert result["data"]["version_mismatch"] is False
    [warning] = result["warnings"]
    assert "not compared" in warning


def test_probe_still_reads_when_the_cache_refresh_breaks(monkeypatch):
    """Python 3.14 with an editable homeassistant install (the official
    container image) raises KeyError from invalidate_caches (#1891)."""

    def _broken() -> None:
        raise KeyError("__editable__.homeassistant.finder.__path_hook__")

    monkeypatch.setattr(server_version.importlib, "invalidate_caches", _broken)
    monkeypatch.setattr(server_version, "get_version", lambda: "8.7.0.dev3077")

    assert server_version.detect_installed_version() == "8.7.0.dev3077"


def test_unreadable_metadata_is_no_installed_version(monkeypatch):
    """get_version's "unknown" is a failed read, not a version to restart into."""
    monkeypatch.setattr(server_version, "get_version", lambda: "unknown")

    assert server_version.detect_installed_version() is None


async def test_report_flags_a_reinstall_the_process_has_not_loaded(
    monkeypatch,
    mock_mcp,  # noqa: F811
):
    monkeypatch.setattr(server_version, "__version__", "8.7.0")
    monkeypatch.setattr(server_version, "get_version", lambda: "8.7.0.dev3077")
    client = MagicMock()
    client.get_config = AsyncMock(return_value={"version": "2026.10.2"})
    client.get_states = AsyncMock(return_value=[])
    client.get_error_log = AsyncMock(return_value=ErrorLogPage(text="", has_more=False))
    register_bug_report_tools(mock_mcp, client)
    report = mock_mcp._tools["ha_report_issue"]
    while hasattr(report, "__wrapped__"):
        report = report.__wrapped__

    result = await report()

    assert result["diagnostic_info"]["installed_version"] == "8.7.0.dev3077"
    assert result["diagnostic_info"]["version_mismatch"] is True
    assert "8.7.0.dev3077 is installed" in result["formatted_report"]
