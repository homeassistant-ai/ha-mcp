"""Locale parity across the translated surfaces.

A language reaches users through four catalogs: the web settings UI, the
custom component's config/options flow, and the two add-on flavors. Two of
them are authored directly — the settings UI catalogs
(``src/ha_mcp/settings_ui/locales/<code>.json``, the canonical store) and the
component catalogs — while both add-on flavors' ``translations/*.yaml`` and
the ``FEATURE_META`` block in ``settings.js`` are *generated* from the
canonical store by ``scripts/generate_locales.py``
(``test_derived_catalogs_match_the_canonical_store``). Cross-surface wording
identity therefore holds by construction; what these tests police is the
authored content.

A locale still ships everywhere or not at all: add a catalog to one surface
without the others and CI goes red naming the files that are missing —
Simplified Chinese shipped settings-UI-only in #1992 and the same
half-install was proposed again for French in #2038.

The per-surface content rules are enforced elsewhere — ``_i18n.load_catalogs``
validates settings UI placeholders and inline markup, and
``tests/addon/test_addon_structure.py`` requires a ``name``/``description``
for every add-on ``schema:`` key. The component catalogs had no such guard,
so their key and placeholder parity is checked here.

``scripts/translate_locales.py`` is the pipeline that keeps the authored
catalogs green: it machine-translates the keys the baseline check below
reports as changed or missing, regenerates the derived catalogs, and repins
the baseline. It runs AFTER merge, on a daily schedule
(``.github/workflows/locale-sync.yml``), so any PR — fork or same-repo —
merges without owing translations; the checks that police the translated
content are gated behind ``LOCALE_COMPLETENESS_CHECKS`` (see the marker
below) and run in that workflow, not in PR CI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from ha_mcp.settings_ui._tools_meta import FEATURE_GATED_TOOLS, primary_tag

_REPO_ROOT = Path(__file__).resolve().parents[3]

# The tool set comes from the same static AST parse that generates the docs,
# not from its committed output — see ``_renderable_groups_and_tools``.
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import extract_tools  # noqa: E402
import generate_locales  # noqa: E402

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

# Completeness of the TRANSLATED catalogs — missing or orphaned keys, English
# moving out from under a translation, cross-surface shared wording,
# untranslated-share ceilings — is owed by the post-merge locale-sync
# workflow, not by the PR that edits English: any PR merges untranslated and
# the daily sync lands the translations afterwards, so between a merge and
# the next sync run these checks fail on master by design. They run only
# under LOCALE_COMPLETENESS_CHECKS=1, which locale-sync.yml sets after
# running scripts/translate_locales.py — there a failure means a human is
# owed (the engine pasted English back, a hand edit broke parity, or a gated
# stub awaits review). Everything NOT gated binds the PR author to
# deterministic, engine-free work: English-side pins, generated-file
# byte-exactness (run scripts/generate_locales.py), the structural surface
# rules — and placeholder parity on keys whose English is current, the one
# translated-content rule a hand edit can break in a way the sync cannot
# repair. test_locale_sync_gate_shape.py pins the env-var wiring.
completeness = pytest.mark.skipif(
    not os.environ.get("LOCALE_COMPLETENESS_CHECKS"),
    reason=(
        "translated-catalog completeness is verified by the post-merge "
        "locale-sync workflow — set LOCALE_COMPLETENESS_CHECKS=1 to run"
    ),
)

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

# The tool titles and descriptions have no catalog of their own on the
# English side: they are parsed from the tool definitions, so the baseline
# files them under a surface name rather than a path.
TOOL_SOURCES_SURFACE = "settings UI tool titles and descriptions"


def _catalogs_by_surface(locale: str) -> dict[str, dict[str, str]]:
    """One locale's *authored* catalogs, flattened, keyed by directory.

    The add-on YAMLs are deliberately absent: they are generated projections
    of the settings UI catalogs (``scripts/generate_locales.py``), so hashing
    them into the baseline would double-report every canonical edit.
    """
    return {
        "src/ha_mcp/settings_ui/locales": _flatten(
            json.loads((SETTINGS_LOCALES / f"{locale}.json").read_text("utf-8"))
        ),
        "custom_components/ha_mcp_tools/translations": _component_catalog(locale),
    }


def _summary_paragraph(tool: dict[str, Any]) -> str:
    """A tool description's first paragraph, joined onto one line.

    What a translator reads as the sentence, as opposed to the shorter text
    the row displays — see ``_english_tool_sources`` for why the two differ.
    """
    return " ".join(str(tool.get("description") or "").split("\n\n")[0].split())


def _english_tool_sources() -> dict[str, str]:
    """The English tool texts a settings UI catalog translates.

    ``en.json`` leaves ``tools`` empty — English for those comes from the tool
    definitions at runtime — so these 174 strings live in no catalog and the
    baseline did not cover them. An edit to a docstring therefore left every
    locale describing the old behaviour with nothing going red:
    ``ha_dev_manage_settings`` gained the Tools/Policies/Backups surfaces and
    four locales went on saying "directly".

    ``tool_groups`` is the sibling case and stays uncovered on purpose: a group
    key *is* its own English text, so a renamed heading cannot move out from
    under a translation without ``test_settings_catalog_keys_name_real_groups
    _and_tools`` naming it.

    A feature-gated tool has two English renderings and a setting decides which
    one the UI shows, so where the stub and the parsed text differ, both are
    pinned.

    What gets hashed is the summary *paragraph*, while the row displays its
    first physical line cut at 120 characters — the same text for 84 of the 87
    tools. ``ha_config_set_helper`` wraps its summary and the Chinese catalog
    translates the half that wraps off, so hashing the displayed text would
    leave "(28 types, unified interface)" free to move while a shipped
    translation states it. The copy checks stay on the displayed text, because
    a paste is of what was on screen.
    """
    rendered = _english_tool_texts()
    parsed = _english_tool_texts(as_rendered=False)
    summaries = {
        f"{str(tool['name'])}.description": _summary_paragraph(tool)
        for tool in extract_tools.extract_tools()
    }
    sources = dict(rendered)
    for key, text in parsed.items():
        # ``summaries`` is keyed by ``.description`` alone, so every title takes
        # the default arm and is pinned as its own parsed text. The default is
        # load-bearing: tightening it to ``summaries[key]`` drops every title
        # key out of the baseline.
        pinned = summaries.get(key, text)
        # A key whose rendered text differs is showing a stub. "(parsed)" is the
        # other rendering, whatever produced it — a docstring for a description,
        # the ``title=`` kwarg for ``ha_config_set_yaml.title``.
        if text != rendered.get(key):
            sources[f"{key} (parsed)"] = pinned
        else:
            sources[key] = pinned
    return sources


def english_sources() -> dict[str, dict[str, str]]:
    """Hash every English string a translation is written against, per surface.

    Imported by ``scripts/update_locale_baseline.py`` so the baseline and the
    check that reads it can never disagree about what is hashed.
    """
    sources = _catalogs_by_surface("en")
    sources[TOOL_SOURCES_SURFACE] = _english_tool_sources()
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


@completeness
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


def _pending_component_keys() -> frozenset[str]:
    """Component keys whose English moved since the baseline was pinned."""
    return _pending_keys("custom_components/ha_mcp_tools/translations")


@pytest.mark.parametrize("locale", _translated_component_locales())
def test_component_catalog_keeps_english_placeholders(locale: str) -> None:
    """A dropped or renamed ``{placeholder}`` loses the value HA substitutes.

    Deliberately NOT gated behind ``completeness``: this is the guard on
    hand edits, so it must run in the PR that makes them. Keys whose English
    moved since the baseline are excluded — their translations are owed a
    machine rewrite that restores the placeholders, and until the sync runs
    the old translation legitimately carries the old set.
    """
    english = _component_catalog("en")
    translated = _component_catalog(locale)
    pending = _pending_component_keys()

    mismatched = {
        key: (
            sorted(_PLACEHOLDER_RE.findall(english[key])),
            sorted(_PLACEHOLDER_RE.findall(text)),
        )
        for key, text in translated.items()
        if key in english
        and key not in pending
        and set(_PLACEHOLDER_RE.findall(english[key]))
        != set(_PLACEHOLDER_RE.findall(text))
    }

    assert not mismatched, (
        f"custom_components/ha_mcp_tools/translations/{locale}.json changes the "
        f"placeholder set of (key: english, translated) {mismatched}"
    )


@completeness
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


def test_the_baseline_hashes_more_than_the_first_line() -> None:
    """Hashing the paragraph only covers a wrapped clause while one wraps.

    ``_english_tool_sources`` pins the summary paragraph rather than the line
    the row is built from, so a clause wrapping off that line cannot move
    unseen. If ``extract_tools()`` ever truncated ``description`` to its first
    line, exactly one key would move — reading like an ordinary wording edit —
    and the wrapped clause would drop out of coverage with nothing saying so.

    Deliberately measured against the *physical* first line rather than the
    displayed one: the 120-character cut makes two more descriptions differ on
    its own, which would hold this green with the wrap gone.
    """
    wrapping = sorted(
        str(tool["name"])
        for tool in extract_tools.extract_tools()
        if _summary_paragraph(tool) != str(tool.get("description") or "").split("\n")[0]
    )

    assert wrapping, (
        "no tool's summary paragraph outruns its first physical line, so "
        "hashing the paragraph discriminates nothing — either every summary "
        "now fits one line, or extract_tools() began truncating description "
        "to its first line"
    )


def test_no_gated_stub_pins_a_paragraph_the_ui_hides() -> None:
    """A stub equal to its parsed text takes the ``else`` arm in the pin.

    That arm pins the summary paragraph under the plain key, while a reader
    with the feature flag off sees the stub — so a gated tool whose summary
    wraps would have text pinned that no reader of that tool can see. None
    wraps today; this fails when one starts to, which is when the arm needs
    splitting.
    """
    rendered = _english_tool_texts()
    parsed = _english_tool_texts(as_rendered=False)
    hidden = []
    for tool in extract_tools.extract_tools():
        name = str(tool["name"])
        key = f"{name}.description"
        if name not in FEATURE_GATED_TOOLS or rendered.get(key) != parsed.get(key):
            continue
        # Against the physical first line, as in the sibling guard: whether the
        # summary wraps is a property of the docstring, not of the display cut.
        if (
            _summary_paragraph(tool)
            != str(tool.get("description") or "").split("\n")[0]
        ):
            hidden.append(name)

    assert not hidden, (
        f"{sorted(hidden)} show a stub whose text equals their parsed first "
        "line while their summary wraps, so _english_tool_sources pins the "
        "paragraph under the plain key and the stub stops being the pinned "
        "text. Split the else arm before this ships."
    )


def test_component_english_catalog_mirrors_strings_json() -> None:
    """``strings.json`` is the source; ``en.json`` is its shipped copy."""
    assert json.loads(COMPONENT_STRINGS.read_text("utf-8")) == json.loads(
        (COMPONENT_TRANSLATIONS / "en.json").read_text("utf-8")
    ), (
        "custom_components/ha_mcp_tools/strings.json and translations/en.json "
        "have drifted — every translated catalog is keyed against en.json"
    )


def test_derived_catalogs_match_the_canonical_store() -> None:
    """The committed add-on YAMLs and ``FEATURE_META`` are generator output.

    Cross-surface wording identity used to be policed after the fact, by
    byte-identity tests over a hand-maintained pin of every shared (surface,
    key) place. Generation makes it hold by construction: both add-on
    flavors' ``translations/*.yaml`` and the ``FEATURE_META`` block in
    ``settings.js`` are projections of the settings UI catalogs, so the one
    check left is that the committed files are exactly what the generator
    emits — same guarantee, no pin to maintain.
    """
    stale = [
        str(path.relative_to(_REPO_ROOT))
        for path, content in generate_locales.generated_files().items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    ]
    assert not stale, (
        f"derived locale catalogs are out of sync with the canonical store: "
        f"{stale}. Run: python scripts/generate_locales.py"
    )


# The strings shipped from BOTH authored surfaces, by (surface, key).
# Generation holds cross-surface identity for the derived catalogs, but the
# component catalog is authored separately from the settings store, so a
# string shared between those two can still drift: either side can be edited
# alone. Membership is pinned because the grouping is by English text — a key
# whose English moves on one side only silently *leaves* the check, which is
# exactly the drift it exists to catch.
AUTHORED_SHARED_PLACES = {
    "src/ha_mcp/settings_ui/locales": (
        "messages.advanced.extra_yaml_write_keys.label",
    ),
    "custom_components/ha_mcp_tools/translations": (
        "options.step.tools_info.data.extra_yaml_keys",
    ),
}


@cache
def _authored_shared_groups() -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """English strings that appear byte-identical on both authored surfaces."""
    places: dict[str, list[tuple[str, str]]] = {}
    for surface, strings in _catalogs_by_surface("en").items():
        for key, text in strings.items():
            places.setdefault(text, []).append((surface, key))
    return tuple(
        (text, tuple(where))
        for text, where in places.items()
        if len({surface for surface, _ in where}) > 1
    )


def test_authored_shared_strings_are_discovered() -> None:
    """The check below only covers what the grouping still finds."""
    found: dict[str, set[str]] = {}
    for _, where in _authored_shared_groups():
        for surface, key in where:
            found.setdefault(surface, set()).add(key)

    assert {surface: sorted(keys) for surface, keys in found.items()} == {
        surface: sorted(keys) for surface, keys in AUTHORED_SHARED_PLACES.items()
    }, (
        "the strings shared between the settings store and the component "
        "catalog are no longer the pinned ones. A key that left usually did "
        "so because its English moved on one side only — fix the wording, not "
        "the pin. A key that arrived is newly covered. Update "
        "AUTHORED_SHARED_PLACES once the difference is the intended one."
    )


@completeness
@pytest.mark.parametrize("locale", _translated_component_locales())
def test_authored_shared_strings_read_the_same(locale: str) -> None:
    """One option described on both authored surfaces reads as one wording."""
    catalogs = _catalogs_by_surface(locale)
    divergent = []
    for english, where in _authored_shared_groups():
        rendered: dict[str, list[str]] = {}
        for surface, key in where:
            value = catalogs[surface].get(key)
            label = f"{surface}/{locale}: {key}"
            if value is None:
                # Settings messages may omit a key; English is the fallback,
                # so the screen still disagrees with the translated sibling.
                value, label = english, f"{label} (missing, renders English)"
            rendered.setdefault(value, []).append(label)
        if len(rendered) > 1:
            divergent.append(
                " vs ".join(", ".join(labels) for labels in rendered.values())
            )

    assert not divergent, (
        f"{locale} renders {len(divergent)} English string(s) differently "
        "depending on the surface — the English is byte-identical, so pick "
        "one wording for both:\n" + "\n".join(divergent)
    )


def test_connect_local_lan_quotes_the_bind_host_option() -> None:
    """The sentence names an on-screen option, so it has to name that option.

    ``connect_local_lan`` tells the reader which dropdown entry produces this
    URL, and every catalog quotes the label untranslated for that reason —
    the reader matches it against the form. Renaming the option in
    ``config_flow.py`` would leave seven catalogs quoting a label that no longer
    exists, silently, which is the drift class the ceilings test closed for
    percentages and this one closes for a literal.
    """
    source = (COMPONENT_STRINGS.parent / "config_flow.py").read_text("utf-8")
    match = re.search(r'value=BIND_HOST_ALL,\s*label="([^"]+)"', source)
    assert match, (
        "could not find the BIND_HOST_ALL option label in config_flow.py — "
        "the selector was restructured, so update this test with it"
    )
    label = match.group(1)

    quoted = "Local network"
    assert label.startswith(quoted), (
        f"the bind-host dropdown now reads {label!r}, which no longer starts "
        f"with the {quoted!r} the catalogs quote — rename it in every "
        "common.connect_local_lan string, then here"
    )

    checked = 0
    for locale in ["en", *_translated_component_locales()]:
        catalog = _component_catalog(locale)
        if locale != "en" and "common.connect_local_lan" not in catalog:
            # A catalog the post-merge sync has not filled yet (a new
            # language mid-fill) legitimately lacks the key — completeness
            # is the gated checks' business, the literal is this one's.
            continue
        checked += 1
        value = catalog["common.connect_local_lan"]
        assert quoted in value, (
            f"custom_components/ha_mcp_tools/translations/{locale}.json "
            f"common.connect_local_lan is {value!r}, which does not quote the "
            f"{quoted!r} option the reader has to find on the form"
        )
    # The mid-fill tolerance must not let the check degrade to English-only:
    # if every translated catalog lost the key, that is drift, not a stub.
    assert checked > 1, (
        "no translated catalog carries common.connect_local_lan at all — the "
        "check above covered only English, which is the 'guard that silently "
        "stopped running' shape tests/pytest.ini exists to prevent"
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
    committed ``site/src/data/tools.json``: that file is generator output a
    separate post-merge workflow (``sync-tool-docs.yml``) keeps current, and
    this check must judge the tree it runs on, not trust an artifact with
    its own update schedule.
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


# ``settings.js`` renders a tool row's description as
# ``(t.description || '').split('\n')[0].slice(0, 120)``. Both cuts model the
# same thing — the text a translator can actually see and paste.
_DISPLAYED_DESCRIPTION_CHARS = 120


@cache
def _english_tool_texts(*, as_rendered: bool = True) -> dict[str, str]:
    """English tool titles and descriptions, keyed like a flat catalog.

    What a translator has on screen is what a paste is of, so that is what a
    translated value is compared against: ``settings.js`` cuts a description
    at the first newline and then at 120 characters, and both halves of that
    cut belong here. Leaving the second one out let a paste of the two
    descriptions that outrun 120 characters — ``ha_get_state`` at 130 and
    ``ha_search`` at 171 — byte-differ from the text they were pasted from,
    and pass.

    A feature-gated tool has two English renderings, and which one a
    translator is looking at depends on a setting. With its flag off — the
    default — the tool never registers, so the UI shows the hand-written
    ``FEATURE_GATED_TOOLS`` stub; with the flag on it shows the parsed
    docstring. Five of the seven differ (``ha_config_set_yaml`` reads "Set
    YAML Config" as a stub and "Raw YAML Config Edit" parsed), so checking
    only one of them lets a paste of the other through. ``as_rendered``
    selects which set this call returns; the paste check consults both.
    """
    texts: dict[str, str] = {}
    # Same discovery guard as the group/name helper: without it a shrunken
    # parse reaches the share check as a ZeroDivisionError.
    _renderable_groups_and_tools()
    for tool in extract_tools.extract_tools():
        name = str(tool["name"])
        stub = FEATURE_GATED_TOOLS.get(name) if as_rendered else None
        if stub is not None:
            texts[f"{name}.title"] = stub["title"]
            # A stub reaches the row through the same field as a docstring
            # (``_render_stub`` feeds ``description``), so it takes the same cut.
            texts[f"{name}.description"] = stub["description"][
                :_DISPLAYED_DESCRIPTION_CHARS
            ]
            continue
        texts[f"{name}.title"] = str(tool.get("title") or "")
        first_line = str(tool.get("description") or "").split("\n")[0]
        texts[f"{name}.description"] = first_line[:_DISPLAYED_DESCRIPTION_CHARS]
    return texts


def test_settings_ui_locales_are_discovered() -> None:
    """The parametrized checks below collect nothing on an empty glob."""
    assert _non_english_settings_locales(), (
        "no translated catalogs found in src/ha_mcp/settings_ui/locales/ — the "
        "per-locale checks below would pass by collecting zero cases"
    )


@completeness
@pytest.mark.parametrize("locale", _non_english_settings_locales())
def test_settings_catalog_keys_name_real_groups_and_tools(locale: str) -> None:
    """Both sections are keyed off the tool catalog, and nothing checked it.

    ``settings.js`` resolves a group heading by ``primary_tag`` and falls
    back to English on any key it does not find, so a stale or misspelled
    entry is invisible: ``build_payload`` happily ships it and nothing reads
    it. The reverse direction is the one that actually bites — a new tool, or
    a new tag that sorts first for an existing tool, leaves *every* locale
    showing English for it with no test going red.

    The tool set is parsed from the sources — see
    ``_renderable_groups_and_tools`` for why the committed ``tools.json``
    would be the wrong reference. A tool the codebase gained is missing here
    until the post-merge sync fills it, which is why this check is gated to
    that workflow.
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


@completeness
@pytest.mark.parametrize("locale", _non_english_settings_locales())
def test_settings_messages_carry_no_key_english_dropped(locale: str) -> None:
    """The one direction nothing else here looks in.

    ``messages`` may omit keys — English is the per-key fallback, and AGENTS.md
    states the allowance — so this asserts the other direction only. A key with
    no English counterpart is not a fallback, it is text that reaches nobody:
    ``build_payload`` ships it and ``t()`` never asks for it.

    #2043 removed ``advanced.entity_search_limit.label`` and ``.help`` from
    ``en`` and from the five catalogs it knew about, and two got past it by
    different routes — ``es`` was not one of the five, and ``it`` was written
    against the older English source and rebased past the removal. Neither
    failed anything: the ceilings count English keys a locale is missing, and
    the baseline hashes English sources only, so a key English does not have is
    outside both. The sibling sections have had this covered all along, by
    ``test_settings_catalog_keys_name_real_groups_and_tools``; the component
    and add-on surfaces by their own key checks.
    """
    english = set(_settings_catalog("en")["messages"])
    orphaned = sorted(set(_settings_catalog(locale)["messages"]) - english)

    assert not orphaned, (
        f"src/ha_mcp/settings_ui/locales/{locale}.json translates message "
        f"key(s) en.json does not have: {orphaned}. Nothing renders them — "
        "delete them, or restore the English key if it went missing by "
        "mistake."
    )


# A catalog wholesale-copied from English passes key parity, placeholder
# parity and the markup allowlist — every existing check. Both authored
# surfaces get a ceiling, and so does each generated add-on projection
# (test_generated_addon_projections_are_translated): the ~21 addon-only
# strings are under 5% of the whole settings catalog, so without their own
# per-flavor ceiling an all-English Supervisor page would ride under the
# catalog-wide share.
#
# The ceilings differ because what legitimately repeats differs. The settings
# UI messages sit far under theirs: the highest among the shipped locales is
# a few words of 440+ that genuinely read the same in that language. The
# component catalogs are short and carry the product names as keys of their
# own, so ``de``'s 7 of 93 (7.5%) — six product names plus ``Update`` — is
# correct and the ceiling has to clear it.
_MAX_ENGLISH_IDENTICAL_SHARE = 0.05
_MAX_COMPONENT_IDENTICAL_SHARE = 0.15


def _untranslated_keys(
    english: dict[str, str],
    translated: dict[str, str],
    alternate: dict[str, str] | None = None,
) -> list[str]:
    """The English keys ``translated`` does not translate.

    A missing key counts as untranslated: English is the per-key fallback, so
    an omitted key renders exactly like a copied one. Counting only the keys a
    catalog carries let ``messages: {}`` score 0% and a 20-key all-English stub
    score 4.8% — both under any sane ceiling, and since omission is legal
    everywhere else, nothing else caught them.

    ``alternate`` is a second English rendering of the same keys, for a surface
    that has one. A paste of either rendering is English on screen, so the same
    argument that makes the paste check consult both applies key by key here.
    """
    alternates = alternate or {}
    return sorted(
        key
        for key, text in english.items()
        if (value := translated.get(key, text)) == text or value == alternates.get(key)
    )


def _assert_not_a_copy(
    label: str,
    english: dict[str, str],
    translated: dict[str, str],
    ceiling: float,
    alternate: dict[str, str] | None = None,
) -> None:
    untranslated = _untranslated_keys(english, translated, alternate)
    share = len(untranslated) / len(english)

    assert share <= ceiling, (
        f"{label} leaves {len(untranslated)} of {len(english)} strings "
        f"({share:.1%}) untranslated — byte-identical to English or missing "
        f"outright — over the {ceiling:.0%} ceiling. This reads as a partly "
        f"untranslated catalog. Untranslated keys: {untranslated[:20]}"
    )


@completeness
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


@completeness
@pytest.mark.parametrize("locale", _non_english_settings_locales())
def test_generated_addon_projections_are_translated(locale: str) -> None:
    """The add-on subset needs its own ceiling, per flavor.

    All ~21 ``addon.*`` strings left untranslated are under 5% of the whole
    settings catalog — invisible to the catalog-wide share above — yet they
    project an essentially English Supervisor configuration page. This is
    the deleted per-flavor anti-copy check reborn, computed from the
    canonical store the projections are generated from rather than from the
    committed YAML (which the derived-files test already pins byte-exact).
    """
    catalogs = generate_locales.load_catalogs()
    english = catalogs["en"]
    messages = catalogs.get(locale, {})
    for flavor, addon_dir in generate_locales.ADDON_FLAVORS.items():
        texts = {
            (key, field): generate_locales.resolve_text(
                messages, english, flavor, key, field
            )
            for key in generate_locales.schema_keys(addon_dir)
            for field in ("name", "description")
        }
        untranslated = sorted(
            f"{key}.{field}"
            for (key, field), text in texts.items()
            if text
            == generate_locales.resolve_text(english, english, flavor, key, field)
        )
        share = len(untranslated) / len(texts)
        assert share <= _MAX_ENGLISH_IDENTICAL_SHARE, (
            f"the {flavor} add-on projection for {locale} leaves "
            f"{len(untranslated)} of {len(texts)} option strings "
            f"({share:.1%}) byte-identical to English — over the "
            f"{_MAX_ENGLISH_IDENTICAL_SHARE:.0%} ceiling. Untranslated: "
            f"{untranslated[:10]}"
        )


@completeness
@pytest.mark.parametrize("locale", _non_english_settings_locales())
def test_settings_catalog_tools_are_translated(locale: str) -> None:
    """Exactness says every tool is present, not that any was translated.

    87 titles and 87 descriptions is the largest translated surface in the
    repo, and nothing looked at the values. The exactness rule above also
    obliges every tool-adding PR to touch six languages before it can go
    green, which is pressure toward pasting the English in — this is what
    notices. Every shipped locale translates all 174 today.
    """
    english = _english_tool_texts()
    catalog = _settings_catalog(locale).get("tools", {})
    translated = {
        f"{name}.{field}": text
        for name, entry in catalog.items()
        for field, text in entry.items()
        if field in {"title", "description"}
    }

    # The share alone does not cover the case this check exists for: adding one
    # tool and pasting its English title and description into all six locales
    # scores 2 of 176 and passes. One wholly-English tool is the signature, and
    # naming it beats a percentage. A single matching title stays legal — some
    # tool names genuinely read the same in another language.
    #
    # Both English renderings count: a feature-gated tool shows the stub text
    # with its flag off and the parsed docstring with it on, and a translator
    # pastes whichever one their instance put on screen.
    as_parsed = _english_tool_texts(as_rendered=False)
    assert english != as_parsed, (
        "the two English renderings are identical, so consulting both below "
        "discriminates nothing — either FEATURE_GATED_TOOLS no longer overrides "
        "any title or description, or the stub lookup in _english_tool_texts "
        "stopped taking effect"
    )

    english_sources = (english, as_parsed)
    pasted = sorted(
        name
        for name, entry in catalog.items()
        if f"{name}.title" in english
        and any(
            entry.get("title") == source[f"{name}.title"]
            and entry.get("description") == source[f"{name}.description"]
            for source in english_sources
        )
    )
    assert not pasted, (
        f"src/ha_mcp/settings_ui/locales/{locale}.json carries {len(pasted)} "
        f"tool(s) whose title and description are both still English: "
        f"{pasted[:20]}. Adding a tool obliges every locale, and an "
        "untranslated paste is what that pressure produces."
    )

    _assert_not_a_copy(
        f"src/ha_mcp/settings_ui/locales/{locale}.json (tools)",
        english,
        translated,
        _MAX_ENGLISH_IDENTICAL_SHARE,
        alternate=as_parsed,
    )


def test_the_tools_share_counts_both_english_renderings() -> None:
    """No shipped locale pastes either English, so real data cannot show this.

    A description pasted from the *parsed* English of a feature-gated tool is
    English on screen but byte-differs from the rendered text, so counting only
    the rendered one reads it as translated. A handful of keys (the
    feature-gated stubs) differ between the renderings — a small share,
    invisible under the ceiling on their own and enough to hide a genuinely
    untranslated remainder underneath it.
    """
    english = _english_tool_texts()
    as_parsed = _english_tool_texts(as_rendered=False)
    differing = sorted(key for key in english if english[key] != as_parsed[key])
    assert differing, "the two renderings no longer differ — see the tools check"

    translated = {key: f"traducido {text}" for key, text in english.items()}
    translated.update({key: as_parsed[key] for key in differing})

    # Nothing here is byte-identical to the rendered English, so the rendered
    # set alone finds no untranslated key at all — a zero ceiling holds.
    _assert_not_a_copy("rendered English only", english, translated, 0.0)

    with pytest.raises(AssertionError, match="untranslated"):
        _assert_not_a_copy(
            "both renderings", english, translated, 0.0, alternate=as_parsed
        )


@completeness
@pytest.mark.parametrize("locale", _translated_component_locales())
def test_component_catalog_is_not_a_copy_of_english(locale: str) -> None:
    """Key parity says every key is present, not that any was translated."""
    _assert_not_a_copy(
        f"custom_components/ha_mcp_tools/translations/{locale}.json",
        _component_catalog("en"),
        _component_catalog(locale),
        _MAX_COMPONENT_IDENTICAL_SHARE,
    )


# A digit run preceded by a letter or another digit belongs to an identifier
# ("Z2M", "MQTT5"), not to a claim about quantity, so it is not a number a
# translation owes. A trailing unit is deliberately left out of the token:
# a locale that writes "5 тыс." for "5K" still matches on the digits, while
# "46K" where the English says 90% and 5K still reports as changed.
#
# Separators inside a number are kept as GROUP BOUNDARIES rather than deleted.
# Deleting them tolerates locale punctuation but also erases the difference
# between the "4.5:1" contrast ratio and "45:1", and between the component
# version "1.2.4" and "12.4" -- both live English strings. Comparing the tuple
# of groups keeps "4.5" and "4,5" equal while "45" stays a different number.
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,   ]\d+)*")
# A magnitude suffix is part of the claim: "5K" and "5M" share their digits.
_MAGNITUDE_RE = re.compile(r"(?<![A-Za-z0-9])(\d+)([KMGT])(?![A-Za-z])")
# A percentage is a unit every catalog keeps, spaced or not: four write
# "90 %" and four "90%", so the sign is required but the space is free.
_PERCENT_RE = re.compile(r"(?<![A-Za-z0-9])(\d+)\s?%")
# A storage unit is part of the claim too: "1-256 MB" and "1-256 GB" differ by
# three orders of magnitude while the digits match.
_COMPOUND_UNIT_RE = re.compile(r"(?<![A-Za-z0-9])(\d+)\s*([A-Za-z]{2})(?![A-Za-z])")
_KNOWN_UNITS = frozenset({"KB", "MB", "GB", "TB"})
# "N > 0" and "N < 0" are opposite conditions with identical numbers.
_COMPARISON_RE = re.compile(r"([A-Za-z_]\w*)\s*([<>]=?)\s*(\d+)")
# A range is an ordered claim, and reversing it leaves the digits untouched:
# "Range 1-600" and "Range 600-1" hold the same multiset while the second
# names a lower bound above its upper one. The endpoints are spelled by
# reference to the number pattern rather than repeated, so a grouped bound
# ("1 024") keeps reading as one endpoint here too, and every dash a catalog
# might set counts -- hyphen through em dash. A ratio is the same ordered
# claim behind a different separator, and the corpus ships one: the "4.5:1"
# contrast ratio every catalog carries. Reversing it to "1:4,5" leaves the
# digits untouched exactly as a reversed range does, so a colon counts here
# too -- both the ASCII one and the fullwidth colon a CJK catalog may set.
_ORDERED_PAIR_RE = re.compile(
    rf"({_NUMBER_RE.pattern})\s*([-‐-―:：])\s*({_NUMBER_RE.pattern})(?![A-Za-z0-9])"
)
_GROUP_SEPARATOR_RE = re.compile(r"[.,   ]")

