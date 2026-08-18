"""Unit coverage for ``scripts/translate_locales.py``.

The translation pipeline runs unattended in CI and commits to PR branches, so
its network-free logic is pinned here: the validation gate that decides
whether an engine answer may be written at all, the baseline-diff planning
that decides what gets retranslated or deleted, the request chunking, the
retry/backoff branching (with a monkeypatched transport — no real network),
and the catalog write-back. Only ``_call_gemini``'s happy path against the
real API is left to the live workflow run.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import translate_locales  # noqa: E402
from translate_locales import (  # noqa: E402
    Plan,
    WorkItem,
    _chunk,
    _flatten,
    _plan_locale_groups,
    _plan_locale_messages,
    _plan_locale_tools,
    _unflatten,
    _validate,
)

_SETTINGS_LOCALES = _REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales"
# Catalogs that carry translated text. A catalog with no `messages` yet has no
# wording to sample, so sampling it would fail on emptiness rather than on the
# property this checks. Such a catalog cannot ship anyway — the `Decision` and
# `PredicateOp` word checks in test_settings_ui_i18n.py apply to every catalog
# (AGENTS.md § Translations) — so the exclusion narrows this check rather than
# opening a way past it.
_TRANSLATED_LOCALES = sorted(
    path.stem
    for path in _SETTINGS_LOCALES.glob("*.json")
    if path.stem != "en" and json.loads(path.read_text("utf-8")).get("messages")
)
# The other authored surface samples its own catalog, never the settings one
# (`_surface_catalogs`), so it needs its own list. Read the production constant
# rather than rebuilding the path: a catalog this list misses is a catalog the
# check below silently never runs on. An empty catalog is excluded for the same
# reason as above — AGENTS.md § Translations lets a component catalog start
# empty, and there is no wording in one to sample.
_COMPONENT_LOCALES = sorted(
    path.stem
    for path in translate_locales.COMPONENT_DIR.glob("*.json")
    if path.stem != "en" and _flatten(json.loads(path.read_text("utf-8")))
)


_SAMPLE_ENGLISH = {
    "long": "Restart the server, then refresh the tool list in your AI client.",
    "neutral": "Server Settings",
    "short": "Your browser blocks storage.",
    "middle": "Reload the page, then re-apply your changes.",
}


def _item(section: str = "messages", english: str = "Hello {name}") -> WorkItem:
    return WorkItem("de", section, "greeting", english)


class TestValidate:
    def test_accepts_a_plain_translation(self) -> None:
        assert _validate(_item(english="Hello"), "Hallo") is None

    def test_refuses_a_swapped_number_and_accepts_a_spelled_out_one(self) -> None:
        """The engine asks the narrow question, and the corpus says which.

        Across the 6751 shipped pairs every number difference is one-sided —
        Russian writes "the 5 experimental sub-flags" in words, Chinese keeps
        a clause the English rendering cuts — and none is a swap. Comparing
        full multisets here would refuse those two correct strings on every
        run and leave their keys to be planned again tomorrow; comparing
        nothing let a swapped number land as a backfilled key, where the
        merge gate reports it and holds the whole tree back. A swap is wrong
        in every language, so that is what this refuses.
        """
        assert _validate(_item(english="Range 1-600."), "Bereich 1-900.") is not None
        assert (
            _validate(_item(english="keeps 30 entries"), "behaelt 50 Eintraege")
            is not None
        )
        assert _validate(_item(english="Range 1-600."), "Bereich 1-600.") is None

    def test_rejects_empty_and_non_string(self) -> None:
        assert _validate(_item(), "") is not None
        assert _validate(_item(), "   ") is not None
        assert _validate(_item(), None) is not None
        assert _validate(_item(), 42) is not None

    def test_rejects_placeholder_drift_both_directions(self) -> None:
        assert _validate(_item(), "Hallo") is not None  # dropped {name}
        assert _validate(_item(english="Hello"), "Hallo {name}") is not None
        assert _validate(_item(), "Hallo {name}") is None

    def test_rejects_markup_outside_the_allowlist_in_messages(self) -> None:
        english = "a <code>word</code>"
        assert _validate(_item(english=english), "ein <b>Wort</b>") is not None
        assert _validate(_item(english=english), "ein <CODE>Wort</CODE>") is not None
        assert _validate(_item(english=english), "ein <code>Wort</code>") is None

    def test_rejects_formatting_tag_multiset_drift(self) -> None:
        """A dropped closing tag or an invented allowlisted tag is still
        malformed markup for tHtml, even though every tag passes the
        allowlist individually."""
        english = "a <strong>bold</strong> word"
        assert _validate(_item(english=english), "ein <strong>fettes Wort") is not None
        assert _validate(_item(english="plain"), "ein <code>Wort</code>") is not None
        assert (
            _validate(_item(english=english), "ein <strong>fettes</strong> Wort")
            is None
        )

    def test_rejects_output_the_merge_gate_would_refuse(self) -> None:
        """The engine must not produce what the parity gate later refuses.

        A dropped identifier that is accepted here lands as a backfilled key,
        and no later run re-queues it: from then on the merge-time arm goes
        red every day, the push is held back whole, and the run re-spends its
        quota planning work it cannot land. One rejection and one retry costs
        a single string instead. The first three cases below are the faults
        #2180 repaired by hand; the fourth is the unit contradiction the same
        arm reports.
        """
        for english, bad in (
            ("See docs/beta.md for limits.", "Siehe die Beta-Dokumentation."),
            ("Set enable_tool_search to true.", "Aktiviere die Werkzeugsuche."),
            ("roughly 90% less", "etwa 46K weniger"),
            ("limit is 1-256 MB", "Grenze ist 1-256 GB"),
        ):
            assert _validate(_item(english=english), bad) is not None, (
                f"the engine accepted {bad!r} for {english!r}, which the "
                "merge-time literal-parity check refuses"
            )

    def test_accepts_a_number_the_translation_spells_out(self) -> None:
        """The one arm the engine deliberately does not run.

        Both tolerances the merge-time check carries are number-count
        tolerances: Russian writes "the 5 experimental sub-flags" in words,
        Chinese keeps a clause the English rendering cuts. Comparing number
        multisets here would refuse correct output on every run, retry once,
        and leave the key to be planned again tomorrow — the stall the call
        above exists to prevent, moved one step upstream.
        """
        assert (
            _validate(
                _item(english="the 5 experimental sub-flags"),
                "die fuenf experimentellen Unterschalter",
            )
            is None
        )

    def test_markup_rules_apply_only_to_messages(self) -> None:
        # Tool and component strings render through escapeHtml / HA core, so
        # the settings-UI markup allowlist deliberately does not gate them.
        assert _validate(_item(section="tools", english="x"), "<b>y</b>") is None

    def test_rejects_panel_link_target_drift(self) -> None:
        english = 'see the <a href="#" data-panel-link="tools">Tools</a> tab'
        ok = 'siehe <a href="#" data-panel-link="tools">Tools</a>'
        wrong = 'siehe <a href="#" data-panel-link="backups">Tools</a>'
        assert _validate(_item(english=english), ok) is None
        assert _validate(_item(english=english), wrong) is not None


class TestHardcodedOptionLabels:
    """Python hardcodes a handful of on-screen names — selector labels and the
    titles of the config entries themselves — so Home Assistant shows them in
    English whatever the reader's language is. A catalog that localises one
    sends the reader looking for something that is not on the screen: the
    failure that stopped a whole sync run, since the engine only had a prose
    rule telling it not to."""

    def test_the_name_set_is_read_from_the_source(self) -> None:
        # Without this the check degrades silently: an empty name set accepts
        # every translation and still reports a clean run. Both kinds are
        # asserted because either regex can stop matching on its own.
        names = translate_locales._hardcoded_ui_names()
        assert names, "no on-screen names found — the sources were restructured"
        assert any(name.startswith("Local network") for name in names), (
            "no selector label found — check the config flow's selector syntax"
        )
        assert "HA-MCP File & YAML Tools" in names, (
            "no config-entry title found — check the *_ENTRY_TITLE constants"
        )

    def test_rejects_a_localised_hardcoded_label(self) -> None:
        english = 'Local/LAN (when Network access is "Local network"): {url}'
        localised = 'Lokaal/LAN (wanneer Netwerktoegang "Lokaal netwerk" is): {url}'
        kept = 'Lokaal/LAN (wanneer Netwerktoegang "Local network" is): {url}'
        assert _validate(_item(section="component", english=english), localised)
        assert _validate(_item(section="component", english=english), kept) is None

    def test_reads_the_label_through_typographic_quotes(self) -> None:
        # Shipped English already quotes both ways, so which mark an author
        # reached for must not decide whether the label is protected.
        english = "Local/LAN (when Network access is “Local network”): {url}"
        localised = 'Lokaal/LAN (wanneer Netwerktoegang "Lokaal netwerk" is): {url}'
        assert _validate(_item(section="component", english=english), localised)

    def test_rejects_a_localised_entry_title(self) -> None:
        # The exact string the 2026-08-08 nl fill shipped: the reader is sent
        # to an entry whose title the integration hardcodes, under a name that
        # entry never has.
        english = (
            'Not installed — press "Add entry" on this integration\'s page and '
            'choose "HA-MCP File & YAML Tools" to add it'
        )
        localised = (
            'Niet geïnstalleerd — druk op "Item toevoegen" op de pagina van deze '
            'integratie en kies "HA-MCP Bestands- & YAML-tools" om deze toe te voegen'
        )
        kept = (
            'Niet geïnstalleerd — druk op "Item toevoegen" op de pagina van deze '
            'integratie en kies "HA-MCP File & YAML Tools" om deze toe te voegen'
        )
        assert _validate(_item(section="component", english=english), localised)
        assert _validate(_item(section="component", english=english), kept) is None

    def test_rejects_a_localised_name_in_single_quotes(self) -> None:
        # Shipped English at options.step.init.data_description.enable_llm_api
        # spells this one with apostrophes rather than double quotes. Single
        # quotes are matched against the known names instead of being paired
        # like the other marks: the apostrophe in "integration's" is the same
        # character, so pairing on it shifts every quote in such a sentence —
        # measured on the shipped catalogs, pairing loses a live violation.
        english = "agents can select 'HA-MCP Server' under Control Home Assistant"
        localised = "agenten kunnen 'HA-MCP Servidor' kiezen onder Bediening"
        kept = "agenten kunnen 'HA-MCP Server' kiezen onder Bediening"
        assert _validate(_item(section="component", english=english), localised)
        assert _validate(_item(section="component", english=english), kept) is None

    def test_apostrophes_do_not_hide_a_double_quoted_name(self) -> None:
        # The regression the obvious repair would have caused: this English
        # carries an apostrophe before the quoted title.
        english = (
            'press "Add entry" on this integration\'s page and choose '
            '"HA-MCP File & YAML Tools" to add it'
        )
        localised = 'druk op "Item toevoegen" en kies "HA-MCP Bestands-tools"'
        assert _validate(_item(section="component", english=english), localised)

    def test_leaves_other_quoted_text_translatable(self) -> None:
        # "Add entry" is Home Assistant's own button: HA translates it, so
        # every shipped catalog translates the quote too. Only labels the
        # config flow hardcodes are pinned to English.
        english = 'Not installed — press "Add entry" on this integration\'s page'
        assert (
            _validate(
                _item(section="component", english=english),
                'Nicht installiert — klicke auf "Eintrag hinzufügen"',
            )
            is None
        )


class TestChunk:
    def test_splits_on_the_character_budget(self) -> None:
        big = "x" * (translate_locales._MAX_CHARS_PER_REQUEST - 10)
        batch = {"a": big, "b": "small", "c": big}
        chunks = _chunk(batch)
        assert [list(chunk) for chunk in chunks] == [["a", "b"], ["c"]]

    def test_single_item_over_budget_gets_its_own_chunk(self) -> None:
        huge = "x" * (translate_locales._MAX_CHARS_PER_REQUEST + 1)
        assert [list(c) for c in _chunk({"a": huge, "b": "y"})] == [["a"], ["b"]]

    def test_empty_batch_yields_no_chunks(self) -> None:
        assert _chunk({}) == []


class TestStyleSamples:
    """The samples are the catalog's address register, and only the samples.

    Nothing downstream can recover it: ``_validate`` checks placeholders and
    markup, the parity suite checks keys and how much text is still English.
    A sample pair whose English does not address the reader therefore leaves
    the engine on its own default for the language — measured against the
    shipped catalogs, that turns German formal in a catalog whose own strings
    address the reader informally throughout.
    """

    def test_prefers_reader_addressing_sources_shortest_first(self) -> None:
        translated = dict.fromkeys(_SAMPLE_ENGLISH, "…")
        assert translate_locales._style_sample_keys(_SAMPLE_ENGLISH, translated) == [
            "short",
            "middle",
            "long",
        ]

    def test_skips_keys_the_catalog_has_not_translated(self) -> None:
        """``messages`` may omit keys — English is the per-key fallback — so an
        untranslated key carries no register and must not be sampled."""
        translated = {"long": "…", "neutral": "…"}
        assert translate_locales._style_sample_keys(_SAMPLE_ENGLISH, translated) == [
            "long"
        ]

    @pytest.mark.parametrize("blank", ["", " ", "\n\t "])
    def test_skips_keys_whose_translation_is_blank(self, blank: str) -> None:
        """A blank value is a present key with nothing in it, and this sampler
        is where one still turns up: `_validate_string_map` rejects it at load,
        but this script reads the catalogs with `json.loads` instead, and the
        parity ceilings count a key untranslated only when it equals the English
        or is missing, so `""` reads there as translated. Sampled, it spends one
        of three slots on a pair whose target side is empty — and a catalog
        whose register rests on one key would hand the engine that and nothing
        else. Whitespace counts as blank: it renders the same.
        """
        translated = dict.fromkeys(_SAMPLE_ENGLISH, "…") | {"short": blank}
        assert translate_locales._style_sample_keys(_SAMPLE_ENGLISH, translated) == [
            "middle",
            "long",
        ]

    def test_no_addressing_source_yields_no_samples(self) -> None:
        """Degenerate but defined: an English catalog that never addresses the
        reader gives the prompt no style block rather than a neutral one."""
        assert (
            translate_locales._style_sample_keys({"a": "Server Settings"}, {"a": "…"})
            == []
        )

    def test_keys_in_the_request_are_not_sampled(self) -> None:
        """A key is in a request because its English moved, so its committed
        translation renders the previous English. Shown as the sample for the
        very string being translated, the model answers with that stale text —
        measured against the live engine, byte-identical to the old wording."""
        translated = dict.fromkeys(_SAMPLE_ENGLISH, "…")
        assert translate_locales._style_sample_keys(
            _SAMPLE_ENGLISH, translated, {"short"}
        ) == ["middle", "long"]

    def test_reflexive_second_person_counts_as_addressing(self) -> None:
        """`yourself` / `yourselves` address the reader as much as `your`; no
        English string uses them today, so this pins the intent rather than a
        current behaviour."""
        english = {"a": "Give yourself access.", "b": "Help yourselves."}
        assert translate_locales._style_sample_keys(
            english, dict.fromkeys(english, "…")
        ) == ["b", "a"]

    def test_a_meta_only_stub_samples_nothing(self) -> None:
        """A new language starts as a stub the pipeline fills, so an empty
        catalog must yield no samples rather than raise."""
        assert translate_locales._style_sample_keys({"a": "Your setting"}, {}) == []

    def test_the_translated_catalogs_are_discovered(self) -> None:
        """The parametrised check below runs over a glob, and an empty glob
        would collapse it to a silent skip rather than a failure."""
        assert _TRANSLATED_LOCALES, "no translated catalogs found to check samples for"

    @pytest.mark.parametrize("locale", _TRANSLATED_LOCALES)
    def test_every_shipped_catalog_gets_reader_addressing_samples(
        self, locale: str
    ) -> None:
        """The guarantee has to hold for what actually ships, not just for a
        fixture: every shipped catalog must yield samples, and every one of
        them must address the reader."""
        english = json.loads((_SETTINGS_LOCALES / "en.json").read_text("utf-8"))[
            "messages"
        ]
        translated = json.loads(
            (_SETTINGS_LOCALES / f"{locale}.json").read_text("utf-8")
        ).get("messages", {})
        keys = translate_locales._style_sample_keys(english, translated)

        assert keys, (
            f"{locale}.json yields no style sample, so the engine gets no "
            "signal about how this catalog addresses its reader"
        )
        assert all(
            translate_locales._SECOND_PERSON_RE.search(english[key]) for key in keys
        ), f"{locale}.json samples {keys} do not address the reader"

    @pytest.mark.parametrize("locale", _TRANSLATED_LOCALES)
    def test_settings_samples_survive_their_own_anchor_being_queued(
        self, locale: str
    ) -> None:
        """The production caller excludes what the run is about to rewrite.

        ``_prompt`` hands ``queued_keys`` to ``_style_samples``, and
        ``_style_sample_keys`` drops those from the candidates -- so a catalog
        whose only reader-addressing key is itself queued for retranslation
        sends a request with neither a tone sample nor the register rule, both
        of which are guarded on ``if samples``. The two tests above pass the
        default empty ``exclude``, which is the one call production never
        makes, so they cannot see it.

        One anchor short of the sample count is enough to be safe here: losing
        any single candidate still leaves one to imitate.
        """
        english = json.loads((_SETTINGS_LOCALES / "en.json").read_text("utf-8"))[
            "messages"
        ]
        translated = json.loads(
            (_SETTINGS_LOCALES / f"{locale}.json").read_text("utf-8")
        ).get("messages", {})
        for queued in translate_locales._style_sample_keys(english, translated):
            assert translate_locales._style_sample_keys(
                english, translated, frozenset({queued})
            ), (
                f"{locale}.json falls back to no style sample at all once "
                f"{queued!r} is queued for retranslation, which is exactly the "
                "run that would have needed one"
            )

    def test_the_component_catalogs_are_discovered(self) -> None:
        """Same glob guard as above, for the other authored surface."""
        assert _COMPONENT_LOCALES, "no component catalogs found to check samples for"

    @pytest.mark.parametrize("locale", _COMPONENT_LOCALES)
    def test_every_shipped_component_catalog_gets_reader_addressing_samples(
        self, locale: str
    ) -> None:
        """The component surface carries the same guarantee and less margin.

        A settings catalog samples from hundreds of keys, so one English string
        losing its second person costs it one candidate. A component catalog
        starts empty and is filled a key at a time, so early on the whole
        surface can rest on a single shared key — today `eo` is exactly that,
        one key, while every other shipped catalog carries all 93. Lose the
        addressing there and the engine is told nothing about
        how this language addresses its reader and falls back to its own
        register for every later string, and the only trace is a line on
        stderr inside an unattended workflow run. Goes through
        ``_surface_catalogs`` on purpose: that it reads the component catalog
        rather than the settings one is the property at issue.
        """
        english, translated = translate_locales._surface_catalogs(locale, "component")
        keys = translate_locales._style_sample_keys(english, translated)

        assert keys, (
            f"component {locale}.json yields no style sample, so the engine "
            "gets no signal about how this catalog addresses its reader"
        )
        assert all(
            translate_locales._SECOND_PERSON_RE.search(english[key]) for key in keys
        ), f"component {locale}.json samples {keys} do not address the reader"

    @pytest.mark.parametrize("locale", _COMPONENT_LOCALES)
    def test_component_samples_survive_their_own_anchor_being_queued(
        self, locale: str
    ) -> None:
        """The settings property, on the surface that has less to spare.

        The exclusion is the same one production applies, and so is the
        consequence: a component request whose only reader-addressing key is
        queued goes out with neither tone sample nor register rule. What
        differs is the margin. A settings catalog draws its candidates from
        453 English messages, so the surface only loses its last one by a
        rewording of the English; a component catalog is filled a key at a
        time and can hold its whole register on the one key that arrived
        first. That is the case where the run this guards is likeliest to
        happen -- rewording that key is what queues it.
        """
        english, translated = translate_locales._surface_catalogs(locale, "component")
        for queued in translate_locales._style_sample_keys(english, translated):
            assert translate_locales._style_sample_keys(
                english, translated, frozenset({queued})
            ), (
                f"component {locale}.json falls back to no style sample at all "
                f"once {queued!r} is queued for retranslation, which is exactly "
                "the run that would have needed one"
            )


class TestPromptRegisterRule:
    """The rule points at the samples, so it carries nothing without them."""

    @staticmethod
    def _locales(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        de: dict,
        component_de: dict | None = None,
    ) -> None:
        locales = tmp_path / "locales"
        locales.mkdir()
        (locales / "en.json").write_text(
            json.dumps(
                {
                    "meta": {"native_name": "English"},
                    "messages": {"greeting": "Restart your client."},
                }
            ),
            encoding="utf-8",
        )
        (locales / "de.json").write_text(
            json.dumps({"meta": {"native_name": "Deutsch"}, "messages": de}),
            encoding="utf-8",
        )
        monkeypatch.setattr(translate_locales, "LOCALES_DIR", locales)

        component = tmp_path / "component"
        component.mkdir()
        (component / "en.json").write_text(
            json.dumps({"step": {"title": "Check your token."}}), encoding="utf-8"
        )
        (component / "de.json").write_text(
            json.dumps({"step": component_de or {}}), encoding="utf-8"
        )
        monkeypatch.setattr(translate_locales, "COMPONENT_DIR", component)

    def test_rule_ships_with_the_samples(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._locales(tmp_path, monkeypatch, {"greeting": "Starte deinen Client neu."})
        prompt = translate_locales._prompt(
            "de", {"messages:other": "Save"}, "messages", set()
        )
        assert "Starte deinen Client neu." in prompt
        assert "Address the reader the way the sample translations" in prompt

    def test_stub_catalog_gets_neither(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._locales(tmp_path, monkeypatch, {})
        prompt = translate_locales._prompt(
            "de", {"messages:greeting": "Save"}, "messages", set()
        )
        assert "Match the tone and terminology" not in prompt
        assert "Address the reader the way" not in prompt

    def test_a_key_queued_for_a_later_chunk_is_not_sampled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Results are applied only after every chunk finishes, so a key another
        chunk still owes carries the translation of the PREVIOUS English while
        this chunk is prompted. Excluding only the current chunk's keys would
        show that stale pair as the sample — the whole queue has to go."""
        self._locales(tmp_path, monkeypatch, {"greeting": "Starte deinen Client neu."})
        prompt = translate_locales._prompt(
            "de", {"messages:other": "Save"}, "messages", {"greeting"}
        )
        assert "Starte deinen Client neu." not in prompt
        assert "Match the tone and terminology" not in prompt

    def test_component_work_samples_the_component_catalog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two authored surfaces need not share a register — French ships a
        `vous`-only component catalog next to a settings catalog that mixes `tu`
        and `vous` — so a component request must not be told to imitate the
        settings catalog's register."""
        self._locales(
            tmp_path,
            monkeypatch,
            {"greeting": "Starte deinen Client neu."},
            component_de={"title": "Prüfen Sie Ihr Token."},
        )
        prompt = translate_locales._prompt(
            "de", {"component:other": "Save"}, "component", set()
        )
        assert "Prüfen Sie Ihr Token." in prompt
        assert "Starte deinen Client neu." not in prompt


