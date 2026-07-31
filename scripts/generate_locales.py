"""Generate the derived locale catalogs from the canonical settings catalogs.

The canonical store is ``src/ha_mcp/settings_ui/locales/<code>.json`` — one
file per language holding every translatable string, including the add-on
option strings under ``addon.<key>.*`` / ``addon_stable.<key>.*`` and the
shared feature strings under ``features.<key>.*``. The derived catalogs stay
committed at the fixed paths Supervisor and the web UI read them from:

- ``homeassistant-addon/translations/<code>.yaml``
- ``homeassistant-addon-dev/translations/<code>.yaml``
- the ``FEATURE_META`` block in ``src/ha_mcp/settings_ui/settings.js``

Each add-on flavor's key list comes from its own ``config.yaml`` ``schema:``,
so the two flavors project different subsets of the one canonical store. Per
key and field the text is resolved in override order:

1. ``addon_<flavor>.<key>.<name|description>`` — flavor-specific wording
2. ``features.<key>.<label|help>`` — strings shared with the settings UI
3. ``addon.<key>.<name|description>`` — add-on-only options

A locale that lacks a key falls back to English, mirroring the settings UI's
own per-key fallback, so every generated catalog is structurally complete.

``FEATURE_META`` (the settings UI's English fallback for feature rows, and
the row order) is generated from ``en.json``'s ``features.*`` keys between
marker comments in ``settings.js``.

Usage::

    python scripts/generate_locales.py          # rewrite the derived files
    python scripts/generate_locales.py --check  # exit 1 naming stale files
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from ha_mcp.settings_ui._i18n import _validate_string_map

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCALES_DIR = REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales"
SETTINGS_JS = REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "settings.js"
ADDON_FLAVORS = {
    "stable": REPO_ROOT / "homeassistant-addon",
    "dev": REPO_ROOT / "homeassistant-addon-dev",
}

FEATURE_META_BEGIN = "// FEATURE_META:BEGIN GENERATED (scripts/generate_locales.py)"
FEATURE_META_END = "// FEATURE_META:END GENERATED"

_FEATURE_KEY_RE = re.compile(r"^features\.([a-z0-9_]+)\.label$")


def load_catalogs() -> dict[str, dict[str, str]]:
    """Every canonical catalog's ``messages`` section, keyed by locale code.

    Validated with the runtime loader's own ``_validate_string_map`` so a
    malformed catalog fails here with the file named, not as a raw exception
    from deep inside the YAML emitter.
    """
    catalogs: dict[str, dict[str, str]] = {}
    for path in sorted(LOCALES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        catalogs[path.stem] = _validate_string_map(
            data.get("messages"), context=f"{path.name}.messages"
        )
    if "en" not in catalogs:
        raise SystemExit(f"no en.json in {LOCALES_DIR}")
    return catalogs


def schema_keys(addon_dir: Path) -> list[str]:
    """The option keys of one flavor's ``config.yaml``, in schema order."""
    config = yaml.safe_load((addon_dir / "config.yaml").read_text(encoding="utf-8"))
    return list(config["schema"])


def feature_keys_in_order(english: dict[str, str]) -> list[str]:
    """The ``features.<key>`` set in ``en.json`` order — also the UI row order."""
    return [
        match.group(1)
        for key in english
        if (match := _FEATURE_KEY_RE.match(key)) is not None
    ]


def resolve_text(
    messages: dict[str, str],
    english: dict[str, str],
    flavor: str,
    key: str,
    field: str,
) -> str:
    """One (key, field) string for one locale, English as the final fallback."""
    ui_field = "label" if field == "name" else "help"
    candidates = (
        f"addon_{flavor}.{key}.{field}",
        f"features.{key}.{ui_field}",
        f"addon.{key}.{field}",
    )
    for candidate in candidates:
        if candidate in messages:
            return messages[candidate]
    for candidate in candidates:
        if candidate in english:
            return english[candidate]
    raise SystemExit(
        f"no canonical string for add-on option {key!r} field {field!r} — add "
        f"addon.{key}.{field} (or features.{key}.{ui_field}) to en.json"
    )