# Tokens a reader has to type, search for, or find on disk. Prose slashes
# ("read/write") are not paths, hence the requirement that a path start at a
# separator; a bare word is not an identifier, hence the required underscore.
_LITERAL_RE = re.compile(
    r"""(?:
        # Files come FIRST: with snake_case ahead of them, "tool_policy.json"
        # tokenised as "tool_policy" plus ".json", and neither half is the name
        # a reader has to find. "*" belongs in the stem for "packages/*.yaml".
        [\w.~*/-]*\.(?:yaml|yml|json|py|md|txt)  # files: configuration.yaml
      | [a-z][a-z0-9]*(?:_[a-z0-9]+)+           # snake_case: enable_tool_search
      | [A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+           # ALL_CAPS: DISABLED_TOOLS
      | (?<![\w/<])~?/[\w.*-]+(?:/[\w.*-]+)*    # paths: /api/settings/features
      | [a-z][a-z0-9+.-]*://\S*                 # any scheme, bare one included
      | (?<![\w.])[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+(?![\w])  # group.set
      | (?<!\w)[A-Za-z][A-Za-z_]*\d+(?!\w)      # Jinja2, alert2
        # A repository slug, both halves hyphenated. That shape is what keeps
        # "read/write", "enable/disable" and "re-add/refresh" out -- prose
        # pairs a slash without hyphenating both sides. A slug that hyphenates
        # neither would slip through; none ships today.
      | (?<![\w/])[a-z0-9]+(?:-[a-z0-9]+)+/[a-z0-9]+(?:-[a-z0-9]+)+(?![\w/])
      | (?<!\w)[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+(?!\w)  # WebSocket, Z2M
    )""",
    re.X,
)

