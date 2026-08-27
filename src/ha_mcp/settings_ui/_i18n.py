"""Translation catalog loading and locale selection for the settings UI.

Catalogs are discovered from ``locales/*.json`` so adding a language does not
require Python or JavaScript changes.  English is the canonical fallback;
individual translations may be incomplete and safely inherit missing values.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

from ._locale_policy import is_best_effort_locale

_LOGGER = logging.getLogger(__name__)

DEFAULT_LOCALE = "en"
LOCALE_COOKIE = "ha_mcp_locale"
LOCALES_DIR = Path(__file__).parent / "locales"
# The page whose tab buttons define the valid cross-panel link targets.
_SETTINGS_HTML = Path(__file__).parent / "settings.html"

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _validate_string_map(value: Any, *, context: str) -> dict[str, str]:
    """Return a validated ``str -> str`` catalog section.

    A blank value is rejected rather than stored. English is the per-key
    fallback, but only for a key that is ABSENT: ``t()`` and ``tHtml()`` pick
    the catalog value with ``hasOwnProperty``, so a present-but-empty string
    wins over English and renders as nothing at all. Omitting a key and
    emptying it therefore look identical in the JSON and behave oppositely on
    screen, which is the kind of difference no reviewer catches by reading.
    Nothing else covers it either — the parity ceilings count a key
    untranslated only when it equals the English or is missing, so ``""``
    reads there as translated.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    result: dict[str, str] = {}
    for key, text in value.items():
        if not isinstance(key, str) or not isinstance(text, str):
            raise ValueError(f"{context} must contain only string keys and values")
        if not text.strip():
            raise ValueError(
                f"{context}.{key} is blank — English renders only for an "
                "absent key, so translate it, or leave the key out where the "
                "section allows a missing one"
            )
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
            # Same rule as _validate_string_map: blank loses to nothing, an
            # omitted field falls back to the tool's own English metadata.
            if not field_value.strip():
                raise ValueError(
                    f"{context}.{tool_name}.{field} is blank — omit the field "
                    "instead, so the English tool metadata renders"
                )
            translated[field] = field_value
        unknown = set(tool_values) - {"title", "description"}
        if unknown:
            raise ValueError(
                f"{context}.{tool_name} has unsupported fields: {sorted(unknown)}"
            )
        result[tool_name] = translated
    return result


def _load_catalog_file(path: Path) -> dict[str, Any]:
    """Load and validate one catalog without coupling it to its siblings."""
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

    return {
        "meta": {"native_name": native_name, "dir": direction},
        "messages": _validate_string_map(
            raw.get("messages"), context=f"{path.name}.messages"
        ),
        "tool_groups": _validate_string_map(
            raw.get("tool_groups"), context=f"{path.name}.tool_groups"
        ),
        "tools": _validate_tools(raw.get("tools"), context=f"{path.name}.tools"),
    }


def _warn_best_effort_catalog(locale: str, path: Path, exc: Exception) -> None:
    _LOGGER.warning("Skipping best-effort locale %s from %s: %s", locale, path, exc)


def _warn_best_effort_entry(locale: str, entry: str, exc: Exception) -> None:
    _LOGGER.warning(
        "Ignoring invalid best-effort locale %s %s; using English fallback: %s",
        locale,
        entry,
        exc,
    )


