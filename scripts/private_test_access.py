#!/usr/bin/env python3
"""Reject private names that unit and app tests reach through strings.

Ruff (SLF001) checks attribute syntax and the private-import guard checks
imports. This checks the names a test passes as strings: reflection
(``getattr``, ``hasattr``, ``setattr``, ``delattr``, ``getattr_static``,
``attrgetter``, ``__getattribute__``), imports (``import_module``,
``__import__``), patch targets (``patch``, ``patch.object``, ``patch.dict``,
``patch.multiple``, ``monkeypatch.setattr``/``delattr``/``setitem``) and
namespace lookups (``vars(x)[...]``, ``x.__dict__[...]``).

A name counts when it is a string literal, a name the file assigns strings to
(including loop, ``parametrize`` and local-helper arguments), or a
concatenation or f-string of those. A name only known at runtime is left to
review. Dunder names, the ``ha_mcp._vendor`` package and the test's own
``self``/``cls`` are allowed. There is no exemption list.
"""

from __future__ import annotations

import argparse
import ast
import itertools
import math
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TREES = ("tests/src/unit", "tests/addon")
UNKNOWN = "\0"
MAX_ALTERNATIVES = 256
TOO_MANY = "\1"  # more alternatives than are checked: reported, never dropped
REFLECTION = {"getattr", "hasattr", "setattr", "delattr"}
DUNDER_REFLECTION = {"__getattribute__", "__getattr__", "__setattr__", "__delattr__"}
PATCH_MULTIPLE_OPTIONS = {
    "target",
    "spec",
    "spec_set",
    "create",
    "new_callable",
    "autospec",
}
PATCH_DICT_OPTIONS = {"in_dict", "values", "clear"}
OWN = {"self", "cls"}

Scope = dict[str, list[ast.expr]]
Seen = frozenset[tuple[int, str]]
Resolved = tuple[ast.expr, list[Scope], Seen]
Passed = dict[tuple[str, str], set[str]]
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEFINITIONS = (*_FUNCTIONS, ast.Lambda, ast.ClassDef)
_SEQUENCES = (ast.Tuple, ast.List, ast.Set)


class _Item(ast.expr):
    """An item a ``for`` takes from ``iterable``, or element ``index`` of it."""

    iterable: ast.expr
    index: int | None


def private(target: str) -> bool:
    parts = target.replace(":", ".").split(".")
    for index, part in enumerate(parts):
        if not part.startswith("_") or (part.startswith("__") and part.endswith("__")):
            continue
        if index == 1 and parts[0] == "ha_mcp" and part == "_vendor":
            continue
        return True
    return False


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(aliases.get(node.id, node.id))
    return ".".join(reversed(parts))


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
    return aliases


# --- What each scope binds ---------------------------------------------------


def _scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Nodes in one scope, leaving nested definitions' bodies to their own scope."""
    for child in ast.iter_child_nodes(scope):
        yield child
        if not isinstance(child, _DEFINITIONS):
            yield from _scope_nodes(child)


def _bind(scope: Scope, target: ast.expr, value: ast.expr | None) -> None:
    if isinstance(target, ast.Name):
        scope.setdefault(target.id, []).append(value or ast.Constant(UNKNOWN))
    elif isinstance(target, (ast.Tuple, ast.List)):
        values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
        same_shape = len(values) == len(target.elts)
        for index, element in enumerate(target.elts):
            _bind(scope, element, values[index] if same_shape else None)


def _bind_loop(scope: Scope, target: ast.expr, iterable: ast.expr) -> None:
    if isinstance(iterable, _SEQUENCES):
        for element in iterable.elts:
            _bind(scope, target, element)
        return
    elements = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
    for index, element in enumerate(elements):
        item = _Item()
        item.iterable, item.index = iterable, (None if element is target else index)
        _bind(scope, element, item)


