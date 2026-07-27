"""Settings UI locale discovery, selection, fallback, and safe rendering."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest
from starlette.requests import Request

from ha_mcp.settings_ui import _render_settings_html
from ha_mcp.settings_ui._i18n import (
    CATALOGS,
    build_payload,
    load_catalogs,
    normalize_locale,
    select_locale,
    serialize_payload,
)


def _shipped_locales() -> list[str]:
    """Registered catalog codes except English, which has no translations."""
    return sorted(code for code in CATALOGS if code != "en")


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
        f"{locale}.json needs a non-empty meta.native_name — the language "
        "picker renders it as the option label"
    )
    assert catalog["meta"].get("dir") in {"ltr", "rtl"}, (
        f"{locale}.json meta.dir is {catalog['meta'].get('dir')!r}; the "
        "rendered <html dir> attribute only accepts 'ltr' or 'rtl'"
    )
    assert catalog["tool_groups"], (
        f"{locale}.json has an empty tool_groups — the tools tab would fall "
        "back to English group headings for this language"
    )
    assert catalog["tools"], (
        f"{locale}.json has an empty tools section — every tool title and "
        "description would fall back to English for this language"
    )


def test_native_names_name_their_own_language() -> None:
    """One rule per locale cannot pin the name each locale must carry.

    Collapsing the four hand-written tests dropped every specific
    ``native_name`` assertion, so ``es.json`` shipping ``"Deutsch"`` — or
    English's own name — is green today: the picker then offers the same label
    twice, and the label does not name the language it selects. Comparing the
    catalogs to each other pins that without hardcoding six literals here.
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


def test_tlh_catalog_loads_and_is_registered() -> None:
    from ha_mcp.settings_ui._i18n import CATALOGS

    assert "tlh" in CATALOGS
    assert CATALOGS["tlh"]["meta"]["native_name"] == "tlhIngan Hol"
    assert CATALOGS["tlh"]["meta"]["dir"] == "ltr"
    assert CATALOGS["tlh"]["messages"]["actions.save"] == "pol"


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
