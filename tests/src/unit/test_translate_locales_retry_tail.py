"""The retries, request pacing and time budget of ``scripts/translate_locales.py``.

Every retryable failure in ``_call_gemini``, a transport error as much as an
unusable answer, ends in one shared tail: stop after the fifth attempt, stop
once the time budget is spent, otherwise wait and retry. The first tests drive
it with a monkeypatched transport, through a transport error and a retryable
HTTP status. The corrective retry ``_accept_or_retry`` asks for a rejected
string runs only while the same budget lasts. Each chunk request is followed
by a wait, answered or failed, while the budget lasts and the engine has not
been given up on, and a run whose budget runs out between two requests stops
there. Every test records the sleeps and runs on a stopped clock.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import translate_locales

_NOW = 1e12  # no real monotonic clock gets here: losing the stop fails the tests


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every wait and stop the clock the time budget reads, so only
    ``_deadline_in`` decides whether the budget is spent."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(translate_locales, "_DEADLINE", None)
    monkeypatch.setattr(translate_locales.time, "monotonic", lambda: _NOW)
    recorded: list[float] = []
    monkeypatch.setattr(translate_locales.time, "sleep", recorded.append)
    return recorded


def _deadline_in(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Put the time budget's deadline ``seconds`` from now (negative: spent)."""
    monkeypatch.setattr(translate_locales, "_DEADLINE", _NOW + seconds)


@pytest.mark.parametrize("failure", ["transport", "status"])
def test_failed_attempts_wait_before_each_retry_but_not_after_the_last(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], failure: str
) -> None:
    """Both retryable failures share the tail. The fifth attempt ends the call
    even when the budget runs out during it, so the error names no budget."""
    _deadline_in(monkeypatch, 3600)
    waits_before: list[int] = []

    def fails(*_args: Any, **_kwargs: Any) -> Any:
        waits_before.append(len(sleeps))
        if len(waits_before) == 5:
            _deadline_in(monkeypatch, -1)
        if failure == "transport":
            raise translate_locales.httpx.ConnectError("name resolution failed")
        return translate_locales.httpx.Response(503, text="unavailable")

    monkeypatch.setattr(translate_locales.httpx, "post", fails)
    with pytest.raises(SystemExit) as exc_info:
        translate_locales._call_gemini("prompt")
    assert "time budget" not in str(exc_info.value)
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
        _deadline_in(monkeypatch, -1)
        raise translate_locales.httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(translate_locales.httpx, "post", times_out_past_the_deadline)
    with pytest.raises(
        SystemExit, match=r"transport error: .*\(time budget exhausted; not retrying\)"
    ):
        translate_locales._call_gemini("prompt")
    assert len(calls) == 1
    assert sleeps == []


