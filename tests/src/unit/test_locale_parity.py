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
from collections.abc import Callable, Sequence
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from ha_mcp.settings_ui._tools_meta import FEATURE_GATED_TOOLS, primary_tag

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


def _catalogs_by_surface(locale: str) -> dict[str, dict[str, str]]:
    """One locale's catalogs, flattened, keyed by the directory they live in.

    Shared by the baseline hashes and the cross-surface check below, so those
    two cannot end up reading a different set of files than one another. The
    per-surface helpers above still read their own catalog directly; this is
    for the checks that have to look at all four at once.
    """
    catalogs = {
        "src/ha_mcp/settings_ui/locales": _flatten(
            json.loads((SETTINGS_LOCALES / f"{locale}.json").read_text("utf-8"))
        ),
        "custom_components/ha_mcp_tools/translations": _component_catalog(locale),
    }
    for addon_dir in ADDON_DIRS:
        catalogs[f"{addon_dir.name}/translations"] = _flatten(
            yaml.safe_load(
                (addon_dir / "translations" / f"{locale}.yaml").read_text("utf-8")
            )
        )
    return catalogs


def _english_tool_sources() -> dict[str, str]:
    """The English tool texts a settings UI catalog translates.

    ``en.json`` leaves ``tools`` empty — English for those comes from the tool
    definitions at runtime — so these 174 strings live in no catalog, and they
    were the one translated surface the baseline did not cover. An edit to a
    docstring therefore left every locale describing the old behaviour with
    nothing going red: ``ha_dev_manage_settings`` gained the
    Tools/Policies/Backups surfaces and four locales went on saying "directly".

    A feature-gated tool has two English renderings and a setting decides which
    one the UI shows, so where the stub and the docstring differ, both are
    pinned.
    """
    rendered = _english_tool_texts()
    parsed = _english_tool_texts(as_rendered=False)
    sources = dict(rendered)
    for key, text in parsed.items():
        if text != rendered.get(key):
            sources[f"{key} (docstring)"] = text
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


def test_component_english_catalog_mirrors_strings_json() -> None:
    """``strings.json`` is the source; ``en.json`` is its shipped copy."""
    assert json.loads(COMPONENT_STRINGS.read_text("utf-8")) == json.loads(
        (COMPONENT_TRANSLATIONS / "en.json").read_text("utf-8")
    ), (
        "custom_components/ha_mcp_tools/strings.json and translations/en.json "
        "have drifted — every translated catalog is keyed against en.json"
    )


