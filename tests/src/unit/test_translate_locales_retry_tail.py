"""The retries, request pacing and time budget of ``scripts/translate_locales.py``.

Every retryable failure of an engine request, a transport error as much as an
unusable answer, ends in one shared tail: stop after the fifth attempt, stop
once the time budget is spent, otherwise wait and retry. The corrective retry
a rejected string gets runs only while the same budget lasts. Each request is
followed by a wait, answered or failed, while the budget lasts and the engine
has not been given up on, and a run whose budget runs out between two requests
stops there.

Each test runs the script's command line on a scratch repository, with the
Gemini API answering at httpx's transport. The clock the budget reads stands
still unless the engine spends the budget while it answers, and every wait is
recorded rather than slept.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from ._locale_sync_repo import LocaleRepo, Request, answer
from ._locale_sync_repo import repo as repo

_HALLO = {"messages:greeting": "Hallo"}
# Far longer than one request carries alongside anything else.
_HUGE = "word " * 10_000


Step = Callable[[Request], httpx.Response]


def _engine(repo: LocaleRepo, *steps: Step) -> list[int]:
    """The engine answers request ``n`` with ``steps[n]``; the waits made
    before each request are returned as they happen."""
    waits_before: list[int] = []

    def reply(request: Request) -> httpx.Response:
        waits_before.append(len(repo.sleeps))
        return steps[len(waits_before) - 1](request)

    repo.engine.reply = reply
    return waits_before


def _respond(
    response: httpx.Response | Exception, *, spend: LocaleRepo | None = None
) -> Step:
    """A step answering ``response``; with ``spend``, the budget runs out first."""

    def step(_request: Request) -> httpx.Response:
        if spend is not None:
            spend.clock.spend_the_budget()
        if isinstance(response, Exception):
            raise response
        return response

    return step


def _failure(run_err: str, key: str) -> str:
    """The failure line the run reported for ``key``."""
    [line] = [line.strip() for line in run_err.splitlines() if f"/{key}: " in line]
    return line


@pytest.mark.parametrize("failure", ["transport", "status"])
def test_failed_attempts_wait_before_each_retry_but_not_after_the_last(
    repo: LocaleRepo, failure: str
) -> None:
    """Both retryable failures share the tail. The fifth attempt ends the call
    even when the budget runs out during it, so the error names no budget."""
    failed: httpx.Response | Exception = (
        httpx.ConnectError("name resolution failed")
        if failure == "transport"
        else httpx.Response(503, text="unavailable")
    )
    waits_before = _engine(
        repo, *([_respond(failed)] * 4), _respond(failed, spend=repo)
    )
    run = repo.run()
    assert run.code == 2
    assert "time budget" not in _failure(run.err, "greeting")
    assert waits_before == [0, 1, 2, 3, 4], "each retry follows one more wait"
    assert len(repo.sleeps) == 4, "no wait after the fifth attempt"
    assert all(delay > 0 for delay in repo.sleeps), "a retry must wait"


def test_spent_budget_stops_the_retries(repo: LocaleRepo) -> None:
    """The budget can run out while an attempt is in flight; the tail must
    then stop instead of sleeping into another attempt."""
    _engine(repo, _respond(httpx.ReadTimeout("read timed out"), spend=repo))
    run = repo.run()
    assert run.code == 2
    assert len(repo.engine.requests) == 1
    assert "(time budget exhausted; not retrying)" in _failure(run.err, "greeting")
    assert "transport error" in _failure(run.err, "greeting")
    assert repo.sleeps == []


def test_rejected_string_gets_one_retry_while_the_budget_lasts(
    repo: LocaleRepo,
) -> None:
    waits_before = _engine(
        repo, _respond(answer({"messages:greeting": ""})), _respond(answer(_HALLO))
    )
    run = repo.run()
    assert run.code == 0, run
    assert [request.is_retry for request in repo.engine.requests] == [False, True]
    assert waits_before == [0, 1]
    assert len(repo.sleeps) == 2, "the retry is paced like any other request"
    assert all(delay > 0 for delay in repo.sleeps), "a retry must wait"
    assert repo.settings("de")["messages"]["greeting"] == "Hallo"


def test_spent_budget_skips_the_validation_retry(repo: LocaleRepo) -> None:
    """An answer can arrive after the deadline; each string it got wrong must
    then fail at once, without a retry call and its pacing wait."""
    _engine(repo, _respond(answer({"messages:greeting": ""}), spend=repo))
    run = repo.run()
    assert run.code == 2
    assert len(repo.engine.requests) == 1
    assert repo.sleeps == []
    assert _failure(run.err, "greeting").endswith(
        "(time budget exhausted; not retrying)"
    )


@pytest.mark.parametrize("budget_lasts", [True, False])
def test_valid_answer_is_kept_without_a_retry_or_a_wait(
    repo: LocaleRepo, budget_lasts: bool
) -> None:
    """A valid answer, late or not, must not cost a retry or a 7 s wait of its
    own: the only wait is the one pacing the request, while the budget lasts."""
    _engine(repo, _respond(answer(_HALLO), spend=None if budget_lasts else repo))
    run = repo.run()
    assert run.code == 0, run
    assert len(repo.engine.requests) == 1
    assert repo.settings("de")["messages"]["greeting"] == "Hallo"
    assert len(repo.sleeps) == (1 if budget_lasts else 0)


def test_retry_answered_after_the_deadline_skips_its_pacing_wait(
    repo: LocaleRepo,
) -> None:
    """The retry can return after the deadline; its pacing wait then paces
    no request and only eats into the slack before the job timeout."""
    _engine(
        repo,
        _respond(answer({"messages:greeting": ""})),
        _respond(answer(_HALLO), spend=repo),
    )
    run = repo.run()
    assert run.code == 0, run
    assert repo.engine.requests[-1].is_retry, "the retry ran"
    assert repo.settings("de")["messages"]["greeting"] == "Hallo"
    assert len(repo.sleeps) == 1, "only the first request was paced"


@pytest.mark.parametrize("crosses_deadline", [False, True])
def test_failed_retry_fails_the_string_not_the_run(
    repo: LocaleRepo, crosses_deadline: bool
) -> None:
    """A retry the engine fails must leave only its string failed; its wait
    still paces the next request while the budget lasts. A retry that failed
    after the deadline says the budget is spent, so the failure summary does
    not hide it."""
    repo.write_settings(
        "en",
        {"greeting": "Hello", "farewell": "Goodbye"},
        native_name="English",
        tools=False,
    )
    _engine(
        repo,
        _respond(answer({"messages:greeting": "", "messages:farewell": "Tschüss"})),
        _respond(httpx.Response(400), spend=repo if crosses_deadline else None),
    )
    run = repo.run()
    assert run.code == 2
    assert repo.settings("de")["messages"] == {"farewell": "Tschüss"}
    assert len(repo.sleeps) == (1 if crosses_deadline else 2)
    budget_note = _failure(run.err, "greeting").endswith(
        "(retry failed; time budget exhausted)"
    )
    assert budget_note is crosses_deadline


@pytest.mark.parametrize("first_call_fails", [False, True])
def test_each_request_is_paced_while_the_budget_lasts(
    repo: LocaleRepo, first_call_fails: bool
) -> None:
    """Unpaced requests run into the engine's rate limit; while the budget
    lasts, every request must be followed by a wait, a failed one too: it can
    end on a rate-limit answer. Each surface goes out in a request of its
    own, so a settings string and a component string make two."""
    repo.write_component("en", {"farewell": "Goodbye"})
    first = (
        _respond(httpx.Response(400)) if first_call_fails else _respond(answer(_HALLO))
    )
    waits_before = _engine(
        repo, first, _respond(answer({"component:farewell": "Tschüss"}))
    )
    run = repo.run()
    assert run.code == (2 if first_call_fails else 0), run
    assert waits_before == [0, 1], "a wait between the requests"
    assert len(repo.sleeps) == 2, "a wait after each request"
    assert all(delay > 0 for delay in repo.sleeps), "a request must be paced"


def test_a_request_failing_past_the_deadline_skips_its_pacing_wait(
    repo: LocaleRepo,
) -> None:
    """A failed request can return after the deadline; its pacing wait then
    paces no request, since the run stops on the budget."""
    _engine(repo, _respond(httpx.Response(400), spend=repo))
    assert repo.run().code == 2
    assert repo.sleeps == []


def test_giving_up_on_the_engine_skips_the_pacing_wait(repo: LocaleRepo) -> None:
    """The failed request that gives up on the engine stops the run's engine
    calls, so a wait after it would pace no request."""
    repo.write_settings(
        "en",
        {key: f"{key} {_HUGE}" for key in ("one", "two", "three")},
        native_name="English",
        tools=False,
    )
    _engine(repo, *([_respond(httpx.Response(400))] * 3))
    run = repo.run()
    assert run.code == 2
    assert "de/messages/three: not attempted" in run.err, "the engine was given up on"
    assert len(repo.sleeps) == len(repo.engine.requests) - 1, (
        "no wait after the give-up failure"
    )


def test_budget_spent_mid_run_keeps_the_answers_and_stops(repo: LocaleRepo) -> None:
    """The budget can run out between two requests; the strings already
    translated must be kept, and the run must stop with the budget as its
    reason instead of waiting out the request pacing or calling the engine
    again."""
    repo.write_component("en", {"farewell": "Goodbye"})
    _engine(repo, _respond(answer(_HALLO), spend=repo))
    run = repo.run()
    assert run.code == 2
    assert len(repo.engine.requests) == 1
    assert repo.settings("de")["messages"]["greeting"] == "Hallo"
    assert "farewell" not in repo.component("de")
    assert "de: time budget exhausted — stopping this run" in run.err
    assert repo.sleeps == []
