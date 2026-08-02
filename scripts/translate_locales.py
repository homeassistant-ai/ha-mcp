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

The engine is one function (``_call_gemini``): the Gemini API free tier,
keyed by ``GEMINI_API_KEY``, with ``GEMINI_MODEL`` / ``GEMINI_API_URL``
overrides for any Gemini-compatible endpoint. The fallback when the engine is
down is a HUMAN: hand-edit the translations, run
``python scripts/generate_locales.py`` and
``python scripts/update_locale_baseline.py``, and push — the next pipeline
run sees nothing stale and no-ops. Hand-edits always win; the machine only
touches strings whose English changed.
Every returned string is validated (placeholder parity, the settings UI markup
allowlist, formatting-tag parity, panel-link parity) before it is written; a
failure leaves that string unwritten and the run red rather than shipping a
broken translation.

Rate limits and outages: requests are paced under the free-tier rate and
retry transient errors with backoff; a persistently failing batch marks its
strings failed and the run continues, and two consecutive dead batches stop
the run early. Partial runs are resumable — completed work is recorded in
``tests/src/unit/locale_sync_progress.json`` and skipped on the rerun; only a
fully successful run repins the baseline and deletes that record, so the
parity suite stays red until every string is translated.

Usage::

    python scripts/translate_locales.py            # translate, regenerate, repin
    python scripts/translate_locales.py --dry-run  # report the work, change nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Container
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_locales  # type: ignore[import-not-found]  # noqa: E402
from update_locale_baseline import (  # type: ignore[import-not-found]  # noqa: E402
    _load_test_module,
)

from ha_mcp.settings_ui._i18n import (  # noqa: E402
    _ALLOWED_TAGS_RE,
    _PANEL_LINK_RE,
    _PLACEHOLDER_RE,
    _TAG_LIKE_RE,
)

LOCALES_DIR = REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales"
COMPONENT_DIR = REPO_ROOT / "custom_components" / "ha_mcp_tools" / "translations"
# Committed by the workflow after a PARTIAL run: which locales already
# received each changed string, so a rerun (quota hit, crash) resumes instead
# of re-spending the budget retranslating the early locales. Deleted again by
# the first fully successful run.
PROGRESS_PATH = REPO_ROOT / "tests" / "src" / "unit" / "locale_sync_progress.json"

SETTINGS_SURFACE = "src/ha_mcp/settings_ui/locales"
COMPONENT_SURFACE = "custom_components/ha_mcp_tools/translations"
TOOLS_SURFACE = "settings UI tool titles and descriptions"

DEFAULT_MODEL = "gemini-2.5-flash"
# Free-tier pacing: stay comfortably under the strictest published RPM.
_SECONDS_BETWEEN_REQUESTS = 7.0
# Self-imposed wall-clock bound, kept below the workflow's job timeout: when
# it trips, the run degrades exactly like a quota hit — remaining strings
# marked failed, finished work written, progress recorded, resumable — where
# a job-level cancellation would lose everything uncommitted.
_TIME_BUDGET_SECONDS = float(os.environ.get("TRANSLATE_TIME_BUDGET_SECONDS", "2400"))
_DEADLINE: float | None = None


def _out_of_time() -> bool:
    return _DEADLINE is not None and time.monotonic() > _DEADLINE


# Chunks are bounded by source characters, not string count — help texts run
# to ~1500 characters each.
_MAX_CHARS_PER_REQUEST = 6000

# The style samples are the only signal the engine gets about how a catalog
# addresses its reader — du/Sie, tu/vous, 你/您 — and nothing downstream can
# recover it: placeholder, markup and key-parity validation pass either way,
# and the ceilings only count text left untranslated. Samples whose English
# does not address the reader leave the model on its own default for the
# language, which is how formal German reaches a catalog that addresses its
# reader informally in every string of its own. Prefer English sources that
# address the reader, shortest first so the per-request overhead stays small.
_SECOND_PERSON_RE = re.compile(
    r"\b(?:you|your|yours|yourself|yourselves)\b", re.IGNORECASE
)
_STYLE_SAMPLE_COUNT = 3


