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

So a pull request need not edit the baseline, and two of them do not
conflict over it. After a merge,
``.github/workflows/sync-ratchet-baselines.yml`` runs

    python scripts/duplicate_code_ratchet.py

on master and commits the rewritten baseline. The command writes the baseline
only when every group passes, so it cannot accept a new copy: import the
existing function instead. ``--check`` reports new copies without writing the
baseline; the lefthook pre-commit hook runs it with ``--staged`` to read the
staged content. CI also runs ``--base <ref>``, which fails when the baseline
lists a group that the baseline at that commit does not allow, or when the
files hold a copy that commit's own copies do not allow.
"""

from __future__ import annotations

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


def _arguments(args: ast.arguments) -> list[ast.arg]:
    optional = [arg for arg in (args.vararg, args.kwarg) if arg is not None]
    return [*args.posonlyargs, *args.args, *args.kwonlyargs, *optional]


def _scope_bindings(
    function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> set[str]:
    """Return the names a function binds in its own scope.

    Nested functions and classes are scopes of their own; only their names
    belong to this one.
    """
    names = {arg.arg for arg in _arguments(function.args)}
    body = function.body if isinstance(function.body, list) else [function.body]
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        names.update(_bound_names(node))
        if not isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))
    return names


_NAMED_BINDINGS = (ast.ExceptHandler, ast.MatchAs, ast.MatchStar, *_DEFS)


def _bound_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
        return [node.id]
    if isinstance(node, _NAMED_BINDINGS) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchMapping) and node.rest:
        return [node.rest]
    if isinstance(node, ast.alias):
        return [node.asname or node.name.partition(".")[0]]
    return []


def _binds(node: ast.AST, field: str) -> bool:
    """Whether ``field`` of ``node`` is a name the node binds.

    An import's ``name`` is the module, only its ``asname`` is bound.
    """
    if isinstance(node, ast.alias):
        return field == "asname"
    if isinstance(node, ast.MatchMapping):
        return field == "rest"
    return field == "name" and isinstance(node, _NAMED_BINDINGS)


class _Fingerprint:
    """Serialize a definition in its normalized form and count its statements.

    Names bound in a function scope are renamed in order of use, per scope,
    the way Python resolves them: a function sees its own names and those of
    the functions around it, not those of a class body. Class attributes and
    method names keep their names: they are the class's interface, and two
    classes that differ only in them are not copies.
    """

    def __init__(
        self, definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        # One entry per enclosing scope: its number and, for a function, the
        # names it binds. A class body has no entry in the lookup.
        self._scopes: list[tuple[int, set[str] | None]] = []
        self._scope_count = 0
        self._renamed: dict[tuple[int, str], str] = {}
        self._parts: list[str] = [type(definition).__name__]
        self.statements = 0
        if isinstance(definition, ast.ClassDef):
            self._dump(definition.bases)
            self._dump(definition.keywords)
        else:
            self._enter(_scope_bindings(definition))
            self._dump(definition.args)
        self._dump(_without_docstring(definition.body))

    def digest(self) -> str:
        return hashlib.sha1("\0".join(self._parts).encode()).hexdigest()[:12]

    def _enter(self, bound: set[str] | None) -> None:
        self._scopes.append((self._scope_count, bound))
        self._scope_count += 1

    def _in_function(self) -> bool:
        return bool(self._scopes) and self._scopes[-1][1] is not None

    def _name(self, name: str) -> str:
        for scope, bound in reversed(self._scopes):
            if bound is not None and name in bound:
                return self._renamed.setdefault((scope, name), f"_{len(self._renamed)}")
        return name

    def _dump(self, value: object) -> None:
        parts = self._parts
        if isinstance(value, list):
            parts.append("[")
            for item in value:
                self._dump(item)
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
        self._dump_fields(value)

    def _dump_fields(self, node: ast.AST) -> None:
        depth = len(self._scopes)
        for field in node._fields:
            if field in _IGNORED_FIELDS:
                continue
            if field == "annotation" and isinstance(node, ast.AnnAssign):
                if self._in_function():
                    continue
            # A function's name belongs to the scope around it; its arguments
            # and body to its own. A class's bases are evaluated outside it.
            if field == "args" and isinstance(node, _FUNCTIONS):
                self._enter(_scope_bindings(node))
            elif field == "body" and isinstance(node, ast.ClassDef):
                self._enter(None)
            child = getattr(node, field, None)
            if field == "body" and isinstance(node, _DEFS):
                child = _without_docstring(node.body)
            elif child and _binds(node, field):
                child = self._name(child)
            self._parts.append(field)
            self._dump(child)
        del self._scopes[depth:]


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


def scan(
    repo_root: Path, staged: bool = False, ref: str | None = None
) -> dict[str, list[str]]:
    """Return the groups of copies in the tracked Python files in scope."""
    sources = module_size_ratchet.read_sources(repo_root, staged, ref)
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


def find_added_groups(
    baseline: dict[str, list[str]], base: dict[str, list[str]]
) -> list[str]:
    """Return one message per group ``baseline`` lists that ``base`` does
    not allow."""
    return [
        f"{', '.join(group)}: listed in {BASELINE_NAME}, but the base branch's "
        f"baseline does not allow this group. Only `{REPIN_COMMAND}` edits the "
        "baseline, and it never accepts a new copy: import the existing "
        "definition instead."
        for code, group in sorted(baseline.items())
        if not _is_listed(code, group, base)
    ]


def find_copies_since(repo_root: Path, ref: str) -> list[str]:
    """Return one message per group of copies that the baseline the sync
    would write from ``ref`` does not allow.

    Until the sync runs, ``ref``'s baseline can still list a copy ``ref``
    removed. Checking against it would let a new copy take that place:
    accepted silently if the sync runs after the merge, or failing master's
    own check if the sync dropped the old copy first.
    """
    return find_violations(scan(repo_root), scan(repo_root, ref=ref))


def main(argv: list[str] | None = None, repo_root: Path = REPO_ROOT) -> int:
    """With ``--base``, compare the baseline and the files with that commit.
    Otherwise fail on a new copy, and unless ``--check`` rewrite the baseline
    to the current groups."""
    args = module_size_ratchet.parse_args(argv, __doc__.splitlines()[0])
    if args.base:
        status: int = module_size_ratchet.compare_with_base(
            repo_root, args.base, BASELINE_NAME, find_added_groups, find_copies_since
        )
        return status
    baseline_path = repo_root / BASELINE_NAME
    # With --staged the baseline comes from the index too, so an unstaged
    # edit to it cannot let a staged copy through.
    baseline = json.loads(
        module_size_ratchet.read_text(repo_root, BASELINE_NAME, args.staged)
    )
    groups = scan(repo_root, staged=args.staged)
    violations = find_violations(groups, baseline)
    # Rewriting on a failure would drop the entry of a group that gained a
    # copy, so removing the new copy would then fail as well.
    if violations:
        status = module_size_ratchet.report(violations)
        return status
    if args.check:
        return 0
    baseline_path.write_text(
        json.dumps(groups, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{len(groups)} groups of copies, {len(baseline)} before")
    return 0


if __name__ == "__main__":
    sys.exit(main())
