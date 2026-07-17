"""Translation catalog loading and locale selection for the settings UI.

Catalogs are discovered from ``locales/*.json`` so adding a language does not
require Python or JavaScript changes.  English is the canonical fallback;
individual translations may be incomplete and safely inherit missing values.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DEFAULT_LOCALE = "en"
LOCALE_COOKIE = "ha_mcp_locale"
LOCALES_DIR = Path(__file__).parent / "locales"

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _validate_string_map(value: Any, *, context: str) -> dict[str, str]:
    """Return a validated ``str -> str`` catalog section."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    result: dict[str, str] = {}
    for key, text in value.items():
        if not isinstance(key, str) or not isinstance(text, str):
            raise ValueError(f"{context} must contain only string keys and values")
        result[key] = text
    return result


def _validate_tools(value: Any, *, context: str) -> dict[str, dict[str, str]]:
    """Return validated optional per-tool UI translations."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    result: dict[str, dict[str, str]] = {}
    for tool_name, tool_values in value.items():
        if not isinstance(tool_name, str) or not isinstance(tool_values, dict):
            raise ValueError(f"{context} entries must be objects keyed by tool name")
        translated: dict[str, str] = {}
        for field in ("title", "description"):
            field_value = tool_values.get(field)
            if field_value is None:
                continue
            if not isinstance(field_value, str):
                raise ValueError(f"{context}.{tool_name}.{field} must be a string")
            translated[field] = field_value
        unknown = set(tool_values) - {"title", "description"}
        if unknown:
            raise ValueError(
                f"{context}.{tool_name} has unsupported fields: {sorted(unknown)}"
            )
        result[tool_name] = translated
    return result


def load_catalogs(directory: Path = LOCALES_DIR) -> dict[str, dict[str, Any]]:
    """Load and validate every JSON translation catalog in ``directory``."""
    catalogs: dict[str, dict[str, Any]] = {}
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as exc:  # pragma: no cover - packaging guard
        raise ImportError(f"Unable to enumerate settings UI locales at {directory}") from exc

    for path in paths:
        locale = path.stem.lower().replace("_", "-")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ImportError(f"Invalid settings UI locale catalog: {path}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Locale catalog {path} must contain a JSON object")

        meta = raw.get("meta")
        if not isinstance(meta, dict):
            raise ValueError(f"Locale catalog {path} must define a meta object")
        native_name = meta.get("native_name")
        direction = meta.get("dir", "ltr")
        if not isinstance(native_name, str) or not native_name.strip():
            raise ValueError(f"Locale catalog {path} needs meta.native_name")
        if direction not in ("ltr", "rtl"):
            raise ValueError(f"Locale catalog {path} meta.dir must be ltr or rtl")

        unknown_sections = set(raw) - {"meta", "messages", "tool_groups", "tools"}
        if unknown_sections:
            raise ValueError(
                f"Locale catalog {path} has unsupported sections: "
                f"{sorted(unknown_sections)}"
            )

        catalogs[locale] = {
            "meta": {"native_name": native_name, "dir": direction},
            "messages": _validate_string_map(
                raw.get("messages"), context=f"{path.name}.messages"
            ),
            "tool_groups": _validate_string_map(
                raw.get("tool_groups"), context=f"{path.name}.tool_groups"
            ),
            "tools": _validate_tools(raw.get("tools"), context=f"{path.name}.tools"),
        }

    if DEFAULT_LOCALE not in catalogs:
        raise ImportError(
            f"The settings UI requires {DEFAULT_LOCALE}.json in {directory}"
        )
    _validate_placeholder_parity(catalogs)
    return catalogs


def _validate_placeholder_parity(catalogs: dict[str, dict[str, Any]]) -> None:
    """Reject translations that drop or invent ``{name}`` placeholders."""
    english = catalogs[DEFAULT_LOCALE]["messages"]
    for locale, catalog in catalogs.items():
        if locale == DEFAULT_LOCALE:
            continue
        for key, translated in catalog["messages"].items():
            source = english.get(key)
            if source is None:
                continue
            source_fields = set(_PLACEHOLDER_RE.findall(source))
            translated_fields = set(_PLACEHOLDER_RE.findall(translated))
            if source_fields != translated_fields:
                raise ValueError(
                    f"Locale {locale} message {key!r} has placeholders "
                    f"{sorted(translated_fields)}, expected {sorted(source_fields)}"
                )


CATALOGS = load_catalogs()


def normalize_locale(
    value: str | None, catalogs: dict[str, dict[str, Any]] = CATALOGS
) -> str | None:
    """Resolve a locale or regional locale to a supported catalog code."""
    if not value:
        return None
    candidate = value.strip().lower().replace("_", "-")
    if candidate in catalogs:
        return candidate
    base = candidate.split("-", 1)[0]
    return base if base in catalogs else None


def _accept_language_candidates(header: str | None) -> list[str]:
    """Return Accept-Language values ordered by descending quality."""
    if not header:
        return []
    candidates: list[tuple[float, int, str]] = []
    for index, item in enumerate(header.split(",")):
        parts = [part.strip() for part in item.split(";")]
        language = parts[0]
        quality = 1.0
        for part in parts[1:]:
            if part.startswith("q="):
                try:
                    quality = float(part[2:])
                except ValueError:
                    quality = 0.0
        if language and language != "*" and quality > 0:
            candidates.append((quality, -index, language))
    candidates.sort(reverse=True)
    return [language for _, _, language in candidates]


def select_locale(
    *,
    cookie_locale: str | None = None,
    ha_language: str | None = None,
    accept_language: str | None = None,
    catalogs: dict[str, dict[str, Any]] = CATALOGS,
) -> str:
    """Choose locale: explicit cookie, HA hint, browser header, then English."""
    for value in (cookie_locale, ha_language):
        if selected := normalize_locale(value, catalogs):
            return selected
    for value in _accept_language_candidates(accept_language):
        if selected := normalize_locale(value, catalogs):
            return selected
    return DEFAULT_LOCALE


def build_payload(
    locale: str, catalogs: dict[str, dict[str, Any]] = CATALOGS
) -> dict[str, Any]:
    """Build a single merged catalog payload for the rendered page."""
    selected_locale = normalize_locale(locale, catalogs) or DEFAULT_LOCALE
    english = catalogs[DEFAULT_LOCALE]
    selected = catalogs[selected_locale]

    tools: dict[str, dict[str, str]] = {
        name: dict(values) for name, values in english["tools"].items()
    }
    for name, values in selected["tools"].items():
        tools.setdefault(name, {}).update(values)

    return {
        "locale": selected_locale,
        "dir": selected["meta"]["dir"],
        "messages": {**english["messages"], **selected["messages"]},
        "tool_groups": {**english["tool_groups"], **selected["tool_groups"]},
        "tools": tools,
        "languages": [
            {
                "code": code,
                "native_name": catalog["meta"]["native_name"],
                "dir": catalog["meta"]["dir"],
            }
            for code, catalog in sorted(catalogs.items())
        ],
    }


def serialize_payload(payload: dict[str, Any]) -> str:
    """Serialize JSON safely for an inline ``application/json`` script."""
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
