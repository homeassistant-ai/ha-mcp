"""Locale parity across the four translated surfaces.

A language reaches users through four independent catalogs: the web settings
UI, the custom component's config/options flow, and the two add-on flavors.
Nothing links them, so a locale can be added to one surface and silently miss
the rest — Simplified Chinese shipped for the settings UI in #1992 and left
Chinese users an English config flow and English add-on options for a week,
and the same half-install was proposed again for French in #2038.

This test makes "a locale ships everywhere or not at all" structural: add a
catalog to one surface without the other three and CI goes red, naming the
files that are missing.

The per-surface content rules are enforced elsewhere — ``_i18n.load_catalogs``
validates settings UI placeholders and inline markup, and
``tests/addon/test_addon_structure.py`` requires a ``name``/``description``
for every add-on ``schema:`` key. The component catalogs had no such guard,
so their key and placeholder parity is checked here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

SETTINGS_LOCALES = _REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales"
COMPONENT_TRANSLATIONS = (
    _REPO_ROOT / "custom_components" / "ha_mcp_tools" / "translations"
)
COMPONENT_STRINGS = _REPO_ROOT / "custom_components" / "ha_mcp_tools" / "strings.json"
ADDON_DIRS = (
    _REPO_ROOT / "homeassistant-addon",
    _REPO_ROOT / "homeassistant-addon-dev",
)

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _surfaces() -> dict[str, set[str]]:
    """Return locale codes per surface, keyed by the path pattern to fix."""
    surfaces = {
        "src/ha_mcp/settings_ui/locales/<code>.json": {
            path.stem for path in SETTINGS_LOCALES.glob("*.json")
        },
        "custom_components/ha_mcp_tools/translations/<code>.json": {
            path.stem for path in COMPONENT_TRANSLATIONS.glob("*.json")
        },
    }
    for addon_dir in ADDON_DIRS:
        surfaces[f"{addon_dir.name}/translations/<code>.yaml"] = {
            path.stem for path in (addon_dir / "translations").glob("*.yaml")
        }
    return surfaces


def _flatten(value: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            flat.update(_flatten(item, path))
        else:
            flat[path] = item
    return flat


def _component_catalog(locale: str) -> dict[str, str]:
    return _flatten(
        json.loads((COMPONENT_TRANSLATIONS / f"{locale}.json").read_text("utf-8"))
    )


def _translated_component_locales() -> list[str]:
    return sorted(
        path.stem for path in COMPONENT_TRANSLATIONS.glob("*.json") if path.stem != "en"
    )


def test_every_locale_ships_on_every_surface() -> None:
    surfaces = _surfaces()
    assert surfaces["src/ha_mcp/settings_ui/locales/<code>.json"], (
        "no settings UI locale catalogs found — check the paths in this test"
    )

    every_locale: set[str] = set().union(*surfaces.values())
    gaps = {
        pattern: sorted(every_locale - present)
        for pattern, present in surfaces.items()
        if every_locale - present
    }

    assert not gaps, (
        "every language must ship on all four translated surfaces, using the "
        "same Home Assistant language code (de, ru, zh-Hans) in each filename. "
        "Missing: "
        + "; ".join(
            f"{pattern} for {', '.join(codes)}"
            for pattern, codes in sorted(gaps.items())
        )
    )


@pytest.mark.parametrize("locale", _translated_component_locales())
def test_component_catalog_matches_english_keys(locale: str) -> None:
    english = _component_catalog("en")
    translated = _component_catalog(locale)

    missing = sorted(set(english) - set(translated))
    unknown = sorted(set(translated) - set(english))

    assert not missing, (
        f"custom_components/ha_mcp_tools/translations/{locale}.json is missing "
        f"{len(missing)} key(s) present in en.json: {missing}"
    )
    assert not unknown, (
        f"custom_components/ha_mcp_tools/translations/{locale}.json has key(s) "
        f"that no longer exist in en.json: {unknown}"
    )


@pytest.mark.parametrize("locale", _translated_component_locales())
def test_component_catalog_keeps_english_placeholders(locale: str) -> None:
    """A dropped or renamed ``{placeholder}`` renders as literal text in HA."""
    english = _component_catalog("en")
    translated = _component_catalog(locale)

    mismatched = {
        key: (
            sorted(_PLACEHOLDER_RE.findall(english[key])),
            sorted(_PLACEHOLDER_RE.findall(text)),
        )
        for key, text in translated.items()
        if key in english
        and set(_PLACEHOLDER_RE.findall(english[key]))
        != set(_PLACEHOLDER_RE.findall(text))
    }

    assert not mismatched, (
        f"custom_components/ha_mcp_tools/translations/{locale}.json changes the "
        f"placeholder set of (key: english, translated) {mismatched}"
    )


def test_component_english_catalog_mirrors_strings_json() -> None:
    """``strings.json`` is the source; ``en.json`` is its shipped copy."""
    assert json.loads(COMPONENT_STRINGS.read_text("utf-8")) == json.loads(
        (COMPONENT_TRANSLATIONS / "en.json").read_text("utf-8")
    ), (
        "custom_components/ha_mcp_tools/strings.json and translations/en.json "
        "have drifted — every translated catalog is keyed against en.json"
    )