class TestFlattenRoundTrip:
    def test_round_trips_nested_catalogs(self) -> None:
        nested = {"config": {"step": {"user": {"title": "Hi", "desc": "There"}}}}
        flat = _flatten(nested)
        assert flat == {
            "config.step.user.title": "Hi",
            "config.step.user.desc": "There",
        }
        assert _unflatten(flat) == nested

    def test_flatten_refuses_non_string_leaves(self) -> None:
        """A leaf the round-trip would silently delete must fail loudly
        instead — the pipeline rewrites whole files unattended in CI."""
        with pytest.raises(ValueError, match=r"a\.c"):
            _flatten({"a": {"b": "x", "c": [1, 2]}})


class TestPlanning:
    def test_missing_and_changed_messages_are_planned(self) -> None:
        plan = Plan()
        _plan_locale_messages(
            plan,
            "de",
            en_messages={"kept": "Kept", "changed": "New text", "new": "Added"},
            messages={"kept": "Behalten", "changed": "Alter Text", "orphan": "Weg"},
            changed_messages={"changed"},
        )
        assert [(i.key, i.english) for i in plan.items] == [
            ("changed", "New text"),
            ("new", "Added"),
        ]
        assert plan.deletions == [("de", "messages", "orphan")]

    def test_tools_plan_fills_changed_and_missing_fields(self) -> None:
        plan = Plan()
        _plan_locale_tools(
            plan,
            "de",
            catalog={"tools": {"ha_a": {"title": "T"}, "ha_gone": {}}},
            changed_tools={"ha_b.title"},
            tool_texts={
                "ha_a.title": "A",
                "ha_a.description": "A desc",
                "ha_b.title": "B",
                "ha_b.description": "B desc",
            },
            tool_names=frozenset({"ha_a", "ha_b"}),
        )
        assert sorted(i.key for i in plan.items) == [
            "ha_a.description",  # missing field
            "ha_b.description",  # missing entirely
            "ha_b.title",  # missing + changed
        ]
        assert plan.deletions == [("de", "tools", "ha_gone")]

    def test_groups_plan_uses_the_english_key_as_source(self) -> None:
        plan = Plan()
        _plan_locale_groups(
            plan,
            "de",
            catalog={"tool_groups": {"Old": "Alt"}},
            groups=frozenset({"Lights", "Old"}),
        )
        assert [(i.key, i.english) for i in plan.items] == [("Lights", "Lights")]
        assert plan.deletions == []

    def test_run_wide_queue_reaches_every_chunk_and_the_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_translate_locale`` is what computes the exclusion set and hands it
        to each request; the per-``_prompt`` tests cannot see that. Two chunks
        plus a forced retry, and no prompt may quote a key the run still owes."""
        locales = tmp_path / "locales"
        locales.mkdir()
        # Each over half the character budget, so the two land in separate
        # chunks — with both in one chunk the run-wide set and the chunk-local
        # one are the same thing and the check below proves nothing.
        big = "x" * (translate_locales._MAX_CHARS_PER_REQUEST // 2 + 100)
        english = {
            "first": f"Restart your client. {big}",
            "second": f"Reload your page. {big}",
            "sample": "Check your token.",
        }
        (locales / "en.json").write_text(
            json.dumps({"meta": {"native_name": "English"}, "messages": english}),
            encoding="utf-8",
        )
        (locales / "de.json").write_text(
            json.dumps(
                {
                    "meta": {"native_name": "Deutsch"},
                    "messages": {k: f"DE-{k} deinen" for k in english},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(translate_locales, "LOCALES_DIR", locales)
        monkeypatch.setattr(translate_locales.time, "sleep", lambda _s: None)

        prompts: list[str] = []

        chunk_prompts: list[str] = []

        def fake_engine(prompt: str) -> dict[str, str]:
            prompts.append(prompt)
            if "previous attempt was rejected" not in prompt:
                chunk_prompts.append(prompt)
            # Empty answers fail validation, so every string also exercises the
            # retry prompt in _accept_or_retry.
            return dict.fromkeys(("messages:first", "messages:second"), "")

        monkeypatch.setattr(translate_locales, "_call_gemini", fake_engine)

        items = [
            WorkItem("de", "messages", key, english[key]) for key in ("first", "second")
        ]
        translate_locales._translate_locale("de", items)

        assert len(chunk_prompts) >= 2, (
            f"expected the work to span two chunks, got {len(chunk_prompts)} "
            "requests — the run-wide set is indistinguishable from the "
            "chunk-local one inside a single chunk"
        )
        assert len(prompts) > len(chunk_prompts), "expected retry prompts too"
        for prompt in prompts:
            assert "DE-first" not in prompt
            assert "DE-second" not in prompt
        assert any("DE-sample" in prompt for prompt in prompts), (
            "the untouched key should still be sampled — otherwise this test "
            "would pass with sampling switched off entirely"
        )

    def test_engine_failures_count_across_a_locale_not_per_surface(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One failure ending the settings surface and one opening the component
        surface are two consecutive failures against the same engine. A counter
        living inside the per-surface call resets between them, so the run keeps
        calling an engine that has already answered twice in a row."""
        locales = tmp_path / "locales"
        component = tmp_path / "component"
        locales.mkdir()
        component.mkdir()
        (locales / "en.json").write_text(
            json.dumps(
                {"meta": {"native_name": "English"}, "messages": {"a": "Your page."}}
            ),
            encoding="utf-8",
        )
        (locales / "de.json").write_text(
            json.dumps({"meta": {"native_name": "Deutsch"}, "messages": {"a": "…"}}),
            encoding="utf-8",
        )
        (component / "en.json").write_text(json.dumps({"x": "Your token."}), "utf-8")
        (component / "de.json").write_text(json.dumps({"x": "…"}), "utf-8")
        monkeypatch.setattr(translate_locales, "LOCALES_DIR", locales)
        monkeypatch.setattr(translate_locales, "COMPONENT_DIR", component)
        monkeypatch.setattr(translate_locales.time, "sleep", lambda _s: None)

        calls: list[str] = []

        def dead_engine(prompt: str) -> dict[str, str]:
            calls.append(prompt)
            raise SystemExit("engine down")

        monkeypatch.setattr(translate_locales, "_call_gemini", dead_engine)

        _results, _failures, _failed, dead = translate_locales._translate_locale(
            "de",
            [
                WorkItem("de", "messages", "a", "Your page."),
                WorkItem("de", "component", "x", "Your token."),
            ],
        )

        assert dead, "two failed chunks in a row must declare the engine dead"
        assert len(calls) == 2, (
            f"expected the run to stop after the second failure, saw {len(calls)} "
            "engine calls"
        )

    # Same gate as ``test_locale_parity.completeness`` (see the marker
    # comment there): this asserts the LIVE tree owes no translations, which
    # any PR that changes an English string legitimately violates until the
    # post-merge sync runs. ``test_locale_sync_gate_shape`` pins the wiring.
    @pytest.mark.skipif(
        not os.environ.get("LOCALE_COMPLETENESS_CHECKS"),
        reason=(
            "translated-catalog completeness is verified by the post-merge "
            "locale-sync workflow — set LOCALE_COMPLETENESS_CHECKS=1 to run"
        ),
    )
    def test_clean_tree_plans_no_work(self) -> None:
        """The sync's own no-op invariant: after a run repins the baseline,
        a rerun must find nothing — verified in the workflow, where it runs
        against the freshly translated tree."""
        module = translate_locales._load_test_module()
        plan = translate_locales.build_plan(module)
        assert plan.items == []
        assert plan.deletions == []