@pytest.fixture
def retries(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stands in for the engine: records each retry prompt and answers it
    with a valid translation of the one string these tests use."""
    prompts: list[str] = []

    def engine(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        return {"messages:greeting": "Hallo"}

    monkeypatch.setattr(translate_locales, "_call_gemini", engine)
    return prompts


def _accept(
    results: dict[tuple[str, str], str],
    failures: list[str],
    translated: str | None = None,
) -> set[tuple[str, str]]:
    """Run ``_accept_or_retry`` on one string; by default the engine answered
    it with nothing."""
    failed: set[tuple[str, str]] = set()
    item = translate_locales.WorkItem("de", "messages", "greeting", "Hello")
    translate_locales._accept_or_retry(
        "de", item, translated, results, failures, failed, "messages", frozenset()
    )
    return failed


def test_rejected_string_gets_one_retry_while_the_budget_lasts(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], retries: list[str]
) -> None:
    _deadline_in(monkeypatch, 3600)
    results: dict[tuple[str, str], str] = {}
    failed = _accept(results, [])
    assert len(retries) == 1
    assert "previous attempt was rejected" in retries[0]
    assert len(sleeps) == 1, "the retry is paced like any other request"
    assert sleeps[0] > 0, "a retry must wait"
    assert results == {("messages", "greeting"): "Hallo"}
    assert failed == set()


def test_spent_budget_skips_the_validation_retry(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], retries: list[str]
) -> None:
    """An answer can arrive after the deadline; each string it got wrong must
    then fail at once, without a retry call and its pacing wait."""
    _deadline_in(monkeypatch, -1)
    failures: list[str] = []
    failed = _accept({}, failures)
    assert retries == []
    assert sleeps == []
    assert failed == {("messages", "greeting")}
    assert failures[0].endswith("(time budget exhausted; not retrying)")


@pytest.mark.parametrize("deadline_in", [3600, -1])
def test_valid_answer_is_kept_without_a_retry_or_a_wait(
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    retries: list[str],
    deadline_in: float,
) -> None:
    """A valid answer, late or not, must not cost a retry or a 7 s wait."""
    _deadline_in(monkeypatch, deadline_in)
    results: dict[tuple[str, str], str] = {}
    failed = _accept(results, [], translated="Hallo")
    assert results == {("messages", "greeting"): "Hallo"}
    assert failed == set()
    assert retries == []
    assert sleeps == []


def test_retry_answered_after_the_deadline_skips_its_pacing_wait(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], retries: list[str]
) -> None:
    """The retry can return after the deadline; its pacing wait then paces
    no request and only eats into the slack before the job timeout."""
    _deadline_in(monkeypatch, 3600)
    engine = translate_locales._call_gemini

    def answers_past_the_deadline(prompt: str) -> dict[str, Any]:
        _deadline_in(monkeypatch, -1)
        return engine(prompt)

    monkeypatch.setattr(translate_locales, "_call_gemini", answers_past_the_deadline)
    results: dict[tuple[str, str], str] = {}
    failed = _accept(results, [])
    assert len(retries) == 1, "the retry ran"
    assert results == {("messages", "greeting"): "Hallo"}
    assert failed == set()
    assert sleeps == []


@pytest.mark.parametrize("crosses_deadline", [False, True])
def test_failed_retry_fails_the_string_not_the_run(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], crosses_deadline: bool
) -> None:
    """A retry the engine fails must leave only its string failed; its wait
    still paces the next request while the budget lasts. A retry that failed
    after the deadline says the budget is spent, so the failure summary does
    not hide it."""
    _deadline_in(monkeypatch, 3600)

    def fails(_prompt: str) -> dict[str, Any]:
        if crosses_deadline:
            _deadline_in(monkeypatch, -1)
        raise SystemExit("Gemini API call failed: HTTP 503")

    monkeypatch.setattr(translate_locales, "_call_gemini", fails)
    failures: list[str] = []
    failed = _accept({}, failures)
    assert failed == {("messages", "greeting")}
    assert len(sleeps) == (0 if crosses_deadline else 1)
    budget_note = failures[0].endswith("(retry failed; time budget exhausted)")
    assert budget_note is crosses_deadline


@pytest.mark.parametrize("first_call_fails", [False, True])
def test_each_chunk_request_is_paced_while_the_budget_lasts(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float], first_call_fails: bool
) -> None:
    """Unpaced chunk requests run into the engine's rate limit; while the
    budget lasts, every chunk request must be followed by a wait, a failed one
    too: it can end on a rate-limit answer."""
    _deadline_in(monkeypatch, 3600)
    monkeypatch.setattr(translate_locales, "_MAX_CHARS_PER_REQUEST", 1)
    waits_before: list[int] = []

    def engine(_prompt: str) -> dict[str, str]:
        waits_before.append(len(sleeps))
        if first_call_fails and len(waits_before) == 1:
            raise SystemExit("Gemini API call failed: HTTP 429")
        return {"messages:greeting": "Hallo", "messages:farewell": "Tschüss"}

    monkeypatch.setattr(translate_locales, "_call_gemini", engine)
    items = [
        translate_locales.WorkItem("de", "messages", "greeting", "Hello"),
        translate_locales.WorkItem("de", "messages", "farewell", "Goodbye"),
    ]
    _, failures, _, _ = translate_locales._translate_locale("de", items)
    assert len(failures) == (1 if first_call_fails else 0)
    assert waits_before == [0, 1], "a wait between the requests"
    assert len(sleeps) == 2, "a wait after each request"
    assert all(delay > 0 for delay in sleeps), "a chunk request must be paced"


def test_a_chunk_call_failing_past_the_deadline_skips_its_pacing_wait(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """A failed chunk call can return after the deadline; its pacing wait then
    paces no request, since the run stops on the budget."""
    _deadline_in(monkeypatch, 3600)

    def fails_past_the_deadline(_prompt: str) -> dict[str, str]:
        _deadline_in(monkeypatch, -1)
        raise SystemExit("Gemini API call failed: HTTP 503")

    monkeypatch.setattr(translate_locales, "_call_gemini", fails_past_the_deadline)
    items = [translate_locales.WorkItem("de", "messages", "greeting", "Hello")]
    translate_locales._translate_locale("de", items)
    assert sleeps == []


def test_giving_up_on_the_engine_skips_the_pacing_wait(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """The failed call that gives up on the engine stops the run's engine
    calls, so a wait after it would pace no request."""
    _deadline_in(monkeypatch, 3600)
    monkeypatch.setattr(translate_locales, "_MAX_CHARS_PER_REQUEST", 1)
    calls: list[str] = []

    def engine(prompt: str) -> dict[str, str]:
        calls.append(prompt)
        raise SystemExit("Gemini API call failed: HTTP 503")

    monkeypatch.setattr(translate_locales, "_call_gemini", engine)
    items = [
        translate_locales.WorkItem("de", "messages", key, "Hello")
        for key in ("one", "two", "three")
    ]
    _, _, _, dead = translate_locales._translate_locale("de", items)
    assert dead
    assert len(sleeps) == len(calls) - 1, "no wait after the give-up failure"


def test_budget_spent_mid_run_keeps_the_answers_and_stops(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """The budget can run out between two requests for the same surface; the
    strings already translated must be kept, and the run must stop with the
    budget as its reason instead of waiting out the request pacing or calling
    the engine again."""
    _deadline_in(monkeypatch, 3600)
    monkeypatch.setattr(translate_locales, "_MAX_CHARS_PER_REQUEST", 1)
    prompts: list[str] = []

    def answers_then_spends_the_budget(prompt: str) -> dict[str, Any]:
        prompts.append(prompt)
        _deadline_in(monkeypatch, -1)
        return {"messages:greeting": "Hallo"}

    monkeypatch.setattr(
        translate_locales, "_call_gemini", answers_then_spends_the_budget
    )
    items = [
        translate_locales.WorkItem("de", "messages", "greeting", "Hello"),
        translate_locales.WorkItem("de", "messages", "farewell", "Goodbye"),
    ]
    results, failures, failed, _ = translate_locales._translate_locale("de", items)
    assert len(prompts) == 1
    assert results == {("messages", "greeting"): "Hallo"}
    assert failures[0] == "de: time budget exhausted — stopping this run"
    assert failed == {("messages", "farewell")}
    assert sleeps == []
