"""Settings UI locale discovery, selection, fallback, and safe rendering."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import get_args

import pytest
from starlette.requests import Request

from ha_mcp.policy.approval_queue import Decision
from ha_mcp.policy.model import PredicateOp
from ha_mcp.settings_ui import _render_settings_html
from ha_mcp.settings_ui._i18n import (
    CATALOGS,
    DEFAULT_LOCALE,
    LOCALES_DIR,
    build_payload,
    load_catalogs,
    normalize_locale,
    select_locale,
    serialize_payload,
)


def _shipped_locales() -> list[str]:
    """Registered catalog codes except English, which has no translations."""
    return sorted(code for code in CATALOGS if code != DEFAULT_LOCALE)


def _catalog_file(locale: str) -> str:
    """The on-disk file name for a registered catalog code.

    ``load_catalogs`` lowercases the stem, so ``zh-Hans.json`` registers under
    the code ``zh-hans``. Spelling a failure message as ``f"{locale}.json"``
    therefore names a file that does not exist for that language, and the
    contributor it is written for has to guess the casing back.
    """
    for path in sorted(LOCALES_DIR.glob("*.json")):
        if path.stem.lower().replace("_", "-") == locale:
            return path.name
    return f"{locale}.json"


def _write_catalog(
    directory: Path,
    locale: str,
    *,
    native_name: str,
    messages: dict[str, str],
    tools: dict[str, dict[str, str]] | None = None,
) -> None:
    (directory / f"{locale}.json").write_text(
        json.dumps(
            {
                "meta": {"native_name": native_name, "dir": "ltr"},
                "messages": messages,
                "tool_groups": {},
                "tools": tools or {},
            }
        ),
        encoding="utf-8",
    )


def test_catalogs_are_discovered_without_registration(tmp_path: Path) -> None:
    _write_catalog(tmp_path, "en", native_name="English", messages={"a": "A"})
    _write_catalog(tmp_path, "it", native_name="Italiano", messages={"a": "Uno"})

    catalogs = load_catalogs(tmp_path)

    assert sorted(catalogs) == ["en", "it"]
    assert build_payload("it", catalogs)["messages"]["a"] == "Uno"


def test_incomplete_catalog_falls_back_to_english(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={"translated": "English", "fallback": "Fallback"},
    )
    _write_catalog(
        tmp_path,
        "ru",
        native_name="Русский",
        messages={"translated": "Русский"},
    )
    catalogs = load_catalogs(tmp_path)

    payload = build_payload("ru", catalogs)

    assert payload["messages"] == {
        "translated": "Русский",
        "fallback": "Fallback",
    }


def test_locale_normalization_and_priority(tmp_path: Path) -> None:
    _write_catalog(tmp_path, "en", native_name="English", messages={})
    _write_catalog(tmp_path, "ru", native_name="Русский", messages={})
    catalogs = load_catalogs(tmp_path)

    assert normalize_locale("ru-RU", catalogs) == "ru"
    assert (
        select_locale(
            cookie_locale="ru",
            ha_language="en-US",
            accept_language="en;q=1.0",
            catalogs=catalogs,
        )
        == "ru"
    )
    assert (
        select_locale(
            ha_language="de-DE",
            accept_language="en;q=0.4, ru-RU;q=0.9",
            catalogs=catalogs,
        )
        == "ru"
    )
    assert select_locale(accept_language="de-DE", catalogs=catalogs) == "en"


def test_zh_cn_region_normalizes_to_zh_hans(tmp_path: Path) -> None:
    _write_catalog(tmp_path, "en", native_name="English", messages={})
    _write_catalog(
        tmp_path,
        "zh-Hans",
        native_name="简体中文",
        messages={"greeting": "你好"},
    )
    catalogs = load_catalogs(tmp_path)

    assert normalize_locale("zh-CN", catalogs) == "zh-hans"
    assert normalize_locale("zh-SG", catalogs) == "zh-hans"
    assert normalize_locale("zh", catalogs) == "zh-hans"
    assert normalize_locale("zh-Hans", catalogs) == "zh-hans"
    # zh-TW should NOT resolve to zh-hans (no Traditional Chinese catalog)
    assert normalize_locale("zh-TW", catalogs) is None
    assert select_locale(ha_language="zh-CN", catalogs=catalogs) == "zh-hans"
    assert (
        select_locale(accept_language="zh-CN,zh;q=0.9", catalogs=catalogs) == "zh-hans"
    )


def test_traditional_chinese_regions_normalize_to_zh_hant(tmp_path: Path) -> None:
    _write_catalog(tmp_path, "en", native_name="English", messages={})
    _write_catalog(
        tmp_path,
        "zh-Hans",
        native_name="简体中文",
        messages={"greeting": "你好"},
    )
    _write_catalog(
        tmp_path,
        "zh-Hant",
        native_name="繁體中文",
        messages={"greeting": "您好"},
    )
    catalogs = load_catalogs(tmp_path)

    for locale in ("zh-TW", "zh-HK", "zh-MO"):
        assert normalize_locale(locale, catalogs) == "zh-hant"
        assert select_locale(ha_language=locale, catalogs=catalogs) == "zh-hant"
    assert normalize_locale("zh-Hans-HK", catalogs) == "zh-hans"
    assert normalize_locale("zh-Hans-TW", catalogs) == "zh-hans"
    assert normalize_locale("zh-Hant-CN", catalogs) == "zh-hant"
    assert select_locale(accept_language="zh-TW,zh;q=0.9", catalogs=catalogs) == (
        "zh-hant"
    )


def test_placeholder_mismatch_is_rejected(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={"saved": "Saved {count}"},
    )
    _write_catalog(
        tmp_path,
        "ru",
        native_name="Русский",
        messages={"saved": "Сохранено"},
    )

    with pytest.raises(ValueError, match="placeholders"):
        load_catalogs(tmp_path)


def test_tool_placeholder_mismatch_is_rejected(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={},
        tools={"ha_example": {"description": "Run for {entity}"}},
    )
    _write_catalog(
        tmp_path,
        "ru",
        native_name="Русский",
        messages={},
        tools={"ha_example": {"description": "Выполнить"}},
    )

    with pytest.raises(
        ValueError, match=r"tool 'ha_example'.*description.*placeholders"
    ):
        load_catalogs(tmp_path)


def test_unknown_text_direction_is_rejected(tmp_path: Path) -> None:
    """``meta.dir`` reaches the rendered ``<html dir>`` attribute verbatim.

    This is the guard that makes a bad direction impossible, and it had no
    test: the registered-catalog check downstream can only ever see ``ltr`` or
    ``rtl`` because loading raises here first, and a missing key defaults
    rather than failing.
    """
    (tmp_path / "en.json").write_text(
        json.dumps(
            {
                "meta": {"native_name": "English", "dir": "sideways"},
                "messages": {},
                "tool_groups": {},
                "tools": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=re.escape("meta.dir must be ltr or rtl")):
        load_catalogs(tmp_path)


def test_catalog_without_a_native_name_is_rejected(tmp_path: Path) -> None:
    """The language picker has no label to render without it."""
    (tmp_path / "en.json").write_text(
        json.dumps(
            {
                "meta": {"native_name": "   "},
                "messages": {},
                "tool_groups": {},
                "tools": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=re.escape("needs meta.native_name")):
        load_catalogs(tmp_path)


@pytest.mark.parametrize("blank", ["", " ", "\n\t "])
@pytest.mark.parametrize("section", ["messages", "tool_groups"])
def test_blank_catalog_value_is_rejected(
    tmp_path: Path, section: str, blank: str
) -> None:
    """Blank and absent look the same in JSON and behave oppositely on screen.

    English is the per-key fallback only for a key that is ABSENT: `t()` and
    `tHtml()` resolve with `hasOwnProperty`, so an empty string wins over the
    English source and renders as nothing. A translator emptying a value to
    mean "not translated yet" would silently blank that piece of UI, and no
    other check objects — the parity ceilings count a key untranslated only
    when it equals the English or is missing.
    """
    catalog = {
        "meta": {"native_name": "English", "dir": "ltr"},
        "messages": {},
        "tool_groups": {},
        "tools": {},
    }
    catalog[section] = {"a": blank}
    (tmp_path / "en.json").write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(f"en.json.{section}.a is blank")):
        load_catalogs(tmp_path)


@pytest.mark.parametrize("blank", ["", " ", "\n\t "])
@pytest.mark.parametrize("field", ["title", "description"])
def test_blank_tool_field_is_rejected(tmp_path: Path, field: str, blank: str) -> None:
    """Same rule on the per-tool surface, which validates separately."""
    (tmp_path / "en.json").write_text(
        json.dumps(
            {
                "meta": {"native_name": "English", "dir": "ltr"},
                "messages": {},
                "tool_groups": {},
                "tools": {"ha_get_state": {field: blank}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match=re.escape(f"en.json.tools.ha_get_state.{field} is blank")
    ):
        load_catalogs(tmp_path)


def test_inline_payload_escapes_script_breakout() -> None:
    serialized = serialize_payload({"messages": {"unsafe": "</script><b>&"}})

    assert "</script>" not in serialized
    assert "<b>" not in serialized
    assert "\\u003c/script\\u003e" in serialized


def _request(
    *, query: bytes = b"", cookie: str | None = None, language: str | None = None
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if language:
        headers.append((b"accept-language", language.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/mcp/settings",
            "query_string": query,
            "headers": headers,
        }
    )


def test_render_uses_home_assistant_language_hint() -> None:
    html = _render_settings_html(_request(query=b"ha_lang=ru-RU"))

    assert '<html lang="ru" dir="ltr">' in html
    assert '"locale":"ru"' in html
    assert '"app.title":"Настройки HA-MCP"' in html
    assert "__HA_MCP_" not in html


def test_render_cookie_overrides_home_assistant_and_browser_language() -> None:
    html = _render_settings_html(
        _request(
            query=b"ha_lang=en",
            cookie="ha_mcp_locale=ru",
            language="en-US,en;q=0.9",
        )
    )

    assert '<html lang="ru" dir="ltr">' in html


def test_shipped_locales_are_discovered() -> None:
    """The parametrized check below collects nothing on an empty registry."""
    assert _shipped_locales(), (
        "no non-English catalogs registered in ha_mcp.settings_ui._i18n.CATALOGS "
        "— the per-locale check below would pass by collecting zero cases"
    )


@pytest.mark.parametrize("locale", _shipped_locales())
def test_shipped_catalog_loads_and_is_registered(locale: str) -> None:
    """One rule for every locale, rather than a copy per language.

    The four hand-written copies this replaces had already drifted: ``ru``
    had no test at all and ``de`` asserted neither ``tool_groups`` nor
    ``tools``, so a catalog could ship those two sections empty and stay
    green — the half-install ``test_locale_parity`` exists to prevent.
    """
    catalog = CATALOGS[locale]

    assert catalog["meta"]["native_name"], (
        f"{_catalog_file(locale)} needs a non-empty meta.native_name — the "
        "language picker renders it as the option label"
    )
    assert catalog["meta"].get("dir") in {"ltr", "rtl"}, (
        f"{_catalog_file(locale)} meta.dir is {catalog['meta'].get('dir')!r}; "
        "the rendered <html dir> attribute only accepts 'ltr' or 'rtl'"
    )


# Same gate as ``test_locale_parity.completeness`` (see the marker comment
# there): filled tool sections are the post-merge locale-sync workflow's to
# owe, not the PR's — a new language legitimately merges as a near-empty
# catalog the daily sync then fills, carrying only what the ungated checks in
# this file ask of it. ``test_locale_sync_gate_shape`` pins the env-var wiring
# on both files.
_completeness = pytest.mark.skipif(
    not os.environ.get("LOCALE_COMPLETENESS_CHECKS"),
    reason=(
        "translated-catalog completeness is verified by the post-merge "
        "locale-sync workflow — set LOCALE_COMPLETENESS_CHECKS=1 to run"
    ),
)


@_completeness
@pytest.mark.parametrize("locale", _shipped_locales())
def test_shipped_catalog_translates_the_tools_tab(locale: str) -> None:
    """Both tool sections must be filled once the sync has run.

    Split from the structural check above so a near-empty catalog can merge
    and be filled post-merge; an empty section here after a clean sync run
    means the fill never happened.
    """
    catalog = CATALOGS[locale]

    assert catalog["tool_groups"], (
        f"{_catalog_file(locale)} has an empty tool_groups — the tools tab "
        "would fall back to English group headings for this language"
    )
    assert catalog["tools"], (
        f"{_catalog_file(locale)} has an empty tools section — every tool "
        "title and description would fall back to English for this language"
    )


def test_native_names_name_their_own_language() -> None:
    """One rule per locale cannot pin the name each locale must carry.

    Collapsing the four hand-written tests dropped every specific
    ``native_name`` assertion, so ``es.json`` shipping ``"Deutsch"`` — or
    English's own name — is green today: the picker then offers the same label
    twice, and the label does not name the language it selects. Comparing the
    catalogs to each other pins that without hardcoding seven literals here.
    """
    english = CATALOGS["en"]["meta"]["native_name"]
    names = {
        locale: CATALOGS[locale]["meta"]["native_name"] for locale in _shipped_locales()
    }

    borrowed = sorted(locale for locale, name in names.items() if name == english)
    assert not borrowed, (
        f"{borrowed} carry English's own native_name {english!r} — the "
        "language picker would offer it twice and select the wrong catalog"
    )

    counts = Counter(names.values())
    shared = sorted(name for name, count in counts.items() if count > 1)
    assert not shared, (
        f"native_name {shared} is used by more than one catalog ({names}) — "
        "each option label in the language picker must name its own language"
    )


def test_disallowed_inline_markup_is_rejected(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={"note": "Use <code>x</code>"},
    )
    _write_catalog(
        tmp_path,
        "de",
        native_name="Deutsch",
        messages={"note": "Nutze <Code >x</Code>"},
    )
    with pytest.raises(ValueError, match="inline markup"):
        load_catalogs(tmp_path)


def test_allowlisted_inline_markup_loads(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={
            "note": (
                "See <strong>docs</strong>, <code>x</code> or the "
                '<a href="#" data-panel-link="tools">Tools</a> tab; 1 < 5 is prose.'
            )
        },
    )
    catalogs = load_catalogs(tmp_path)
    assert "note" in catalogs["en"]["messages"]


def _write_settings_html(directory: Path, *panels: str) -> Path:
    """A stand-in settings page declaring exactly ``panels`` as tabs.

    The panel-link tests own their tab ids this way rather than borrowing the
    shipped page's, so renaming a real tab cannot make them fail for a reason
    that has nothing to do with what they assert.
    """
    path = directory / "settings.html"
    path.write_text(
        "\n".join(
            f'<button class="tab" data-panel="{panel}" role="tab">{panel}</button>'
            for panel in panels
        ),
        encoding="utf-8",
    )
    return path


def test_panel_link_to_unknown_tab_is_rejected(tmp_path: Path) -> None:
    """A mistyped target passes the markup allowlist and then does nothing.

    ``settings.js`` hands the value straight to ``activateTab``, which no-ops
    on an id no panel declares — so the link silently stops working in that
    one language while the visible link text still reads correctly.
    """
    settings_html = _write_settings_html(tmp_path, "alpha", "beta")
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={"note": '<a href="#" data-panel-link="alpha">Alpha</a>'},
    )
    _write_catalog(
        tmp_path,
        "fr",
        native_name="Français",
        messages={"note": '<a href="#" data-panel-link="alfa">Alfa</a>'},
    )

    with pytest.raises(ValueError, match=re.escape("settings.html does not declare")):
        load_catalogs(tmp_path, settings_html)


def test_panel_link_must_target_the_same_tab_as_english(tmp_path: Path) -> None:
    """Both ids are real, so only a cross-locale comparison catches this."""
    settings_html = _write_settings_html(tmp_path, "alpha", "beta")
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={"note": '<a href="#" data-panel-link="alpha">Alpha</a>'},
    )
    _write_catalog(
        tmp_path,
        "de",
        native_name="Deutsch",
        messages={"note": '<a href="#" data-panel-link="beta">Beta</a>'},
    )

    with pytest.raises(ValueError, match="but English links to"):
        load_catalogs(tmp_path, settings_html)


def test_panel_links_may_be_reordered_by_a_translation(tmp_path: Path) -> None:
    """Grammar reorders links; the targets are what must match, not the order.

    ``load_catalogs`` runs at import, so rejecting a legitimately reordered
    pair would stop the server from starting.
    """
    settings_html = _write_settings_html(tmp_path, "alpha", "beta")
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={
            "note": (
                '<a href="#" data-panel-link="alpha">Alpha</a> then '
                '<a href="#" data-panel-link="beta">Beta</a>'
            )
        },
    )
    _write_catalog(
        tmp_path,
        "fr",
        native_name="Français",
        messages={
            "note": (
                '<a href="#" data-panel-link="beta">Bêta</a> puis '
                '<a href="#" data-panel-link="alpha">Alpha</a>'
            )
        },
    )

    catalogs = load_catalogs(tmp_path, settings_html)

    assert "note" in catalogs["fr"]["messages"]


def test_dropping_one_of_two_panel_links_is_rejected(tmp_path: Path) -> None:
    """Order-independence must not become "any subset will do"."""
    settings_html = _write_settings_html(tmp_path, "alpha", "beta")
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={
            "note": (
                '<a href="#" data-panel-link="alpha">Alpha</a> then '
                '<a href="#" data-panel-link="beta">Beta</a>'
            )
        },
    )
    _write_catalog(
        tmp_path,
        "fr",
        native_name="Français",
        messages={"note": '<a href="#" data-panel-link="alpha">Alpha</a>'},
    )

    with pytest.raises(ValueError, match="but English links to"):
        load_catalogs(tmp_path, settings_html)


def test_panel_link_attribute_shown_as_literal_text_is_not_a_link(
    tmp_path: Path,
) -> None:
    """Help text may document the attribute; that is not navigation.

    The extraction matches the whole allowlisted anchor, so a message showing
    ``data-panel-link="example"`` inside ``<code>`` neither has to name a real
    panel nor has to match English's targets.
    """
    settings_html = _write_settings_html(tmp_path, "alpha")
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={"note": 'Write <code>data-panel-link="example"</code> to link.'},
    )
    _write_catalog(
        tmp_path,
        "de",
        native_name="Deutsch",
        messages={"note": 'Schreibe <code>data-panel-link="beispiel"</code>.'},
    )

    catalogs = load_catalogs(tmp_path, settings_html)

    assert "note" in catalogs["de"]["messages"]


def test_only_tab_buttons_declare_panels(tmp_path: Path) -> None:
    """A page that merely mentions the attribute declares no tab.

    Scanning the whole page would let a code sample or doc comment register a
    panel that has no button, so a link to it would pass this check and then
    dead-end in the UI — the same "textually present, functionally absent" gap
    the link side closes.
    """
    settings_html = tmp_path / "settings.html"
    settings_html.write_text(
        '<!-- to add a tab, set data-panel="ghost" on the button -->\n'
        '<button class="tab" data-panel="alpha" role="tab">Alpha</button>',
        encoding="utf-8",
    )
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={"note": '<a href="#" data-panel-link="ghost">Ghost</a>'},
    )

    with pytest.raises(ValueError, match=re.escape("settings.html does not declare")):
        load_catalogs(tmp_path, settings_html)


def test_english_message_with_unknown_panel_target_is_rejected(tmp_path: Path) -> None:
    """The source catalog is checked too, not just the translations.

    The cross-locale comparison skips English by definition, so only the
    known-panel check covers it. Nothing else would notice a dead link written
    into ``en.json`` itself.
    """
    settings_html = _write_settings_html(tmp_path, "alpha")
    _write_catalog(
        tmp_path,
        "en",
        native_name="English",
        messages={"note": '<a href="#" data-panel-link="bogus">Bogus</a>'},
    )

    with pytest.raises(ValueError, match=re.escape("settings.html does not declare")):
        load_catalogs(tmp_path, settings_html)


def test_panel_link_in_a_locale_only_key_is_still_checked(tmp_path: Path) -> None:
    """A key English does not have skips the comparison, not the panel check."""
    settings_html = _write_settings_html(tmp_path, "alpha")
    _write_catalog(tmp_path, "en", native_name="English", messages={"other": "hi"})
    _write_catalog(
        tmp_path,
        "fr",
        native_name="Français",
        messages={"extra": '<a href="#" data-panel-link="bogus">Bogus</a>'},
    )

    with pytest.raises(ValueError, match=re.escape("settings.html does not declare")):
        load_catalogs(tmp_path, settings_html)


def _assert_catalog_words(prefix: str, values: list[str], what: str) -> None:
    """Every catalog translates ``prefix + value`` for each backend value.

    A key one catalog omits still renders: ``build_payload`` merges English
    under the selected locale, so what reaches the page is English's word for
    that value, sitting inside otherwise translated copy. Only a value English
    does not carry either reaches ``settings.js``'s own fallback and renders as
    the raw enum. Both are English on screen, which is what this guards.

    ``values`` is derived from the type the backend sends, which makes
    extending that type without adding words a failure here rather than on
    screen — the merge is silent and a couple of English words stay far under
    the identical-share ceiling, so nothing else notices.
    """
    assert values, (
        f"no {what} is defined any more — the settings.js branch these words "
        "serve cannot be reached, so either the branch or this check is dead"
    )

    for locale, catalog in sorted(CATALOGS.items()):
        messages = catalog["messages"]
        missing = [key for value in values if (key := prefix + value) not in messages]
        assert not missing, (
            f"{_catalog_file(locale)} is missing {missing}. The page payload "
            f"merges English underneath, so this language shows English's word "
            f"for that {what} inside a translated sentence — and the raw "
            f"{what} where English is missing it too. The key belongs in every "
            "catalog, not only in en.json."
        )
        blank = [value for value in values if not messages[prefix + value].strip()]
        assert not blank, (
            f"{_catalog_file(locale)} leaves {blank} empty or whitespace-only. "
            "The word is interpolated into a sentence rather than shown alone, "
            "so a blank value renders a hole in that sentence — it does not "
            "fall back to English."
        )
        if locale == DEFAULT_LOCALE:
            # English is where these words legitimately read as the enum does.
            continue
        # A present key is not a translated one. The English value survives a
        # copied catalog and an en-fallback autofill, and a couple of such
        # words stay far under the identical-share ceiling, so nothing else
        # reds on them. Stripped and casefolded because sentence-casing a
        # display word is what a translator actually does, and a capitalised
        # copy renders the same English the backend spells.
        untranslated = [
            value
            for value in values
            if messages[prefix + value].strip().casefold() == value
        ]
        assert not untranslated, (
            f"{_catalog_file(locale)} still spells {untranslated} the way the "
            "backend does. That word renders inside translated copy, which is "
            "the English-in-a-translated-sentence bug these keys prevent."
        )


def test_every_decided_outcome_has_a_catalog_word() -> None:
    """The 409 body carries a backend enum; the sentence around it is translated.

    ``settings.js`` renders ``policies.pending.already_decided`` with the
    ``current_decision`` value the conflict response returns. That value is a
    ``Decision`` literal, not display text, so interpolating it raw dropped an
    English word into an otherwise translated sentence — Italian read
    ``Questa approvazione è già stata approved``. It is mapped through the
    catalog now, which only holds while every outcome the queue can report has
    a translated key in every catalog: adding one to ``Decision`` and nothing
    else would put the English back, and a key that lands in ``en.json`` alone
    renders English in every other language while staying far under the
    identical-share ceiling, so nothing else would notice either.

    This is the type-derived half of the check and runs everywhere.
    ``TestAlreadyDecidedCopy`` in ``test_settings_ui_js_behavior.py`` drives the
    409 handler under each catalog and asserts on the alert the user actually
    reads; it needs the JSDOM harness and skips without it, which is why the
    key coverage lives here rather than only there.
    """
    _assert_catalog_words(
        "policies.pending.decision.",
        sorted(set(get_args(Decision)) - {"pending"}),
        "Decision outcome",
    )


def test_every_predicate_operator_has_a_catalog_word() -> None:
    """Condition rows read the operator through the catalog, same as the 409.

    ``displayPredicate`` renders ``policies.operators.<op>`` in the middle of
    an otherwise translated condition row. Every operator has a word in every
    catalog today, so this is not a live defect — it is the one thing the fix
    above built machinery for and then left unpinned. Drop
    ``policies.operators.regex`` from one catalog and that language reads
    ``args.domain matches regex "light"`` mid-sentence, English's own phrasing
    merged in underneath; add an operator to ``PredicateOp`` and ship without
    catalog entries at all and it is the bare literal, in every language
    including English. Nothing else would say so: the decided-outcome check
    derives only from ``Decision``, and one missing key is absorbed by the
    identical-share ceiling.
    """
    _assert_catalog_words(
        "policies.operators.",
        sorted(get_args(PredicateOp)),
        "PredicateOp operator",
    )
