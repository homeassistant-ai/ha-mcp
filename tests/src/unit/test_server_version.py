"""Running vs installed server version in ``ha_dev_manage_server(info)`` (#2766).

``server_version`` used to come from the package metadata on disk, so after a
reinstall the running process had not loaded, ``info`` reported the new version
while the old code kept serving.
"""

from __future__ import annotations

import pytest

from ha_mcp.tools import server_version
from ha_mcp.tools.tools_dev import DevTools

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


@pytest.mark.parametrize("installed", ["8.7.0", None], ids=["match", "probe-failed"])
async def test_info_flags_no_mismatch_without_a_newer_install(monkeypatch, installed):
    result = await _info(monkeypatch, installed=installed)

    assert result["data"]["installed_version"] == installed
    assert result["data"]["version_mismatch"] is False
    assert "warnings" not in result