def test_connect_local_lan_quotes_the_bind_host_option() -> None:
    """The sentence names an on-screen option, so it has to name that option.

    ``connect_local_lan`` tells the reader which dropdown entry produces this
    URL, and every catalog quotes the label untranslated for that reason —
    the reader matches it against the form. Renaming the option in
    ``config_flow.py`` would leave six catalogs quoting a label that no longer
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


# The (surface, key) places that take part in the grouping below, by name.
# A per-surface total would be compensable: one place can leave the
# grouping while another arrives on the same surface, and the sum holds.
# Membership is per place, not per group, so one narrow case still passes:
# reword a key's English onto another already-shared text and the place
# stays pinned while the group it left keeps enough surfaces to survive.
SHARED_ENGLISH_PLACES = {
    "custom_components/ha_mcp_tools/translations": (
        "options.step.tools_info.data.extra_yaml_keys",
    ),
    "homeassistant-addon-dev/translations": (
        "configuration.auto_backup_retain_per_entity.description",
        "configuration.auto_backup_retain_per_entity.name",
        "configuration.auto_backup_throttle_minutes.description",
        "configuration.auto_backup_throttle_minutes.name",
        "configuration.backup_hint.description",
        "configuration.backup_hint.name",
        "configuration.disabled_tools.description",
        "configuration.disabled_tools.name",
        "configuration.enable_auto_backup.description",
        "configuration.enable_auto_backup.name",
        "configuration.enable_beta_features.description",
        "configuration.enable_beta_features.name",
        "configuration.enable_code_mode.description",
        "configuration.enable_code_mode.name",
        "configuration.enable_dashboard_screenshot.description",
        "configuration.enable_dashboard_screenshot.name",
        "configuration.enable_filesystem_tools.description",
        "configuration.enable_filesystem_tools.name",
        "configuration.enable_lite_docstrings.description",
        "configuration.enable_lite_docstrings.name",
        "configuration.enable_mandatory_bps.description",
        "configuration.enable_mandatory_bps.name",
        "configuration.enable_snapshot_delete.description",
        "configuration.enable_snapshot_delete.name",
        "configuration.enable_strict_mandatory_bps.description",
        "configuration.enable_strict_mandatory_bps.name",
        "configuration.enable_tool_search.description",
        "configuration.enable_tool_search.name",
        "configuration.enable_tool_security_policies.description",
        "configuration.enable_tool_security_policies.name",
        "configuration.enable_yaml_config_editing.description",
        "configuration.enable_yaml_config_editing.name",
        "configuration.enable_yaml_edit_confirm.description",
        "configuration.enable_yaml_edit_confirm.name",
        "configuration.enable_yaml_packages_automation.description",
        "configuration.enable_yaml_packages_automation.name",
        "configuration.enable_yaml_packages_scene.description",
        "configuration.enable_yaml_packages_scene.name",
        "configuration.enable_yaml_packages_script.description",
        "configuration.enable_yaml_packages_script.name",
        "configuration.pinned_tools.description",
        "configuration.pinned_tools.name",
        "configuration.read_only_mode.description",
        "configuration.read_only_mode.name",
        "configuration.secret_path.description",
        "configuration.secret_path.name",
        "configuration.snapshot_delete_min_age_days.description",
        "configuration.snapshot_delete_min_age_days.name",
        "configuration.tool_search_max_results.description",
        "configuration.tool_search_max_results.name",
        "configuration.verify_ssl.description",
        "configuration.verify_ssl.name",
    ),
    "homeassistant-addon/translations": (
        "configuration.auto_backup_retain_per_entity.description",
        "configuration.auto_backup_retain_per_entity.name",
        "configuration.auto_backup_throttle_minutes.description",
        "configuration.auto_backup_throttle_minutes.name",
        "configuration.backup_hint.description",
        "configuration.backup_hint.name",
        "configuration.disabled_tools.description",
        "configuration.disabled_tools.name",
        "configuration.enable_auto_backup.description",
        "configuration.enable_auto_backup.name",
        "configuration.enable_mandatory_bps.description",
        "configuration.enable_mandatory_bps.name",
        "configuration.enable_snapshot_delete.description",
        "configuration.enable_snapshot_delete.name",
        "configuration.enable_strict_mandatory_bps.description",
        "configuration.enable_strict_mandatory_bps.name",
        "configuration.enable_tool_search.name",
        "configuration.enable_tool_security_policies.description",
        "configuration.enable_tool_security_policies.name",
        "configuration.pinned_tools.description",
        "configuration.pinned_tools.name",
        "configuration.read_only_mode.description",
        "configuration.read_only_mode.name",
        "configuration.secret_path.description",
        "configuration.secret_path.name",
        "configuration.snapshot_delete_min_age_days.description",
        "configuration.snapshot_delete_min_age_days.name",
        "configuration.tool_search_max_results.description",
        "configuration.tool_search_max_results.name",
        "configuration.verify_ssl.description",
        "configuration.verify_ssl.name",
    ),
    "src/ha_mcp/settings_ui/locales": (
        "messages.advanced.extra_yaml_write_keys.label",
        "messages.backup.fields.enable_snapshot_delete.label",
        "messages.backup.fields.snapshot_delete_min_age_days.label",
        "messages.features.enable_beta_features.help",
        "messages.features.enable_beta_features.label",
        "messages.features.enable_code_mode.help",
        "messages.features.enable_code_mode.label",
        "messages.features.enable_dashboard_screenshot.help",
        "messages.features.enable_dashboard_screenshot.label",
        "messages.features.enable_filesystem_tools.help",
        "messages.features.enable_filesystem_tools.label",
        "messages.features.enable_lite_docstrings.help",
        "messages.features.enable_lite_docstrings.label",
        "messages.features.enable_mandatory_bps.help",
        "messages.features.enable_mandatory_bps.label",
        "messages.features.enable_strict_mandatory_bps.help",
        "messages.features.enable_strict_mandatory_bps.label",
        "messages.features.enable_tool_search.help",
        "messages.features.enable_tool_search.label",
        "messages.features.enable_tool_security_policies.help",
        "messages.features.enable_tool_security_policies.label",
        "messages.features.enable_yaml_config_editing.help",
        "messages.features.enable_yaml_config_editing.label",
        "messages.features.enable_yaml_edit_confirm.help",
        "messages.features.enable_yaml_edit_confirm.label",
        "messages.features.enable_yaml_packages_automation.help",
        "messages.features.enable_yaml_packages_automation.label",
        "messages.features.enable_yaml_packages_scene.help",
        "messages.features.enable_yaml_packages_scene.label",
        "messages.features.enable_yaml_packages_script.help",
        "messages.features.enable_yaml_packages_script.label",
        "messages.features.read_only_mode.help",
        "messages.features.read_only_mode.label",
        "messages.features.tool_search_max_results.help",
        "messages.features.tool_search_max_results.label",
        "messages.tools.read_only.label",
    ),
}


@cache
def _shared_english_strings() -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """English strings that reach the reader from more than one surface.

    The add-on options and the settings UI describe the same switches, so a
    number of English strings are written once and shipped from two or three
    catalogs — ``read_only_mode`` is the add-on's ``name``, the settings UI's
    ``messages.features.read_only_mode.label`` and its
    ``messages.tools.read_only.label``.

    Grouping them by text rather than by key is what makes the check below
    possible at all: the keys differ per surface and only the text ties them
    together. That same grouping lets an option leave the check quietly, so
    what this finds is pinned — see
    ``test_shared_english_strings_are_discovered``.

    Selection is deliberately cross-surface only. The same English twice
    *within* one catalog may legitimately differ — Spanish agrees
    ``activado``/``activada`` with the noun each sentence is about, and
    Russian words the policies tab and the policies heading differently.
    Across surfaces there is no such context to differ by: it is one option,
    described twice.

    Selection is not comparison, though: once a group is in, every place in
    it is compared, same-surface siblings included. ``Read Only Mode`` is the
    only text where that happens today — it is an add-on ``name`` as well, so
    its two settings UI keys follow the shared wording rather than the
    within-catalog exemption.
    """
    places: dict[str, list[tuple[str, str]]] = {}
    for surface, strings in _catalogs_by_surface("en").items():
        for key, text in strings.items():
            places.setdefault(text, []).append((surface, key))
    return tuple(
        (text, tuple(where))
        for text, where in places.items()
        if len({surface for surface, _ in where}) > 1
    )


def test_shared_english_strings_are_discovered() -> None:
    """The check below only covers what the grouping still finds.

    A wording that moves on one surface and not the other stops being shared,
    so the option leaves the check instead of failing it. The English edit is
    caught by ``test_translations_are_checked_against_current_english`` — but
    the sanctioned answer there is to regenerate the baseline, and that would
    carry the lost coverage away with it. Hence a pin: it is not written by
    ``scripts/update_locale_baseline.py`` and has to be changed by hand.
    """
    found: dict[str, set[str]] = {}
    for _, where in _shared_english_strings():
        for surface, key in where:
            found.setdefault(surface, set()).add(key)

    surfaces = SHARED_ENGLISH_PLACES.keys() | found.keys()
    lost = {
        surface: sorted(
            set(SHARED_ENGLISH_PLACES.get(surface, ())) - found.get(surface, set())
        )
        for surface in surfaces
    }
    gained = {
        surface: sorted(
            found.get(surface, set()) - set(SHARED_ENGLISH_PLACES.get(surface, ()))
        )
        for surface in surfaces
    }
    drift = {
        surface: {"left the grouping": lost[surface], "newly shared": gained[surface]}
        for surface in sorted(surfaces)
        if lost[surface] or gained[surface]
    }

    assert not drift, (
        "the strings shipped from more than one surface are no longer the "
        f"ones this check was written for — {drift}. A key that left usually "
        "did so because its English moved on one surface and not the other, "
        "which is the drift the check below exists to catch: fix the wording, "
        "not the pin. A key that arrived is newly covered. Update "
        "SHARED_ENGLISH_PLACES once the difference is the intended one."
    )


def _excerpt(text: str, limit: int = 50) -> str:
    """Enough English to recognise the group; the keys beside it are its name.

    Three groups share their first 82 characters, so the excerpt alone does
    not always tell them apart — which is why each line leads with its places
    and carries this only as orientation.
    """
    return repr(text) if len(text) <= limit else f"{text[:limit]!r}…"


def _assert_one_wording_per_group(
    locale: str,
    groups: Sequence[tuple[str, tuple[tuple[str, str], ...]]],
    catalogs_for: Callable[[str], dict[str, dict[str, str]]] = _catalogs_by_surface,
) -> None:
    """Every place in a group renders one text, or name the ones that do not.

    ``catalogs_for`` is a seam: the shipped catalogs have no divergence left
    to show, so the failure path is driven from synthetic ones below.
    """
    catalogs = catalogs_for(locale)

    # The places are the identity and the English is context, not the other
    # way round: these are multi-sentence help strings, and leading with two
    # 400-character paragraphs buries the one thing the reader needs, which
    # is where to look.
    divergent: list[str] = []
    for english, where in groups:
        rendered: dict[str, list[str]] = {}
        for surface, key in where:
            value = catalogs[surface].get(key)
            label = f"{surface}/{locale}: {key}"
            if value is None:
                # Omission is legal for settings UI messages, and English is
                # the per-key fallback — so the screen still disagrees.
                value, label = english, f"{label} (missing, renders English)"
            rendered.setdefault(value, []).append(label)
        if len(rendered) > 1:
            divergent.append(
                " vs ".join(", ".join(labels) for labels in rendered.values())
                + f" — {_excerpt(english)}"
            )

    assert not divergent, (
        f"{locale} renders {len(divergent)} English string(s) differently "
        "depending on which surface they appear on. The English is "
        "byte-identical in each group, so there is no context to adapt to: "
        "pick one wording per group and use it on every surface — where a "
        "group has a settings UI member, that is the wording the others "
        "follow today.\n" + "\n".join(divergent)
    )


@pytest.mark.parametrize("locale", _translated_component_locales())
def test_one_english_string_reads_the_same_on_every_surface(locale: str) -> None:
    """Same sentence in the add-on options and the web UI, same translation.

    Each catalog was checked against English and nothing compared them to each
    other, so a translator working one surface at a time could — and did —
    write two different sentences for one switch: ``de`` phrased 25 of these
    groups differently between the add-on and the settings UI, ``zh-Hans`` 18.
    A reader who configures the add-on and then opens the panel meets the same
    option twice and has to work out whether it is the same option.

    Byte-identical English is the whole justification: there is no context to
    adapt to, or the English would have adapted first.
    """
    _assert_one_wording_per_group(locale, _shared_english_strings())


def test_one_wording_per_group_names_the_surface_that_disagrees() -> None:
    """No group diverges in any shipped locale, so real data cannot show this.

    The check asserts an absence, and a green run cannot tell an absence apart
    from a comparison that stopped comparing: read the locale's catalogs as
    English and nothing ever differs, or require three renderings instead of
    two and the 40 two-place groups drop out. Both keep the suite green on the
    shipped catalogs, so the failure path is driven from synthetic ones — the
    same answer as ``test_the_tools_share_counts_both_english_renderings``.
    """
    groups = (
        (
            "Read only mode",
            (("ui", "features.read_only.label"), ("addon", "read_only.name")),
        ),
    )
    # English agrees with itself, so a comparison that loads "en" instead of
    # the locale it was asked for finds nothing in any of the three sets.
    english: dict[str, dict[str, str]] = {
        "ui": {"features.read_only.label": "Read only mode"},
        "addon": {"read_only.name": "Read only mode"},
    }
    agreeing: dict[str, dict[str, str]] = {
        "ui": {"features.read_only.label": "Nur-Lese-Modus"},
        "addon": {"read_only.name": "Nur-Lese-Modus"},
    }
    _assert_one_wording_per_group(
        "xx", groups, {"xx": agreeing, "en": english}.__getitem__
    )

    disagreeing: dict[str, dict[str, str]] = {
        "ui": {"features.read_only.label": "Nur-Lese-Modus"},
        "addon": {"read_only.name": "Schreibgeschützter Modus"},
    }
    with pytest.raises(AssertionError, match="depending on which surface"):
        _assert_one_wording_per_group(
            "xx", groups, {"xx": disagreeing, "en": english}.__getitem__
        )

    # A place the catalog omits falls back to English while the other surface
    # is translated, which is the same disagreement on screen.
    omitted: dict[str, dict[str, str]] = {
        "ui": {"features.read_only.label": "Nur-Lese-Modus"},
        "addon": {},
    }
    with pytest.raises(AssertionError, match="renders English"):
        _assert_one_wording_per_group(
            "xx", groups, {"xx": omitted, "en": english}.__getitem__
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
def _english_tool_texts(*, as_rendered: bool = True) -> dict[str, str]:
    """English tool titles and descriptions, keyed like a flat catalog.

    The settings UI catalogs translate the title and the description's first
    line, so that is what a translated value is compared against.

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
            texts[f"{name}.description"] = stub["description"]
            continue
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
    obliges every tool-adding PR to touch five languages before it can go
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
    # tool and pasting its English title and description into all five locales
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
