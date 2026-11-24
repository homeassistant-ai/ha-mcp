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
from packaging.specifiers import SpecifierSet


def _response(status: int, retry_after: str | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = "https://raw.githubusercontent.com/requirements_all.txt"
    response.raw = io.BytesIO(b"aiohttp==3.13.0\n")
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return response


class _Fetch:
    """Runs the fetch against the answers it is given, in turn; keeps the
    waits it made and the requests it sent."""

    def __init__(self, module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        self._module = module
        self._monkeypatch = monkeypatch
        self.waits: list[float] = []
        self.sent: list[str] = []

    def __call__(
        self, answers: list[requests.Response | Exception]
    ) -> dict[str, SpecifierSet]:
        replies = iter(answers)

        def get(url: str, timeout: float) -> requests.Response:
            self.sent.append(url)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        self._monkeypatch.setattr(self._module.requests, "get", get)
        self._monkeypatch.setattr(self._module.time, "sleep", self.waits.append)
        specs: dict[str, SpecifierSet] = self._module.fetch_requirements_all(
            "2026.11.2", waits=_WAITS, wait_cap=_CAP
        )
        return specs


# The test's own schedule: four waits, and the longest a ``Retry-After`` is obeyed.
_WAITS = (2, 3, 4, 5)
_CAP = 10


@pytest.fixture
def fetch(monkeypatch: pytest.MonkeyPatch) -> _Fetch:
    """The E2E module's fetch on the test's own schedule, with ``tests/`` on
    the path as in the E2E run: the module imports ``test_constants`` from
    there."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    name = "tests.src.e2e.workflows.embedded.test_embedded_no_stomp"
    return _Fetch(importlib.import_module(name), monkeypatch)


def test_a_throttled_fetch_waits_as_long_as_github_asks(fetch: _Fetch) -> None:
    """As long as the answer asks, never shorter than its own wait for that
    attempt, never longer than the cap, and its own wait when the answer names
    no number of seconds. GitHub throttles with a 429 or a 403; a server error
    can ask for a wait as well."""
    asked = _WAITS[0] + 1
    throttled = [_response(429, str(asked)), _response(403, "0")]
    throttled.append(_response(503, str(_CAP * 100)))
    dated = _response(429, "Wed, 25 Nov 2026 07:28:00 GMT")

    specs = fetch([*throttled, dated, _response(200)])

    assert fetch.waits == [asked, _WAITS[1], _CAP, _WAITS[3]]
    assert str(specs["aiohttp"]) == "==3.13.0"


def test_a_fetch_that_never_succeeds_fails_after_its_last_attempt(
    fetch: _Fetch,
) -> None:
    """One attempt for each wait and one more, with no wait after the last."""
    with pytest.raises(AssertionError, match="could not fetch"):
        fetch([_response(503)] * (len(_WAITS) + 1))

    assert (len(fetch.sent), fetch.waits) == (len(_WAITS) + 1, list(_WAITS))


def test_a_lost_connection_or_a_timeout_is_tried_again(fetch: _Fetch) -> None:
    lost = requests.ConnectionError("reset")

    specs = fetch([lost, _response(408), _response(200)])

    assert (list(specs), fetch.waits) == (["aiohttp"], list(_WAITS[:2]))


def test_a_release_that_does_not_exist_fails_at_once(fetch: _Fetch) -> None:
    with pytest.raises(AssertionError, match="404"):
        fetch([_response(404)])

    assert (len(fetch.sent), fetch.waits) == (1, [])
