"""The gate in ``scripts/translate_locales.py`` that decides what may be written.

The translation pipeline runs unattended in CI and commits to PR branches, so
an engine answer reaches a catalog only after it passes the checks the merge
gate runs later. Each test runs the script's command line on a scratch
repository that owes ``de`` one string, with the engine giving the same answer
to the request and to the retry, and reads back what was written: a refused
answer leaves the string unwritten and the run red. The last test is the
sync's own no-op check, run by the locale-sync workflow on the real tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from ._locale_sync_repo import REPO_ROOT, LocaleRepo, Request, answer


@pytest.fixture
def repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> LocaleRepo:
    return LocaleRepo(tmp_path, monkeypatch, capsys)


def _translate(
    repo: LocaleRepo, english: str, translated: object, section: str = "messages"
) -> str | None:
    """Run the pipeline with the engine answering ``translated``; what it wrote."""
    if section == "messages":
        repo.write_settings(
            "en", {"greeting": english}, native_name="English", tools=False
        )
    else:
        repo.write_component("en", {"step": {"greeting": english}})

    def reply(request: Request) -> httpx.Response:
        return answer(dict.fromkeys(request.strings, translated))

    repo.engine.reply = reply
    run = repo.run()
    if section == "messages":
        written = repo.settings("de").get("messages", {}).get("greeting")
        key = "greeting"
    else:
        written = repo.component("de").get("step", {}).get("greeting")
        key = "step.greeting"
    if written is None:
        assert run.code == 2, run
        assert f"de/{section}/{key}: " in run.err, run.err
        return None
    assert run.code == 0, run
    return str(written)


def _expect(repo: LocaleRepo, english: str, translated: str, *, accepted: bool) -> None:
    """``translated`` is written when ``accepted``, and refused otherwise."""
    assert _translate(repo, english, translated) == (translated if accepted else None)


class TestValidate:
    def test_accepts_a_plain_translation(self, repo: LocaleRepo) -> None:
        assert _translate(repo, "Hello", "Hallo") == "Hallo"

    @pytest.mark.parametrize(
        ("english", "bad"),
        [
            ("Range 1-600.", "Bereich 1-900."),
            ("keeps 30 entries", "behaelt 50 Eintraege"),
            ("keeps 30 entries", "behaelt Eintraege"),
            ("keeps entries", "behaelt 30 Eintraege"),
            ("the 5 experimental sub-flags", "die fuenf experimentellen Unterschalter"),
        ],
    )
    def test_refuses_any_number_the_english_does_not_state(
        self, repo: LocaleRepo, english: str, bad: str
    ) -> None:
        """A swapped, dropped or invented number, or an English digit spelled out.

        The engine asks what the merge gate asks: a number difference let
        through here lands as a backfilled key the merge gate then refuses,
        and that holds the whole push back every day.
        """
        assert _translate(repo, english, bad) is None

    def test_accepts_the_numbers_the_english_states(self, repo: LocaleRepo) -> None:
        assert _translate(repo, "Range 1-600.", "Bereich 1-600.") == "Bereich 1-600."

    @pytest.mark.parametrize(
        ("english", "translated", "accepted_locales"),
        [
            ("90%", "%90", {"tr"}),
            ("90%", "90 %", {"de", "tr"}),
            ("90%", "90", set()),
            ("90%", "%95", set()),
            ("90.5%", "%90,5", {"tr"}),
            ("5% and 90.5", "5 ve %90,5", set()),
        ],
    )
    def test_percentage_placement(
        self,
        repo: LocaleRepo,
        english: str,
        translated: str,
        accepted_locales: set[str],
    ) -> None:
        repo.write_settings(
            "en", {"saving": english}, native_name="English", tools=False
        )
        repo.write_settings("tr", {}, native_name="Türkçe")
        repo.engine.reply = lambda request: answer(
            dict.fromkeys(request.strings, translated)
        )

        run = repo.run()
        for locale in ("de", "tr"):
            written = repo.settings(locale)["messages"].get("saving")
            assert written == (translated if locale in accepted_locales else None), run
        assert run.code == (0 if accepted_locales == {"de", "tr"} else 2), run

    @pytest.mark.parametrize("bad", ["", "   ", None, 42])
    def test_rejects_empty_and_non_string(self, repo: LocaleRepo, bad: object) -> None:
        assert _translate(repo, "Hello", bad) is None

    @pytest.mark.parametrize(
        ("english", "translated", "accepted"),
        [
            ("Hello {name}", "Hallo", False),  # dropped {name}
            ("Hello", "Hallo {name}", False),  # invented {name}
            ("Hello {name}", "Hallo {name}", True),
        ],
    )
    def test_rejects_placeholder_drift_both_directions(
        self, repo: LocaleRepo, english: str, translated: str, accepted: bool
    ) -> None:
        _expect(repo, english, translated, accepted=accepted)

    @pytest.mark.parametrize(
        ("translated", "accepted"),
        [
            ("ein <b>Wort</b>", False),
            ("ein <CODE>Wort</CODE>", False),
            ("ein <code>Wort</code>", True),
        ],
    )
    def test_rejects_markup_outside_the_allowlist_in_messages(
        self, repo: LocaleRepo, translated: str, accepted: bool
    ) -> None:
        written = _translate(repo, "a <code>word</code>", translated)
        assert written == (translated if accepted else None)

    @pytest.mark.parametrize(
        ("english", "translated", "accepted"),
        [
            ("a <strong>bold</strong> word", "ein <strong>fettes Wort", False),
            ("plain", "ein <code>Wort</code>", False),
            ("a <strong>bold</strong> word", "ein <strong>fettes</strong> Wort", True),
        ],
    )
    def test_rejects_formatting_tag_multiset_drift(
        self, repo: LocaleRepo, english: str, translated: str, accepted: bool
    ) -> None:
        """A dropped closing tag or an invented allowlisted tag is still
        malformed markup for tHtml, even though every tag passes the
        allowlist individually."""
        _expect(repo, english, translated, accepted=accepted)

    @pytest.mark.parametrize(
        ("english", "bad"),
        [
            ("See docs/beta.md for limits.", "Siehe die Beta-Dokumentation."),
            ("Set enable_tool_search to true.", "Aktiviere die Werkzeugsuche."),
            ("roughly 90% less", "etwa 46K weniger"),
            ("limit is 1-256 MB", "Grenze ist 1-256 GB"),
            ("limit is 1-256 MB", "Grenze ist 1-256"),
            (
                "call ha_get_skill_guide, then ha_get_skill_guide again",
                "rufe ha_get_skill_guide, dann wrong_tool erneut",
            ),
        ],
    )
    def test_rejects_output_the_merge_gate_would_refuse(
        self, repo: LocaleRepo, english: str, bad: str
    ) -> None:
        """The engine must not produce what the parity gate later refuses.

        A dropped identifier that is accepted here lands as a backfilled key,
        and no later run re-queues it: from then on the merge-time arm goes
        red every day, the push is held back whole, and the run re-spends its
        quota planning work it cannot land. One rejection and one retry costs
        a single string instead. The first three cases are the faults #2180
        repaired by hand; the rest are a wrong unit, a dropped unit, and a
        repeated identifier the translation corrupts once.
        """
        assert _translate(repo, english, bad) is None, (
            f"the engine's {bad!r} for {english!r} was written, which the "
            "merge-time literal-parity check refuses"
        )

    @pytest.mark.parametrize(
        ("translated", "accepted"),
        [
            ("5분 동안 5회 이상 잘못된 PIN을 입력하면 채널이 차단됩니다.", True),
            ("Fuenf falsche PINs innerhalb von fuenf Minuten sperren den Kanal.", True),
            ("6분 동안 5회 잘못된 PIN", False),
        ],
    )
    def test_accepts_digits_for_a_number_the_english_spells_out(
        self, repo: LocaleRepo, translated: str, accepted: bool
    ) -> None:
        """Korean writes "five minutes" as "5분"; the word may stay a word too.

        Locale Sync accepted this string and the pre-push check refused it,
        blocking the whole push on every run until the two agreed.
        """
        english = "Five wrong PINs within five minutes close the channel."
        _expect(repo, english, translated, accepted=accepted)

    def test_markup_rules_apply_only_to_messages(self, repo: LocaleRepo) -> None:
        """Tool and component strings render through escapeHtml / HA core, so
        the settings-UI markup allowlist deliberately does not gate them."""
        assert _translate(repo, "x", "<b>y</b>", section="component") == "<b>y</b>"

    @pytest.mark.parametrize(
        ("target", "accepted"), [("tools", True), ("backups", False)]
    )
    def test_rejects_panel_link_target_drift(
        self, repo: LocaleRepo, target: str, accepted: bool
    ) -> None:
        english = 'see the <a href="#" data-panel-link="tools">Tools</a> tab'
        translated = f'siehe <a href="#" data-panel-link="{target}">Tools</a>'
        _expect(repo, english, translated, accepted=accepted)


class TestHardcodedOptionLabels:
    """Python hardcodes a handful of on-screen names — selector labels and the
    titles of the config entries themselves — so Home Assistant shows them in
    English whatever the reader's language is. A catalog that localises one
    sends the reader looking for something that is not on the screen: the
    failure that stopped a whole sync run, since the engine only had a prose
    rule telling it not to.

    The names are read from the sources, so an empty name set would accept
    every translation and still report a clean run. A selector label and a
    config-entry title are each covered below, because either source pattern
    can stop matching on its own."""

    @pytest.mark.parametrize(
        ("translated", "accepted"),
        [
            ('Lokaal/LAN (wanneer Netwerktoegang "Lokaal netwerk" is): {url}', False),
            ('Lokaal/LAN (wanneer Netwerktoegang "Local network" is): {url}', True),
        ],
    )
    def test_rejects_a_localised_hardcoded_label(
        self, repo: LocaleRepo, translated: str, accepted: bool
    ) -> None:
        english = 'Local/LAN (when Network access is "Local network"): {url}'
        written = _translate(repo, english, translated, section="component")
        assert written == (translated if accepted else None)

    def test_reads_the_label_through_typographic_quotes(self, repo: LocaleRepo) -> None:
        # Shipped English already quotes both ways, so which mark an author
        # reached for must not decide whether the label is protected.
        english = "Local/LAN (when Network access is “Local network”): {url}"
        localised = 'Lokaal/LAN (wanneer Netwerktoegang "Lokaal netwerk" is): {url}'
        assert _translate(repo, english, localised, section="component") is None

    @pytest.mark.parametrize(
        ("title", "accepted"),
        [("HA-MCP Bestands- & YAML-tools", False), ("HA-MCP File & YAML Tools", True)],
    )
    def test_rejects_a_localised_entry_title(
        self, repo: LocaleRepo, title: str, accepted: bool
    ) -> None:
        # The exact string the 2026-08-08 nl fill shipped: the reader is sent
        # to an entry whose title the integration hardcodes, under a name that
        # entry never has.
        english = (
            'Not installed — press "Add entry" on this integration\'s page and '
            'choose "HA-MCP File & YAML Tools" to add it'
        )
        translated = (
            'Niet geïnstalleerd — druk op "Item toevoegen" op de pagina van deze '
            f'integratie en kies "{title}" om deze toe te voegen'
        )
        written = _translate(repo, english, translated, section="component")
        assert written == (translated if accepted else None)

    @pytest.mark.parametrize(
        ("name", "accepted"), [("HA-MCP Servidor", False), ("HA-MCP Server", True)]
    )
    def test_rejects_a_localised_name_in_single_quotes(
        self, repo: LocaleRepo, name: str, accepted: bool
    ) -> None:
        # Shipped English at options.step.init.data_description.enable_llm_api
        # spells this one with apostrophes rather than double quotes. Single
        # quotes are matched against the known names instead of being paired
        # like the other marks: the apostrophe in "integration's" is the same
        # character, so pairing on it shifts every quote in such a sentence —
        # measured on the shipped catalogs, pairing loses a live violation.
        english = "agents can select 'HA-MCP Server' under Control Home Assistant"
        translated = f"agenten kunnen '{name}' kiezen onder Bediening"
        written = _translate(repo, english, translated, section="component")
        assert written == (translated if accepted else None)

    def test_apostrophes_do_not_hide_a_double_quoted_name(
        self, repo: LocaleRepo
    ) -> None:
        # The regression the obvious repair would have caused: this English
        # carries an apostrophe before the quoted title.
        english = (
            'press "Add entry" on this integration\'s page and choose '
            '"HA-MCP File & YAML Tools" to add it'
        )
        localised = 'druk op "Item toevoegen" en kies "HA-MCP Bestands-tools"'
        assert _translate(repo, english, localised, section="component") is None

    def test_leaves_other_quoted_text_translatable(self, repo: LocaleRepo) -> None:
        # "Add entry" is Home Assistant's own button: HA translates it, so
        # every shipped catalog translates the quote too. Only labels the
        # config flow hardcodes are pinned to English.
        english = 'Not installed — press "Add entry" on this integration\'s page'
        translated = 'Nicht installiert — klicke auf "Eintrag hinzufügen"'
        written = _translate(repo, english, translated, section="component")
        assert written == translated


# Same gate as ``test_locale_parity.completeness`` (see the marker comment
# there): this asserts the LIVE tree owes no translations, which any PR that
# changes an English string legitimately violates until the post-merge sync
# runs. ``test_locale_sync_gate_shape`` pins the wiring.
@pytest.mark.skipif(
    not os.environ.get("LOCALE_COMPLETENESS_CHECKS"),
    reason=(
        "translated-catalog completeness is verified by the post-merge "
        "locale-sync workflow — set LOCALE_COMPLETENESS_CHECKS=1 to run"
    ),
)
def test_clean_tree_plans_no_work() -> None:
    """The sync's own no-op invariant: after a run repins the baseline, a
    rerun must find nothing — verified in the workflow, where it runs against
    the freshly translated tree."""
    completed = subprocess.run(
        [sys.executable, "scripts/translate_locales.py", "--dry-run"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.startswith(
        "0 string(s) to translate across 0 locale(s); 0 orphan deletion(s)"
    ), completed.stdout