# Closed set: the apply functions branch on this and raise on anything else,
# so a typo'd section fails loudly instead of silently dropping a translation
# or leaving an orphan behind.
Section = Literal["messages", "tools", "tool_groups", "component"]


class ItemRef(NamedTuple):
    """One (locale, section, key) address — shared by deletions and errors."""

    locale: str
    section: Section
    key: str


@dataclass
class WorkItem:
    """One string to translate for one locale."""

    locale: str
    section: Section
    key: str  # dotted target key ("<tool>.title" inside tools)
    english: str
    # True when queued because the ENGLISH changed (vs. filling a missing
    # key); only these are worth recording in the resumable-progress file —
    # a filled key's presence in the catalog is its own progress marker.
    changed: bool = False


@dataclass
class Plan:
    items: list[WorkItem] = field(default_factory=list)
    deletions: list[ItemRef] = field(default_factory=list)


def _load_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _dump_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _progress_load() -> dict[str, Any]:
    if not PROGRESS_PATH.exists():
        return {}
    data: dict[str, Any] = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    return data


def _progress_done(
    progress: dict[str, Any], section: str, key: str, english: str, locale: str
) -> bool:
    """Whether ``locale`` already received ``key`` for the CURRENT English."""
    entry = progress.get(f"{section}|{key}")
    return (
        isinstance(entry, dict)
        and entry.get("english_sha") == _text_sha(english)
        and locale in entry.get("locales", [])
    )


def _record_progress(completed: dict[tuple[str, str], set[str]], plan: Plan) -> None:
    """Persist which locales received each changed string this partial run."""
    english_by_ref: dict[tuple[str, str], str] = {
        (item.section, item.key): item.english for item in plan.items if item.changed
    }
    progress = _progress_load()
    for (section, key), locales in completed.items():
        english = english_by_ref.get((section, key))
        if english is None:
            continue
        entry_key = f"{section}|{key}"
        entry = progress.get(entry_key)
        sha = _text_sha(english)
        if isinstance(entry, dict) and entry.get("english_sha") == sha:
            entry["locales"] = sorted(set(entry.get("locales", [])) | locales)
        else:
            progress[entry_key] = {"english_sha": sha, "locales": sorted(locales)}
    PROGRESS_PATH.write_text(
        json.dumps(progress, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"recorded partial progress in {PROGRESS_PATH.relative_to(REPO_ROOT)}")


def _clear_progress() -> None:
    if PROGRESS_PATH.exists():
        PROGRESS_PATH.unlink()
        print(f"cleared {PROGRESS_PATH.relative_to(REPO_ROOT)}")


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
    """Flatten a catalog to dotted keys, refusing shapes it cannot round-trip.

    ``_apply_component`` rewrites whole files through this flatten/unflatten
    pair, so a silently dropped leaf (a list-valued selector option, say)
    would be silently DELETED from every translated catalog by an unattended
    CI run. Fail loudly instead; extend the round-trip when a non-string leaf
    legitimately appears.
    """
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, str):
        flat[prefix] = value
    else:
        raise ValueError(
            f"catalog leaf {prefix!r} is {type(value).__name__}, not str — "
            "the translate pipeline only round-trips string leaves"
        )
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
            plan.items.append(
                WorkItem(locale, "messages", key, text, changed=key in changed_messages)
            )
    for key in messages:
        if key not in en_messages:
            plan.deletions.append(ItemRef(locale=locale, section="messages", key=key))


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
                    WorkItem(
                        locale,
                        "tools",
                        tool_key,
                        tool_texts[tool_key],
                        changed=tool_key in changed_tools,
                    )
                )
    for name in tools:
        if name not in tool_names:
            plan.deletions.append(ItemRef(locale=locale, section="tools", key=name))


def _plan_locale_groups(
    plan: Plan, locale: str, catalog: dict[str, Any], groups: frozenset[str]
) -> None:
    tool_groups: dict[str, str] = catalog.get("tool_groups", {})
    for group in sorted(groups):
        if group not in tool_groups:
            plan.items.append(WorkItem(locale, "tool_groups", group, group))
    for group in tool_groups:
        if group not in groups:
            plan.deletions.append(
                ItemRef(locale=locale, section="tool_groups", key=group)
            )


