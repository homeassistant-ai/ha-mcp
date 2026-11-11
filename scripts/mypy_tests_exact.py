#!/usr/bin/env python3
"""Run mypy on the unit and app tests and keep the list of exempt modules exact.

``pyproject.toml`` lists the test modules written before mypy ran on the tests
(``ignore_errors`` under ``[[tool.mypy.overrides]]``). This runs mypy without
that list and compares the modules that have errors with it. A module with
errors that is not listed fails, as plain mypy would. So does a listed module
with no errors left: a fixed file comes off the list, which keeps it checked.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TREES = ("tests/src/unit/", "tests/addon/")
_TABLE = re.compile(r"^\[", re.MULTILINE)
_ERROR = re.compile(r"^(?P<path>[^:\n]+\.py):\d+(?::\d+)?: error: ", re.MULTILINE)


def _exempt_modules(table: str) -> list[str]:
    """The modules of ``table`` when it is a list of exempt test modules."""
    if not table.startswith("[[tool.mypy.overrides]]"):
        return []
    override = tomllib.loads(table)["tool"]["mypy"]["overrides"][0]
    modules = override.get("module", [])
    modules = [modules] if isinstance(modules, str) else list(modules)
    if override.get("ignore_errors") is True and all(
        module.startswith("tests.") for module in modules
    ):
        return modules
    return []


def split_exempt(text: str) -> tuple[str, set[str]]:
    """``pyproject.toml`` without the exempt test modules, and those modules."""
    starts = [match.start() for match in _TABLE.finditer(text)]
    tables = [text[: starts[0]]] + [
        text[start:end]
        for start, end in zip(starts, [*starts[1:], len(text)], strict=True)
    ]
    kept = []
    exempt: set[str] = set()
    for table in tables:
        modules = _exempt_modules(table)
        if modules:
            exempt.update(modules)
        else:
            kept.append(table)
    return "".join(kept), exempt


def modules_with_errors(output: str) -> set[str]:
    return {
        match["path"].replace("\\", "/").removesuffix(".py").replace("/", ".")
        for match in _ERROR.finditer(output)
    }


def compare(exempt: set[str], output: str) -> list[str]:
    """What is wrong: errors in unlisted modules, and listed modules without any."""
    failing = modules_with_errors(output)
    unlisted = failing - exempt
    problems = [
        line
        for line in output.splitlines()
        if (match := _ERROR.match(line)) and modules_with_errors(match[0]) & unlisted
    ]
    problems.extend(
        f"{module} has no mypy errors left: remove it from the ignore_errors "
        "list in pyproject.toml"
        for module in sorted(exempt - failing)
    )
    return problems


def main() -> int:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    config, exempt = split_exempt(text)
    # A pyproject-style file by another name, next to the real one so the
    # relative paths in it still resolve.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".toml", dir=REPO, encoding="utf-8", delete=False
    ) as handle:
        handle.write(config)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "mypy", "--config-file", handle.name, *TREES],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    finally:
        Path(handle.name).unlink()
    output = proc.stdout + proc.stderr
    # 0 is clean and 1 is type errors; anything else is mypy itself failing.
    if proc.returncode not in (0, 1) or (
        proc.returncode == 1 and not _ERROR.search(output)
    ):
        print(output)
        print("mypy did not finish; see its output above")
        return 2

    problems = compare(exempt, output)
    if problems:
        print("\n".join(problems))
        print(
            "\nThe unit and app tests are type-checked against Home Assistant "
            "Core's real types (tests/AGENTS.md, 'Unit Tests Run on Real Home "
            "Assistant Core'). Fix the errors above; a new test is never added "
            "to the ignore_errors list."
        )
        return 1
    print(f"mypy: the tests are clean outside the {len(exempt)} listed modules.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
