"""``ha-mcp-oauth`` (``main_oauth``) started up to where it would serve.

The settings UI's routes skip the OAuth check, so they must never sit under
the MCP path every connector knows (GHSA-mx64-982r-65vg).
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

from ha_mcp.config import OAUTH_MODE_TOKEN

from ._oauth_startup import PUBLIC_URL, route_paths, started_oauth_server
from ._oauth_startup import isolated_oauth_runtime as isolated_oauth_runtime


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        pytest.param("", OAUTH_MODE_TOKEN, id="docker-empty-token"),
        pytest.param("an-admin-token", "an-admin-token", id="token-given"),
    ],
)
async def test_oauth_mode_starts_without_a_server_token(
    monkeypatch: pytest.MonkeyPatch, token: str, expected: str
) -> None:
    """The image sets HOMEASSISTANT_TOKEN="" and users give theirs at sign-in (#886)."""
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", token)

    await started_oauth_server(monkeypatch)

    assert os.environ["HOMEASSISTANT_TOKEN"] == expected


async def test_the_logged_settings_url_serves_the_settings_ui_off_the_mcp_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The admin finds the UI at the URL in the startup log, and nowhere else."""
    mcp = await started_oauth_server(monkeypatch, HA_MCP_DISABLE_SETTINGS_UI=None)

    [url] = re.findall(rf"{PUBLIC_URL}(/private_\S+/settings)", caplog.text)
    app = mcp.http_app(path="/mcp", stateless_http=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL
    ) as http:
        settings = await http.get(url)
        under_mcp = await http.get("/mcp/settings")

    assert settings.status_code == 200
    assert under_mcp.status_code == 404
    assert not [p for p in route_paths(app.routes) if p.startswith("/mcp/")]


async def test_disabling_the_settings_ui_mounts_none_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = await started_oauth_server(monkeypatch, HA_MCP_DISABLE_SETTINGS_UI="1")

    app = mcp.http_app(path="/mcp", stateless_http=True)

    assert not [p for p in route_paths(app.routes) if "settings" in p]