def _plan_settings(plan: Plan, module: Any, changed: dict[str, set[str]]) -> None:
    en_messages: dict[str, str] = _load_json(LOCALES_DIR / "en.json")["messages"]
    changed_messages = {
        key.removeprefix("messages.")
        for key in changed.get(SETTINGS_SURFACE, set())
        if key.startswith("messages.")
    }
    # A " (parsed)"-suffixed baseline key pins a feature-gated tool's parsed
    # docstring; the catalogs translate the hand-written stub, so a
    # parsed-only change is no translation work — retranslating would
    # overwrite every locale with a fresh translation of the unchanged stub.
    # It IS review work (both renderings reach readers, flag-dependent), so
    # _repin_baseline deliberately keeps such keys stale until a human
    # confirms the stub and repins manually.
    changed_tools = {
        key
        for key in changed.get(TOOLS_SURFACE, set())
        if not key.endswith(" (parsed)")
    }
    tool_texts: dict[str, str] = dict(module._english_tool_texts())
    groups, tool_names = module._renderable_groups_and_tools()

    progress = _progress_load()
    for locale in _target_locales():
        catalog = _load_json(LOCALES_DIR / f"{locale}.json")
        # A changed key a previous partial run already translated for this
        # locale (same English) is not re-queued — resumability after a
        # quota hit or crash, without repinning unfinished keys.
        loc_changed_messages = {
            key
            for key in changed_messages
            if not _progress_done(progress, "messages", key, en_messages[key], locale)
        }
        loc_changed_tools = {
            key
            for key in changed_tools
            if not _progress_done(progress, "tools", key, tool_texts[key], locale)
        }
        _plan_locale_messages(
            plan, locale, en_messages, catalog.get("messages", {}), loc_changed_messages
        )
        _plan_locale_tools(
            plan, locale, catalog, loc_changed_tools, tool_texts, tool_names
        )
        _plan_locale_groups(plan, locale, catalog, groups)


def _plan_component(plan: Plan, changed: dict[str, set[str]]) -> None:
    en_flat = _flatten(_load_json(COMPONENT_DIR / "en.json"))
    changed_component = changed.get(COMPONENT_SURFACE, set())
    progress = _progress_load()
    for path in sorted(COMPONENT_DIR.glob("*.json")):
        if path.stem == "en":
            continue
        flat = _flatten(_load_json(path))
        loc_changed = {
            key
            for key in changed_component
            if not _progress_done(progress, "component", key, en_flat[key], path.stem)
        }
        for key, text in en_flat.items():
            if key in loc_changed or key not in flat:
                plan.items.append(
                    WorkItem(
                        path.stem, "component", key, text, changed=key in loc_changed
                    )
                )
        for key in flat:
            if key not in en_flat:
                plan.deletions.append(
                    ItemRef(locale=path.stem, section="component", key=key)
                )


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
        # Multiset parity, like panel links: a dropped </strong> or an
        # invented <code> is all-allowlisted yet malforms what tHtml restores.
        if Counter(_TAG_LIKE_RE.findall(translated)) != Counter(
            _TAG_LIKE_RE.findall(item.english)
        ):
            return "formatting-tag set differs from English"
        if Counter(_PANEL_LINK_RE.findall(translated)) != Counter(
            _PANEL_LINK_RE.findall(item.english)
        ):
            return "panel-link targets differ from English"
    return None


def _language_label(locale: str) -> str:
    catalog = _load_json(LOCALES_DIR / f"{locale}.json")
    native = catalog.get("meta", {}).get("native_name", locale)
    return f"{native} ({locale})"


def _style_sample_keys(
    english: dict[str, str],
    translated: dict[str, str],
    exclude: Container[str] = frozenset(),
) -> list[str]:
    """The keys whose sample pair shows this catalog's address register.

    ``exclude`` drops the keys of the request being built. A key is in a
    request only because its English moved or it is missing, so its committed
    translation renders the PREVIOUS English: pairing it with the new source
    presents a mismatch as the thing to imitate, and when the key is itself in
    the batch the model answers with the stale text it was just shown — which
    validates, gets written, and repins the baseline as if it were current.
    """
    return sorted(
        (
            key
            for key, text in english.items()
            if key in translated
            and key not in exclude
            and _SECOND_PERSON_RE.search(text)
        ),
        key=lambda key: (len(english[key]), key),
    )[:_STYLE_SAMPLE_COUNT]