# An exact argument a caller has to pass: yaml_path='automation'. The word
# alone is ordinary prose a locale should translate, so only the quoted form
# counts -- and the parameter name is captured with it, because the value on
# its own says nothing about which argument it belongs to: "scope='snapshot',
# action='delete'" and "scope='delete', action='snapshot'" share both values
# and describe opposite calls.
_QUOTED_ASSIGNMENT_RE = re.compile(r"(?<![\w])([a-z_]+\s*=\s*'[^']+')")

# Latin abbreviations the dotted-identifier arm would otherwise claim. Prose,
# not names: no reader types "e.g" into anything.
_PROSE_ABBREVIATIONS = frozenset({"e.g", "i.e"})

# (locale, surface, key) -> why this pair cannot satisfy the rule. Keep the
# reason specific enough that a later reader can re-decide it; an exception
# whose justification is "the check is noisy" belongs in the check instead.
# The tolerated loss is named, not the pair: everything else about the key --
# any other number, every literal -- is still checked, so a later hand edit
# cannot hide behind the exception.
LITERAL_PARITY_EXCEPTIONS: dict[
    tuple[str, str, str], tuple[frozenset[Any], frozenset[Any], str]
] = {
    (
        "zh-Hans",
        TOOL_SOURCES_SURFACE,
        "ha_config_set_helper.description",
    ): (
        frozenset(),
        frozenset({("28",)}),
        "The Chinese catalog translates the tool's full summary, including the "
        '"(28 types, unified interface)" clause that sits on the second line '
        "of the docstring. The rendering the engine sends is the first line "
        "alone, so the catalog states more than its source, not something else.",
    ),
    (
        "ru",
        "src/ha_mcp/settings_ui/locales",
        "messages.features.enable_beta_features.help",
    ): (
        frozenset({("5",)}),
        frozenset(),
        'Russian spells the count out: "для пяти экспериментальных '
        'подпараметров" for "the 5 experimental sub-flags". The digit is '
        "missing because the sentence is right, not because a number was lost.",
    ),
}