class TestCallGeminiRetry:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(translate_locales.time, "sleep", lambda _s: None)

    @staticmethod
    def _response(status_code: int, payload: dict[str, Any] | None = None) -> Any:
        return SimpleNamespace(
            status_code=status_code,
            text="body",
            json=lambda: (
                payload
                or {
                    "candidates": [
                        {"content": {"parts": [{"text": json.dumps({"s0": "Hallo"})}]}}
                    ]
                }
            ),
        )

    def test_uses_current_model_without_deprecated_sampling_parameters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        monkeypatch.delenv("GEMINI_API_URL", raising=False)
        request: dict[str, Any] = {}

        def fake_post(url: str, **kwargs: Any) -> Any:
            request["url"] = url
            request["body"] = kwargs["json"]
            return self._response(200)

        monkeypatch.setattr(translate_locales.httpx, "post", fake_post)
        assert translate_locales._call_gemini("prompt") == {"s0": "Hallo"}
        assert request["url"] == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.5-flash-lite:generateContent"
        )
        generation_config = request["body"]["generationConfig"]
        assert generation_config["response_mime_type"] == "application/json"
        assert {"temperature", "topP", "topK"}.isdisjoint(generation_config)

    def test_retries_transient_statuses_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []
        responses = [self._response(429), self._response(503), self._response(200)]

        def fake_post(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(1)
            return responses[len(calls) - 1]

        monkeypatch.setattr(translate_locales.httpx, "post", fake_post)
        assert translate_locales._call_gemini("prompt") == {"s0": "Hallo"}
        assert len(calls) == 3

    def test_retries_transport_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        def fake_post(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                raise translate_locales.httpx.ConnectTimeout("boom")
            return self._response(200)

        monkeypatch.setattr(translate_locales.httpx, "post", fake_post)
        assert translate_locales._call_gemini("prompt") == {"s0": "Hallo"}
        assert len(calls) == 2

    def test_terminal_status_raises_without_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def fake_post(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(1)
            return self._response(400)

        monkeypatch.setattr(translate_locales.httpx, "post", fake_post)
        with pytest.raises(SystemExit, match="HTTP 400"):
            translate_locales._call_gemini("prompt")
        assert len(calls) == 1

    def test_missing_key_is_a_clear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GEMINI_API_KEY")
        with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
            translate_locales._call_gemini("prompt")

    def test_blocked_response_is_a_clear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 200 with an empty candidates list (safety-filter block) must
        read like the other engine failures, not a raw IndexError."""
        blocked = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        monkeypatch.setattr(
            translate_locales.httpx,
            "post",
            lambda *_a, **_k: self._response(200, blocked),
        )
        with pytest.raises(SystemExit, match="unusable response"):
            translate_locales._call_gemini("prompt")


class TestEngineFailureDegradation:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(translate_locales.time, "sleep", lambda _s: None)
        # One string per chunk so chunk boundaries are deterministic.
        monkeypatch.setattr(translate_locales, "_MAX_CHARS_PER_REQUEST", 1)

    def test_one_failed_chunk_degrades_to_per_string_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = iter([SystemExit("quota"), {"messages:second": "Zwei"}])

        def fake_call(_prompt: str) -> dict[str, str]:
            answer = next(answers)
            if isinstance(answer, SystemExit):
                raise answer
            return answer

        monkeypatch.setattr(translate_locales, "_call_gemini", fake_call)
        items = [
            WorkItem("de", "messages", "first", "One"),
            WorkItem("de", "messages", "second", "Two"),
        ]
        results, failures, failed, dead = translate_locales._translate_locale(
            "de", items
        )
        assert results == {("messages", "second"): "Zwei"}
        assert failures == ["de/messages/first: engine call failed (quota)"]
        assert failed == {("messages", "first")}
        assert dead is False

    def test_two_consecutive_failures_declare_the_engine_dead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_call(_prompt: str) -> dict[str, str]:
            raise SystemExit("quota")

        monkeypatch.setattr(translate_locales, "_call_gemini", fake_call)
        items = [
            WorkItem("de", "messages", "first", "One"),
            WorkItem("de", "messages", "second", "Two"),
            WorkItem("de", "messages", "third", "Three"),
        ]
        results, failures, failed, dead = translate_locales._translate_locale(
            "de", items
        )
        assert results == {}
        assert dead is True
        # Two chunk failures trip the give-up; the never-attempted third
        # string is still recorded as failed so nothing slips the report.
        assert len(failures) == 3
        assert failed == {
            ("messages", "first"),
            ("messages", "second"),
            ("messages", "third"),
        }


class TestResumableProgress:
    @pytest.fixture(autouse=True)
    def _tmp_progress(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            translate_locales, "PROGRESS_PATH", tmp_path / "progress.json"
        )
        monkeypatch.setattr(translate_locales, "REPO_ROOT", tmp_path)

    def test_partial_run_records_and_next_plan_skips(self) -> None:
        plan = Plan(
            items=[
                WorkItem("de", "messages", "greeting", "Hello", changed=True),
                WorkItem("ru", "messages", "greeting", "Hello", changed=True),
            ]
        )
        translate_locales._record_progress({("messages", "greeting"): {"de"}}, plan)
        progress = translate_locales._progress_load()
        assert translate_locales._progress_done(
            progress, "messages", "greeting", "Hello", "de"
        )
        # ru never completed; and a further English change invalidates de too.
        assert not translate_locales._progress_done(
            progress, "messages", "greeting", "Hello", "ru"
        )
        assert not translate_locales._progress_done(
            progress, "messages", "greeting", "Hello again", "de"
        )

    def test_record_merges_and_clear_removes(self) -> None:
        plan = Plan(
            items=[WorkItem("de", "messages", "greeting", "Hello", changed=True)]
        )
        translate_locales._record_progress({("messages", "greeting"): {"de"}}, plan)
        plan.items[0] = WorkItem("es", "messages", "greeting", "Hello", changed=True)
        translate_locales._record_progress({("messages", "greeting"): {"es"}}, plan)
        progress = translate_locales._progress_load()
        assert progress["messages|greeting"]["locales"] == ["de", "es"]
        translate_locales._clear_progress()
        assert translate_locales._progress_load() == {}

    def test_full_success_leaves_no_progress_file(self) -> None:
        translate_locales._clear_progress()  # no file: a clean no-op
        assert not translate_locales.PROGRESS_PATH.exists()


class TestTimeBudget:
    def test_exhausted_budget_degrades_like_a_quota_hit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the self-imposed wall-clock bound trips, no further engine
        calls happen and every unfinished string is a recorded failure — the
        same resumable shape as an engine failure, instead of the job timeout
        losing everything uncommitted."""

        def must_not_call(_prompt: str) -> dict[str, Any]:
            raise AssertionError("engine called after the deadline")

        monkeypatch.setattr(translate_locales, "_call_gemini", must_not_call)
        monkeypatch.setattr(
            translate_locales, "_DEADLINE", translate_locales.time.monotonic() - 1
        )
        items = [
            WorkItem("de", "messages", "first", "One"),
            WorkItem("de", "messages", "second", "Two"),
        ]
        results, failures, failed, dead = translate_locales._translate_locale(
            "de", items
        )
        assert results == {}
        assert dead is True
        assert failures[0] == "de: time budget exhausted — stopping this run"
        assert failed == {("messages", "first"), ("messages", "second")}

    def test_engine_refuses_to_start_past_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worst-case engine call is ~20 minutes of timeouts and backoff —
        it must not start once the budget is spent, or it eats the slack
        between the script's bound and the work-losing job timeout."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(
            translate_locales.httpx,
            "post",
            lambda *_a, **_k: pytest.fail("HTTP call after the deadline"),
        )
        monkeypatch.setattr(
            translate_locales, "_DEADLINE", translate_locales.time.monotonic() - 1
        )
        with pytest.raises(SystemExit, match="time budget"):
            translate_locales._call_gemini("prompt")


class TestRepinBaseline:
    def test_changed_parsed_keys_stay_stale_for_human_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A moved parsed docstring on a feature-gated tool keeps its OLD
        baseline hash — the red staleness test is the human checkpoint that
        the hand-written stub still describes the tool; only the manual
        update_locale_baseline.py repin (the confirmation) clears it."""
        monkeypatch.setattr(translate_locales, "REPO_ROOT", tmp_path)
        baseline = tmp_path / "baseline.json"
        surface = "settings UI tool titles and descriptions"
        baseline.write_text(
            json.dumps(
                {
                    surface: {
                        "ha_x.description (parsed)": "oldhash",
                        "ha_x.description": "oldrendered",
                    }
                }
            ),
            encoding="utf-8",
        )
        module = SimpleNamespace(
            BASELINE_PATH=baseline,
            english_sources=lambda: {
                surface: {
                    "ha_x.description (parsed)": "NEWhash",
                    "ha_x.description": "NEWrendered",
                }
            },
        )
        translate_locales._repin_baseline(module)
        written = json.loads(baseline.read_text(encoding="utf-8"))
        assert written[surface]["ha_x.description (parsed)"] == "oldhash"
        assert written[surface]["ha_x.description"] == "NEWrendered"

    def test_new_parsed_keys_pin_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(translate_locales, "REPO_ROOT", tmp_path)
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({"s": {}}), encoding="utf-8")
        module = SimpleNamespace(
            BASELINE_PATH=baseline,
            english_sources=lambda: {"s": {"ha_new.description (parsed)": "h1"}},
        )
        translate_locales._repin_baseline(module)
        written = json.loads(baseline.read_text(encoding="utf-8"))
        assert written["s"]["ha_new.description (parsed)"] == "h1"


class TestMetaOnlyStub:
    def test_documented_stub_language_flow_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A new language starts as a near-empty catalog the pipeline fills, and
        the sections it does not carry yet are absent rather than empty — so a
        catalog with no messages/tools/tool_groups sections must plan cleanly
        and be writable. What a shipped catalog owes beyond that is checked
        against the real files (see the address-register check above), not
        here."""
        locales = tmp_path / "locales"
        locales.mkdir()
        (locales / "en.json").write_text(
            json.dumps(
                {
                    "meta": {"native_name": "English", "dir": "ltr"},
                    "messages": {"greeting": "Hello"},
                    "tool_groups": {},
                    "tools": {},
                }
            ),
            encoding="utf-8",
        )
        (locales / "xx.json").write_text(
            json.dumps({"meta": {"native_name": "Testish", "dir": "ltr"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(translate_locales, "LOCALES_DIR", locales)
        monkeypatch.setattr(translate_locales, "REPO_ROOT", tmp_path)

        module = translate_locales._load_test_module()
        plan = Plan()
        translate_locales._plan_settings(plan, module, changed={})
        stub_messages = [
            item
            for item in plan.items
            if item.locale == "xx" and item.section == "messages"
        ]
        assert [item.key for item in stub_messages] == ["greeting"]

        translate_locales._apply_settings(
            "xx", translations={("messages", "greeting"): "Hallo-xx"}, deletions=[]
        )
        written = json.loads((locales / "xx.json").read_text(encoding="utf-8"))
        assert written["messages"] == {"greeting": "Hallo-xx"}


class TestSharedTranslationReuse:
    @pytest.fixture()
    def catalogs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path, Any]:
        locales = tmp_path / "locales"
        component = tmp_path / "component"
        locales.mkdir()
        component.mkdir()
        (locales / "en.json").write_text(
            json.dumps(
                {
                    "meta": {"native_name": "English", "dir": "ltr"},
                    "messages": {"shared": "Unknown", "other": "Other"},
                    "tool_groups": {},
                    "tools": {},
                }
            ),
            encoding="utf-8",
        )
        (locales / "de.json").write_text(
            json.dumps(
                {
                    "meta": {"native_name": "Deutsch", "dir": "ltr"},
                    "messages": {},
                    "tool_groups": {},
                    "tools": {},
                }
            ),
            encoding="utf-8",
        )
        (component / "en.json").write_text(
            json.dumps({"common": {"version_unknown": "Unknown"}}),
            encoding="utf-8",
        )
        (component / "de.json").write_text(
            json.dumps({"common": {"version_unknown": "unbekannt"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(translate_locales, "LOCALES_DIR", locales)
        monkeypatch.setattr(translate_locales, "COMPONENT_DIR", component)
        monkeypatch.setattr(translate_locales, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(translate_locales.generate_locales, "write", lambda: None)
        monkeypatch.setattr(translate_locales.time, "sleep", lambda _s: None)
        module = SimpleNamespace(
            _authored_shared_groups=lambda: (
                (
                    "Unknown",
                    (
                        (translate_locales.SETTINGS_SURFACE, "messages.shared"),
                        (
                            translate_locales.COMPONENT_SURFACE,
                            "common.version_unknown",
                        ),
                    ),
                ),
            )
        )
        return locales, component, module

    def test_missing_shared_key_reuses_existing_sibling_before_engine_call(
        self,
        catalogs: tuple[Path, Path, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        locales, _component, module = catalogs
        prompts: list[str] = []

        def fake_engine(prompt: str) -> dict[str, str]:
            prompts.append(prompt)
            return {"messages:other": "Andere"}

        monkeypatch.setattr(translate_locales, "_call_gemini", fake_engine)
        items = [
            WorkItem("de", "messages", "shared", "Unknown"),
            WorkItem("de", "messages", "other", "Other"),
        ]
        failures, _completed = translate_locales._translate_and_apply(
            Plan(items=items), {"de": items}, module
        )

        assert failures == []
        assert len(prompts) == 1
        assert "messages:other" in prompts[0]
        assert "messages:shared" not in prompts[0]
        written = json.loads((locales / "de.json").read_text(encoding="utf-8"))
        assert written["messages"] == {
            "shared": "unbekannt",
            "other": "Andere",
        }

    def test_english_sibling_is_translated_instead_of_reused(
        self,
        catalogs: tuple[Path, Path, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        locales, component, module = catalogs
        (component / "de.json").write_text(
            json.dumps({"common": {"version_unknown": "Unknown"}}),
            encoding="utf-8",
        )
        prompts: list[str] = []

        def fake_engine(prompt: str) -> dict[str, str]:
            prompts.append(prompt)
            return {"messages:shared": "unbekannt"}

        monkeypatch.setattr(translate_locales, "_call_gemini", fake_engine)
        items = [WorkItem("de", "messages", "shared", "Unknown")]
        failures, _completed = translate_locales._translate_and_apply(
            Plan(items=items), {"de": items}, module
        )

        assert failures == []
        assert len(prompts) == 1
        assert "messages:shared" in prompts[0]
        written = json.loads((locales / "de.json").read_text(encoding="utf-8"))
        assert written["messages"]["shared"] == "unbekannt"

    def test_disagreeing_siblings_are_translated_instead_of_reused(
        self,
        catalogs: tuple[Path, Path, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        locales, component, _module = catalogs
        component_en = json.loads((component / "en.json").read_text(encoding="utf-8"))
        component_en["common"]["other_unknown"] = "Unknown"
        (component / "en.json").write_text(json.dumps(component_en), encoding="utf-8")
        component_de = json.loads((component / "de.json").read_text(encoding="utf-8"))
        component_de["common"]["other_unknown"] = "nicht bekannt"
        (component / "de.json").write_text(json.dumps(component_de), encoding="utf-8")
        module = SimpleNamespace(
            _authored_shared_groups=lambda: (
                (
                    "Unknown",
                    (
                        (translate_locales.SETTINGS_SURFACE, "messages.shared"),
                        (
                            translate_locales.COMPONENT_SURFACE,
                            "common.version_unknown",
                        ),
                        (
                            translate_locales.COMPONENT_SURFACE,
                            "common.other_unknown",
                        ),
                    ),
                ),
            )
        )
        prompts: list[str] = []

        def fake_engine(prompt: str) -> dict[str, str]:
            prompts.append(prompt)
            return {"messages:shared": "unbekannt"}

        monkeypatch.setattr(translate_locales, "_call_gemini", fake_engine)
        items = [WorkItem("de", "messages", "shared", "Unknown")]
        failures, _completed = translate_locales._translate_and_apply(
            Plan(items=items), {"de": items}, module
        )

        assert failures == []
        assert len(prompts) == 1
        assert "messages:shared" in prompts[0]
        written = json.loads((locales / "de.json").read_text(encoding="utf-8"))
        assert written["messages"]["shared"] == "unbekannt"

    def test_sibling_invalid_for_destination_is_translated_instead_of_reused(
        self,
        catalogs: tuple[Path, Path, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        locales, component, module = catalogs
        (component / "de.json").write_text(
            json.dumps({"common": {"version_unknown": "<script>unbekannt</script>"}}),
            encoding="utf-8",
        )
        prompts: list[str] = []

        def fake_engine(prompt: str) -> dict[str, str]:
            prompts.append(prompt)
            return {"messages:shared": "unbekannt"}

        monkeypatch.setattr(translate_locales, "_call_gemini", fake_engine)
        items = [WorkItem("de", "messages", "shared", "Unknown")]
        failures, _completed = translate_locales._translate_and_apply(
            Plan(items=items), {"de": items}, module
        )

        assert failures == []
        assert len(prompts) == 1
        assert "messages:shared" in prompts[0]
        written = json.loads((locales / "de.json").read_text(encoding="utf-8"))
        assert written["messages"]["shared"] == "unbekannt"

    def test_fully_stale_shared_group_is_translated_instead_of_reused(
        self,
        catalogs: tuple[Path, Path, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        locales, component, module = catalogs
        settings = json.loads((locales / "de.json").read_text(encoding="utf-8"))
        settings["messages"]["shared"] = "alt"
        (locales / "de.json").write_text(json.dumps(settings), encoding="utf-8")
        prompts: list[str] = []

        def fake_engine(prompt: str) -> dict[str, str]:
            prompts.append(prompt)
            if "messages:shared" in prompt:
                return {"messages:shared": "neu"}
            return {"component:common.version_unknown": "anders"}

        monkeypatch.setattr(translate_locales, "_call_gemini", fake_engine)
        items = [
            WorkItem("de", "messages", "shared", "Unknown", changed=True),
            WorkItem(
                "de",
                "component",
                "common.version_unknown",
                "Unknown",
                changed=True,
            ),
        ]
        failures, completed = translate_locales._translate_and_apply(
            Plan(items=items), {"de": items}, module
        )

        assert failures == []
        assert len(prompts) == 2
        assert any("messages:shared" in prompt for prompt in prompts)
        assert any("component:common.version_unknown" in prompt for prompt in prompts)
        settings_written = json.loads((locales / "de.json").read_text(encoding="utf-8"))
        component_written = json.loads(
            (component / "de.json").read_text(encoding="utf-8")
        )
        assert settings_written["messages"]["shared"] == "neu"
        assert component_written["common"]["version_unknown"] == "neu"
        assert completed == {
            ("messages", "shared"): {"de"},
            ("component", "common.version_unknown"): {"de"},
        }


class TestApplyWrites:
    @pytest.fixture()
    def catalog_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path]:
        locales = tmp_path / "locales"
        component = tmp_path / "component"
        locales.mkdir()
        component.mkdir()
        (locales / "en.json").write_text(
            json.dumps(
                {
                    "meta": {"native_name": "English", "dir": "ltr"},
                    "messages": {"first": "One", "second": "Two"},
                    "tool_groups": {},
                    "tools": {},
                }
            ),
            encoding="utf-8",
        )
        (locales / "de.json").write_text(
            json.dumps(
                {
                    "meta": {"native_name": "Deutsch", "dir": "ltr"},
                    # Deliberately not in en.json order; orphan must go.
                    "messages": {"second": "Zwei", "orphan": "Weg"},
                    "tool_groups": {},
                    "tools": {},
                }
            ),
            encoding="utf-8",
        )
        (component / "en.json").write_text(
            json.dumps({"config": {"title": "Hi"}}), encoding="utf-8"
        )
        (component / "de.json").write_text(
            json.dumps({"config": {"title": "Alt"}}), encoding="utf-8"
        )
        monkeypatch.setattr(translate_locales, "LOCALES_DIR", locales)
        monkeypatch.setattr(translate_locales, "COMPONENT_DIR", component)
        # The "updated <path>" print renders paths repo-relative.
        monkeypatch.setattr(translate_locales, "REPO_ROOT", tmp_path)
        return locales, component

    def test_settings_writes_mirror_en_order_and_drop_orphans(
        self, catalog_dirs: tuple[Path, Path]
    ) -> None:
        locales, _component = catalog_dirs
        translate_locales._apply_settings(
            "de",
            translations={("messages", "first"): "Eins"},
            deletions=[("de", "messages", "orphan")],
        )
        written = json.loads((locales / "de.json").read_text(encoding="utf-8"))
        assert list(written["messages"]) == ["first", "second"]
        assert written["messages"]["first"] == "Eins"
        assert "orphan" not in written["messages"]

    def test_component_write_round_trips_nested_shape(
        self, catalog_dirs: tuple[Path, Path]
    ) -> None:
        _locales, component = catalog_dirs
        translate_locales._apply_component(
            "de", translations={("component", "config.title"): "Hallo"}, deletions=[]
        )
        written = json.loads((component / "de.json").read_text(encoding="utf-8"))
        assert written == {"config": {"title": "Hallo"}}

    def test_apply_is_idempotent(self, catalog_dirs: tuple[Path, Path]) -> None:
        """A second no-work pass must not rewrite the file — this is the
        property that keeps the CI auto-commit loop from ping-ponging."""
        locales, _component = catalog_dirs
        translate_locales._apply_settings(
            "de", translations={}, deletions=[("de", "messages", "orphan")]
        )
        settled = (locales / "de.json").read_text(encoding="utf-8")
        translate_locales._apply_settings("de", translations={}, deletions=[])
        assert (locales / "de.json").read_text(encoding="utf-8") == settled
