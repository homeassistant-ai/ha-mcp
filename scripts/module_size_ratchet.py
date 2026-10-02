"""Lower the module-size baseline after a listed file shrank.

``tests/src/unit/test_module_size_ratchet.py`` fails when a tracked source file
crosses ``LINE_LIMIT`` or when a file listed in the baseline changes size. Run
this after shrinking or deleting a listed file:

    python scripts/module_size_ratchet.py

and commit the changed baseline. The lefthook pre-commit hook does both.

The command lowers or drops entries. It never raises an entry and never adds a
file, so it cannot accept growth: split the file instead.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "tests" / "src" / "unit" / "module_size_baseline.json"

# Pylint's max-module-lines default, the figure AGENTS.md names.
LINE_LIMIT = 1000
SOURCE_SUFFIXES = (".py", ".js", ".mjs", ".ts", ".astro")
# Copied from the dev tree by scripts/webhook_proxy_sync.py; the dev tree is
# the one a pull request edits and the one counted here.
STABLE_PROXY_COPY = "homeassistant-addon-webhook-proxy/"
REPIN_COMMAND = "python scripts/module_size_ratchet.py"
BASELINE_NAME = BASELINE_PATH.relative_to(REPO_ROOT).as_posix()


def excluded_prefixes(repo_root: Path) -> tuple[str, ...]:
    """Return the path prefixes left out: ruff's ``extend-exclude`` trees
    (vendored code and fixtures) and the stable proxy copy."""
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text("utf-8"))
    ruff_excluded = pyproject["tool"]["ruff"]["extend-exclude"]
    prefixes = [f"{entry.rstrip('/')}/" for entry in ruff_excluded if "*" not in entry]
    return (*prefixes, STABLE_PROXY_COPY)


def in_scope(path: str, excluded: tuple[str, ...]) -> bool:
    return path.endswith(SOURCE_SUFFIXES) and not path.startswith(excluded)


def count_lines(content: bytes) -> int:
    """Count lines, including a last line that has no newline."""
    unterminated = bool(content) and not content.endswith(b"\n")
    return content.count(b"\n") + unterminated


def measure(repo_root: Path) -> dict[str, int]:
    """Return the line count of every tracked source file in scope."""
    tracked = subprocess.run(
        # The unit-test job runs as a different user than the checkout owner,
        # which git refuses without this.
        ["git", "-c", "safe.directory=*", "ls-files", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    excluded = excluded_prefixes(repo_root)
    sizes: dict[str, int] = {}
    for path in tracked.split("\0"):
        file = repo_root / path
        # A tracked path can be missing from the working tree before its
        # deletion is committed, and a submodule is a directory.
        if in_scope(path, excluded) and file.is_file():
            sizes[path] = count_lines(file.read_bytes())
    return sizes


def find_violations(
    sizes: dict[str, int], baseline: dict[str, int], limit: int
) -> list[str]:
    """Return one message per file that breaks the ratchet."""
    violations: list[str] = []
    for path, lines in sorted(sizes.items()):
        allowed = baseline.get(path)
        if allowed is None:
            if lines > limit:
                violations.append(
                    f"{path}: {lines} lines is over the {limit}-line limit. "
                    "Split it along responsibilities."
                )
        elif lines > allowed:
            violations.append(
                f"{path}: grew from {allowed} to {lines} lines and is already "
                f"over the {limit}-line limit. Move code out of it."
            )
        elif lines < allowed:
            violations.append(
                f"{path}: shrank from {allowed} to {lines} lines. "
                f"Run `{REPIN_COMMAND}` and commit {BASELINE_NAME}."
            )
    violations.extend(
        f"{path}: listed in the baseline but not a tracked source file. "
        f"Run `{REPIN_COMMAND}` and commit {BASELINE_NAME}."
        for path in sorted(baseline.keys() - sizes.keys())
    )
    return violations


def lowered_baseline(
    sizes: dict[str, int], baseline: dict[str, int], limit: int
) -> dict[str, int]:
    """Return the baseline with shrunk files lowered and cleared files dropped."""
    lowered: dict[str, int] = {}
    for path, allowed in baseline.items():
        lines = sizes.get(path)
        if lines is not None and lines > limit:
            lowered[path] = min(lines, allowed)
    return lowered


def main() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    lowered = lowered_baseline(measure(REPO_ROOT), baseline, LINE_LIMIT)
    BASELINE_PATH.write_text(
        json.dumps(lowered, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{len(baseline) - len(lowered)} entries dropped, {len(lowered)} remain")


if __name__ == "__main__":
    main()