def _canonical_number(token: str) -> tuple[str, ...]:
    """One number as its groups, with thousands grouping folded away.

    A separator followed by groups of exactly three digits is grouping, not a
    boundary: English ships `10000`, `1440` and `65535`, and a locale writing
    `10.000` states the same number. Anything else keeps its groups, so the
    "4.5:1" ratio and the version "1.2.4" stay distinct from "45" and "12.4".
    A decimal written to exactly three places is the ambiguous case and folds
    with the thousands; no English string has one.
    """
    groups = _GROUP_SEPARATOR_RE.split(token)
    if (
        len(groups) > 1
        and len(groups[0]) <= 3
        and all(len(group) == 3 for group in groups[1:])
    ):
        return ("".join(groups),)
    return tuple(groups)


def _numbers(text: str) -> Counter[tuple[str, ...]]:
    return Counter(_canonical_number(token) for token in _NUMBER_RE.findall(text))


def _lost_magnitudes(english: str, translated: str) -> list[str]:
    """Magnitude suffixes the translation contradicts rather than drops.

    A unit is part of the claim, and the value comparison cannot see it: "5K"
    and "5M" carry the same digits, and so do "90%" and a bare "90". The
    percent sign is required outright because every catalog keeps it -- four
    write "90 %" and four "90%", so only the space is free. Requiring a
    magnitude suffix outright would be wrong, though: Polish writes
    "5 tys." and Russian "5 тыс." for it, and both are correct. So the suffix
    is only compared when the translation itself puts a Latin letter straight
    onto those digits -- six of the nine catalogs do, Polish and Russian spell
    it out, and the Italian value is one this branch deletes for the sync to
    rewrite.
    """
    contradicted = set()
    for digits, unit in _COMPOUND_UNIT_RE.findall(english):
        if unit not in _KNOWN_UNITS:
            continue
        carried = re.search(
            rf"(?<![A-Za-z0-9]){re.escape(digits)}\s*([A-Za-z]{{2}})(?![A-Za-z])",
            translated,
        )
        # French writes "Mo" and Russian "МБ"; only a unit from the same
        # vocabulary is comparable, anything else is a localised spelling.
        if carried and carried.group(1) in _KNOWN_UNITS and carried.group(1) != unit:
            contradicted.add(f"{digits} {unit}")
    for name, operator, digits in _COMPARISON_RE.findall(english):
        if not re.search(
            rf"{re.escape(name)}\s*{re.escape(operator)}\s*{digits}", translated
        ):
            contradicted.add(f"{name} {operator} {digits}")
    for digits in _PERCENT_RE.findall(english):
        if not re.search(rf"(?<![A-Za-z0-9]){re.escape(digits)}\s?%", translated):
            contradicted.add(f"{digits}%")
    for digits, suffix in _MAGNITUDE_RE.findall(english):
        carried = re.search(
            rf"(?<![A-Za-z0-9]){re.escape(digits)}([A-Za-z])", translated
        )
        if carried and carried.group(1).upper() != suffix.upper():
            contradicted.add(f"{digits}{suffix}")
    return sorted(contradicted)


