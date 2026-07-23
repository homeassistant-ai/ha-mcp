"""Settings UI locale discovery, selection, fallback, and safe rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.requests import Request

from ha_mcp.settings_ui import _render_settings_html
from ha_mcp.settings_ui._i18n import (
    build_payload,
    load_catalogs,
    normalize_locale,
    select_locale,
    serialize_payload,
)


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


def test_de_catalog_loads_and_is_registered() -> None:
    from ha_mcp.settings_ui._i18n import CATALOGS

    assert "de" in CATALOGS
    assert CATALOGS["de"]["meta"]["native_name"] == "Deutsch"
    assert CATALOGS["de"]["meta"]["dir"] == "ltr"


def test_zh_hans_catalog_loads_and_is_registered() -> None:
    from ha_mcp.settings_ui._i18n import CATALOGS

    assert "zh-hans" in CATALOGS
    assert CATALOGS["zh-hans"]["meta"]["native_name"] == "简体中文"
    assert CATALOGS["zh-hans"]["meta"]["dir"] == "ltr"
    # 工具分组与工具 UI 翻译须已填充，避免空翻译漏过 CI
    assert CATALOGS["zh-hans"]["tool_groups"]
    assert CATALOGS["zh-hans"]["tools"]


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
