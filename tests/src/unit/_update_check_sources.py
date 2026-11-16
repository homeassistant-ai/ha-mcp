"""What the self-update check asks, over real HTTP on loopback.

Outside the app the check asks PyPI's JSON API for the newest release of the
running channel's package: ``pypi`` serves that API and points
``update_check.PYPI_JSON_URL`` at it. Inside the app it asks Supervisor for the
app's own info: ``app_store`` points it at the stand-in Supervisor of
``_tools_addons_core``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web

from ha_mcp import update_check

from ._tools_addons_core import APP_TOKEN, Supervisor


@dataclass
class PyPI:
    """What PyPI publishes, and the packages it was asked about, in order."""

    releases: dict[str, dict[str, str | None]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    def publish(
        self, package: str, version: str, requires_python: str | None = None
    ) -> None:
        """``version`` becomes ``package``'s newest release."""
        self.releases[package] = {
            "version": version,
            "requires_python": requires_python,
        }

    async def serve(self, request: web.Request) -> web.Response:
        package = request.match_info["package"]
        self.asked.append(package)
        info = self.releases.get(package)
        if info is None:
            return web.json_response({"message": "Not Found"}, status=404)
        return web.json_response({"info": {"name": package, **info}})


@pytest.fixture
async def pypi(
    aiohttp_client: Any, socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> PyPI:
    """PyPI with nothing published yet, where the check asks it."""
    found = PyPI()
    app = web.Application()
    app.router.add_get("/pypi/{package}/json", found.serve)
    server = await aiohttp_client(app)
    monkeypatch.setattr(
        update_check,
        "PYPI_JSON_URL",
        f"http://127.0.0.1:{server.port}/pypi/{{package}}/json",
    )
    return found


def app_store(
    monkeypatch: pytest.MonkeyPatch,
    supervisor: Supervisor,
    *,
    installed: str,
    latest: str,
    update_available: bool,
) -> None:
    """Run as the app does, with Supervisor's store offering ``latest``."""
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", APP_TOKEN)
    monkeypatch.setenv("SUPERVISOR_BASE_URL", f"http://{supervisor.url}")
    supervisor.answer(
        "GET",
        "/addons/self/info",
        {
            "version": installed,
            "version_latest": latest,
            "update_available": update_available,
        },
    )