def _reversed_ordered_pairs(english: str, translated: str) -> list[str]:
    """Ordered number pairs the translation states back to front.

    A range is an ordered claim that the value comparison is blind to: both
    endpoints of "Range 1-600" are still present in "Range 600-1", so the
    number multiset balances while the text now names a floor above its
    ceiling.

    Only an actual inversion reports. A catalog is free to write "von 1 bis
    600" instead of setting a dash, and its numbers stay guarded by the value
    comparison either way; demanding the dash form back would fail a correct
    rendering rather than a wrong one.

    A ratio is the same claim with a colon for a dash: "4.5:1" and "1:4,5"
    hold the same two numbers, and the second states a contrast threshold no
    checker would ever set. The separator the English used is carried into
    the report so a ratio is not named as a range.
    """
    translated_bounds = {
        (_canonical_number(low), _canonical_number(high))
        for low, _, high in _ORDERED_PAIR_RE.findall(translated)
    }
    return sorted(
        {
            f"{low}{':' if separator in ':：' else '-'}{high}"
            for low, separator, high in _ORDERED_PAIR_RE.findall(english)
            if (bounds := (_canonical_number(low), _canonical_number(high)))
            and bounds[0] != bounds[1]
            and bounds[::-1] in translated_bounds
            and bounds not in translated_bounds
        }
    )


