"""Core's own ``hassio`` integration in front of a Supervisor that ``aioclient_mock`` answers.

A unit test has no Supervisor. Core's ``hassio`` integration is set up as on
Home Assistant OS (``_hassio_core``), so Core validates every command and
builds every reply. Only the Supervisor's HTTP API at its address is replaced,
answered by ``aioclient_mock`` as Core's own hassio tests answer it.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ._hassio_core import set_up_hassio, supervisor_error, supervisor_ok

# The Supervisor's address, as Core's hassio tests give it.
SUPERVISOR = "127.0.0.1"


def _answer(
    aioclient_mock: AiohttpClientMocker,
    method: str,
    path: str,
    body: dict[str, Any],
    status: int = 200,
) -> None:
    aioclient_mock.request(
        method.lower(),
        f"http://{SUPERVISOR}{path}",
        status=status,
        json=body,
        headers={"Content-Type": "application/json"},
    )


class _MockedSupervisor:
    """What Core's hassio integration reads while it sets up, through
    ``aioclient_mock``. ``aioclient_mock`` serves the first registration that
    matches a request, so a later one for the same request is never served."""

    url = SUPERVISOR

    def __init__(self, aioclient_mock: AiohttpClientMocker) -> None:
        self._mock = aioclient_mock

    def default(self, method: str, path: str, data: Any = None) -> None:
        _answer(self._mock, method, path, supervisor_ok(data))

    def default_refusal(
        self, method: str, path: str, message: str, status: int = 400
    ) -> None:
        _answer(self._mock, method, path, supervisor_error(message), status)

    def forget_requests(self) -> None:
        self._mock.mock_calls.clear()


async def load_supervisor_api(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[tuple[str, str], Any],
) -> None:
    """Set up Core's ``hassio``, which registers ``supervisor/api``, then
    clear every earlier ``aioclient_mock`` registration: the Supervisor answers
    only ``answers`` and what the test registers afterwards.
    ``SUPERVISOR_TOKEN`` is left set, as in Core's container."""
    await set_up_hassio(hass, _MockedSupervisor(aioclient_mock), monkeypatch)
    aioclient_mock.clear_requests()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "abcdefgh")
    for (method, path), data in answers.items():
        _answer(aioclient_mock, method, path, supervisor_ok(data))


def supervisor_requests(aioclient_mock: AiohttpClientMocker) -> list[tuple[str, str]]:
    """``(method, path)`` of every request that reached the Supervisor."""
    prefix = f"http://{SUPERVISOR}"
    return [
        (method.lower(), str(url).removeprefix(prefix))
        for method, url, *_ in aioclient_mock.mock_calls
    ]
