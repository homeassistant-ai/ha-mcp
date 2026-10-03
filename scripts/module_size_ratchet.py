"""Lower the module-size baseline after a listed file shrank.

``tests/src/unit/test_module_size_ratchet.py`` fails when a tracked source file
crosses ``LINE_LIMIT`` or when a file listed in the baseline changes size. Run
this after shrinking or deleting a listed file:

    python scripts/module_size_ratchet.py

and commit the changed baseline. The lefthook pre-commit hook does both. It
passes ``--staged`` to measure the staged content, so the baseline it stages
matches the files in the commit and ignores unstaged changes.

The command lowers or drops entries. It never raises an entry and never adds a
file, so it cannot accept growth: split the file instead.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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


def read_text(repo_root: Path, path: str, staged: bool = False) -> str:
    """Return one file's text from the working tree, or with ``staged``
    from the index."""
    if staged:
        return _git(repo_root, "show", f":{path}").decode("utf-8")
    return (repo_root / path).read_text("utf-8")


def excluded_prefixes(repo_root: Path, staged: bool = False) -> tuple[str, ...]:
    """Return the path prefixes left out: ruff's ``extend-exclude`` trees
    (vendored code and fixtures) and the stable proxy copy."""
    pyproject = tomllib.loads(read_text(repo_root, "pyproject.toml", staged))
    ruff_excluded = pyproject["tool"]["ruff"]["extend-exclude"]
    prefixes = [f"{entry.rstrip('/')}/" for entry in ruff_excluded if "*" not in entry]
    return (*prefixes, STABLE_PROXY_COPY)


def in_scope(path: str, excluded: tuple[str, ...]) -> bool:
    return path.endswith(SOURCE_SUFFIXES) and not path.startswith(excluded)


def count_lines(content: bytes) -> int:
    """Count lines, including a last line that has no newline."""
    unterminated = bool(content) and not content.endswith(b"\n")
    return content.count(b"\n") + unterminated


def _git(repo_root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        # The unit-test job runs as a different user than the checkout owner,
        # which git refuses without this.
        ["git", "-c", "safe.directory=*", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        input=stdin,
    ).stdout


def _working_tree_contents(
    repo_root: Path, excluded: tuple[str, ...]
) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    for path in _git(repo_root, "ls-files", "-z").decode("utf-8").split("\0"):
        file = repo_root / path
        # A tracked path can be missing from the working tree before its
        # deletion is committed, and a submodule is a directory.
        if in_scope(path, excluded) and file.is_file():
            contents[path] = file.read_bytes()
    return contents


def _staged_contents(repo_root: Path, excluded: tuple[str, ...]) -> dict[str, bytes]:
    blobs: dict[str, str] = {}
    entries = _git(repo_root, "ls-files", "-s", "-z").decode("utf-8")
    for entry in entries.split("\0"):
        # Each entry is "<mode> <object> <stage>\t<path>".
        meta, _, path = entry.partition("\t")
        # Regular files only: a submodule or a symlink has another mode.
        if in_scope(path, excluded) and meta.startswith("100"):
            blobs[path] = meta.split()[1]
    # One git process for every blob. Each reply is a "<object> blob <size>"
    # line, then that many bytes, then a newline.
    replies = _git(
        repo_root, "cat-file", "--batch", stdin="\n".join(blobs.values()).encode()
    )
    contents: dict[str, bytes] = {}
    offset = 0
    for path in blobs:
        header_end = replies.index(b"\n", offset)
        size = int(replies[offset:header_end].split()[2])
        contents[path] = replies[header_end + 1 : header_end + 1 + size]
        offset = header_end + 1 + size + 1
    return contents


def read_sources(repo_root: Path, staged: bool = False) -> dict[str, bytes]:
    """Return the content of every tracked source file in scope.

    Reads the working tree, or with ``staged`` the index: the content the
    next commit holds, which differs when a change is left unstaged.
    """
    excluded = excluded_prefixes(repo_root, staged)
    read = _staged_contents if staged else _working_tree_contents
    return read(repo_root, excluded)


def measure(repo_root: Path, staged: bool = False) -> dict[str, int]:
    """Return the line count of every tracked source file in scope."""
    return {
        path: count_lines(content)
        for path, content in read_sources(repo_root, staged).items()
    }


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


def main(argv: list[str] | None = None, repo_root: Path = REPO_ROOT) -> int:
    """Lower the baseline, then return 1 if a file is still over its limit."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--staged",
        action="store_true",
        help="measure the staged content instead of the working tree",
    )
    args = parser.parse_args(argv)
    baseline_path = repo_root / BASELINE_NAME
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    sizes = measure(repo_root, staged=args.staged)
    lowered = lowered_baseline(sizes, baseline, LINE_LIMIT)
    baseline_path.write_text(
        json.dumps(lowered, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{len(baseline) - len(lowered)} entries dropped, {len(lowered)} remain")
    # A commit that stages no Python file runs no unit tests, so this command
    # is the only check on it.
    violations = find_violations(sizes, lowered, LINE_LIMIT)
    for violation in violations:
        print(violation, file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
