"""How ``scripts/translate_locales.py`` talks to the engine and survives it failing.

Each test runs the script's command line on a scratch repository that owes
``de`` one or more strings, with the Gemini API answering at httpx's transport:
the request the script builds, the retries it makes, and how a failed request
degrades the run. Only the real API's happy path is left to the live workflow.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from ._locale_sync_repo import LocaleRepo, Request, answer, envelope

_HALLO = answer({"messages:greeting": "Hallo"})


@pytest.fixture
def repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> LocaleRepo:
    repo = LocaleRepo(tmp_path, monkeypatch, capsys)
    repo.write_settings("en", {"greeting": "Hello"}, native_name="English", tools=False)
    return repo


def _replies(repo: LocaleRepo, *responses: httpx.Response | Exception) -> None:
    """The engine answers each request with the next of ``responses``."""
    queue: Iterator[httpx.Response | Exception] = iter(responses)

    def reply(_request: Request) -> httpx.Response:
        response = next(queue)
        if isinstance(response, Exception):
            raise response
        return response

    repo.engine.reply = reply


def _greeting(repo: LocaleRepo) -> Any:
    return repo.settings("de").get("messages", {}).get("greeting")


class TestCallGeminiRetry:
    def test_uses_current_model_without_deprecated_sampling_parameters(
        self, repo: LocaleRepo
    ) -> None:
        _replies(repo, _HALLO)
        assert repo.run().code == 0
        [request] = repo.engine.requests
        assert request.url == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.5-flash-lite:generateContent"
        )
        generation_config = request.body["generationConfig"]
        assert generation_config["response_mime_type"] == "application/json"
        assert {"temperature", "topP", "topK"}.isdisjoint(generation_config)

    def test_retries_transient_statuses_then_succeeds(self, repo: LocaleRepo) -> None:
        _replies(repo, httpx.Response(429), httpx.Response(503), _HALLO)
        assert repo.run().code == 0
        assert len(repo.engine.requests) == 3
        assert _greeting(repo) == "Hallo"

    def test_retries_transport_errors(self, repo: LocaleRepo) -> None:
        _replies(repo, httpx.ConnectTimeout("boom"), _HALLO)
        assert repo.run().code == 0
        assert len(repo.engine.requests) == 2
        assert _greeting(repo) == "Hallo"

    def test_terminal_status_fails_the_string_without_retry(
        self, repo: LocaleRepo
    ) -> None:
        _replies(repo, httpx.Response(400, text="bad request"))
        run = repo.run()
        assert run.code == 2
        assert len(repo.engine.requests) == 1
        assert "de/messages/greeting: engine call failed" in run.err
        assert "HTTP 400" in run.err

    def test_missing_key_is_a_clear_error(
        self, repo: LocaleRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GEMINI_API_KEY")
        run = repo.run()
        assert run.code == 2
        assert "GEMINI_API_KEY is not set" in run.err
        assert repo.engine.requests == []

    def test_blocked_response_is_a_clear_error(self, repo: LocaleRepo) -> None:
        """HTTP 200 with an empty candidates list (safety-filter block) must
        read like the other engine failures, not a raw IndexError — and a
        prompt-level block is deterministic, so it is not retried."""
        blocked = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        _replies(repo, envelope(blocked))
        run = repo.run()
        assert run.code == 2
        assert len(repo.engine.requests) == 1
        assert "unusable response" in run.err
        assert 'promptFeedback={"blockReason": "SAFETY"}' in run.err

    # A 200 whose JSON object stops a third of the way in, as the engine
    # answered once for a 95-string chunk the identical prompt then
    # completed on the next call.
    _CUT_OFF: ClassVar[dict[str, Any]] = {
        "candidates": [
            {
                "content": {"parts": [{"text": '{\n "s0": "你好",\n'}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"candidatesTokenCount": 7},
    }

    @pytest.mark.parametrize(
        "unusable",
        [
            pytest.param(envelope(_CUT_OFF), id="cut-off answer"),
            # A proxy answering ``[]``, or an HTML error page behind a 200.
            pytest.param(envelope([]), id="non-object envelope"),
            pytest.param(httpx.Response(200, text="<html>"), id="not json"),
            pytest.param(
                envelope({"candidates": {"not": "a list"}, "promptFeedback": "x"}),
                id="odd-shaped envelope fields",
            ),
            # Every ``response.get`` downstream would be an AttributeError.
            pytest.param(
                envelope({"candidates": [{"content": {"parts": [{"text": "[]"}]}}]}),
                id="non-object answer",
            ),
        ],
    )
    def test_an_unusable_answer_is_retried_like_a_transient_status(
        self, repo: LocaleRepo, unusable: httpx.Response
    ) -> None:
        """An answer the script cannot read reaches the same report and retry
        as a transient status, not a traceback from the envelope parse."""
        _replies(repo, unusable, _HALLO)
        assert repo.run().code == 0
        assert len(repo.engine.requests) == 2
        assert _greeting(repo) == "Hallo"

    def test_persistently_unparseable_answer_names_the_finish_reason(
        self, repo: LocaleRepo
    ) -> None:
        _replies(repo, *(envelope(self._CUT_OFF) for _ in range(5)))
        run = repo.run()
        assert run.code == 2
        assert len(repo.engine.requests) == 5
        assert "unusable response" in run.err
        assert "finishReason=STOP" in run.err
        assert '"candidatesTokenCount": 7' in run.err
        cut_text = self._CUT_OFF["candidates"][0]["content"]["parts"][0]["text"]
        assert f"ends {cut_text!r})" in run.err


class TestEngineFailureDegradation:
    """Each surface goes out in a request of its own, so a run owing one
    settings string and one component string makes two requests."""

    @pytest.fixture(autouse=True)
    def _two_requests(self, repo: LocaleRepo) -> None:
        repo.write_settings("en", {"first": "One"}, native_name="English", tools=False)
        repo.write_component("en", {"second": "Two"})

    def test_one_failed_request_degrades_to_per_string_failures(
        self, repo: LocaleRepo
    ) -> None:
        _replies(repo, httpx.Response(400), answer({"component:second": "Zwei"}))
        run = repo.run()
        assert run.code == 2
        assert "de/messages/first: engine call failed" in run.err
        assert "first" not in repo.settings("de").get("messages", {})
        assert repo.component("de") == {"second": "Zwei"}

    def test_failures_count_across_a_locale_not_per_surface(
        self, repo: LocaleRepo
    ) -> None:
        """One failure ending the settings surface and one opening the component
        surface are two consecutive failures against the same engine. A counter
        living inside the per-surface call resets between them, so the run keeps
        calling an engine that has already failed twice in a row."""
        # A later locale the engine, given up on, must not be asked about.
        repo.write_settings("fr", {}, native_name="Français")
        repo.write_component("fr", {})
        _replies(repo, httpx.Response(400), httpx.Response(400))
        run = repo.run()
        assert run.code == 2
        assert len(repo.engine.requests) == 2, "the run must stop after two failures"
        assert "fr: 2 string(s) skipped" in run.err


class TestTimeBudget:
    def test_exhausted_budget_degrades_like_a_quota_hit(
        self, repo: LocaleRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the self-imposed wall-clock bound trips, no further engine
        calls happen and every unfinished string is a recorded failure — the
        same resumable shape as an engine failure, instead of the job timeout
        losing everything uncommitted."""
        monkeypatch.setenv("TRANSLATE_TIME_BUDGET_SECONDS", "-1")
        repo.write_settings(
            "en", {"first": "One", "second": "Two"}, native_name="English", tools=False
        )
        run = repo.run()
        assert run.code == 2
        assert repo.engine.requests == []
        assert "de: time budget exhausted — stopping this run" in run.err
        assert "de/messages/first: not attempted" in run.err
        assert "de/messages/second: not attempted" in run.err

    def test_engine_refuses_to_start_past_the_deadline(
        self, repo: LocaleRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worst-case engine call is ~20 minutes of timeouts and backoff —
        it must not start once the budget is spent, or it eats the slack
        between the script's bound and the work-losing job timeout. The clock
        here moves on every reading, so the budget runs out between the
        run's own check and the engine's."""
        monkeypatch.setenv("TRANSLATE_TIME_BUDGET_SECONDS", "1.5")
        clock = repo.clock

        def ticking() -> float:
            clock.now += 1
            return clock.now

        monkeypatch.setattr("time.monotonic", ticking)
        run = repo.run()
        assert run.code == 2
        assert repo.engine.requests == []
        assert "time budget exhausted before the engine call" in run.err
