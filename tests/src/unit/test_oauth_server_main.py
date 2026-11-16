"""``ha-mcp-oauth`` (``main_oauth``) started up to where it would serve.

The settings UI's routes skip the OAuth check, so they must never sit under
the MCP path every connector knows (GHSA-mx64-982r-65vg).
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import httpx
import pytest
from starlette.routing import BaseRoute

from ha_mcp.__main__ import main_oauth
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.config import OAUTH_MODE_TOKEN

PUBLIC_URL = "https://connector.example"


async def started_oauth_server(
    monkeypatch: pytest.MonkeyPatch, **env: str | None
) -> FastMCP:
    """Run ``main_oauth`` with ``env`` (``None`` unsets) on top of a valid OAuth
    setup; the server it would serve, after the serve call returned at once."""
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:8123")
    monkeypatch.setenv("MCP_BASE_URL", PUBLIC_URL)
    monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    # Startup state of the process, put back after the test.
    monkeypatch.setattr("ha_mcp.__main__._server", None)
    monkeypatch.setattr("ha_mcp.__main__._shutdown_event", None)
    monkeypatch.setattr("ha_mcp.settings_ui._http_settings_prefix", None)
    monkeypatch.setattr("ha_mcp.settings_ui._http_settings_mounted", False)
    # caplog listens on the root logger, which the CLI's setup replaces.
    monkeypatch.setattr("ha_mcp.__main__._setup_logging", lambda *a, **kw: None)
    runs: list[Any] = []
    monkeypatch.setattr("ha_mcp.__main__._run_entrypoint", lambda c, _: runs.append(c))
    served: list[FastMCP] = []

    async def serve(self: FastMCP, **kwargs: Any) -> None:
        served.append(self)

    monkeypatch.setattr(FastMCP, "run_async", serve)

    main_oauth()
    [run] = runs
    await asyncio.gather(run)
    [mcp] = served
    return mcp


def route_paths(routes: list[BaseRoute]) -> list[str]:
    paths = []
    for route in routes:
        paths.append(getattr(route, "path", ""))
        paths.extend(route_paths(getattr(route, "routes", [])))
    return paths


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