def _style_samples(locale: str, exclude: Container[str] = frozenset()) -> str:
    catalog = _load_json(LOCALES_DIR / f"{locale}.json")
    english = _load_json(LOCALES_DIR / "en.json")["messages"]
    translated: dict[str, str] = catalog.get("messages", {})
    return "\n\n".join(
        f"EN: {english[key]}\n{locale.upper()}: {translated[key]}"
        for key in _style_sample_keys(english, translated, exclude)
    )


def _prompt(
    locale: str, batch: dict[str, str], queued_message_keys: Container[str]
) -> str:
    """One request. ``queued_message_keys`` is every ``messages`` key this run
    still has to translate — not just this chunk's. Results are applied after
    all chunks finish, so a key queued in a later chunk still has its OLD
    translation on disk while this one is prompted; sampling it would show a
    stale pair as the thing to imitate."""
    samples = _style_samples(locale, queued_message_keys)
    style = (
        f"Match the tone and terminology of these existing translations:\n{samples}\n\n"
        if samples
        else ""
    )
    # Carried by the samples, but not read off them on its own: with samples
    # that address the reader and no rule, a single-string request still came
    # back formal for a catalog that is informal throughout. Both halves are
    # load-bearing — the rule points at the samples, so it says nothing
    # without them. Worded as a majority rather than "exactly": a catalog can
    # be internally inconsistent (fr mixes tu and vous today), and telling the
    # model to imitate contradictory samples exactly is an instruction it
    # cannot follow.
    register_rule = (
        "- Address the reader the way the sample translations predominantly "
        "do; if they disagree, follow the form most of them use and apply it "
        "consistently.\n"
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
        "- The keys are stable identifiers that also name where the string "
        "appears in the UI (section:dotted.key); use them as context for "
        "wording and grammatical agreement, and return them unchanged.\n"
        "- Preserve every {placeholder} token exactly as written.\n"
        "- Preserve inline HTML tags exactly (<code>, <strong>, "
        '<a href="#" data-panel-link="...">, </a>); translate only the '
        "human-readable text around and inside them.\n"
        "- Do not translate: product and client names (Home Assistant, "
        "HA-MCP, Supervisor, claude.ai, Claude Desktop, ChatGPT, Gemini, "
        "GitHub Copilot), MCP tool names (ha_*), environment variables, "
        "file paths, URLs, configuration keys, and any text in double "
        "quotes that names an on-screen option or a literal value.\n"
        f"{register_rule}"
        "- Keep the ⚠ and ⚠️ symbols where English has them.\n\n"
        f"{style}"
        "Strings to translate (JSON):\n"
        f"{json.dumps(batch, ensure_ascii=False, indent=1)}"
    )