def addon_yaml(
    flavor: str,
    keys: list[str],
    messages: dict[str, str],
    english: dict[str, str],
    code: str,
) -> str:
    """The full text of one flavor's ``translations/<code>.yaml``."""
    configuration = {
        key: {
            "name": resolve_text(messages, english, flavor, key, "name"),
            "description": resolve_text(messages, english, flavor, key, "description"),
        }
        for key in keys
    }
    body: str = yaml.safe_dump(
        {"configuration": configuration},
        allow_unicode=True,
        sort_keys=False,
        width=80,
    )
    return (
        "---\n"
        f"# GENERATED FILE — do not edit. Translations live in\n"
        f"# src/ha_mcp/settings_ui/locales/{code}.json (keys addon.*, "
        "features.*,\n"
        f"# addon_{flavor}.*); regenerate with: python scripts/generate_locales.py\n"
        + body
    )


def feature_meta_block(english: dict[str, str]) -> str:
    """The generated ``FEATURE_META`` region for ``settings.js``.

    English fallback for feature rows when the i18n payload lacks a
    ``features.<field>`` key; the object's key order is the row render order
    and follows the ``features.*`` key order in ``en.json``.
    """
    lines = [
        FEATURE_META_BEGIN,
        "// English fallback + row order for the feature toggles. Do not edit:",
        "// the strings and their order come from the features.* keys in",
        "// locales/en.json — edit that file, then run",
        "// scripts/generate_locales.py.",
        "// The master/sub-flag gating these rows describe is enforced by",
        "// config.py:_apply_feature_flag_overrides; the UI dims sub-rows and",
        "// re-renders live when the master toggle flips.",
        "const FEATURE_META = {",
    ]
    for key in feature_keys_in_order(english):
        label = json.dumps(english[f"features.{key}.label"], ensure_ascii=False)
        help_text = json.dumps(english[f"features.{key}.help"], ensure_ascii=False)
        lines += [
            f"  {key}: {{",
            f"    label: {label},",
            f"    help: {help_text},",
            "  },",
        ]
    lines += ["};", FEATURE_META_END]
    return "\n".join(lines)


def settings_js_with_block(block: str) -> str:
    """``settings.js`` with the marker region replaced by ``block``."""
    text = SETTINGS_JS.read_text(encoding="utf-8")
    begin = text.find(FEATURE_META_BEGIN)
    end = text.find(FEATURE_META_END)
    if begin == -1 or end == -1:
        raise SystemExit(
            f"marker comments not found in {SETTINGS_JS} — expected "
            f"{FEATURE_META_BEGIN!r} and {FEATURE_META_END!r}"
        )
    return text[:begin] + block + text[end + len(FEATURE_META_END) :]


def generated_files() -> dict[Path, str]:
    """Every derived file's full generated content, keyed by path."""
    catalogs = load_catalogs()
    english = catalogs["en"]
    outputs: dict[Path, str] = {}
    for flavor, addon_dir in ADDON_FLAVORS.items():
        keys = schema_keys(addon_dir)
        for code, messages in catalogs.items():
            outputs[addon_dir / "translations" / f"{code}.yaml"] = addon_yaml(
                flavor, keys, messages, english, code
            )
    outputs[SETTINGS_JS] = settings_js_with_block(feature_meta_block(english))
    return outputs


def check() -> int:
    """Exit 1 naming every derived file that no longer matches the canon."""
    stale: list[str] = []
    for path, content in generated_files().items():
        committed = path.read_text(encoding="utf-8") if path.exists() else ""
        if committed != content:
            stale.append(str(path.relative_to(REPO_ROOT)))
            diff = difflib.unified_diff(
                committed.splitlines()[:20],
                content.splitlines()[:20],
                lineterm="",
                n=1,
            )
            print("\n".join(list(diff)[:12]), file=sys.stderr)
    if stale:
        print(
            "derived locale catalogs are out of sync with the canonical "
            f"store: {stale}. Run: python scripts/generate_locales.py",
            file=sys.stderr,
        )
        return 1
    print("derived locale catalogs are in sync")
    return 0


def write() -> int:
    changed = 0
    for path, content in generated_files().items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            changed += 1
            print(f"wrote {path.relative_to(REPO_ROOT)}")
    print(f"{changed} file(s) updated")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the derived files match instead of writing them",
    )
    args = parser.parse_args()
    return check() if args.check else write()


if __name__ == "__main__":
    raise SystemExit(main())