def _runtime_names(node: ast.AST) -> list[str]:
    """Names a node binds to a value only known at runtime."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(alias.asname or alias.name).split(".")[0] for alias in node.names]
    if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.ExceptHandler):
        return [node.name] if node.name else []
    if isinstance(node, (ast.AugAssign, ast.withitem)):
        stores = (n for n in ast.walk(node) if isinstance(n, ast.Name))
        return [n.id for n in stores if isinstance(n.ctx, ast.Store)]
    return []


def _bind_node(scope: Scope, node: ast.AST) -> None:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            _bind(scope, target, node.value)
    elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
        _bind(scope, node.target, node.value)
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        _bind_loop(scope, node.target, node.iter)
    else:
        for name in _runtime_names(node):
            _bind(scope, ast.Name(name), None)


def _parametrized(function: ast.FunctionDef | ast.AsyncFunctionDef) -> Scope:
    """The values ``pytest.mark.parametrize`` gives each argument."""
    scope: Scope = {}
    for decorator in function.decorator_list:
        if not (
            isinstance(decorator, ast.Call)
            and _dotted(decorator.func, {}).endswith("parametrize")
            and len(decorator.args) >= 2
        ):
            continue
        names, rows = decorator.args[:2]
        if isinstance(names, ast.Constant) and isinstance(names.value, str):
            fields = [name.strip() for name in names.value.split(",")]
        else:
            elements = names.elts if isinstance(names, _SEQUENCES) else []
            fields = [str(e.value) for e in elements if isinstance(e, ast.Constant)]
        if not isinstance(rows, _SEQUENCES):
            # Rows named by a constant: each field is one item of it.
            for index, field in enumerate(fields):
                item = _Item()
                item.iterable, item.index = rows, (index if len(fields) > 1 else None)
                scope.setdefault(field, []).append(item)
            continue
        for row in rows.elts:
            if isinstance(row, ast.Call) and _dotted(row.func, {}).endswith("param"):
                cells = list(row.args)
            elif len(fields) > 1 and isinstance(row, (ast.Tuple, ast.List)):
                cells = list(row.elts)
            else:
                cells = [row]
            for field, cell in zip(fields, cells, strict=False):
                scope.setdefault(field, []).append(cell)
    return scope


def _parameters(
    function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> list[str]:
    arguments = function.args
    every = (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    return [a.arg for a in (*every, *filter(None, (arguments.vararg, arguments.kwarg)))]


def _bindings(scope_node: ast.AST, passed: Passed) -> Scope:
    scope: Scope = {}
    if isinstance(scope_node, (*_FUNCTIONS, ast.Lambda)):
        scope = {name: [ast.Constant(UNKNOWN)] for name in _parameters(scope_node)}
    if isinstance(scope_node, _FUNCTIONS):
        for name, values in _parametrized(scope_node).items():
            scope.setdefault(name, []).extend(values)
        # A local helper's parameter can be any string its callers pass.
        for name in scope:
            for value in sorted(passed.get((scope_node.name, name), ())):
                scope[name].append(ast.Constant(value))
    for node in _scope_nodes(scope_node):
        _bind_node(scope, node)
    return scope


# --- The strings an expression can be ----------------------------------------


def _lookup(name: str, chain: list[Scope], seen: Seen) -> list[Resolved]:
    """The expressions ``name`` is bound to, each with the scopes it resolves in."""
    for depth in range(len(chain) - 1, -1, -1):
        if name in chain[depth]:
            key = (depth, name)
            if key in seen:
                return []
            inner = chain[: depth + 1]
            return [(value, inner, seen | {key}) for value in chain[depth][name]]
    return []


def _sequences(node: ast.expr, chain: list[Scope], seen: Seen) -> list[Resolved]:
    """The literal tuples, lists and sets an iterable expression can be."""
    if isinstance(node, _SEQUENCES):
        return [(node, chain, seen)]
    if isinstance(node, ast.Name):
        return [s for v in _lookup(node.id, chain, seen) for s in _sequences(*v)]
    return []


def _items(item: _Item, chain: list[Scope], seen: Seen) -> list[Resolved]:
    found: list[Resolved] = []
    for sequence, inner, inner_seen in _sequences(item.iterable, chain, seen):
        for element in sequence.elts:  # type: ignore[attr-defined]
            if item.index is None:
                found.append((element, inner, inner_seen))
            elif (
                isinstance(element, (ast.Tuple, ast.List))
                and len(element.elts) > item.index
            ):
                found.append((element.elts[item.index], inner, inner_seen))
    return found


def _parts(node: ast.expr | None) -> list[ast.expr] | None:
    """The pieces a concatenation or f-string joins, or None for anything else."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [node.left, node.right]
    if isinstance(node, ast.JoinedStr):
        return node.values
    if isinstance(node, ast.FormattedValue):
        plain = node.conversion == -1 and node.format_spec is None
        return [node.value] if plain else None
    return None


