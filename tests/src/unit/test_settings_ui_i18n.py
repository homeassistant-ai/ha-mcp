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
) -> None:
    (directory / f"{locale}.json").write_text(
        json.dumps(
            {
                "meta": {"native_name": native_name, "dir": "ltr"},
                "messages": messages,
                "tool_groups": {},
                "tools": {},
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
