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
the baseline.
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
    sources["settings UI tool titles and descriptions"] = _english_tool_sources()
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

    for locale in ["en", *_translated_component_locales()]:
        value = _component_catalog(locale)["common.connect_local_lan"]
        assert quoted in value, (
            f"custom_components/ha_mcp_tools/translations/{locale}.json "
            f"common.connect_local_lan is {value!r}, which does not quote the "
            f"{quoted!r} option the reader has to find on the form"
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
    subsequent PR red across all six locales, landing the failure on whoever
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
# surfaces get a ceiling: leaving one out accepts a wholesale-English catalog
# there. The generated add-on YAMLs need none of their own — their strings
# are the settings UI ``messages`` (``addon.*`` / ``features.*``), so the
# settings ceiling already covers them.
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
    the rendered one reads it as translated. Six of the 174 keys differ between
    the renderings today — 3.4%, invisible under the ceiling on their own and
    enough to hide a genuinely untranslated remainder underneath it.
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


@pytest.mark.parametrize("locale", _translated_component_locales())
def test_component_catalog_is_not_a_copy_of_english(locale: str) -> None:
    """Key parity says every key is present, not that any was translated."""
    _assert_not_a_copy(
        f"custom_components/ha_mcp_tools/translations/{locale}.json",
        _component_catalog("en"),
        _component_catalog(locale),
        _MAX_COMPONENT_IDENTICAL_SHARE,
    )


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