def _catalog_fragment(
    catalog: dict[str, Any],
    *,
    messages: dict[str, str] | None = None,
    tools: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return a minimal catalog used to validate one translated entry."""
    return {
        "meta": catalog["meta"],
        "messages": messages or {},
        "tool_groups": {},
        "tools": tools or {},
    }


def _sanitize_best_effort_catalog(
    locale: str,
    catalog: dict[str, Any],
    english: dict[str, Any],
    settings_html: Path,
) -> dict[str, Any]:
    """Drop only invalid translated entries so the rest of a locale survives."""
    sanitized = {
        "meta": dict(catalog["meta"]),
        "messages": dict(catalog["messages"]),
        "tool_groups": dict(catalog["tool_groups"]),
        "tools": {
            tool_name: dict(fields) for tool_name, fields in catalog["tools"].items()
        },
    }
    known_panels = _known_panels(settings_html)

    for key, translated in tuple(sanitized["messages"].items()):
        english_messages = (
            {key: english["messages"][key]} if key in english["messages"] else {}
        )
        candidate = {
            DEFAULT_LOCALE: _catalog_fragment(english, messages=english_messages),
            locale: _catalog_fragment(sanitized, messages={key: translated}),
        }
        try:
            _validate_placeholder_parity(candidate)
            _validate_inline_markup(candidate)
            _validate_panel_links(candidate, settings_html, known_panels=known_panels)
        except ValueError as exc:
            del sanitized["messages"][key]
            _warn_best_effort_entry(locale, f"message {key!r}", exc)

    for tool_name, translated_tool in tuple(sanitized["tools"].items()):
        for field, translated in tuple(translated_tool.items()):
            english_field = english["tools"].get(tool_name, {}).get(field)
            english_tools = (
                {tool_name: {field: english_field}} if english_field is not None else {}
            )
            candidate = {
                DEFAULT_LOCALE: _catalog_fragment(english, tools=english_tools),
                locale: _catalog_fragment(
                    sanitized,
                    tools={tool_name: {field: translated}},
                ),
            }
            try:
                _validate_placeholder_parity(candidate)
            except ValueError as exc:
                del translated_tool[field]
                _warn_best_effort_entry(
                    locale, f"tool {tool_name!r} field {field!r}", exc
                )
        if not translated_tool:
            del sanitized["tools"][tool_name]

    return sanitized


def load_catalogs(
    directory: Path = LOCALES_DIR, settings_html: Path = _SETTINGS_HTML
) -> dict[str, dict[str, Any]]:
    """Load and validate every JSON translation catalog in ``directory``.

    ``settings_html`` is the page whose tab buttons define the valid
    cross-panel link targets; overridable so tests need not depend on the
    shipped page's panel ids."""
    catalogs: dict[str, dict[str, Any]] = {}
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as exc:  # pragma: no cover - packaging guard
        raise ImportError(
            f"Unable to enumerate settings UI locales at {directory}"
        ) from exc

    for path in paths:
        locale = path.stem.lower().replace("_", "-")
        try:
            catalogs[locale] = _load_catalog_file(path)
        except (ImportError, ValueError) as exc:
            if not is_best_effort_locale(locale):
                raise
            _warn_best_effort_catalog(locale, path, exc)

    if DEFAULT_LOCALE not in catalogs:
        raise ImportError(
            f"The settings UI requires {DEFAULT_LOCALE}.json in {directory}"
        )

    strict_catalogs = {
        locale: catalog
        for locale, catalog in catalogs.items()
        if not is_best_effort_locale(locale)
    }
    _validate_placeholder_parity(strict_catalogs)
    _validate_inline_markup(strict_catalogs)
    _validate_panel_links(strict_catalogs, settings_html)

    for locale in sorted(set(catalogs) - set(strict_catalogs)):
        catalogs[locale] = _sanitize_best_effort_catalog(
            locale, catalogs[locale], catalogs[DEFAULT_LOCALE], settings_html
        )
    return catalogs


def _validate_placeholder_parity(catalogs: dict[str, dict[str, Any]]) -> None:
    """Reject catalog-backed translations with mismatched placeholders.

    Tool metadata normally comes from the runtime API rather than ``en.json``;
    ``settings.js`` performs the corresponding parity check against those
    canonical values before displaying a translated tool field.
    """
    english_messages = catalogs[DEFAULT_LOCALE]["messages"]
    english_tools = catalogs[DEFAULT_LOCALE]["tools"]
    for locale, catalog in catalogs.items():
        if locale == DEFAULT_LOCALE:
            continue
        for key, translated in catalog["messages"].items():
            source = english_messages.get(key)
            if source is None:
                continue
            source_fields = set(_PLACEHOLDER_RE.findall(source))
            translated_fields = set(_PLACEHOLDER_RE.findall(translated))
            if source_fields != translated_fields:
                raise ValueError(
                    f"Locale {locale} message {key!r} has placeholders "
                    f"{sorted(translated_fields)}, expected {sorted(source_fields)}"
                )
        for tool_name, translated_tool in catalog["tools"].items():
            source_tool = english_tools.get(tool_name, {})
            for field, translated in translated_tool.items():
                source = source_tool.get(field)
                if source is None:
                    continue
                source_fields = set(_PLACEHOLDER_RE.findall(source))
                translated_fields = set(_PLACEHOLDER_RE.findall(translated))
                if source_fields != translated_fields:
                    raise ValueError(
                        f"Locale {locale} tool {tool_name!r} field {field!r} "
                        f"has placeholders {sorted(translated_fields)}, expected "
                        f"{sorted(source_fields)}"
                    )


# Anything that looks like an HTML tag in a catalog message. A bare "<" in
# prose (e.g. "< 5") deliberately does not match — it renders fine escaped.
_TAG_LIKE_RE = re.compile(r"</?[a-zA-Z][^>]*>")
# The exact tag shapes settings.js::tHtml restores after escaping. Keep in
# sync with the restore regexes there — any other spelling (case, spacing,
# attribute order) survives escaping and shows the user literal markup text.
_ALLOWED_TAGS_RE = re.compile(
    r'</?code>|</?strong>|</a>|<a href="#" data-panel-link="[a-z][a-z-]*">'
)
# The tab a cross-panel link switches to. The allowlist above only accepts the
# tag *shape*, so "outils" or "backup" passes it and then silently does
# nothing: settings.js hands the value to activateTab, which no-ops on an
# unknown panel. Read the real ids out of the page rather than restating them.
# Matches the whole anchor, not a bare attribute: a message may legitimately
# show `<code>data-panel-link="example"</code>` as literal help text, and that
# is documentation, not navigation.
_PANEL_LINK_RE = re.compile(r'<a href="#" data-panel-link="([a-z][a-z-]*)">')
_PANEL_ID_RE = re.compile(r'data-panel="([a-z][a-z-]*)"')
# Only a real tab button declares a panel. Scanning the whole page for the
# attribute would let a future code sample or doc comment mentioning
# `data-panel="example"` register a tab that does not exist — the same gap
# between "textually present" and "functionally real" this module closes on
# the link side.
_TAB_BUTTON_RE = re.compile(r"<button\b[^>]*>")


def _known_panels(settings_html: Path = _SETTINGS_HTML) -> set[str]:
    """Panel ids declared by the settings page's own tab buttons."""
    try:
        markup = settings_html.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - packaging guard
        raise ImportError(
            f"settings.html missing at {settings_html}. It must ship in "
            "package-data (wheel), MANIFEST.in (sdist), and the PyInstaller "
            "datas (binary) -- this is a packaging bug, not a usage error."
        ) from exc
    return {
        panel
        for tag in _TAB_BUTTON_RE.findall(markup)
        if 'role="tab"' in tag
        for panel in _PANEL_ID_RE.findall(tag)
    }


def _validate_panel_links(
    catalogs: dict[str, dict[str, Any]],
    settings_html: Path = _SETTINGS_HTML,
    *,
    known_panels: set[str] | None = None,
) -> None:
    """Reject cross-panel links that point at a tab which does not exist.

    A mistyped target is invisible to every other check: the tag shape is
    allowlisted, the string carries no placeholders, and the visible link text
    still reads correctly. The link just stops working, in that one language.

    Scoped to ``messages`` for the same reason ``_validate_inline_markup`` is:
    only those render through ``tHtml``, which restores the anchor. Tool names
    and group labels go through ``escapeHtml``, so a link written there shows
    as visible garbled markup rather than a dead link — wrong, but not silent.
    """
    panels = known_panels if known_panels is not None else _known_panels(settings_html)
    english_messages = catalogs[DEFAULT_LOCALE]["messages"]
    for locale, catalog in catalogs.items():
        for key, value in catalog["messages"].items():
            targets = _PANEL_LINK_RE.findall(value)
            unknown = sorted(set(targets) - panels)
            if unknown:
                raise ValueError(
                    f"Locale {locale} message {key!r} links to panel(s) "
                    f"{unknown}, which settings.html does not declare; "
                    f"known panels: {sorted(panels)}"
                )
            source = english_messages.get(key)
            if source is None or locale == DEFAULT_LOCALE:
                continue
            source_targets = _PANEL_LINK_RE.findall(source)
            # Order-independent: a translation may reorder two links to suit
            # its grammar and still point at the same tabs. Counter keeps the
            # multiplicity check, so dropping one of a repeated pair fails.
            if Counter(targets) != Counter(source_targets):
                raise ValueError(
                    f"Locale {locale} message {key!r} links to "
                    f"{sorted(targets)}, but English links to "
                    f"{sorted(source_targets)}"
                )


def _validate_inline_markup(catalogs: dict[str, dict[str, Any]]) -> None:
    """Reject catalog messages whose markup ``tHtml`` cannot restore.

    ``settings.js::tHtml`` escapes every translated value and restores only
    the exact allowlisted tag shapes, so a translation written with
    ``<CODE>`` or ``<code >`` would silently render as literal escaped text.
    Fail fast at load time instead, mirroring the placeholder-parity check.
    Scoped to ``messages``: tool translations are plain text rendered through
    ``escapeHtml`` and carry no markup contract.
    """
    for locale, catalog in catalogs.items():
        for key, value in catalog["messages"].items():
            for tag in _TAG_LIKE_RE.findall(value):
                if _ALLOWED_TAGS_RE.fullmatch(tag) is None:
                    raise ValueError(
                        f"Locale {locale} message {key!r} contains inline "
                        f"markup {tag!r} that the settings UI cannot render; "
                        f"allowed: <code>, <strong>, </a>, and "
                        f'<a href="#" data-panel-link="...">'
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
    if base in catalogs:
        return base
    if base == "zh" and "zh-hans" in catalogs:
        # Map bare "zh" and simplified Chinese region tags (zh-CN, zh-SG) to
        # zh-hans. Do NOT map zh-TW, zh-HK, etc. — those would need a zh-Hant
        # catalog to be registered.
        if candidate == "zh" or candidate.split("-", 1)[-1] in ("cn", "sg"):
            return "zh-hans"
    return None


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