def _show_numbers(counted: Counter[tuple[str, ...]]) -> list[str]:
    return sorted(".".join(groups) for groups in counted.elements())


@cache
def _untranslatable_names() -> frozenset[str]:
    """On-screen names our own Python hardcodes, which must stay English.

    Read from the pipeline at runtime instead of copied, so this check cannot
    drift away from ``translate_locales._untranslatable_name_dropped``, the
    guard that rejects engine output localising one.
    """
    import translate_locales

    return frozenset(translate_locales._hardcoded_ui_names())


def _without_sentence_punctuation(literal: str) -> str:
    """A literal with the prose punctuation it swallowed removed.

    A path and a URI both run to the next space, so "see /api/x." and
    "at skill://y)" carry the sentence's period or bracket into the token, and
    requiring it verbatim fails a translation that keeps the literal but ends
    its sentence differently.

    An ellipsis is left alone: ``/api/webhook/...`` is the one live English
    literal ending in punctuation, and there the dots are part of what the
    reader is shown, not the end of a sentence.
    """
    if literal.endswith("..."):
        return literal
    return literal.rstrip(".,;:!?)]}\"'»”")


def _carries(literal: str, translated: str) -> bool:
    """Whether the translation still contains this literal as a whole token.

    Substring, but not *any* substring: the match may not continue into
    another identifier character, so "enable_tool_search_old" no longer counts
    as carrying `enable_tool_search`, nor "configuration.yaml.bak" as carrying
    `configuration.yaml`. A hyphen or a case change is a different matter --
    German writes "/data-Volume" and Swedish "/data-volymen", and both carry
    the original intact. A dotted extension does not count either, so
    "configuration.yaml.bak" is a different file -- and so is
    "other.configuration.yaml", so a dot is excluded on the left as well --
    while a sentence-ending period after the name is not part of the token and
    still matches.
    """
    return (
        re.search(
            r"(?<![A-Za-z0-9_.])"
            + re.escape(literal)
            + r"(?![A-Za-z0-9_]|\.[A-Za-z0-9])",
            translated,
        )
        is not None
    )


def _lost_literals(english: str, translated: str) -> list[str]:
    """English literals absent from the translation, as substrings.

    Substring rather than token equality on purpose: German writes
    "/data-Volume" and Swedish "/data-volymen" and "skill://-resurs", which
    tokenise as different literals while carrying the original intact.
    Re-extracting from the translation reports every one of them as a loss;
    asking whether the English literal is still findable reports none.

    Both arms go through ``_carries``, so an argument is held to the same
    boundary rule as an identifier: with plain containment, "scope='snapshots'"
    satisfies "scope='snapshot'" while naming a different value.

    Hardcoded on-screen names are left to ``_localised_hardcoded_name``, which
    applies the pipeline's own rule to them. Excluding them here is structural
    rather than load-bearing: every name it lists is several words long and
    none tokenises as a literal, so the filter removes nothing today. It is
    what keeps a name that later gains an extractable shape from reporting
    twice under two different descriptions.
    """
    protected = _untranslatable_names()
    return sorted(
        {
            stripped
            for literal in _LITERAL_RE.findall(english)
            if (stripped := _without_sentence_punctuation(literal))
            and stripped not in protected
            and stripped not in _PROSE_ABBREVIATIONS
            and not _carries(stripped, translated)
        }
        | {
            assignment
            for assignment in _QUOTED_ASSIGNMENT_RE.findall(english)
            if not _carries(assignment, translated)
        }
    )


def _localised_hardcoded_name(english: str, translated: str) -> list[str]:
    """The on-screen name this translation localised away, if any.

    The pipeline already refuses engine output that translates one of these,
    but that gate only ever runs while *accepting* new output. A catalog edited
    by hand, or one pinned before the name existed, is never re-read by it:
    the baseline still matches, so the sync plans no rewrite, and a reader is
    left hunting for an entry title the integration does not display. Asking
    the same question of the pinned pairs costs one call and needs no second
    rule -- ``translate_locales`` owns it, and it is called rather than copied
    so the two cannot drift apart.
    """
    import translate_locales

    dropped = translate_locales._untranslatable_name_dropped(english, translated)
    return [dropped] if dropped is not None else []


@cache
def _english_tool_sent_to_translators() -> dict[str, str]:
    """The English a tool translation is written against, as the sync sends it.

    A tool title or description exists in more than one rendering -- the row's
    first physical line cut at 120 characters, the parsed docstring, and the
    summary paragraph the baseline hashes -- and accepting whichever one
    matches lets a fault present in the real source hide behind another. It
    does not have to be guessed: ``_plan_settings`` feeds the engine
    ``_english_tool_texts()`` and nothing else (``scripts/translate_locales.py``,
    where ``tool_texts`` is built), so that rendering is what a catalog was
    translated from.
    """
    return dict(_english_tool_texts())


