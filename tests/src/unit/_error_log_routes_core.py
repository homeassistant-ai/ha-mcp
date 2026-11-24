"""The three routes the server's client reads Core's error log by, on a real ``hass``.

Inside the app the client reads Core's journal straight from Supervisor
(``in_app_client``). Outside it, the client asks Core whether ``hassio`` is
loaded. A Supervised install is read at ``/api/hassio/core/logs``
(``supervised_journal_route``); there Core's own ``hassio`` integration runs in
front of the stand-in Supervisor of ``_tools_addons_core``. Any other install
is read at Core's own ``/api/error_log``, the file Core names in
``KEY_DATA_LOGGING`` (the container route). The journal is
``FakeGatewayd`` (``_journal_gatewayd.py``), an upstream of the server, not
Home Assistant. A test takes these fixtures after importing their
dependencies from ``_journal_gatewayd``, ``_gateway`` and ``_error_log_core``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient

from ._error_log_core import serve_log
from ._gateway import Gateway
from ._hassio_core import set_up_hassio
from ._journal_gatewayd import FakeGatewayd
from ._tools_addons_core import TargetApp, serve_supervisor


def load_lines(journal: FakeGatewayd, lines: list[str]) -> None:
    """Make ``lines`` the journal, one entry each, oldest first."""
    journal.load([(0, [line]) for line in lines])


@contextmanager
def error_log_reads(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """Spy on the client's error-log reads; the route still answers each one."""
    with patch.object(client, "get_error_log", wraps=client.get_error_log) as spy:
        yield spy


@dataclass(frozen=True)
class ErrorLogRoute:
    """The server's client on one route, and the log it reads there."""

    name: str
    client: HomeAssistantClient
    journal: FakeGatewayd
    # The container route reads this file instead of the journal.
    log_file: Path | None
    # The reverse proxy in front of Core, outside the app.
    gateway: Gateway | None

    async def serve(self, lines: list[str]) -> None:
        """Make ``lines`` the log this route reads."""
        if self.log_file is None:
            load_lines(self.journal, lines)
        else:
            await serve_log(self.log_file, "".join(f"{line}\n" for line in lines))


@pytest.fixture
async def supervised_journal_route(
    hass: HomeAssistant,
    hassio_path_client: HomeAssistantClient,
    gatewayd: FakeGatewayd,
    journal_proxy: Gateway,
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> ErrorLogRoute:
    """``hassio_path_client`` against a Core that has ``hassio`` loaded."""
    supervisor = await serve_supervisor(aiohttp_client, TargetApp())
    await set_up_hassio(hass, supervisor, monkeypatch)
    return ErrorLogRoute(
        "supervised", hassio_path_client, gatewayd, None, journal_proxy
    )


def _route(request: pytest.FixtureRequest, name: str) -> ErrorLogRoute:
    if name == "supervised":
        route: ErrorLogRoute = request.getfixturevalue("supervised_journal_route")
        return route
    journal: FakeGatewayd = request.getfixturevalue("gatewayd")
    if name == "in-app":
        client: HomeAssistantClient = request.getfixturevalue("in_app_client")
        return ErrorLogRoute(name, client, journal, None, None)
    monkeypatch: pytest.MonkeyPatch = request.getfixturevalue("monkeypatch")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    # Core registers /api/error_log only for a file named before it starts.
    log_file: Path = request.getfixturevalue("log_file")
    client = request.getfixturevalue("gateway_client")
    return ErrorLogRoute(
        name, client, journal, log_file, request.getfixturevalue("gateway")
    )


@pytest.fixture(params=["in-app", "supervised", "container"])
def error_log_route(request: pytest.FixtureRequest) -> ErrorLogRoute:
    """Each route to Core's log."""
    return _route(request, request.param)


@pytest.fixture(params=["in-app", "supervised"])
def journald_route(request: pytest.FixtureRequest) -> ErrorLogRoute:
    """Each route to Core's journal."""
    return _route(request, request.param)


@pytest.fixture(params=["supervised", "container"])
def probing_route(request: pytest.FixtureRequest) -> ErrorLogRoute:
    """Each route outside the app, where the client asks Core for its install class."""
    return _route(request, request.param)
