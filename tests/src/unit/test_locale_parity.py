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
import sys
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from ha_mcp.settings_ui._tools_meta import primary_tag

_REPO_ROOT = Path(__file__).resolve().parents[3]

# The tool set comes from the same static AST parse that generates the docs,
# not from its committed output — see ``_renderable_groups_and_tools``.
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import extract_tools  # noqa: E402

SETTINGS_LOCALES = _REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales"
AGENTS_MD = _REPO_ROOT / "AGENTS.md"
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

    Known limit: ``git ls-files`` in the superproject does not descend into
    ``src/ha_mcp/resources/skills-vendor``, so a catalog added inside that
    submodule is invisible here. The exemption lists cannot cover it by
    convention either — nothing would surface it to be recorded.
    """
    try:
        completed = subprocess.run(
            # The unit-test job runs in a container against a checkout owned by
            # another uid, and actions/checkout's safe.directory lands in a temp
            # HOME that container jobs don't see — the Ruff Lint job's "Run ruff
            # format check on changed Python files" step re-adds it by hand for
            # the same reason. Without this, git refuses the repo as dubious
            # ownership and the check dies with an empty stderr.
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


def _settings_catalog(locale: str) -> dict[str, Any]:
    """The raw catalog: ``tools`` nests a dict per tool, the rest is flat."""
    catalog: dict[str, Any] = json.loads(
        (SETTINGS_LOCALES / f"{locale}.json").read_text("utf-8")
    )
    return catalog


def _non_english_settings_locales() -> list[str]:
    """Settings UI catalog codes except ``en``, which carries no overrides."""
    return sorted(
        path.stem for path in SETTINGS_LOCALES.glob("*.json") if path.stem != "en"
    )


@cache
def _renderable_groups_and_tools() -> tuple[frozenset[str], frozenset[str]]:
    """The group headings and tool names the settings UI can actually show.

    ``en.json`` leaves ``tool_groups``/``tools`` empty — English for those
    comes from the tool definitions at runtime — so the tool set is the only
    cross-check a translated catalog has. Group headings come from
    ``_tools_meta``'s own ``primary_tag`` (imported, not copied) so the rule
    and its consumers cannot drift apart.

    The tools are parsed out of their sources rather than read from the
    committed ``site/src/data/tools.json``: that file is regenerated only
    *after* merge, by ``sync-tool-docs.yml`` on a ``[skip ci]`` commit. Reading
    it would let the PR that adds a tool stay green and then turn every
    subsequent PR red across all five locales, landing the failure on whoever
    opens the next one. Parsing the sources puts it on the PR that owes the
    translations.
    """
    tools = extract_tools.extract_tools()
    assert tools, (
        "scripts/extract_tools.py parsed no tools — its TOOL_FILES list is "
        "hardcoded and silently skips a file that has moved. Without this "
        "guard a shrunken parse fails below as 'translates tool(s) that do "
        "not exist', which tells a translator to delete correct entries."
    )
    groups = frozenset(primary_tag(tool["tags"]) for tool in tools)
    names = frozenset(str(tool["name"]) for tool in tools)
    return groups, names


@cache
def _english_tool_texts() -> dict[str, str]:
    """English tool titles and descriptions, keyed like a flat catalog.

    The settings UI catalogs translate the title and the description's first
    line, so that is what a translated value is compared against.
    """
    texts: dict[str, str] = {}
    for tool in extract_tools.extract_tools():
        name = str(tool["name"])
        texts[f"{name}.title"] = str(tool.get("title") or "")
        texts[f"{name}.description"] = str(tool.get("description") or "").split("\n")[0]
    return texts


def test_settings_ui_locales_are_discovered() -> None:
    """The parametrized checks below collect nothing on an empty glob."""
    assert _non_english_settings_locales(), (
        "no translated catalogs found in src/ha_mcp/settings_ui/locales/ — the "
        "per-locale checks below would pass by collecting zero cases"
    )


@pytest.mark.parametrize("locale", _non_english_settings_locales())
def test_settings_catalog_keys_name_real_groups_and_tools(locale: str) -> None:
    """Both sections are keyed off the tool catalog, and nothing checked it.

    ``settings.js`` resolves a group heading by ``primary_tag`` and falls
    back to English on any key it does not find, so a stale or misspelled
    entry is invisible: ``build_payload`` happily ships it and nothing reads
    it. The reverse direction is the one that actually bites — a new tool, or
    a new tag that sorts first for an existing tool, leaves *every* locale
    showing English for it with no test going red.

    The tool set is parsed from the sources, so the PR that adds a tool is the
    one that goes red — see ``_renderable_groups_and_tools`` for why reading
    the committed ``tools.json`` instead would move that failure onto the next
    PR to open.
    """
    groups, tool_names = _renderable_groups_and_tools()
    catalog = _settings_catalog(locale)

    catalog_groups = set(catalog.get("tool_groups", {}))
    catalog_tools = set(catalog.get("tools", {}))

    assert not catalog_groups - groups, (
        f"src/ha_mcp/settings_ui/locales/{locale}.json translates tool_groups "
        f"key(s) no tool can render: {sorted(catalog_groups - groups)}. The "
        "settings UI only looks up a group by a tool's primary tag — delete "
        "them."
    )
    assert not groups - catalog_groups, (
        f"src/ha_mcp/settings_ui/locales/{locale}.json is missing tool_groups "
        f"key(s): {sorted(groups - catalog_groups)}. Those headings render in "
        "English for this language."
    )
    assert not catalog_tools - tool_names, (
        f"src/ha_mcp/settings_ui/locales/{locale}.json translates tool(s) that "
        f"do not exist: {sorted(catalog_tools - tool_names)}"
    )
    assert not tool_names - catalog_tools, (
        f"src/ha_mcp/settings_ui/locales/{locale}.json is missing tool(s): "
        f"{sorted(tool_names - catalog_tools)}. Their title and description "
        "render in English for this language."
    )


# A catalog wholesale-copied from English passes key parity, placeholder
# parity and the markup allowlist — every existing check. All four surfaces
# get a ceiling: leaving one of them out accepts a wholesale-English catalog
# there.
#
# The ceilings differ because what legitimately repeats differs. The settings
# UI messages sit far under theirs: the highest among the shipped locales is 9
# of 419 (2.1%), all words that genuinely read the same in that language. The
# component catalogs are short and carry the product names as keys of their
# own, so ``de``'s 7 of 93 (7.5%) — six product names plus ``Update`` — is
# correct and the ceiling has to clear it. Both add-on flavors translate
# everything today (0%).
_MAX_ENGLISH_IDENTICAL_SHARE = 0.05
_MAX_COMPONENT_IDENTICAL_SHARE = 0.15


def _untranslated_keys(
    english: dict[str, str], translated: dict[str, str]
) -> list[str]:
    """The English keys ``translated`` does not translate.

    A missing key counts as untranslated: English is the per-key fallback, so
    an omitted key renders exactly like a copied one. Counting only the keys a
    catalog carries let ``messages: {}`` score 0% and a 20-key all-English stub
    score 4.8% — both under any sane ceiling, and since omission is legal
    everywhere else, nothing else caught them.
    """
    return sorted(
        key for key, text in english.items() if translated.get(key, text) == text
    )


def _assert_not_a_copy(
    label: str, english: dict[str, str], translated: dict[str, str], ceiling: float
) -> None:
    untranslated = _untranslated_keys(english, translated)
    share = len(untranslated) / len(english)

    assert share <= ceiling, (
        f"{label} leaves {len(untranslated)} of {len(english)} strings "
        f"({share:.1%}) untranslated — byte-identical to English or missing "
        f"outright — over the {ceiling:.0%} ceiling. This reads as a partly "
        f"untranslated catalog. Untranslated keys: {untranslated[:20]}"
    )


@pytest.mark.parametrize("locale", _non_english_settings_locales())
def test_settings_catalog_is_not_a_copy_of_english(locale: str) -> None:
    """A stub catalog has to fail somewhere, and this is the only place.

    Deliberately identical strings are normal — ``Total``, ``{count} min``
    and product names read the same in several languages — so this is a
    share, not a ban.
    """
    _assert_not_a_copy(
        f"src/ha_mcp/settings_ui/locales/{locale}.json (messages)",
        _settings_catalog("en")["messages"],
        _settings_catalog(locale)["messages"],
        _MAX_ENGLISH_IDENTICAL_SHARE,
    )


@pytest.mark.parametrize("locale", _non_english_settings_locales())
def test_settings_catalog_tools_are_translated(locale: str) -> None:
    """Exactness says every tool is present, not that any was translated.

    87 titles and 87 descriptions is the largest translated surface in the
    repo, and nothing looked at the values. The exactness rule above also
    obliges every tool-adding PR to touch five languages before it can go
    green, which is pressure toward pasting the English in — this is what
    notices. Every shipped locale translates all 174 today.
    """
    catalog = _settings_catalog(locale)["tools"]
    translated = {
        f"{name}.{field}": text
        for name, entry in catalog.items()
        for field, text in entry.items()
        if field in {"title", "description"}
    }

    _assert_not_a_copy(
        f"src/ha_mcp/settings_ui/locales/{locale}.json (tools)",
        _english_tool_texts(),
        translated,
        _MAX_ENGLISH_IDENTICAL_SHARE,
    )


@pytest.mark.parametrize("locale", _translated_component_locales())
def test_component_catalog_is_not_a_copy_of_english(locale: str) -> None:
    """Key parity says every key is present, not that any was translated."""
    _assert_not_a_copy(
        f"custom_components/ha_mcp_tools/translations/{locale}.json",
        _component_catalog("en"),
        _component_catalog(locale),
        _MAX_COMPONENT_IDENTICAL_SHARE,
    )


def _addon_catalog(addon_dir: Path, locale: str) -> dict[str, str]:
    return _flatten(
        yaml.safe_load(
            (addon_dir / "translations" / f"{locale}.yaml").read_text("utf-8")
        )
    )


def _addon_locale_cases() -> list[tuple[Path, str]]:
    """Every (add-on flavor, locale) pair that ships a catalog."""
    return [
        (addon_dir, path.stem)
        for addon_dir in ADDON_DIRS
        for path in sorted((addon_dir / "translations").glob("*.yaml"))
        if path.stem != "en"
    ]


def test_addon_locale_cases_are_discovered() -> None:
    """The parametrized check below collects nothing on an empty glob."""
    assert _addon_locale_cases(), (
        "no translated add-on catalogs found — the per-flavor check below "
        "would pass by collecting zero cases"
    )


@pytest.mark.parametrize(
    ("addon_dir", "locale"),
    _addon_locale_cases(),
    ids=lambda param: param.name if isinstance(param, Path) else param,
)
def test_addon_catalog_is_not_a_copy_of_english(addon_dir: Path, locale: str) -> None:
    """The two flavors carry different schemas, so each needs its own check."""
    _assert_not_a_copy(
        f"{addon_dir.name}/translations/{locale}.yaml",
        _addon_catalog(addon_dir, "en"),
        _addon_catalog(addon_dir, locale),
        _MAX_ENGLISH_IDENTICAL_SHARE,
    )


def test_agents_md_lists_every_shipped_locale() -> None:
    """The documented list went stale the moment ``fr`` landed.

    It is the one place a contributor looks up which languages exist, and
    nothing tied it to the catalogs it describes.
    """
    marker = "names every file:"
    lines = [
        line for line in AGENTS_MD.read_text("utf-8").splitlines() if marker in line
    ]
    assert len(lines) == 1, (
        f"expected exactly one AGENTS.md line containing {marker!r} to carry "
        f"the locale list, found {len(lines)} — update this test alongside the "
        "section it guards"
    )

    documented = set(re.findall(r"`([A-Za-z-]+)`", lines[0]))
    shipped = set(_non_english_settings_locales())

    assert documented == shipped, (
        "AGENTS.md § Translations lists "
        f"{sorted(documented)} but the shipped catalogs are {sorted(shipped)}. "
        "Update the line so the documented set matches what ships."
    )
