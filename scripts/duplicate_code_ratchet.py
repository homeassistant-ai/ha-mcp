"""Keep the number of copied Python functions and classes from growing.

``tests/src/unit/test_duplicate_code_ratchet.py`` fails when a function or
class has the same code as another one. Two definitions have the same code
when they match after their docstrings, decorators, type hints and own names
are dropped and the names bound inside functions are renamed in order of use.
Definitions with fewer than ``MIN_STATEMENTS`` statements are not compared.

``duplicate_code_baseline.json`` lists the groups of copies that already
exist, keyed by a hash of their code. A group passes when the baseline has the
same code with at least as many copies, or a group that holds all of its
places. Moving or renaming a copy, removing one, or editing every copy the
same way passes; adding a copy fails.

Run this after removing, moving or editing a copy:

    python scripts/duplicate_code_ratchet.py

and commit the changed baseline. The lefthook pre-commit hook does both. It
passes ``--staged`` to read the staged content, so the baseline it stages
matches the files in the commit.

The command writes the baseline only when every group passes, so it cannot
accept a new copy: import the existing function instead.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import module_size_ratchet  # type: ignore[import-not-found]

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "tests" / "src" / "unit" / "duplicate_code_baseline.json"
BASELINE_NAME = BASELINE_PATH.relative_to(REPO_ROOT).as_posix()
REPIN_COMMAND = "python scripts/duplicate_code_ratchet.py"

# One statement is a stub (`pass`, `...`, an overload) or a one-line
# delegation; two is the smallest helper an agent copies.
MIN_STATEMENTS = 2

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_IGNORED_FIELDS = frozenset(
    {"decorator_list", "returns", "type_comment", "type_params"}
)


def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _names_bound_in_functions(root: ast.AST) -> set[str]:
    """Return the arguments and the names assigned inside functions.

    Class attributes are left out: they are the class's interface, and two
    classes that differ only in their field names are not copies.
    """
    names: set[str] = set()
    stack = [(root, isinstance(root, _FUNCTIONS))]
    while stack:
        node, in_function = stack.pop()
        if isinstance(node, ast.arg):
            names.add(node.arg)
        elif in_function and isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Load):
                names.add(node.id)
        elif in_function and isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        stack.extend(
            (child, in_function or isinstance(child, _FUNCTIONS))
            for child in ast.iter_child_nodes(node)
        )
    return names


class _Fingerprint:
    """Serialize a definition in its normalized form and count its statements."""

    def __init__(
        self, definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        self._local = _names_bound_in_functions(definition)
        self._renamed: dict[str, str] = {}
        self._parts: list[str] = [type(definition).__name__]
        self.statements = 0
        if isinstance(definition, ast.ClassDef):
            self._dump(definition.bases, in_function=False)
            self._dump(definition.keywords, in_function=False)
        else:
            self._dump(definition.args, in_function=True)
        self._dump(
            _without_docstring(definition.body),
            in_function=not isinstance(definition, ast.ClassDef),
        )

    def digest(self) -> str:
        return hashlib.sha1("\0".join(self._parts).encode()).hexdigest()[:12]

    def _name(self, name: str) -> str:
        if name not in self._local:
            return name
        return self._renamed.setdefault(name, f"_{len(self._renamed)}")

    def _dump(self, value: object, in_function: bool) -> None:
        parts = self._parts
        if isinstance(value, list):
            parts.append("[")
            for item in value:
                self._dump(item, in_function)
            parts.append("]")
            return
        if not isinstance(value, ast.AST):
            parts.append(repr(value))
            return
        parts.append(type(value).__name__)
        if isinstance(value, ast.stmt):
            self.statements += 1
        if isinstance(value, ast.Name):
            parts.append(self._name(value.id))
            parts.append(type(value.ctx).__name__)
            return
        if isinstance(value, ast.arg):
            # The annotation is a type hint.
            parts.append(self._name(value.arg))
            return
        self._dump_fields(value, in_function)

    def _dump_fields(self, node: ast.AST, in_function: bool) -> None:
        inner = in_function or isinstance(node, _FUNCTIONS)
        for field in node._fields:
            if field in _IGNORED_FIELDS:
                continue
            if (
                field == "annotation"
                and isinstance(node, ast.AnnAssign)
                and in_function
            ):
                continue
            child = getattr(node, field, None)
            if field == "body" and isinstance(node, _DEFS):
                child = _without_docstring(node.body)
            elif field == "name" and isinstance(node, ast.ExceptHandler) and node.name:
                child = self._name(node.name)
            self._parts.append(field)
            self._dump(child, inner)


def find_copies(sources: dict[str, bytes]) -> dict[str, list[str]]:
    """Return every group of two or more definitions with the same code.

    Each group maps the hash of its code to its places, ``path::qualname``.
    Definitions inside a function are compared only as part of that function:
    a closure cannot be imported from somewhere else.

    A file with the same bytes as another is read once. That is how code that
    both the server and the component ship is shared (``dashboard_patch.py``,
    whose test checks that the two files match), so adding a function to such
    a file is not a new copy.
    """
    unique: dict[bytes, str] = {}
    for path, content in sorted(sources.items()):
        unique.setdefault(content, path)
    places: dict[str, list[str]] = {}
    for content, path in sorted(unique.items(), key=lambda item: item[1]):
        stack: list[tuple[ast.AST, str]] = [(ast.parse(content, filename=path), "")]
        while stack:
            parent, prefix = stack.pop()
            for child in ast.iter_child_nodes(parent):
                if not isinstance(child, _DEFS):
                    stack.append((child, prefix))
                    continue
                qualname = f"{prefix}{child.name}"
                if isinstance(child, ast.ClassDef):
                    stack.append((child, f"{qualname}."))
                fingerprint = _Fingerprint(child)
                if fingerprint.statements >= MIN_STATEMENTS:
                    places.setdefault(fingerprint.digest(), []).append(
                        f"{path}::{qualname}"
                    )
    return {code: sorted(group) for code, group in places.items() if len(group) > 1}


def scan(repo_root: Path, staged: bool = False) -> dict[str, list[str]]:
    """Return the groups of copies in the tracked Python files in scope."""
    sources = module_size_ratchet.read_sources(repo_root, staged)
    return find_copies(
        {path: content for path, content in sources.items() if path.endswith(".py")}
    )


def _is_listed(code: str, group: list[str], baseline: dict[str, list[str]]) -> bool:
    listed = baseline.get(code)
    if listed is not None and len(group) <= len(listed):
        return True
    places = Counter(group)
    return any(not places - Counter(entry) for entry in baseline.values())


def find_violations(
    groups: dict[str, list[str]], baseline: dict[str, list[str]]
) -> list[str]:
    """Return one message per group of copies the baseline does not allow.

    A copied class also copies its methods. Those method groups are left out
    of the messages, so one copied class gives one message.
    """
    new = [
        (code, group)
        for code, group in sorted(groups.items())
        if not _is_listed(code, group, baseline)
    ]
    outer = {place for _, group in new for place in group}
    messages: list[str] = []
    for code, group in new:
        if all(any(place.startswith(f"{o}.") for o in outer) for place in group):
            continue
        listed = baseline.get(code, [])
        added = [place for place in group if place not in listed] or group
        if listed:
            messages.append(
                f"{', '.join(added)}: a new copy of {listed[0]}. "
                "Import or reuse it instead of copying it."
            )
        else:
            messages.append(
                f"{', '.join(group)}: the same code. "
                "Keep one definition and import it in the other places."
            )
    return messages


def main(argv: list[str] | None = None, repo_root: Path = REPO_ROOT) -> int:
    """Fail on a new copy; otherwise rewrite the baseline to the current groups."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--staged",
        action="store_true",
        help="read the staged content instead of the working tree",
    )
    args = parser.parse_args(argv)
    baseline_path = repo_root / BASELINE_NAME
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    groups = scan(repo_root, staged=args.staged)
    violations = find_violations(groups, baseline)
    # Rewriting on a failure would drop the entry of a group that gained a
    # copy, so removing the new copy would then fail as well.
    if violations:
        for violation in violations:
            print(violation, file=sys.stderr)
        return 1
    baseline_path.write_text(
        json.dumps(groups, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{len(groups)} groups of copies, {len(baseline)} before")
    return 0


if __name__ == "__main__":
    sys.exit(main())
