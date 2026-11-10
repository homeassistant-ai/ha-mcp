#!/usr/bin/env python3
"""Fail when a list of tests exempt from the real-Core checks gains an entry.

The unit and app (add-on) tests run on real Home Assistant Core
(tests/AGENTS.md, "Unit Tests Run on Real Home Assistant Core"). Files written
before each check are listed with it: the ``ignores`` of the ast-grep rules,
``_GRANDFATHERED`` in ``tests/core_stand_in_guard.py``, the ``SLF001`` entries
of the two test ``ruff.toml`` files and the ``tests.*`` ``ignore_errors``
modules in ``pyproject.toml``. The lists are plain files, so a pull request
could add its own new test to one and every check would pass. With ``--base``
this compares each list with the base commit and reports the entries that were
added.

A list may gain entries in two cases only: the list did not exist at the base,
or it belongs to an ast-grep rule whose definition changed in the same pull
request, since a wider rule matches older files it did not match before.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = "tests/core_stand_in_guard.py"
RUFF_FILES = ("tests/src/unit/ruff.toml", "tests/addon/ruff.toml")
PYPROJECT = "pyproject.toml"
_IGNORE_LINE = re.compile(r'^\s+-\s+"([^"]+)"\s*$')


def _rule_parts(text: str) -> tuple[set[str], str]:
    """Split an ast-grep rule into its ``ignores`` entries and everything else."""
    entries: set[str] = set()
    rest: list[str] = []
    in_ignores = False
    for line in text.splitlines():
        if line.startswith("ignores:"):
            in_ignores = True
            continue
        match = _IGNORE_LINE.match(line) if in_ignores else None
        if match:
            entries.add(match.group(1))
            continue
        in_ignores = False
        rest.append(line)
    return entries, "\n".join(rest)


def _guard_entries(text: str) -> set[str]:
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_GRANDFATHERED"
            for target in node.targets
        ):
            return {
                constant.value
                for constant in ast.walk(node.value)
                if isinstance(constant, ast.Constant)
                and isinstance(constant.value, str)
            }
    return set()


def _ruff_entries(text: str) -> set[str]:
    ignores = tomllib.loads(text).get("lint", {}).get("extend-per-file-ignores", {})
    return set(ignores)


def _mypy_entries(text: str) -> set[str]:
    overrides = tomllib.loads(text).get("tool", {}).get("mypy", {}).get("overrides", [])
    return {
        module
        for override in overrides
        if override.get("ignore_errors")
        for module in override.get("module", [])
        if module.startswith("tests.")
    }


def added_entries(path: str, base_text: str | None, head_text: str) -> set[str]:
    """The entries ``path``'s list gained since the base, when that is refused."""
    if path.startswith("rules/"):
        head, head_rule = _rule_parts(head_text)
        if base_text is None:
            return set()
        base, base_rule = _rule_parts(base_text)
        return set() if base_rule != head_rule else head - base
    parse = (
        _guard_entries
        if path == GUARD
        else _ruff_entries
        if path in RUFF_FILES
        else _mypy_entries
    )
    head = parse(head_text)
    base = parse(base_text) if base_text is not None else set()
    # An empty base list is a list that did not exist yet.
    return head - base if base else set()


def _base_text(base: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{base}:{path}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def list_files() -> list[str]:
    rules = sorted(
        path.relative_to(REPO).as_posix() for path in (REPO / "rules").glob("*.yml")
    )
    return [*rules, GUARD, *RUFF_FILES, PYPROJECT]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base", required=True, help="commit to compare the lists with"
    )
    args = parser.parse_args(argv)

    failed = False
    for path in list_files():
        head_text = (REPO / path).read_text(encoding="utf-8")
        added = added_entries(path, _base_text(args.base, path), head_text)
        for entry in sorted(added):
            failed = True
            print(f"{path}: {entry} was added to the list of exempt tests")
    if failed:
        print(
            "\nNo file is ever added to these lists (tests/AGENTS.md, 'Unit Tests "
            "Run on Real Home Assistant Core'). Write the new test against real "
            "Core instead: take the core_client fixture, do not reach into private "
            "members, and fix its type errors. A renamed file counts as new."
        )
        return 1
    print("No list of exempt tests gained an entry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
