"""The embedded no-stomp guard's fetch of Home Assistant's requirements.

GitHub throttles the runners, so the fetch waits and asks again; it fails
closed when it cannot have the file.
"""

from __future__ import annotations

import importlib
import io
from pathlib import Path
from types import ModuleType

import pytest
import requests


def _response(status: int, retry_after: str | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = "https://raw.githubusercontent.com/requirements_all.txt"
    response.raw = io.BytesIO(b"aiohttp==3.13.0\n")
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return response


@pytest.fixture
def no_stomp(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The E2E module, with ``tests/`` on the path as in the E2E run: it
    imports ``test_constants`` from there."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    name = "tests.src.e2e.workflows.embedded.test_embedded_no_stomp"
    return importlib.import_module(name)


@pytest.fixture
def fetch(monkeypatch: pytest.MonkeyPatch, no_stomp: ModuleType):
    """Run the fetch against ``answers`` in turn; the waits it made and how
    many requests it sent."""
    waits: list[float] = []
    sent: list[str] = []

    def run(answers: list[requests.Response | Exception]) -> object:
        replies = iter(answers)

        def get(url: str, timeout: float) -> requests.Response:
            sent.append(url)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(no_stomp.requests, "get", get)
        monkeypatch.setattr(no_stomp.time, "sleep", waits.append)
        return no_stomp.fetch_requirements_all("2026.11.2")

    run.waits = waits  # type: ignore[attr-defined]
    run.sent = sent  # type: ignore[attr-defined]
    return run


def test_a_throttled_fetch_waits_as_long_as_github_asks(fetch) -> None:
    """Never shorter than its own wait for that attempt, never longer than
    the cap, and its own wait when GitHub names none."""
    throttled = [_response(429, "7"), _response(429, "0"), _response(429, "9999")]

    specs = fetch([*throttled, _response(429), _response(200)])

    assert fetch.waits == [7, 15, 120, 60]
    assert str(specs["aiohttp"]) == "==3.13.0"


def test_a_fetch_that_never_succeeds_fails_after_five_attempts(fetch) -> None:
    with pytest.raises(AssertionError, match="could not fetch"):
        fetch([_response(503)] * 5)

    assert (len(fetch.sent), fetch.waits) == (5, [5, 15, 30, 60])


def test_a_lost_connection_is_tried_again(fetch) -> None:
    specs = fetch([requests.ConnectionError("reset"), _response(200)])

    assert (list(specs), fetch.waits) == (["aiohttp"], [5])


def test_a_release_that_does_not_exist_fails_at_once(fetch) -> None:
    with pytest.raises(AssertionError, match="404"):
        fetch([_response(404)])

    assert (len(fetch.sent), fetch.waits) == (1, [])
