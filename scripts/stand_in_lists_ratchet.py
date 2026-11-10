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
import os
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


def _mypy_checks_tests(text: str) -> bool:
    """Whether mypy already ran on the tests, so the list already existed."""
    overrides = tomllib.loads(text).get("tool", {}).get("mypy", {}).get("overrides", [])
    return any("tests.*" in override.get("module", []) for override in overrides)


def added_entries(path: str, base_text: str | None, head_text: str) -> set[str]:
    """The entries ``path``'s list gained since the base, when that is refused.

    ``base_text`` is None only when the file did not exist at the base. A list
    that existed there and is empty stays empty: only a list that is new may
    start with entries.
    """
    if path.startswith("rules/"):
        head, head_rule = _rule_parts(head_text)
        if base_text is None:
            return set()
        base, base_rule = _rule_parts(base_text)
        return set() if base_rule != head_rule else head - base
    if path == PYPROJECT:
        if base_text is None or not _mypy_checks_tests(base_text):
            return set()
        return _mypy_entries(head_text) - _mypy_entries(base_text)
    if base_text is None:
        return set()
    parse = _guard_entries if path == GUARD else _ruff_entries
    return parse(head_text) - parse(base_text)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    # A hook or a linked worktree sets GIT_DIR and friends, which would point
    # git at another repository than this file's.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _base_text(base: str, path: str) -> str | None:
    """``path`` at ``base``, or None when the file did not exist there."""
    if _git("cat-file", "-e", f"{base}:{path}").returncode != 0:
        return None
    shown = _git("show", f"{base}:{path}")
    if shown.returncode != 0:
        raise RuntimeError(f"cannot read {path} at {base}: {shown.stderr.strip()}")
    return shown.stdout


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

    # An unreadable base would make every list look new and pass.
    if _git("cat-file", "-e", f"{args.base}^{{commit}}").returncode != 0:
        print(f"{args.base} is not a commit in this repository; fetch it first")
        return 2

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
