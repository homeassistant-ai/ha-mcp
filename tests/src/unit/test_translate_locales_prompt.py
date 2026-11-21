"""What ``scripts/translate_locales.py`` asks the engine.

Each test runs the script's command line on a scratch repository and reads the
requests the engine received: which strings travel together, and which
existing translations the prompt shows as the catalog's style.

The samples are the catalog's address register, and only the samples. Nothing
downstream can recover it: the validation gate checks placeholders and markup,
the parity suite checks keys and how much text is still English. A sample pair
whose English does not address the reader therefore leaves the engine on its
own default for the language — measured against the shipped catalogs, that
turns German formal in a catalog whose own strings address the reader
informally throughout.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ._locale_sync_repo import SETTINGS_SURFACE, LocaleRepo, Request, answer

_SAMPLE_ENGLISH = {
    "long": "Restart the server, then refresh the tool list in your AI client.",
    "neutral": "Server Settings",
    "short": "Your browser blocks storage.",
    "middle": "Reload the page, then re-apply your changes.",
}
# A string the run owes, so a request goes out; it addresses nobody.
_OWED = {"owed": "Save"}

# Far longer than one request carries alongside anything else, so a string
# this size always travels on its own.
_HUGE = "word " * 10_000


@pytest.fixture
def repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> LocaleRepo:
    return LocaleRepo(tmp_path, monkeypatch, capsys)


def _samples(prompt: str) -> list[str]:
    """The English of the sample pairs a prompt shows, in order."""
    return [line[4:] for line in prompt.splitlines() if line.startswith("EN: ")]


def _settings_prompt(
    repo: LocaleRepo, english: dict[str, str], translated: dict[str, str] | None
) -> str:
    """The one request a run owing ``_OWED`` sends for these catalogs."""
    repo.write_settings("en", english | _OWED, native_name="English", tools=False)
    repo.write_settings("de", translated)
    run = repo.run()
    assert run.code == 0, run
    assert len(repo.engine.prompts) == 1, repo.engine.prompts
    return repo.engine.prompts[0]


class TestStyleSamples:
    def test_prefers_reader_addressing_sources_shortest_first(
        self, repo: LocaleRepo
    ) -> None:
        prompt = _settings_prompt(
            repo, _SAMPLE_ENGLISH, dict.fromkeys(_SAMPLE_ENGLISH, "…")
        )
        assert _samples(prompt) == [
            _SAMPLE_ENGLISH["short"],
            _SAMPLE_ENGLISH["middle"],
            _SAMPLE_ENGLISH["long"],
        ]

    def test_skips_keys_the_catalog_has_not_translated(self, repo: LocaleRepo) -> None:
        """``messages`` may omit keys — English is the per-key fallback — so an
        untranslated key carries no register and must not be sampled."""
        prompt = _settings_prompt(repo, _SAMPLE_ENGLISH, {"long": "…", "neutral": "…"})
        assert _samples(prompt) == [_SAMPLE_ENGLISH["long"]]

    @pytest.mark.parametrize("blank", ["", " ", "\n\t "])
    def test_skips_keys_whose_translation_is_blank(
        self, repo: LocaleRepo, blank: str
    ) -> None:
        """A blank value is a present key with nothing in it, and the sampler
        is where one still turns up: the script reads the catalogs with
        `json.loads`, and the parity ceilings count a key untranslated only
        when it equals the English or is missing, so `""` reads there as
        translated. Sampled, it spends one of three slots on a pair whose
        target side is empty — and a catalog whose register rests on one key
        would hand the engine that and nothing else. Whitespace counts as
        blank: it renders the same. The component catalog is where a blank
        value survives: nothing loads it before the script does.
        """
        repo.write_component("en", _SAMPLE_ENGLISH | _OWED)
        repo.write_component(
            "de", dict.fromkeys(_SAMPLE_ENGLISH, "…") | {"short": blank}
        )
        run = repo.run()
        assert run.code == 0, run
        assert _samples(repo.engine.prompts[0]) == [
            _SAMPLE_ENGLISH["middle"],
            _SAMPLE_ENGLISH["long"],
        ]

    def test_no_addressing_source_yields_no_samples(self, repo: LocaleRepo) -> None:
        """Degenerate but defined: an English catalog that never addresses the
        reader gives the prompt no style block rather than a neutral one, and
        the run says so."""
        repo.write_settings(
            "en", {"a": "Server Settings"} | _OWED, native_name="English", tools=False
        )
        repo.write_settings("de", {"a": "…"})
        run = repo.run()
        assert run.code == 0, run
        assert _samples(repo.engine.prompts[0]) == []
        assert "de/messages: no sample addresses the reader" in run.err

    def test_keys_in_the_request_are_not_sampled(self, repo: LocaleRepo) -> None:
        """A key is in a request because its English moved, so its committed
        translation renders the previous English. Shown as the sample for the
        very string being translated, the model answers with that stale text —
        measured against the live engine, byte-identical to the old wording."""
        repo.stale(SETTINGS_SURFACE, "messages.short")
        prompt = _settings_prompt(
            repo, _SAMPLE_ENGLISH, dict.fromkeys(_SAMPLE_ENGLISH, "…")
        )
        assert _samples(prompt) == [_SAMPLE_ENGLISH["middle"], _SAMPLE_ENGLISH["long"]]

    def test_reflexive_second_person_counts_as_addressing(
        self, repo: LocaleRepo
    ) -> None:
        """`yourself` / `yourselves` address the reader as much as `your`; no
        English string uses them today, so this pins the intent rather than a
        current behaviour."""
        english = {"a": "Give yourself access.", "b": "Help yourselves."}
        prompt = _settings_prompt(repo, english, dict.fromkeys(english, "…"))
        assert _samples(prompt) == ["Help yourselves.", "Give yourself access."]


class TestPromptRegisterRule:
    """The rule points at the samples, so it carries nothing without them."""

    def test_rule_ships_with_the_samples(self, repo: LocaleRepo) -> None:
        prompt = _settings_prompt(
            repo,
            {"greeting": "Restart your client."},
            {"greeting": "Starte deinen Client neu."},
        )
        assert "Starte deinen Client neu." in prompt
        assert "Address the reader the way the sample translations" in prompt

    def test_stub_catalog_gets_neither(self, repo: LocaleRepo) -> None:
        """A new language starts as a stub with no ``messages`` section, which
        the pipeline fills: it must yield no samples rather than raise."""
        prompt = _settings_prompt(repo, {"greeting": "Restart your client."}, None)
        assert "Match the tone and terminology" not in prompt
        assert "Address the reader the way" not in prompt

    def test_component_work_samples_the_component_catalog(
        self, repo: LocaleRepo
    ) -> None:
        """The two authored surfaces need not share a register — French ships a
        `vous`-only component catalog next to a settings catalog that mixes `tu`
        and `vous` — so a component request must not be told to imitate the
        settings catalog's register."""
        repo.write_settings(
            "en",
            {"greeting": "Restart your client."},
            native_name="English",
            tools=False,
        )
        repo.write_settings("de", {"greeting": "Starte deinen Client neu."})
        repo.write_component("en", {"step": {"title": "Check your token."} | _OWED})
        repo.write_component("de", {"step": {"title": "Prüfen Sie Ihr Token."}})
        run = repo.run()
        assert run.code == 0, run
        [prompt] = repo.engine.prompts
        assert "component:step.owed" in prompt
        assert "Prüfen Sie Ihr Token." in prompt
        assert "Starte deinen Client neu." not in prompt


