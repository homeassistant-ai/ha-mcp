"""Run OAuth startup through its CLI and inspect the resulting real routes."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from starlette.routing import BaseRoute

from ha_mcp.__main__ import main_oauth, reset_main_runtime
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.settings_ui import reset_http_settings_state

PUBLIC_URL = "https://connector.example"


async def started_oauth_server(
    monkeypatch: pytest.MonkeyPatch, **env: str | None
) -> FastMCP:
    """Run ``main_oauth`` with ``env`` (``None`` unsets) on top of a valid OAuth
    setup; the server it would serve, after the serve call returned at once."""
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:8123")
    # ``main_oauth`` writes its own value over an empty one; this puts it back.
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", os.environ.get("HOMEASSISTANT_TOKEN", ""))
    monkeypatch.setenv("MCP_BASE_URL", PUBLIC_URL)
    monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    # Each startup starts clean; the unit conftest clears it after the test.
    reset_main_runtime()
    reset_http_settings_state()
    # caplog listens on the root logger, which the CLI's setup replaces.
    monkeypatch.setattr("ha_mcp.__main__.setup_logging", lambda *a, **kw: None)
    runs: list[Any] = []
    monkeypatch.setattr("ha_mcp.__main__.run_entrypoint", lambda c, _: runs.append(c))
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
