"""Core's own ``supervisor/api`` in front of a Supervisor that ``aioclient_mock`` answers.

A unit test has no Supervisor. ``hassio``'s client and WebSocket API are set up
as ``hassio``'s ``async_setup`` sets them up (``HassIO`` in ``hass.data``, then
``async_load_websocket_api``), so Core validates every command and builds every
reply. Only the Supervisor's HTTP API at its address is replaced, answered by
``aioclient_mock`` as Core's own hassio tests answer it.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.hassio import const as hassio_const
from homeassistant.components.hassio import websocket_api as hassio_websocket_api
from homeassistant.components.hassio.handler import HassIO
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

# The Supervisor's address, as Core's hassio tests give it.
SUPERVISOR = "127.0.0.1"


def load_supervisor_api(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[tuple[str, str], Any],
) -> None:
    """Register Core's ``supervisor/api``; the Supervisor answers each
    ``(method, path)`` in ``answers`` with ``{"result": "ok", "data": ...}``."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "abcdefgh")
    hass.data[hassio_const.DATA_COMPONENT] = HassIO(
        hass.loop, async_get_clientsession(hass), SUPERVISOR
    )
    hassio_websocket_api.async_load_websocket_api(hass)
    for (method, path), data in answers.items():
        aioclient_mock.request(
            method, f"http://{SUPERVISOR}{path}", json={"result": "ok", "data": data}
        )


def supervisor_requests(aioclient_mock: AiohttpClientMocker) -> list[tuple[str, str]]:
    """``(method, path)`` of every request that reached the Supervisor."""
    prefix = f"http://{SUPERVISOR}"
    return [
        (method.lower(), str(url).removeprefix(prefix))
        for method, url, *_ in aioclient_mock.mock_calls
    ]
