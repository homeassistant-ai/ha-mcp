"""Machine-translate stale and missing catalog strings via the Gemini API.

The English-source baseline (``tests/src/unit/locale_source_baseline.json``)
pins the English every translation was written against. This script turns that
*detector* into an *updater*: keys whose English changed since the baseline are
retranslated in every language, keys a locale is missing are filled, and keys
English no longer has are deleted. It then regenerates the derived catalogs
(``scripts/generate_locales.py``) and rewrites the baseline, so the locale
parity suite goes green in the same commit.

Covered targets, all discovered from the catalogs themselves (adding a
language is adding its catalog files — no pipeline change; the target language
is described to the engine by the catalog's own ``meta.native_name``):

- ``src/ha_mcp/settings_ui/locales/<code>.json`` ``messages``
- the ``tools`` / ``tool_groups`` sections of those catalogs (English comes
  from the tool definitions via ``scripts/extract_tools.py``)
- ``custom_components/ha_mcp_tools/translations/<code>.json``

The engine is one function (``_call_gemini``): the Gemini API free tier, keyed
by ``GEMINI_API_KEY``. ``GEMINI_MODEL`` / ``GEMINI_API_URL`` override the
default model and endpoint, so swapping providers is a one-function change.
Every returned string is validated (placeholder parity, the settings UI markup
allowlist, panel-link parity) before it is written; a failure leaves that
string unwritten and the run red rather than shipping a broken translation.

Usage::

    python scripts/translate_locales.py            # translate, regenerate, repin
    python scripts/translate_locales.py --dry-run  # report the work, change nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_locales  # noqa: E402
from update_locale_baseline import _load_test_module  # noqa: E402

from ha_mcp.settings_ui._i18n import (  # noqa: E402
    _ALLOWED_TAGS_RE,
    _PANEL_LINK_RE,
    _PLACEHOLDER_RE,
    _TAG_LIKE_RE,
)

LOCALES_DIR = REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales"
COMPONENT_DIR = REPO_ROOT / "custom_components" / "ha_mcp_tools" / "translations"

SETTINGS_SURFACE = "src/ha_mcp/settings_ui/locales"
COMPONENT_SURFACE = "custom_components/ha_mcp_tools/translations"
TOOLS_SURFACE = "settings UI tool titles and descriptions"

DEFAULT_MODEL = "gemini-2.5-flash"
# Free-tier pacing: stay comfortably under the strictest published RPM.
_SECONDS_BETWEEN_REQUESTS = 7.0
# Chunks are bounded by source characters, not string count — help texts run
# to ~1500 characters each.
_MAX_CHARS_PER_REQUEST = 6000

_STYLE_SAMPLE_KEYS = (
    "notice.shared_settings",
    "features.read_only_mode.help",
    "tabs.server",
)


@dataclass
class WorkItem:
    """One string to translate for one locale."""

    locale: str
    section: str  # "messages" | "tools" | "tool_groups" | "component"
    key: str  # dotted target key ("<tool>.title" inside tools)
    english: str


@dataclass
class Plan:
    items: list[WorkItem] = field(default_factory=list)
    deletions: list[tuple[str, str, str]] = field(default_factory=list)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _changed_keys(module: Any) -> dict[str, set[str]]:
    """Per surface, the keys whose English hash moved since the baseline."""
    baseline = json.loads(module.BASELINE_PATH.read_text("utf-8"))
    current = module.english_sources()
    return {
        surface: {
            key
            for key, digest in hashes.items()
            if baseline.get(surface, {}).get(key) not in (None, digest)
        }
        for surface, hashes in current.items()
    }


def _target_locales() -> list[str]:
    return sorted(p.stem for p in LOCALES_DIR.glob("*.json") if p.stem != "en")


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, str):
        flat[prefix] = value
    return flat


def _unflatten(flat: dict[str, str]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for dotted, value in flat.items():
        node = root
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return root


def _plan_locale_messages(
    plan: Plan,
    locale: str,
    en_messages: dict[str, str],
    messages: dict[str, str],
    changed_messages: set[str],
) -> None:
    for key, text in en_messages.items():
        if key in changed_messages or key not in messages:
            plan.items.append(WorkItem(locale, "messages", key, text))
    for key in messages:
        if key not in en_messages:
            plan.deletions.append((locale, "messages", key))


def _plan_locale_tools(
    plan: Plan,
    locale: str,
    catalog: dict[str, Any],
    changed_tools: set[str],
    tool_texts: dict[str, str],
    tool_names: frozenset[str],
) -> None:
    tools: dict[str, dict[str, str]] = catalog.get("tools", {})
    for name in sorted(tool_names):
        entry = tools.get(name, {})
        for fld in ("title", "description"):
            tool_key = f"{name}.{fld}"
            if tool_key in changed_tools or fld not in entry:
                plan.items.append(
                    WorkItem(locale, "tools", tool_key, tool_texts[tool_key])
                )
    for name in tools:
        if name not in tool_names:
            plan.deletions.append((locale, "tools", name))


def _plan_locale_groups(
    plan: Plan, locale: str, catalog: dict[str, Any], groups: frozenset[str]
) -> None:
    tool_groups: dict[str, str] = catalog.get("tool_groups", {})
    for group in sorted(groups):
        if group not in tool_groups:
            plan.items.append(WorkItem(locale, "tool_groups", group, group))
    for group in tool_groups:
        if group not in groups:
            plan.deletions.append((locale, "tool_groups", group))


def _plan_settings(plan: Plan, module: Any, changed: dict[str, set[str]]) -> None:
    en_messages: dict[str, str] = _load_json(LOCALES_DIR / "en.json")["messages"]
    changed_messages = {
        key.removeprefix("messages.")
        for key in changed.get(SETTINGS_SURFACE, set())
        if key.startswith("messages.")
    }
    changed_tools = {
        key.removesuffix(" (parsed)") for key in changed.get(TOOLS_SURFACE, set())
    }
    tool_texts: dict[str, str] = dict(module._english_tool_texts())
    groups, tool_names = module._renderable_groups_and_tools()

    for locale in _target_locales():
        catalog = _load_json(LOCALES_DIR / f"{locale}.json")
        _plan_locale_messages(
            plan, locale, en_messages, catalog["messages"], changed_messages
        )
        _plan_locale_tools(plan, locale, catalog, changed_tools, tool_texts, tool_names)
        _plan_locale_groups(plan, locale, catalog, groups)


def _plan_component(plan: Plan, changed: dict[str, set[str]]) -> None:
    en_flat = _flatten(_load_json(COMPONENT_DIR / "en.json"))
    changed_component = changed.get(COMPONENT_SURFACE, set())
    for path in sorted(COMPONENT_DIR.glob("*.json")):
        if path.stem == "en":
            continue
        flat = _flatten(_load_json(path))
        for key, text in en_flat.items():
            if key in changed_component or key not in flat:
                plan.items.append(WorkItem(path.stem, "component", key, text))
        for key in flat:
            if key not in en_flat:
                plan.deletions.append((path.stem, "component", key))


def build_plan(module: Any) -> Plan:
    plan = Plan()
    changed = _changed_keys(module)
    _plan_settings(plan, module, changed)
    _plan_component(plan, changed)
    return plan


def _validate(item: WorkItem, translated: Any) -> str | None:
    """The reason a translation is unusable for this item, or None."""
    if not isinstance(translated, str) or not translated.strip():
        return "empty or non-string translation"
    if set(_PLACEHOLDER_RE.findall(item.english)) != set(
        _PLACEHOLDER_RE.findall(translated)
    ):
        return "placeholder set differs from English"
    if item.section == "messages":
        for tag in _TAG_LIKE_RE.findall(translated):
            if _ALLOWED_TAGS_RE.fullmatch(tag) is None:
                return f"markup {tag!r} not in the settings UI allowlist"
        if Counter(_PANEL_LINK_RE.findall(translated)) != Counter(
            _PANEL_LINK_RE.findall(item.english)
        ):
            return "panel-link targets differ from English"
    return None


def _language_label(locale: str) -> str:
    catalog = _load_json(LOCALES_DIR / f"{locale}.json")
    native = catalog.get("meta", {}).get("native_name", locale)
    return f"{native} ({locale})"


def _style_samples(locale: str) -> str:
    catalog = _load_json(LOCALES_DIR / f"{locale}.json")
    english = _load_json(LOCALES_DIR / "en.json")["messages"]
    samples = [
        f"EN: {english[key]}\n{locale.upper()}: {catalog['messages'][key]}"
        for key in _STYLE_SAMPLE_KEYS
        if key in catalog.get("messages", {}) and key in english
    ]
    return "\n\n".join(samples)


def _prompt(locale: str, batch: dict[str, str]) -> str:
    samples = _style_samples(locale)
    style = (
        f"Match the tone and terminology of these existing translations:\n{samples}\n\n"
        if samples
        else ""
    )
    return (
        "You are the automated translation pipeline of the ha-mcp project "
        "(an MCP server for Home Assistant). Translate the following UI "
        f"strings from English into {_language_label(locale)}.\n"
        "Rules:\n"
        "- Respond with ONLY a JSON object carrying exactly the same keys; "
        "each value is the translation of that key's string.\n"
        "- Preserve every {placeholder} token exactly as written.\n"
        "- Preserve inline HTML tags exactly (<code>, <strong>, "
        '<a href="#" data-panel-link="...">, </a>); translate only the '
        "human-readable text around and inside them.\n"
        "- Do not translate: product and client names (Home Assistant, "
        "HA-MCP, Supervisor, claude.ai, Claude Desktop, ChatGPT, Gemini, "
        "GitHub Copilot), MCP tool names (ha_*), environment variables, "
        "file paths, URLs, configuration keys, and any text in double "
        "quotes that names an on-screen option or a literal value.\n"
        "- Keep the ⚠ and ⚠️ symbols where English has them.\n\n"
        f"{style}"
        "Strings to translate (JSON):\n"
        f"{json.dumps(batch, ensure_ascii=False, indent=1)}"
    )


def _call_gemini(prompt: str) -> dict[str, Any]:
    """One engine call. The single place a provider swap would touch."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is not set but there are strings to translate")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    url = os.environ.get(
        "GEMINI_API_URL",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "response_mime_type": "application/json",
        },
    }
    last_error = ""
    for attempt in range(1, 6):
        response = httpx.post(
            url, json=body, headers={"x-goog-api-key": api_key}, timeout=180
        )
        if response.status_code == 200:
            payload = response.json()
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            parsed: dict[str, Any] = json.loads(text)
            return parsed
        last_error = f"HTTP {response.status_code}: {response.text[:300]}"
        if response.status_code in (429, 500, 502, 503, 504):
            delay = 20 * attempt
            print(f"  engine {last_error}; retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
            continue
        break
    raise SystemExit(f"Gemini API call failed: {last_error}")


def _chunk(batch: dict[str, str]) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    size = 0
    for key, text in batch.items():
        if current and size + len(text) > _MAX_CHARS_PER_REQUEST:
            chunks.append(current)
            current, size = {}, 0
        current[key] = text
        size += len(text)
    if current:
        chunks.append(current)
    return chunks


def _accept_or_retry(
    locale: str,
    string_id: str,
    english: str,
    targets: list[WorkItem],
    translated: Any,
    results: dict[tuple[str, str], str],
    failures: list[str],
) -> None:
    """Apply one translated string to every target it validates for."""
    rejected = [
        (item, reason) for item in targets if (reason := _validate(item, translated))
    ]
    if rejected:
        hint = "; ".join(sorted({reason for _, reason in rejected}))
        retry = _call_gemini(
            _prompt(locale, {string_id: english})
            + f"\n\nYour previous attempt was rejected: {hint}. "
            "Fix that and return the corrected JSON."
        )
        time.sleep(_SECONDS_BETWEEN_REQUESTS)
        translated = retry.get(string_id)
    for item in targets:
        reason = _validate(item, translated)
        if reason is None:
            results[(item.section, item.key)] = str(translated)
        else:
            failures.append(f"{locale}/{item.section}/{item.key}: {reason}")


def _translate_locale(
    locale: str, items: list[WorkItem]
) -> tuple[dict[tuple[str, str], str], list[str]]:
    """Translate one locale's strings, deduplicated by English text.

    Returns ``{(section, key): translation}`` plus a list of failures. One
    English string translated once is applied to every target that shares it,
    keeping cross-surface wording consistent by construction.
    """
    unique: dict[str, list[WorkItem]] = {}
    for item in items:
        unique.setdefault(item.english, []).append(item)
    by_id = {f"s{index}": text for index, text in enumerate(unique)}

    results: dict[tuple[str, str], str] = {}
    failures: list[str] = []
    for chunk in _chunk(by_id):
        response = _call_gemini(_prompt(locale, chunk))
        time.sleep(_SECONDS_BETWEEN_REQUESTS)
        for string_id, english in chunk.items():
            _accept_or_retry(
                locale,
                string_id,
                english,
                unique[english],
                response.get(string_id),
                results,
                failures,
            )
    return results, failures


def _delete_from_settings(
    catalog: dict[str, Any], locale: str, deletions: list[tuple[str, str, str]]
) -> None:
    for own_locale, section, key in deletions:
        if own_locale != locale:
            continue
        if section == "messages":
            catalog["messages"].pop(key, None)
        elif section == "tools":
            catalog.get("tools", {}).pop(key, None)
        elif section == "tool_groups":
            catalog.get("tool_groups", {}).pop(key, None)


def _write_into_settings(
    catalog: dict[str, Any], translations: dict[tuple[str, str], str]
) -> None:
    for (section, key), value in translations.items():
        if section == "messages":
            catalog["messages"][key] = value
        elif section == "tool_groups":
            catalog.setdefault("tool_groups", {})[key] = value
        elif section == "tools":
            name, fld = key.rsplit(".", 1)
            catalog.setdefault("tools", {}).setdefault(name, {})[fld] = value


def _apply_settings(
    locale: str,
    translations: dict[tuple[str, str], str],
    deletions: list[tuple[str, str, str]],
) -> None:
    path = LOCALES_DIR / f"{locale}.json"
    catalog = _load_json(path)
    en_messages: dict[str, str] = _load_json(LOCALES_DIR / "en.json")["messages"]
    before = json.dumps(catalog, sort_keys=True, ensure_ascii=False)
    _delete_from_settings(catalog, locale, deletions)
    _write_into_settings(catalog, translations)

    # Deterministic ordering: messages mirror en.json's key order, the tool
    # sections sort by their own keys.
    catalog["messages"] = {
        key: catalog["messages"][key]
        for key in en_messages
        if key in catalog["messages"]
    }
    catalog["tools"] = dict(sorted(catalog.get("tools", {}).items()))
    catalog["tool_groups"] = dict(sorted(catalog.get("tool_groups", {}).items()))

    if json.dumps(catalog, sort_keys=True, ensure_ascii=False) != before:
        _dump_json(path, catalog)
        print(f"updated {path.relative_to(REPO_ROOT)}")


def _apply_component(
    locale: str,
    translations: dict[tuple[str, str], str],
    deletions: list[tuple[str, str, str]],
) -> None:
    path = COMPONENT_DIR / f"{locale}.json"
    if not path.exists():
        return
    flat = _flatten(_load_json(path))
    before = dict(flat)
    for own_locale, section, key in deletions:
        if own_locale == locale and section == "component":
            flat.pop(key, None)
    for (section, key), value in translations.items():
        if section == "component":
            flat[key] = value
    en_flat = _flatten(_load_json(COMPONENT_DIR / "en.json"))
    ordered = {key: flat[key] for key in en_flat if key in flat}
    if ordered != before:
        _dump_json(path, _unflatten(ordered))
        print(f"updated {path.relative_to(REPO_ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report planned work without calling the engine or writing files",
    )
    args = parser.parse_args()

    module = _load_test_module()
    plan = build_plan(module)
    by_locale: dict[str, list[WorkItem]] = {}
    for item in plan.items:
        by_locale.setdefault(item.locale, []).append(item)

    print(
        f"{len(plan.items)} string(s) to translate across "
        f"{len(by_locale)} locale(s); {len(plan.deletions)} orphan deletion(s)"
    )
    if args.dry_run:
        for item in plan.items[:40]:
            print(f"  {item.locale}/{item.section}/{item.key}")
        if len(plan.items) > 40:
            print(f"  ... and {len(plan.items) - 40} more")
        for locale, section, key in plan.deletions:
            print(f"  delete {locale}/{section}/{key}")
        return 0

    failures: list[str] = []
    for locale in _target_locales():
        results: dict[tuple[str, str], str] = {}
        if locale in by_locale:
            results, locale_failures = _translate_locale(locale, by_locale[locale])
            failures += locale_failures
        _apply_settings(locale, results, plan.deletions)
        _apply_component(locale, results, plan.deletions)

    generate_locales.write()

    if failures:
        print(
            "translation failures (baseline left stale so the run stays red):",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 2

    fresh = _load_test_module()
    fresh.BASELINE_PATH.write_text(
        json.dumps(fresh.english_sources(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"repinned {fresh.BASELINE_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