@cache
def _pending_keys(surface: str) -> frozenset[str]:
    """Keys of one surface whose English moved since the baseline was pinned."""
    recorded = json.loads(BASELINE_PATH.read_text("utf-8")).get(surface, {})
    return frozenset(
        key
        for key, digest in english_sources()[surface].items()
        if recorded.get(key) != digest
    )


def _literal_parity_pairs(locale: str) -> list[tuple[str, str, str, frozenset[str]]]:
    """(surface, key, translated, english variants) for one locale.

    Both authored catalogs plus the tool titles and descriptions, which live
    in the settings catalog but take their English from the tool definitions.
    """
    english = _catalogs_by_surface("en")
    translated = _catalogs_by_surface(locale)
    pairs = [
        (surface, key, text, frozenset({english[surface][key]}))
        for surface, catalog in translated.items()
        for key, text in catalog.items()
        if key in english[surface] and key not in _pending_keys(surface)
    ]
    sent = _english_tool_sent_to_translators()
    pending_tools = _pending_keys(TOOL_SOURCES_SURFACE)
    catalog = _settings_catalog(locale)
    pairs += [
        (TOOL_SOURCES_SURFACE, key, text, frozenset({sent[key]}))
        for key, text in _flatten(catalog.get("tools", {})).items()
        if key in sent and key not in pending_tools
    ]
    # A group key *is* its own English text, so the heading needs no baseline
    # and cannot go stale under a translation. Today no group name carries a
    # literal or a number at all, which makes this arm structurally empty
    # rather than verified — it is here so the surface stops being an
    # exception the day a heading gains one.
    pairs += [
        ("settings UI tool group headings", key, text, frozenset({key}))
        for key, text in _flatten(catalog.get("tool_groups", {})).items()
    ]
    return pairs


@pytest.mark.parametrize("locale", _non_english_settings_locales())
def test_translations_keep_english_numbers_and_identifiers(locale: str) -> None:
    """A renamed identifier or a changed number breaks the reader's next step.

    Deliberately NOT gated behind ``completeness``, for the reason placeholder
    parity is not: the sync cannot repair this. Once the baseline pins an
    English string, a key whose hash still matches is never planned again, so
    a translation that dropped ``docs/beta.md``, localised
    ``enable_tool_search`` into prose, or states "46K" where the English says
    90% keeps saying it indefinitely — one of each shipped, and #2180 repaired
    them by hand because nothing was going to.

    Keys whose English has moved since the baseline are excluded: those are
    owed a machine rewrite, and until the sync runs the old translation
    legitimately carries the old literals.

    Two literal shapes stay out of scope, because a mechanical rule cannot
    separate either from prose. A bare code word is one: English "set to true"
    is a value a reader types, while ``common.none`` is the word "none" as a
    UI label that every locale is right to translate, and nothing in either
    string tells the two apart. A standalone acronym is the other -- ``ZHA``
    is a product name a translation owes, but the all-uppercase tokens locales
    do not carry through are ``UI`` (107 pairs) and ``AI`` (98), both correctly
    translated, and emphasis like ``REQUIRES``, ``NOT`` and ``WARNING``.
    Telling those apart needs a maintained do-not-translate glossary, which is
    a maintainer decision rather than something to infer here.

    A tool string has more than one English rendering, and the check compares
    against the one the sync sends rather than whichever one matches: eleven of
    the 176 tool keys differ between renderings, and accepting any of them
    would let a fault in the real source hide behind another. Which one that is
    needs no guessing -- ``_plan_settings`` builds its ``tool_texts`` from
    ``_english_tool_texts()`` and feeds the engine that alone.
    """
    divergent: dict[str, str] = {}
    for surface, key, text, variants in _literal_parity_pairs(locale):
        tolerated_losses, tolerated_additions, _ = LITERAL_PARITY_EXCEPTIONS.get(
            (locale, surface, key), (frozenset(), frozenset(), "")
        )
        faults: list[str] = []
        for english in variants:
            lost_numbers = Counter(
                {
                    number: count
                    for number, count in (_numbers(english) - _numbers(text)).items()
                    if number not in tolerated_losses
                }
            )
            gained_numbers = Counter(
                {
                    number: count
                    for number, count in (_numbers(text) - _numbers(english)).items()
                    if number not in tolerated_additions
                }
            )
            lost = (
                _lost_literals(english, text)
                + _lost_magnitudes(english, text)
                + _reversed_ordered_pairs(english, text)
                + _localised_hardcoded_name(english, text)
            )
            if not (lost_numbers or gained_numbers or lost):
                faults = []
                break
            faults.append(
                ", ".join(
                    part
                    for part in (
                        f"numbers lost {_show_numbers(lost_numbers)}"
                        if lost_numbers
                        else "",
                        f"numbers added {_show_numbers(gained_numbers)}"
                        if gained_numbers
                        else "",
                        f"identifiers dropped {lost}" if lost else "",
                    )
                    if part
                )
            )
        if faults:
            divergent[f"{surface}: {key}"] = min(faults, key=len)

    assert not divergent, (
        f"the {locale} translation no longer carries what its English states "
        f"verbatim. A number or a code literal that a reader has to type, "
        f"search for, or find on disk changed or vanished; the sync will not "
        f"revisit these keys, so fix the catalog by hand or delete the value "
        f"to queue it for the next run. {divergent}"
    )


@pytest.mark.parametrize(
    ("english", "translated", "expected"),
    [
        # A URI or path runs to the next space, so the sentence's own period
        # rides along; a translation that keeps the literal and ends its
        # sentence differently is still faithful.
        ("read https://example.org/docs.", "siehe https://example.org/docs", []),
        ("look under /api/settings/features.", "unter /api/settings/features", []),
        ("see /api/x)", "siehe /api/x", []),
        # Dropping the literal itself still reports, punctuation or not.
        (
            "read https://example.org/docs.",
            "siehe die Dokumentation",
            ["https://example.org/docs"],
        ),
        # A scheme with nothing after it is a literal in its own right.
        ("its skill:// resource", "seine skill://-Ressource", []),
        ("its skill:// resource", "seine Ressource", ["skill://"]),
        # The ellipsis belongs to what the reader is shown, so it survives
        # stripping and a translation that truncates it is reported.
        ("POST to /api/webhook/...", "POST an /api/webhook/...", []),
        ("POST to /api/webhook/...", "POST an /api/webhook/", ["/api/webhook/..."]),
        # A neighbouring token is a different name, on either side, and a
        # dotted extension makes a different file.
        (
            "edit configuration.yaml",
            "bearbeite configuration.yaml.bak",
            ["configuration.yaml"],
        ),
        ("set enable_tool_search", "setze xenable_tool_search", ["enable_tool_search"]),
        (
            "edit configuration.yaml",
            "bearbeite other.configuration.yaml",
            ["configuration.yaml"],
        ),
        (
            "set enable_tool_search",
            "setze enable_tool_search_old",
            ["enable_tool_search"],
        ),
        # ... while a sentence-final period and a hyphenated compound are not.
        ("edit configuration.yaml", "bearbeite configuration.yaml.", []),
        ("mounted at /data", "eingehängt unter /data-Volume", []),
        ("its skill:// resource", "seine skill://-Ressource", []),
        # A filename whose stem is snake_case is one literal, not two.
        ("read tool_policy.json", "lies tool_policy.json", []),
        ("read tool_policy.json", "lies die Richtliniendatei", ["tool_policy.json"]),
        # Service paths and letter-digit product names are literals too.
        ("via the group.set service", "über den Dienst group.set", []),
        ("via the group.set service", "über den Gruppendienst", ["group.set"]),
        ("templates use Jinja2", "Vorlagen nutzen Jinja2", []),
        ("templates use Jinja2", "Vorlagen nutzen Jinja3", ["Jinja2"]),
        # "e.g" is prose, not a name a reader types.
        ("e.g. a light", "z. B. eine Lampe", []),
        # A repository slug is a literal; a prose pair sharing a slash is not.
        (
            "add homeassistant-ai/ha-mcp-integration",
            "füge homeassistant-ai/ha-mcp-wrong hinzu",
            ["homeassistant-ai/ha-mcp-integration"],
        ),
        ("grants read/write access", "gewährt Lese-/Schreibzugriff", []),
        # A quoted argument value is exact; the same word unquoted is prose.
        (
            "pass scope='snapshot'",
            "übergib scope='archive'",
            ["scope='snapshot'"],
        ),
        ("pass scope='snapshot'", "übergib scope='snapshot'", []),
        # A near-miss value is a different value, so the argument is held to
        # the same boundary rule as an identifier.
        (
            "pass scope='snapshot'",
            "übergib scope='snapshots'",
            ["scope='snapshot'"],
        ),
        # Two arguments that swap their values keep every value in the string
        # while describing the opposite call, so the name is compared with it.
        (
            "pass scope='snapshot', action='delete'",
            "übergib scope='delete', action='snapshot'",
            ["action='delete'", "scope='snapshot'"],
        ),
        # A value that survives without its parameter no longer tells a reader
        # which argument to put it in.
        (
            "pass scope='snapshot'",
            "übergib 'snapshot'",
            ["scope='snapshot'"],
        ),
    ],
)
def test_literal_extraction_ignores_sentence_punctuation(
    english: str, translated: str, expected: list[str]
) -> None:
    """Prose punctuation must not become part of what a translation owes.

    The parity check above is only as good as the token it demands: swallow the
    period at the end of a sentence and every locale that punctuates differently
    reports as having dropped the literal.
    """
    assert _lost_literals(english, translated) == expected


