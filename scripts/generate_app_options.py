"""Generate the app (add-on) options from the ``Settings`` model.

A ``Settings`` field whose ``Setting`` metadata carries an ``AppOption`` is an
option on the app's Configuration page. This script writes:

- the ``options:`` and ``schema:`` blocks of ``homeassistant-addon/config.yaml``
  and ``homeassistant-addon-dev/config.yaml``, after the marker comment; the
  rest of each file is left alone;
- ``homeassistant-addon/app_options.json``, the table ``start.py`` reads to
  export each option to its env var. ``start.py`` cannot import ``ha_mcp``
  for it: importing the package builds the server's settings before
  ``start.py`` has exported the options.

The app translation files are keyed by each ``config.yaml`` schema, so run
``scripts/generate_locales.py`` after this one.

Usage::

    python scripts/generate_app_options.py          # rewrite the files
    python scripts/generate_app_options.py --check  # exit 1 naming stale files
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ha_mcp.config_meta import AppOption, Setting, setting_of
from ha_mcp.config_settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
FLAVOR_DIRS = {
    "stable": REPO_ROOT / "homeassistant-addon",
    "dev": REPO_ROOT / "homeassistant-addon-dev",
}
OPTIONS_TABLE = REPO_ROOT / "homeassistant-addon" / "app_options.json"
MARKER = (
    "# options and schema are generated from ha_mcp.config_settings by\n"
    "# scripts/generate_app_options.py. Do not edit them here.\n"
)
# Handled by start.py itself, not a Settings field: an optional override of
# the generated secret path, kept out of options so Supervisor shows it as
# an advanced field.
APP_ONLY_SCHEMA = {"secret_path": "str?"}


def app_options() -> list[tuple[str, Any, Setting, AppOption]]:
    """Return every ``Settings`` field that is an app option, in field order."""
    found = []
    for name, field in Settings.model_fields.items():
        setting = setting_of(field)
        if setting is not None and setting.app is not None:
            found.append((name, field, setting, setting.app))
    return found


def _schema_type(field: Any, setting: Setting, app: AppOption) -> str:
    if setting.choices is not None:
        kind = f"list({'|'.join(setting.choices)})"
    elif field.annotation is bool:
        kind = "bool"
    elif field.annotation is int and setting.range is not None:
        lo, hi = setting.range
        kind = f"int({lo:g},{hi:g})"
    elif field.annotation is int:
        kind = "int"
    else:
        kind = "str"
    return kind if app.required else f"{kind}?"


def _default(field: Any, app: AppOption, flavor: str) -> Any:
    if flavor == "dev" and app.dev_default is not None:
        return app.dev_default
    return field.default


def options_block(flavor: str) -> str:
    """The generated ``options:`` and ``schema:`` blocks of one flavor."""
    options = [
        (name, field, setting, app)
        for name, field, setting, app in app_options()
        if flavor in app.flavors
    ]
    lines = [MARKER.rstrip("\n"), "options:"]
    lines += [
        f"  {name}: {json.dumps(_default(field, app, flavor))}"
        for name, field, _setting, app in options
    ]
    lines.append("schema:")
    lines += [
        f"  {name}: {_schema_type(field, setting, app)}"
        for name, field, setting, app in options
    ]
    lines += [f"  {key}: {kind}" for key, kind in APP_ONLY_SCHEMA.items()]
    return "\n".join(lines) + "\n"


def replace_blocks(text: str, blocks: str, name: str) -> str:
    """Return ``text`` with the generated blocks replaced by ``blocks``.

    The blocks run from the marker comment to the next top-level line
    after ``schema:``.
    """
    begin = text.find(MARKER)
    if begin == -1:
        raise SystemExit(f"{name}: marker comment not found:\n{MARKER}")
    schema = text.index("\nschema:\n", begin) + 1
    lines = text[schema:].splitlines(keepends=True)
    block_end = schema + len(lines[0])
    for line in lines[1:]:
        if not line.startswith(" "):
            break
        block_end += len(line)
    return text[:begin] + blocks + text[block_end:]


def config_yaml(flavor: str) -> str:
    """One flavor's ``config.yaml`` with its option blocks regenerated."""
    path = FLAVOR_DIRS[flavor] / "config.yaml"
    return replace_blocks(
        path.read_text(encoding="utf-8"), options_block(flavor), str(path)
    )


def options_table() -> str:
    """``app_options.json``: what ``start.py`` needs to export each option."""
    rows = []
    for name, field, setting, app in app_options():
        rows.append(
            {
                "key": name,
                "env": field.alias,
                "type": field.annotation.__name__,
                "default": field.default,
                "range": list(setting.range) if setting.range else None,
                "choices": list(setting.choices) if setting.choices else None,
                # Used when options.json holds an invalid value.
                "invalid": (
                    field.default if app.invalid_value is None else app.invalid_value
                ),
                # An option missing from a flavor is exported only when
                # options.json has it; see AppOption.
                "always": set(app.flavors) == set(FLAVOR_DIRS),
            }
        )
    return json.dumps(rows, indent=2) + "\n"


def generated_files() -> dict[Path, str]:
    """Every generated file's full content, keyed by path."""
    files = {
        FLAVOR_DIRS[flavor] / "config.yaml": config_yaml(flavor)
        for flavor in FLAVOR_DIRS
    }
    files[OPTIONS_TABLE] = options_table()
    return files


def write() -> None:
    for path, content in generated_files().items():
        path.write_text(content, encoding="utf-8")
    print("app options written; now run python scripts/generate_locales.py")


def check() -> int:
    """Return 1 naming every generated file that no longer matches."""
    stale = [
        str(path.relative_to(REPO_ROOT))
        for path, content in generated_files().items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    ]
    if stale:
        print(
            f"app options are out of date in {stale}. "
            "Run: python scripts/generate_app_options.py",
            file=sys.stderr,
        )
        return 1
    print("app options are up to date")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only report stale files")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    write()
    return 0


if __name__ == "__main__":
    sys.exit(main())