class TestRequests:
    def test_a_batch_is_cut_into_requests_without_splitting_a_string(
        self, repo: LocaleRepo
    ) -> None:
        """Small strings share a request; one too long to share goes alone and
        whole, and the strings after it start a new request."""
        english = {
            "first": "One",
            "second": "Two",
            "huge": _HUGE,
            "third": "Three",
        }
        repo.write_settings("en", english, native_name="English", tools=False)
        run = repo.run()
        assert run.code == 0, run
        assert [list(request.strings) for request in repo.engine.requests] == [
            ["messages:first", "messages:second"],
            ["messages:huge"],
            ["messages:third"],
        ]
        assert repo.engine.requests[1].strings["messages:huge"] == english["huge"]

    def test_a_run_that_owes_nothing_calls_no_engine(
        self, repo: LocaleRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The daily sync no-ops on a translated tree; it must not need the
        engine, or its key, to do so."""
        monkeypatch.delenv("GEMINI_API_KEY")
        repo.write_settings(
            "en", {"greeting": "Hello"}, native_name="English", tools=False
        )
        repo.write_settings("de", {"greeting": "Hallo"})
        run = repo.run()
        assert run.code == 0, run
        assert run.out.startswith("0 string(s) to translate")
        assert repo.engine.requests == []

    def test_run_wide_queue_reaches_every_request_and_the_retry(
        self, repo: LocaleRepo
    ) -> None:
        """The exclusion is computed for the whole run and handed to each
        request; a single-request run cannot show that. Two requests plus a
        forced retry, and no prompt may quote a key the run still owes —
        including a key a LATER request owes: results are applied only after
        every request finishes, so that key still carries the translation of
        the previous English."""
        english = {
            "first": f"Restart your client. {_HUGE}",
            "second": f"Reload your page. {_HUGE}",
            "sample": "Check your token.",
        }
        repo.write_settings("en", english, native_name="English", tools=False)
        repo.write_settings("de", {key: f"DE-{key} deinen" for key in english})
        repo.stale(SETTINGS_SURFACE, "messages.first", "messages.second")

        def empty(request: Request) -> httpx.Response:
            # Empty answers fail validation, so every string is also retried.
            return answer(dict.fromkeys(request.strings, ""))

        repo.engine.reply = empty
        repo.run()

        requests = repo.engine.requests
        assert len([r for r in requests if not r.is_retry]) == 2, (
            "the work has to span two requests — inside one, the run-wide set "
            "and the request-local one are the same thing"
        )
        assert any(request.is_retry for request in requests), "expected retries too"
        for prompt in repo.engine.prompts:
            assert "DE-first" not in prompt
            assert "DE-second" not in prompt
        assert any("DE-sample" in prompt for prompt in repo.engine.prompts), (
            "the untouched key should still be sampled — otherwise this test "
            "would pass with sampling switched off entirely"
        )