def _call_gemini(prompt: str) -> dict[str, Any]:
    """One engine call. The single place a provider swap would touch."""
    # Deadline-guarded at entry AND before every retry sleep: a worst-case
    # call (five 180s timeouts plus backoff) runs ~20 minutes, which would
    # otherwise blow straight through the time budget's slack and into the
    # job timeout — the one path that loses uncommitted work.
    if _out_of_time():
        raise SystemExit("time budget exhausted before the engine call")
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
        try:
            response = httpx.post(
                url, json=body, headers={"x-goog-api-key": api_key}, timeout=180
            )
        except httpx.RequestError as exc:
            # Connect/read timeouts, DNS failures, resets — as transient as a
            # 5xx; retry with the same bounded backoff.
            last_error = f"transport error: {exc!r}"
            if _out_of_time():
                last_error += " (time budget exhausted; not retrying)"
                break
            delay = 20 * attempt
            print(f"  engine {last_error}; retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
            continue
        if response.status_code == 200:
            payload = response.json()
            try:
                text = payload["candidates"][0]["content"]["parts"][0]["text"]
                parsed: dict[str, Any] = json.loads(text)
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                # An empty/filtered candidate list or non-JSON answer should
                # read like the HTTP failures, not a raw traceback.
                raise SystemExit(
                    f"Gemini API returned an unusable response ({exc!r}): "
                    f"{json.dumps(payload)[:300]}"
                ) from exc
            return parsed
        last_error = f"HTTP {response.status_code}: {response.text[:300]}"
        if response.status_code in (429, 500, 502, 503, 504):
            if _out_of_time():
                last_error += " (time budget exhausted; not retrying)"
                break
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


def _string_id(item: WorkItem) -> str:
    """The JSON identifier a string travels under — also its UI context."""
    return f"{item.section}:{item.key}"


def _accept_or_retry(
    locale: str,
    item: WorkItem,
    translated: Any,
    results: dict[tuple[str, str], str],
    failures: list[str],
    failed: set[tuple[str, str]],
    queued_message_keys: Container[str],
) -> None:
    """Validate one translated string, retrying once with the reason."""
    reason = _validate(item, translated)
    if reason is not None:
        try:
            retry = _call_gemini(
                _prompt(locale, {_string_id(item): item.english}, queued_message_keys)
                + f"\n\nYour previous attempt was rejected: {reason}. "
                "Fix that and return the corrected JSON."
            )
        except SystemExit as exc:
            # The retry is best-effort; an engine failure here degrades to the
            # original (rejected) answer failing validation below.
            print(f"  retry call failed: {exc}", file=sys.stderr)
        else:
            translated = retry.get(_string_id(item))
        time.sleep(_SECONDS_BETWEEN_REQUESTS)
        reason = _validate(item, translated)
    if reason is None:
        results[(item.section, item.key)] = str(translated)
    else:
        failures.append(f"{locale}/{item.section}/{item.key}: {reason}")
        failed.add((item.section, item.key))


# Consecutive chunk-level engine failures before the run stops calling the
# engine at all. One failure can be content-specific (e.g. a safety-filter
# block on one chunk's strings); two in a row reads as systemic (quota
# exhausted, endpoint down), where further calls only burn retry backoff.
_ENGINE_GIVE_UP_AFTER = 2


def _mark_unattempted(
    locale: str,
    items: list[WorkItem],
    results: dict[tuple[str, str], str],
    failures: list[str],
    failed: set[tuple[str, str]],
) -> None:
    """Record every still-unhandled item so nothing slips the report."""
    for item in items:
        ref = (item.section, item.key)
        if ref not in results and ref not in failed:
            failures.append(
                f"{locale}/{item.section}/{item.key}: not attempted — "
                "engine gave up or time ran out earlier in this run"
            )
            failed.add(ref)


def _translate_locale(
    locale: str, items: list[WorkItem]
) -> tuple[dict[tuple[str, str], str], list[str], set[tuple[str, str]], bool]:
    """Translate one locale's strings with their keys as context.

    Returns ``{(section, key): translation}``, human-readable failures, the
    failed ``(section, key)`` set, and whether the engine was declared dead
    for the rest of the run. Strings travel under their ``section:key``
    identifiers rather than deduplicated by English text — the same English
    may legitimately need different wording per key (Spanish genders
    "enabled" as ``activado``/``activada`` depending on the noun), and the
    key gives the model that context. The one pair *required* to share
    wording across the authored surfaces is aligned afterwards by
    ``_align_authored_shared``. A chunk-level engine failure marks that
    chunk's strings failed and moves on, so one blocked or truncated batch
    cannot take down the whole run.
    """
    by_id = {_string_id(item): item for item in items}
    batch = {sid: item.english for sid, item in by_id.items()}
    # Every message key this locale still owes, so no chunk samples a key
    # another chunk is about to rewrite (its catalog entry is still the
    # translation of the PREVIOUS English until the run applies results).
    queued_message_keys = {item.key for item in items if item.section == "messages"}

    results: dict[tuple[str, str], str] = {}
    failures: list[str] = []
    failed: set[tuple[str, str]] = set()
    dead = False
    consecutive_engine_failures = 0
    for chunk in _chunk(batch):
        if dead:
            break
        if _out_of_time():
            failures.append(f"{locale}: time budget exhausted — stopping this run")
            dead = True
            break
        try:
            response = _call_gemini(_prompt(locale, chunk, queued_message_keys))
        except SystemExit as exc:
            consecutive_engine_failures += 1
            for sid in chunk:
                item = by_id[sid]
                failures.append(
                    f"{locale}/{item.section}/{item.key}: engine call failed ({exc})"
                )
                failed.add((item.section, item.key))
            if consecutive_engine_failures >= _ENGINE_GIVE_UP_AFTER:
                dead = True
            continue
        consecutive_engine_failures = 0
        time.sleep(_SECONDS_BETWEEN_REQUESTS)
        for sid in chunk:
            _accept_or_retry(
                locale,
                by_id[sid],
                response.get(sid),
                results,
                failures,
                failed,
                queued_message_keys,
            )
    if dead:
        _mark_unattempted(locale, items, results, failures, failed)
    return results, failures, failed, dead


def _delete_from_settings(
    catalog: dict[str, Any], locale: str, deletions: list[ItemRef]
) -> None:
    for own_locale, section, key in deletions:
        if own_locale != locale:
            continue
        if section == "messages":
            catalog.get("messages", {}).pop(key, None)
        elif section == "tools":
            catalog.get("tools", {}).pop(key, None)
        elif section == "tool_groups":
            catalog.get("tool_groups", {}).pop(key, None)
        elif section == "component":
            pass  # applied by _apply_component
        else:
            raise ValueError(f"unknown section {section!r} for deletion {key!r}")


def _write_into_settings(
    catalog: dict[str, Any], translations: dict[tuple[str, str], str]
) -> None:
    for (section, key), value in translations.items():
        if section == "messages":
            catalog.setdefault("messages", {})[key] = value
        elif section == "tool_groups":
            catalog.setdefault("tool_groups", {})[key] = value
        elif section == "tools":
            name, fld = key.rsplit(".", 1)
            catalog.setdefault("tools", {}).setdefault(name, {})[fld] = value
        elif section == "component":
            pass  # applied by _apply_component
        else:
            raise ValueError(f"unknown section {section!r} for key {key!r}")


def _apply_settings(
    locale: str,
    translations: dict[tuple[str, str], str],
    deletions: list[ItemRef],
) -> None:
    path = LOCALES_DIR / f"{locale}.json"
    catalog = _load_json(path)
    en_messages: dict[str, str] = _load_json(LOCALES_DIR / "en.json")["messages"]
    before = json.dumps(catalog, sort_keys=True, ensure_ascii=False)
    _delete_from_settings(catalog, locale, deletions)
    _write_into_settings(catalog, translations)

    # Deterministic ordering: messages mirror en.json's key order, the tool
    # sections sort by their own keys.
    messages = catalog.get("messages", {})
    catalog["messages"] = {key: messages[key] for key in en_messages if key in messages}
    catalog["tools"] = dict(sorted(catalog.get("tools", {}).items()))
    catalog["tool_groups"] = dict(sorted(catalog.get("tool_groups", {}).items()))

    if json.dumps(catalog, sort_keys=True, ensure_ascii=False) != before:
        _dump_json(path, catalog)
        print(f"updated {path.relative_to(REPO_ROOT)}")


def _apply_component(
    locale: str,
    translations: dict[tuple[str, str], str],
    deletions: list[ItemRef],
) -> None:
    path = COMPONENT_DIR / f"{locale}.json"
    if not path.exists():
        return
    flat = _flatten(_load_json(path))
    before = dict(flat)
    for ref in deletions:
        if ref.locale == locale and ref.section == "component":
            flat.pop(ref.key, None)
    for (target_section, target_key), value in translations.items():
        if target_section == "component":
            flat[target_key] = value
    en_flat = _flatten(_load_json(COMPONENT_DIR / "en.json"))
    ordered = {key: flat[key] for key in en_flat if key in flat}
    if ordered != before:
        _dump_json(path, _unflatten(ordered))
        print(f"updated {path.relative_to(REPO_ROOT)}")


def _align_authored_shared(
    locale: str, results: dict[tuple[str, str], str], module: Any
) -> None:
    """Byte-align the wording shared across the two authored surfaces.

    Per-key contextual translation may word the settings and component copies
    of one shared English string differently;
    ``test_authored_shared_strings_read_the_same`` requires them identical.
    The settings side wins (the historical rule); when only the component
    side was retranslated this run, the locale's existing settings
    translation is the reference.
    """
    for _english, where in module._authored_shared_groups():
        settings_keys = [
            key.removeprefix("messages.")
            for surface, key in where
            if surface == SETTINGS_SURFACE and key.startswith("messages.")
        ]
        component_keys = [key for surface, key in where if surface == COMPONENT_SURFACE]
        if not settings_keys:
            continue
        value = results.get(("messages", settings_keys[0]))
        if value is None:
            catalog = _load_json(LOCALES_DIR / f"{locale}.json")
            value = catalog.get("messages", {}).get(settings_keys[0])
        if value is None:
            continue
        for key in settings_keys[1:]:
            if ("messages", key) in results:
                results[("messages", key)] = value
        for key in component_keys:
            if ("component", key) in results:
                results[("component", key)] = value


def _translate_and_apply(
    plan: Plan, by_locale: dict[str, list[WorkItem]], module: Any
) -> tuple[list[str], dict[tuple[str, str], set[str]]]:
    """Translate every locale's planned work and write the catalogs.

    Returns the failures plus ``{(section, key): locales}`` for every
    *changed* string that was successfully written — the raw material for the
    resumable-progress file when the run is partial.
    """
    failures: list[str] = []
    completed: dict[tuple[str, str], set[str]] = {}
    engine_dead = False
    try:
        for locale in _target_locales():
            results: dict[tuple[str, str], str] = {}
            if locale in by_locale and engine_dead:
                failures.append(
                    f"{locale}: {len(by_locale[locale])} string(s) skipped — "
                    "the engine was declared unavailable earlier in this run"
                )
            elif locale in by_locale:
                results, locale_failures, _failed, engine_dead = _translate_locale(
                    locale, by_locale[locale]
                )
                failures += locale_failures
                _align_authored_shared(locale, results, module)
                for item in by_locale[locale]:
                    if item.changed and (item.section, item.key) in results:
                        completed.setdefault((item.section, item.key), set()).add(
                            locale
                        )
            _apply_settings(locale, results, plan.deletions)
            _apply_component(locale, results, plan.deletions)
    finally:
        # Whatever happened above — full success, partial fill, engine death,
        # or a crash — the derived catalogs must match the canonical store as
        # it now stands on disk, because the workflow commits partial
        # progress. Without this, a died-halfway run would auto-commit a
        # state that fails test_derived_catalogs_match_the_canonical_store.
        generate_locales.write()
    return failures, completed


def _repin_baseline(module: Any) -> None:
    """Rewrite the baseline — except keys awaiting a human's stub review.

    A change to the PARSED docstring of a feature-gated tool is editorial
    work, not translation work: the catalogs translate the hand-written stub,
    so nothing on screen went stale, but someone must confirm the stub still
    describes the tool. Automation must not wave that through — changed
    ``" (parsed)"`` keys keep their OLD hash here, leaving
    ``test_translations_are_checked_against_current_english`` red until a
    human confirms and runs ``scripts/update_locale_baseline.py`` (the manual
    repin, which is that confirmation).
    """
    current = module.english_sources()
    old = json.loads(module.BASELINE_PATH.read_text("utf-8"))
    held: list[str] = []
    for surface, hashes in current.items():
        for key, old_hash in old.get(surface, {}).items():
            if key.endswith(" (parsed)") and key in hashes and hashes[key] != old_hash:
                hashes[key] = old_hash
                held.append(key)
    module.BASELINE_PATH.write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"repinned {module.BASELINE_PATH.relative_to(REPO_ROOT)}")
    for key in held:
        print(
            f"  held stale (needs human stub review + manual repin): {key}",
            file=sys.stderr,
        )


def main() -> int:
    global _DEADLINE
    _DEADLINE = time.monotonic() + _TIME_BUDGET_SECONDS
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

    failures, completed = _translate_and_apply(plan, by_locale, module)

    if failures:
        _record_progress(completed, plan)
        print(
            "translation failures (baseline left stale so the run stays red):",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 2

    _clear_progress()
    _repin_baseline(_load_test_module())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
