"""Locale parity across the four translated surfaces.

A language reaches users through four independent catalogs: the web settings
UI, the custom component's config/options flow, and the two add-on flavors.
Nothing links them, so a locale can be added to one surface and silently miss
the rest — Simplified Chinese shipped for the settings UI in #1992 and left
Chinese users an English config flow and English add-on options, and the same
half-install was proposed again for French in #2038.

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

import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import yaml

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

GUARDED_SURFACES = {
    "src/ha_mcp/settings_ui/locales",
    "custom_components/ha_mcp_tools/translations",
    "homeassistant-addon/translations",
    "homeassistant-addon-dev/translations",
}

ENGLISH_ONLY_SURFACES = {
    "homeassistant-addon-webhook-proxy/translations": (
        "Webhook Proxy add-on: English-only by maintainer decision — the "
        "translation upkeep is not worth it for this add-on, and its stable "
        "flavor is promote-only anyway (see "
        "homeassistant-addon-webhook-proxy/AGENTS.md)"
    ),
    "homeassistant-addon-webhook-proxy-dev/translations": (
        "Webhook Proxy dev add-on: English-only, same decision as the stable "
        "flavor above"
    ),
    "homeassistant-addon-webhook-proxy/mcp_proxy/translations": (
        "Webhook Proxy's bundled integration: English-only, same decision"
    ),
    "homeassistant-addon-webhook-proxy-dev/mcp_proxy_dev/translations": (
        "Webhook Proxy dev's bundled integration: English-only, same decision"
    ),
}

VENDORED_SURFACES = {
    "tests/initial_test_state/custom_components/hacs/translations": (
        "third-party HACS integration, vendored to seed the e2e container — "
        "upstream's catalogs, not ours to translate"
    ),
}


def _discover_translation_surfaces() -> set[str]:
    """Every catalog directory git tracks.

    Deliberately not a filesystem walk: that reads whatever the working tree
    happens to contain, so a contributor whose virtualenv lives in ``venv/``
    rather than ``.venv/`` gets a red test naming a vendored package's
    ``locales/`` directory they were right to have. Tracked content is the
    thing this rule is actually about.
    """
    try:
        completed = subprocess.run(
            # The unit-test job runs in a container against a checkout owned by
            # another uid, and actions/checkout's safe.directory lands in a temp
            # HOME that container jobs don't see (same reason pr.yml sets it by
            # hand for the mirror job). Without this, git refuses the repo as
            # dubious ownership and the check dies with an empty stderr.
            ["git", "-c", "safe.directory=*", "ls-files", "-z"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        stderr = getattr(exc, "stderr", "") or ""
        raise AssertionError(
            f"cannot list tracked files to find translation surfaces: {exc} {stderr}"
        ) from exc
    tracked = completed.stdout.split("\0")
    return {
        str(parent)
        for path in tracked
        if path
        for parent in [PurePosixPath(path).parent]
        if parent.name in {"translations", "locales"}
    }


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


def _flatten(value: object, prefix: str = "") -> dict[str, str]:
    """Flatten a catalog to ``dotted.key -> text``, lists included.

    Every leaf is rendered as text so nothing can sit outside the baseline: a
    non-string leaf that no rule reaches is exactly the silent gap these tests
    exist to remove.
    """
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            flat.update(_flatten(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            flat.update(_flatten(item, f"{prefix}[{index}]"))
    elif isinstance(value, str):
        flat[prefix] = value
    else:
        flat[prefix] = json.dumps(value, sort_keys=True)
    return flat


def _component_catalog(locale: str) -> dict[str, str]:
    return _flatten(
        json.loads((COMPONENT_TRANSLATIONS / f"{locale}.json").read_text("utf-8"))
    )


def _translated_component_locales() -> list[str]:
    return sorted(
        path.stem for path in COMPONENT_TRANSLATIONS.glob("*.json") if path.stem != "en"
    )


BASELINE_PATH = Path(__file__).with_name("locale_source_baseline.json")


def english_sources() -> dict[str, dict[str, str]]:
    """Hash every English string a translation is written against, per surface.

    Imported by ``scripts/update_locale_baseline.py`` so the baseline and the
    check that reads it can never disagree about what is hashed.
    """
    sources = {
        "src/ha_mcp/settings_ui/locales": _flatten(
            json.loads((SETTINGS_LOCALES / "en.json").read_text("utf-8"))
        ),
        "custom_components/ha_mcp_tools/translations": _component_catalog("en"),
    }
    for addon_dir in ADDON_DIRS:
        sources[f"{addon_dir.name}/translations"] = _flatten(
            yaml.safe_load((addon_dir / "translations" / "en.yaml").read_text("utf-8"))
        )
    return {
        surface: {
            key: hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            for key, text in strings.items()
        }
        for surface, strings in sources.items()
    }


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
        "every language must ship on all four translated surfaces. Create: "
        + ", ".join(
            sorted(
                pattern.replace("<code>", code)
                for pattern, codes in gaps.items()
                for code in codes
            )
        )
    )


def test_every_translation_surface_is_guarded_or_english_only() -> None:
    """A new *surface* must not slip the net the way a new locale used to.

    The parity check above only knows the four directories it is told about,
    so adding ``homeassistant-addon-foo/translations/`` would otherwise go
    unnoticed exactly as zh-Hans did.
    """
    discovered = _discover_translation_surfaces()
    accounted = GUARDED_SURFACES | set(ENGLISH_ONLY_SURFACES) | set(VENDORED_SURFACES)

    unaccounted = sorted(discovered - accounted)
    assert not unaccounted, (
        f"new translation surface(s) {unaccounted}: add them to "
        "GUARDED_SURFACES and ship every locale there, or record them in "
        "ENGLISH_ONLY_SURFACES / VENDORED_SURFACES with the reason they are "
        "exempt"
    )

    vanished = sorted(accounted - discovered)
    assert not vanished, (
        f"{vanished} no longer exist(s) — drop them from GUARDED_SURFACES / "
        "ENGLISH_ONLY_SURFACES / VENDORED_SURFACES so the lists keep meaning "
        "something"
    )


def test_english_only_surfaces_have_not_gained_a_locale() -> None:
    """Catch a locale landing where nobody signed up to maintain it.

    These surfaces are English-only on purpose. A stray catalog here would
    otherwise rot unnoticed: none of the parity, placeholder, or drift checks
    look at them.
    """
    strays = {
        surface: sorted(
            path.name
            for path in (_REPO_ROOT / surface).iterdir()
            if path.suffix in {".json", ".yaml"} and path.stem != "en"
        )
        for surface in ENGLISH_ONLY_SURFACES
    }
    found = {surface: names for surface, names in strays.items() if names}

    assert not found, (
        f"non-English catalog(s) on an English-only surface: {found}. Either "
        "delete them, or move the surface into GUARDED_SURFACES and translate "
        "it everywhere — reasons: "
        + "; ".join(f"{surface}: {ENGLISH_ONLY_SURFACES[surface]}" for surface in found)
    )


def test_locale_codes_use_one_spelling_across_surfaces() -> None:
    """Only the settings UI is case-insensitive about the language code.

    ``_i18n.normalize_locale`` lowercases catalog stems, but Home Assistant
    loads ``translations/<code>.json`` and Supervisor keys add-on catalogs by
    the literal filename stem — so a ``zh-hans.yaml`` typo still renders in the
    settings UI while being invisible to both of those. Left to
    ``test_every_locale_ships_on_every_surface`` alone it also reads as two
    unrelated languages, each missing everywhere else, which points nowhere
    near the actual mistake.
    """
    spellings: dict[str, dict[str, list[str]]] = {}
    for pattern, codes in _surfaces().items():
        for code in codes:
            spellings.setdefault(code.lower(), {}).setdefault(code, []).append(pattern)

    conflicts = {
        lowered: {code: sorted(patterns) for code, patterns in variants.items()}
        for lowered, variants in spellings.items()
        if len(variants) > 1
    }

    assert not conflicts, (
        "a language must use one exact filename spelling on every surface — "
        f"Home Assistant and Supervisor match on the literal stem: {conflicts}"
    )


def test_component_translation_locales_are_discovered() -> None:
    """The two parametrized tests below collect nothing on an empty glob."""
    assert _translated_component_locales(), (
        "no translated catalogs found in "
        "custom_components/ha_mcp_tools/translations/ — the per-locale checks "
        "below would pass by collecting zero cases"
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
    """A dropped or renamed ``{placeholder}`` loses the value HA substitutes."""
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


def test_translations_are_checked_against_current_english() -> None:
    """Catch the drift key parity cannot see: same key, changed meaning.

    #1993 rewrote a policy string from ALL-match to ANY-match. The key stayed,
    the placeholder set stayed empty, and every existing check passed while the
    Chinese text told users the opposite of what the server enforces. The
    baseline pins the English each translation was written against, so moving
    an English string fails here until the locales are revisited.
    """
    baseline = json.loads(BASELINE_PATH.read_text("utf-8"))
    current = english_sources()

    changed: list[str] = []
    unrecorded: list[str] = []
    for surface, hashes in current.items():
        recorded = baseline.get(surface, {})
        changed += [
            f"{surface}: {key}"
            for key, digest in hashes.items()
            if key in recorded and recorded[key] != digest
        ]
        unrecorded += [f"{surface}: {key}" for key in hashes if key not in recorded]
    dropped = [
        f"{surface}: {key}"
        for surface, hashes in baseline.items()
        for key in hashes
        if key not in current.get(surface, {})
    ]

    detail = "; ".join(
        f"{label} {sorted(items)[:10]}{'...' if len(items) > 10 else ''}"
        for label, items in (
            ("english text changed for", changed),
            ("new english strings", unrecorded),
            ("english strings removed", dropped),
        )
        if items
    )
    assert not detail, (
        "the English source moved out from under the translations. Update "
        "every locale carrying the changed keys (or confirm the existing "
        "wording still reads correctly), then run "
        f"`python scripts/update_locale_baseline.py`. {detail}"
    )


def test_component_english_catalog_mirrors_strings_json() -> None:
    """``strings.json`` is the source; ``en.json`` is its shipped copy."""
    assert json.loads(COMPONENT_STRINGS.read_text("utf-8")) == json.loads(
        (COMPONENT_TRANSLATIONS / "en.json").read_text("utf-8")
    ), (
        "custom_components/ha_mcp_tools/strings.json and translations/en.json "
        "have drifted — every translated catalog is keyed against en.json"
    )