def strings(
    node: ast.expr | None, chain: list[Scope], seen: Seen = frozenset()
) -> set[str]:
    """Every string the expression can be, with ``UNKNOWN`` for runtime parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Name, _Item)):
        if isinstance(node, ast.Name):
            values = _lookup(node.id, chain, seen)
        else:
            values = _items(node, chain, seen)
        return set().union(*(strings(*value) for value in values)) or {UNKNOWN}
    if isinstance(node, ast.IfExp):
        return strings(node.body, chain, seen) | strings(node.orelse, chain, seen)
    parts = _parts(node)
    if parts is None:
        return {UNKNOWN}
    alternatives = [sorted(strings(p, chain, seen)) for p in parts]
    # A part already over the limit makes the whole expression over it.
    if any(TOO_MANY in a for a in alternatives) or (
        math.prod(len(a) for a in alternatives) > MAX_ALTERNATIVES
    ):
        return {TOO_MANY}
    return {"".join(combo) for combo in itertools.product(*alternatives)}


# --- Which arguments name a target --------------------------------------------


def _own(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Name) and node.id in OWN


def _namespace(node: ast.expr | None, aliases: dict[str, str]) -> ast.expr | None:
    """The object behind ``vars(x)`` or ``x.__dict__``, if ``node`` is one."""
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return node.value
    if isinstance(node, ast.Call) and node.args:
        if _dotted(node.func, aliases) in {"vars", "builtins.vars"}:
            return node.args[0]
    return None


def _argument(call: ast.Call, index: int, keyword: str = "") -> ast.expr | None:
    leading = call.args[: index + 1]
    if len(leading) > index and not any(isinstance(a, ast.Starred) for a in leading):
        return call.args[index]
    return next((k.value for k in call.keywords if keyword and k.arg == keyword), None)


def _keyword_names(call: ast.Call, options: set[str]) -> list[ast.expr]:
    return [
        ast.Constant(k.arg) for k in call.keywords if k.arg and k.arg not in options
    ]


def _patch_targets(call: ast.Call, tail: str, aliases: dict[str, str]) -> list:
    if tail == "object":
        if _own(_argument(call, 0, "target")):
            return []
        return [_argument(call, 1, "attribute")]
    if tail == "multiple":
        target = _argument(call, 0, "target")
        if _own(target):
            return []
        return [target, *_keyword_names(call, PATCH_MULTIPLE_OPTIONS)]
    target = _argument(call, 0, "in_dict")
    namespace = _namespace(target, aliases)
    if namespace is None:
        return [target]
    if _own(namespace):
        return []
    values = _argument(call, 1, "values")
    keys = values.keys if isinstance(values, ast.Dict) else []
    return [*keys, *_keyword_names(call, PATCH_DICT_OPTIONS)]


def _attribute_targets(call: ast.Call, name: str, tail: str) -> list:
    """Targets of the setattr/getattr family, builtin or ``monkeypatch``."""
    first, second = _argument(call, 0, "target"), _argument(call, 1, "name")
    if tail in DUNDER_REFLECTION:
        receiver = call.func.value  # type: ignore[attr-defined]
        if not (isinstance(receiver, ast.Name) and receiver.id in {"object", "type"}):
            return [] if _own(receiver) else [first]
    if _own(first):
        return []
    builtin = name in {tail, f"builtins.{tail}"}
    # monkeypatch.setattr("pkg.mod.name", value) names its target in one string.
    if not builtin and tail in {"setattr", "delattr"}:
        dotted_form = len(call.args) < (3 if tail == "setattr" else 2)
        if dotted_form and not any(k.arg == "name" for k in call.keywords):
            return [first]
        return [first, second]
    return [second] if builtin or tail in DUNDER_REFLECTION else []


def _namespace_targets(call: ast.Call, tail: str, aliases: dict[str, str]) -> list:
    """Keys of ``vars(x)``/``x.__dict__`` that ``setitem`` or ``.get`` reach."""
    if tail in {"setitem", "delitem"}:
        namespace, key = _namespace(_argument(call, 0), aliases), _argument(call, 1)
    elif tail in {"get", "pop", "setdefault"} and isinstance(call.func, ast.Attribute):
        namespace, key = _namespace(call.func.value, aliases), _argument(call, 0)
    else:
        return []
    return [key] if namespace is not None and not _own(namespace) else []


def _call_targets(call: ast.Call, aliases: dict[str, str]) -> list:
    """Expressions in ``call`` that name an attribute, module or patch target."""
    name = _dotted(call.func, aliases)
    tail = (
        call.func.attr
        if isinstance(call.func, ast.Attribute)
        else name.rsplit(".", 1)[-1]
    )
    owner = name.rsplit(".", 2)[-2] if name.count(".") else ""
    if tail in REFLECTION or (
        tail in DUNDER_REFLECTION and isinstance(call.func, ast.Attribute)
    ):
        return _attribute_targets(call, name, tail)
    if tail == "getattr_static":
        return [] if _own(_argument(call, 0)) else [_argument(call, 1, "attr")]
    if tail == "attrgetter":
        return list(call.args)
    if tail in {"import_module", "__import__"}:
        return [_argument(call, 0, "name")]
    if owner == "patch" and tail in {"object", "dict", "multiple"}:
        return _patch_targets(call, tail, aliases)
    if tail == "patch":
        return [_argument(call, 0, "target")]
    return _namespace_targets(call, tail, aliases)


# --- Scanning a file ------------------------------------------------------------


class _Scanner:
    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.aliases = _import_aliases(tree)
        # Every definition of a name: a call may reach any of them.
        self.signatures: dict[str, list[list[str]]] = {}
        for node in ast.walk(tree):
            if isinstance(node, _FUNCTIONS):
                self.signatures.setdefault(node.name, []).append(_parameters(node))
        self.passed: Passed = {}
        self.findings: set[tuple[int, str]] = set()

    def run(self) -> list[tuple[int, str]]:
        # Repeat until helpers passing strings on to other helpers add no more.
        for _ in range(len(self.signatures) + 1):
            before = {key: set(values) for key, values in self.passed.items()}
            self.findings.clear()
            self.visit(self.tree, [_bindings(self.tree, self.passed)])
            if self.passed == before:
                break
        return sorted(self.findings)

    def check(
        self, line: int, targets: Iterable[ast.expr | None], chain: list[Scope]
    ) -> None:
        for target in targets:
            self.findings.update(
                (line, s) for s in strings(target, chain) if s == TOO_MANY or private(s)
            )

    def record_arguments(self, call: ast.Call, chain: list[Scope]) -> None:
        if not isinstance(call.func, ast.Name) or call.func.id not in self.signatures:
            return
        named = [(k.arg, k.value) for k in call.keywords if k.arg]
        for parameters in self.signatures[call.func.id]:
            for index, value in enumerate(call.args):
                if isinstance(value, ast.Starred) or index >= len(parameters):
                    break
                named.append((parameters[index], value))
        for parameter, value in named:
            known = strings(value, chain) - {UNKNOWN}
            self.passed.setdefault((call.func.id, parameter), set()).update(known)

    def visit_definition(self, node: ast.AST, chain: list[Scope]) -> None:
        header: list[ast.AST] = list(getattr(node, "decorator_list", []))
        if isinstance(node, ast.ClassDef):
            header += [*node.bases, *node.keywords]
        else:
            arguments = node.args  # type: ignore[attr-defined]
            header += [*arguments.defaults, *filter(None, arguments.kw_defaults)]
        for child in header:
            self.visit(child, chain)
        inner = [*chain, _bindings(node, self.passed)]
        body = node.body if isinstance(node.body, list) else [node.body]  # type: ignore[attr-defined]
        for statement in body:
            # A method does not see the names its class body binds.
            method = isinstance(node, ast.ClassDef) and isinstance(
                statement, _DEFINITIONS
            )
            self.visit(statement, chain if method else inner)

    def visit(self, node: ast.AST, chain: list[Scope]) -> None:
        if isinstance(node, _DEFINITIONS):
            self.visit_definition(node, chain)
            return
        if isinstance(node, ast.Call):
            self.record_arguments(node, chain)
            self.check(node.lineno, _call_targets(node, self.aliases), chain)
        elif isinstance(node, ast.Subscript):
            namespace = _namespace(node.value, self.aliases)
            if namespace is not None and not _own(namespace):
                self.check(node.lineno, [node.slice], chain)
        for child in ast.iter_child_nodes(node):
            self.visit(child, chain)


def scan(tree: ast.Module) -> list[tuple[int, str]]:
    return _Scanner(tree).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO)
    args = parser.parse_args(argv)
    violations: list[str] = []
    scanned = 0
    for tree in TREES:
        files = sorted((args.repo / tree).rglob("*.py"))
        if not files:
            violations.append(f"{tree}: no Python files to check")
        for path in files:
            scanned += 1
            name = path.relative_to(args.repo).as_posix()
            try:
                module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as error:
                violations.append(f"{name}:{error.lineno}: {error.msg}")
                continue
            violations.extend(
                f"{name}:{line}: "
                + (
                    f"more than {MAX_ALTERNATIVES} possible names to check; "
                    "name the targets directly"
                    if target == TOO_MANY
                    else f"private name {target!r} reached through a string"
                )
                for line, target in scan(module)
            )
    if violations:
        print("\n".join(violations))
        print("Use the public entry point or real configuration; no test is exempt.")
        return 1
    print(f"No private string targets in {scanned} unit and app test files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
