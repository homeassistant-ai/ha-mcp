"""The retry tail of ``scripts/translate_locales.py``'s ``_call_gemini``.

Every retryable failure, a transport error as much as an unusable answer,
ends in one shared tail: stop after the fifth attempt, stop once the time
budget is spent, otherwise wait and retry. These tests drive it through the
transport-error path with a monkeypatched transport and a recorded sleep.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import translate_locales


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(translate_locales, "_DEADLINE", None)
    recorded: list[float] = []
    monkeypatch.setattr(translate_locales.time, "sleep", recorded.append)
    return recorded


def test_failed_attempts_wait_before_each_retry_but_not_after_the_last(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    waits_before: list[int] = []

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        waits_before.append(len(sleeps))
        raise translate_locales.httpx.ConnectError("name resolution failed")

    monkeypatch.setattr(translate_locales.httpx, "post", unreachable)
    with pytest.raises(SystemExit, match="transport error"):
        translate_locales._call_gemini("prompt")
    assert waits_before == [0, 1, 2, 3, 4], "each retry follows one more wait"
    assert len(sleeps) == 4, "no wait after the fifth attempt"
    assert all(delay > 0 for delay in sleeps), "a retry must wait"


def test_spent_budget_stops_the_retries(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """The budget can run out while an attempt is in flight; the tail must
    then stop instead of sleeping into another attempt."""
    calls: list[int] = []

    def times_out_past_the_deadline(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(1)
        monkeypatch.setattr(
            translate_locales, "_DEADLINE", translate_locales.time.monotonic() - 1
        )
        raise translate_locales.httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(translate_locales.httpx, "post", times_out_past_the_deadline)
    with pytest.raises(
        SystemExit, match=r"transport error: .*\(time budget exhausted; not retrying\)"
    ):
        translate_locales._call_gemini("prompt")
    assert len(calls) == 1
    assert sleeps == []