@pytest.mark.parametrize(
    ("english", "translated", "expected"),
    [
        # A unit carries as much of the claim as the digits do.
        ("limit is 1-256 MB", "Grenze ist 1-256 MB", []),
        ("limit is 1-256 MB", "Grenze ist 1-256 GB", ["256 MB"]),
        # ... but only a unit from the same vocabulary is comparable: French
        # writes "Mo" and Russian "МБ", and neither contradicts "MB".
        ("limit is 1-256 MB", "limite de 1-256 Mo", []),
        ("limit is 1-256 MB", "предел 1-256 МБ", []),
        # A comparison is reversible without touching a single digit.
        ("only when N > 0", "nur wenn N > 0", []),
        ("only when N > 0", "nur wenn N < 0", ["N > 0"]),
        ("only when N > 0", "nur wenn N>0", []),
        # A magnitude suffix is compared only against a Latin one.
        ("about 5K tokens", "etwa 5M Token", ["5K"]),
        ("about 5K tokens", "около 5 тыс. токенов", []),
        # A percentage keeps its sign, spaced or not.
        ("roughly 90% less", "rund 90 % weniger", []),
        ("roughly 90% less", "rund 90 weniger", ["90%"]),
    ],
)
def test_units_and_comparisons_are_compared_where_they_are_comparable(
    english: str, translated: str, expected: list[str]
) -> None:
    """Digits alone do not carry the claim.

    "1-256 MB" and "1-256 GB" differ by three orders of magnitude, "N > 0" and
    "N < 0" are opposite conditions, and "90%" and a bare "90" say different
    things -- none of which the value comparison can see, because the numbers
    are identical in every pair.
    """
    assert _lost_magnitudes(english, translated) == expected


@pytest.mark.parametrize(
    ("english", "translated", "expected"),
    [
        # Reversing a range touches no digit, so nothing else can see it.
        ("Range 1–600.", "Bereich 1–600.", []),
        ("Range 1–600.", "Bereich 600–1.", ["1-600"]),
        # Any dash a catalog might set reads as the same range ...
        ("Range 1–600.", "Bereich 1-600.", []),
        ("Range 1–600.", "Bereich 1—600.", []),
        # ... and a grouped endpoint stays one endpoint, not two numbers.
        ("Range 1–10 000.", "Bereich 10 000–1.", ["1-10 000"]),
        # Spelling the bounds out is a translation's own business: the value
        # comparison still holds both numbers, so nothing goes unguarded.
        ("Range 1–600.", "von 1 bis 600 Sekunden.", []),
        # A range that simply vanishes is a lost number, not a reversed one.
        ("Range 1–600.", "Zeitlimit pro Anfrage.", []),
        # A ratio reverses the same way, and the decimal comma five of the
        # nine catalogs write is not the inversion.
        ("below a 4.5:1 contrast ratio.", "unter 4,5:1 Kontrast.", []),
        ("below a 4.5:1 contrast ratio.", "unter 1:4,5 Kontrast.", ["4.5:1"]),
        # A fullwidth colon is the same separator, so an inversion cannot
        # hide behind CJK punctuation.
        ("below a 4.5:1 contrast ratio.", "对比度低于 1：4.5。", ["4.5:1"]),
    ],
)
def test_a_reversed_ordered_pair_is_reported(
    english: str, translated: str, expected: list[str]
) -> None:
    """An ordered claim needs an ordered comparison.

    "Range 1-600" and "Range 600-1" are the same two numbers in the same two
    positions of the value comparison, and the second one documents a bound no
    setting will accept.
    """
    assert _reversed_ordered_pairs(english, translated) == expected


def test_a_localised_on_screen_name_is_reported() -> None:
    """A name Python fixes in English is not the translation's to change.

    The name is taken from the source rather than written here, so a rename
    cannot leave this test guarding a string nobody displays any more.
    """
    names = sorted(_untranslatable_names())
    assert names, "the pipeline reports no hardcoded on-screen names"
    name = names[0]
    english = f"open the '{name}' entry"
    assert _localised_hardcoded_name(english, f"öffne den Eintrag '{name}'") == []
    localised = name.replace(" ", "-")
    assert localised != name, f"{name!r} has no space to mangle"
    assert _localised_hardcoded_name(english, f"öffne den Eintrag '{localised}'") == [
        name
    ]


def _agents_md_section(title: str) -> str:
    """The body of one ``## `` section of AGENTS.md.

    Scoping matters: a check that greps the whole file answers a question
    about the file, not about the section it claims to guard, and any future
    sentence elsewhere carrying the same shape would fail it.
    """
    text = AGENTS_MD.read_text("utf-8")
    match = re.search(
        rf"^## {re.escape(title)}$(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL
    )
    assert match, f"AGENTS.md has no '## {title}' section — this test guards it"
    return match.group(1)


def test_agents_md_states_the_current_ceilings() -> None:
    """The documented percentages are the ones a contributor plans against.

    Same reason the locale list below is pinned: the prose went stale the
    moment the constant moved, and nothing tied the two together.
    """
    section = _agents_md_section("Translations")
    documented = set(re.findall(r"(\d+)% for the", section))

    assert documented == {
        f"{_MAX_ENGLISH_IDENTICAL_SHARE:.0%}".rstrip("%"),
        f"{_MAX_COMPONENT_IDENTICAL_SHARE:.0%}".rstrip("%"),
    }, (
        f"AGENTS.md § Translations documents ceilings {sorted(documented)} but "
        f"the constants are {_MAX_ENGLISH_IDENTICAL_SHARE:.0%} and "
        f"{_MAX_COMPONENT_IDENTICAL_SHARE:.0%}. Update the prose."
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
