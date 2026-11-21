#!/usr/bin/env python3
"""Refuse production code that decides by the wording of an error.

The wording of an error belongs to the program that wrote it. Home Assistant
Core, the Supervisor, the frontend, Python and FastMCP all reword theirs
freely; the code, HTTP status and exception type are what they keep. A branch
on the wording works until the next release, then fails without a trace.

Where the sender offers nothing but its wording, the test goes into
``src/ha_mcp/error_wording.py`` or the component's ``error_wording.py``, under
a comment that names the source sending the text. This script fails on a
wording test in any other file of ``src/ha_mcp`` and ``custom_components``
(vendored libraries are not scanned). There is no list of exceptions.

What counts as error text: ``str()`` or ``repr()`` of a caught exception, its
``args``, ``message`` or ``msg``, the ``error`` or ``error_message`` (or an
error's ``message``) field of a reply, and anything built from those. The text
is followed through assignments, through arguments into the functions they are
passed to, and out of the functions that return it. A call is followed to the
function its name is bound to: one defined around the call, one imported, or,
for a call on an object, the methods of that name. A decision is a membership,
prefix, suffix or regex test on such text, or its comparison with a literal
that is not a bare code (``not_found``).
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREES = ("src/ha_mcp", "custom_components")
HOMES = frozenset(
    {"src/ha_mcp/error_wording.py", "custom_components/ha_mcp_tools/error_wording.py"}
)

EXCEPTION_PARAMS = {"e", "exc", "err", "error", "exception"}
REPLY_KEYS = {"error", "error_message"}
STR_METHODS = {
    "casefold",
    "format",
    "lower",
    "lstrip",
    "removeprefix",
    "removesuffix",
    "replace",
    "rstrip",
    "strip",
    "upper",
}
TEXT_TESTS = {"count", "endswith", "find", "index", "startswith"}
REGEX_TESTS = {"findall", "fullmatch", "match", "search"}
CODE = re.compile(r"[a-z][a-z0-9_]*")
IMPORT_HOPS = 5

type Func = ast.FunctionDef | ast.AsyncFunctionDef
type Scope = Func | ast.Module


def _name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _body(scope: Scope) -> Iterator[ast.AST]:
    """The nodes of one scope, without the functions and classes nested in it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _params(scope: Scope) -> list[ast.arg]:
    if isinstance(scope, ast.Module):
        return []
    params = [*scope.args.posonlyargs, *scope.args.args]
    if params and params[0].arg in {"self", "cls"}:
        params = params[1:]
    return [*params, *scope.args.kwonlyargs]


class Module:
    """One file: its functions, what it imports, and its prose constants."""

    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.tree = tree
        # Functions by name, per scope that defines them (the module included).
        self.defined: dict[Scope, dict[str, list[Func]]] = {}
        # Functions defined in a class body, by name.
        self.methods: dict[str, list[Func]] = {}
        # ``from x import name as local``: local -> (x, name).
        self.names: dict[str, tuple[Module, str]] = {}
        # ``from . import x`` or ``import a.x as x``: local -> x.
        self.modules: dict[str, Module] = {}
        # ``from re import search``: the local names of re's tests.
        self.regex: set[str] = set()
        self.prose = _prose_constants(tree)
        self.scopes: list[tuple[Scope, Scope | None]] = []
        self._collect(tree, None)

    def _collect(self, scope: Scope, outer: Scope | None) -> None:
        self.scopes.append((scope, outer))
        defined = self.defined.setdefault(scope, {})
        stack = [(child, False) for child in ast.iter_child_nodes(scope)]
        while stack:
            node, in_class = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = self.methods if in_class else defined
                owner.setdefault(node.name, []).append(node)
                self._collect(node, scope)
            else:
                inside = in_class or isinstance(node, ast.ClassDef)
                stack.extend((child, inside) for child in ast.iter_child_nodes(node))


class Program:
    """Every scanned module, and which functions take or return error text."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.modules = [
            Module(path, ast.parse(source, path)) for path, source in sources.items()
        ]
        self._by_stem = {
            module.path.removesuffix(".py").removesuffix("/__init__"): module
            for module in self.modules
        }
        self.methods: dict[str, list[Func]] = {}
        for module in self.modules:
            for name, found in module.methods.items():
                self.methods.setdefault(name, []).extend(found)
            self._read_imports(module)
        self.text_params: dict[Func, set[str]] = {}
        self.exception_params: dict[Func, set[str]] = {}
        self.returns_text: set[Func] = set()
        self.changed = False

    def _find(self, parts: list[str], *, exact: bool) -> Module | None:
        stem = "/".join(parts)
        if stem in self._by_stem or exact:
            return self._by_stem.get(stem)
        ends = [m for s, m in self._by_stem.items() if s.endswith("/" + stem)]
        return ends[0] if len(ends) == 1 else None

    def _read_imports(self, module: Module) -> None:
        here = module.path.split("/")[:-1]
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found = self._find(alias.name.split("."), exact=False)
                    if found and alias.asname:
                        module.modules[alias.asname] = found
            elif isinstance(node, ast.ImportFrom):
                named = node.module.split(".") if node.module else []
                if node.level == 0 and named == ["re"]:
                    module.regex.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name in REGEX_TESTS
                    )
                    continue
                relative = node.level > 0
                parts = (
                    [*here[: len(here) - node.level + 1], *named] if relative else named
                )
                source = self._find(parts, exact=relative)
                for alias in node.names:
                    local = alias.asname or alias.name
                    submodule = self._find([*parts, alias.name], exact=relative)
                    if submodule:
                        module.modules[local] = submodule
                    elif source:
                        module.names[local] = (source, alias.name)

    def functions(self, module: Module, name: str, hops: int = 0) -> list[Func]:
        """The module-level functions ``name`` is bound to in ``module``."""
        defined = module.defined[module.tree].get(name)
        if defined or name not in module.names or hops == IMPORT_HOPS:
            return defined or []
        source, original = module.names[name]
        return self.functions(source, original, hops + 1)

    def is_prose(self, module: Module, name: str, hops: int = 0) -> bool:
        """Whether ``name`` is bound to a prose constant in ``module``."""
        if name in module.prose or name not in module.names or hops == IMPORT_HOPS:
            return name in module.prose
        source, original = module.names[name]
        return self.is_prose(source, original, hops + 1)

    def pass_on(self, func: Func, param: str, *, exception: bool) -> None:
        """Record that ``param`` of ``func`` receives error text or an exception."""
        params = self.exception_params if exception else self.text_params
        if param not in params.setdefault(func, set()):
            params[func].add(param)
            self.changed = True


def _visible(
    node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
) -> dict[ast.AST, set[str]]:
    """For each node of a comprehension, the targets bound where it runs: an
    iterable sees the targets of the ``for`` clauses before its own, and the
    conditions and the element see every target so far."""
    seen: set[str] = set()
    visible: dict[ast.AST, set[str]] = {}
    for each in node.generators:
        visible.update((inner, set(seen)) for inner in ast.walk(each.iter))
        seen |= {n.id for n in ast.walk(each.target) if isinstance(n, ast.Name)}
        for condition in each.ifs:
            visible.update((inner, set(seen)) for inner in ast.walk(condition))
    parts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
    for part in parts:
        visible.update((inner, seen) for inner in ast.walk(part))
    return visible


def _bindings(
    scope: Scope, nodes: list[ast.AST]
) -> tuple[set[str], dict[ast.AST, set[str]]]:
    """The names a scope binds itself, other than its functions, and for every
    node inside a comprehension the targets bound there: a comprehension's
    target is bound in the comprehension alone."""
    looped: dict[ast.AST, set[str]] = {}
    targets: set[ast.AST] = set()
    for node in nodes:
        if isinstance(
            node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            for each in node.generators:
                targets.update(ast.walk(each.target))
            for inner, names in _visible(node).items():
                looped.setdefault(inner, set()).update(names)
    bound = {
        node.id
        for node in nodes
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        and node not in targets
    }
    if not isinstance(scope, ast.Module):
        bound |= {arg.arg for arg in ast.walk(scope.args) if isinstance(arg, ast.arg)}
    return bound, looped


class Flow:
    """Which names in one scope hold an exception and which hold error text."""

    def __init__(
        self, scope: Scope, module: Module, program: Program, outer: Flow | None
    ) -> None:
        self.scope = scope
        self.module = module
        self.program = program
        self.outer = outer
        # A nested function reads the names of the function around it.
        self.exceptions: set[str] = set(outer.exceptions) if outer else set()
        self.texts: set[str] = set(outer.texts) if outer else set()
        self.fixed: set[str] = set(outer.fixed) if outer else set()
        self.nodes = list(_body(scope))
        self.bound, self.looped = _bindings(scope, self.nodes)
        self._read_params()
        for node in self.nodes:
            if isinstance(node, ast.ExceptHandler) and node.name:
                self.exceptions.add(node.name)
            elif isinstance(node, (ast.For, ast.comprehension)):
                names = {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
                if self._fixed(node.iter):
                    self.fixed.update(names)
                elif _name(node.iter) == "exceptions":  # an exception group's
                    self.exceptions.update(names)
        while self._follow_assignments():
            pass

    def _read_params(self) -> None:
        if isinstance(self.scope, ast.Module):
            return
        # A parameter hides the outer name it repeats.
        own = {param.arg for param in _params(self.scope)}
        self.texts -= own
        self.exceptions -= own
        self.fixed -= own
        if self.scope.args.vararg:
            self.fixed.add(self.scope.args.vararg.arg)
        text_params = self.program.text_params.get(self.scope, ())
        exception_params = self.program.exception_params.get(self.scope, ())
        for param in _params(self.scope):
            note = ast.unparse(param.annotation) if param.annotation else ""
            if param.arg in text_params:
                self.texts.add(param.arg)
            elif (
                param.arg in exception_params
                or "Exception" in note
                or "Error" in note
                or (param.arg in EXCEPTION_PARAMS and note in {"", "Any"})
            ):
                self.exceptions.add(param.arg)

    def _follow_assignments(self) -> bool:
        """Mark the names assigned error text or an exception; True while new
        ones turn up."""
        before = len(self.texts) + len(self.exceptions)
        for node in self.nodes:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value:
                targets, value = [node.target], node.value
            else:
                continue
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if self.is_text(value):
                self.texts.update(names)
            elif self.is_exception(value):
                self.exceptions.update(names)
        return len(self.texts) + len(self.exceptions) > before

    def _fixed(self, node: ast.expr) -> bool:
        """Wording written in the source: a literal, a constant, or a name
        that stands for one (a ``*args`` item, or a loop over literals)."""
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(self._fixed(item) for item in node.elts)
        if isinstance(node, ast.Name):
            return node.id.isupper() or node.id in self.fixed
        if isinstance(node, ast.Attribute):
            return node.attr.isupper()
        # Any literal: a one-word needle is a wording test all the same.
        return isinstance(node, ast.JoinedStr) or (
            isinstance(node, ast.Constant) and isinstance(node.value, str)
        )

    def _targets(self, func: ast.expr) -> list[Func]:
        """The functions a call may run: the one its name is bound to, or, on
        an object whose class is not known here, every method of that name."""
        if isinstance(func, ast.Name):
            if func.id in self.looped.get(func, ()):
                return []
            flow: Flow | None = self
            while flow:
                if func.id in flow.module.defined[flow.scope]:
                    return flow.module.defined[flow.scope][func.id]
                if func.id in flow.bound:  # a callback, not the function
                    return []
                flow = flow.outer
            return self.program.functions(self.module, func.id)
        if not isinstance(func, ast.Attribute):
            return []
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id in self.module.modules:
            return self.program.functions(self.module.modules[owner.id], func.attr)
        own = self.module.methods.get(func.attr)
        if own and isinstance(owner, ast.Name) and owner.id in {"self", "cls"}:
            return own
        return self.program.methods.get(func.attr, [])

    def is_exception(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Attribute) and node.attr in {
            "__cause__",
            "__context__",
        }:
            return self.is_exception(node.value)
        if isinstance(node, ast.Call):  # ``task.exception()``
            return _name(node.func) == "exception" and not node.args
        return isinstance(node, ast.Name) and node.id in self.exceptions

    def _is_error_field(self, receiver: ast.expr, key: ast.expr) -> bool:
        if not isinstance(key, ast.Constant):
            return False
        if key.value in REPLY_KEYS:
            return True
        if isinstance(receiver, ast.BoolOp):  # ``(error or {}).get("message")``
            receiver = receiver.values[0]
        return key.value == "message" and (
            self.is_text(receiver)
            or (
                isinstance(receiver, ast.Attribute) and receiver.attr == "response_data"
            )
        )

    def _call_is_text(self, node: ast.Call) -> bool:
        match node:
            case ast.Call(func=ast.Name(id="str" | "repr"), args=[arg, *_]):
                return self.is_exception(arg) or self.is_text(arg)
            case ast.Call(
                func=ast.Attribute(attr="get", value=receiver), args=[key, *_]
            ):
                return self._is_error_field(receiver, key)
            case ast.Call(func=ast.Attribute(attr=attr, value=receiver)) if (
                attr in STR_METHODS
            ):
                filled = [*node.args, *(k.value for k in node.keywords)]
                return self.is_text(receiver) or (
                    attr == "format"
                    and any(self.is_text(a) or self.is_exception(a) for a in filled)
                )
        return any(
            target in self.program.returns_text for target in self._targets(node.func)
        )

    def is_text(self, node: ast.AST) -> bool:
        match node:
            case ast.Name():
                return node.id in self.texts
            case ast.Call():
                return self._call_is_text(node)
            case ast.Subscript(value=ast.Attribute(attr="args", value=owner)):
                return self.is_exception(owner)
            case ast.Subscript():
                return self._is_error_field(node.value, node.slice) or (
                    isinstance(node.slice, ast.Slice) and self.is_text(node.value)
                )
            case ast.Attribute(attr="message" | "msg"):
                return self.is_exception(node.value)
            case ast.JoinedStr():
                return any(
                    isinstance(part, ast.FormattedValue)
                    and (self.is_text(part.value) or self.is_exception(part.value))
                    for part in node.values
                )
            case ast.BinOp(op=ast.Add() | ast.Mod()):  # ``"failed: " + str(exc)``
                right = node.right.elts if isinstance(node.right, ast.Tuple) else []
                return any(
                    self.is_text(part) for part in (node.left, node.right, *right)
                )
            case ast.BoolOp():
                return any(self.is_text(value) for value in node.values)
            case ast.IfExp():
                return self.is_text(node.body) or self.is_text(node.orelse)
        return False

    def spread(self) -> None:
        """Follow error text into the functions it is passed to and returned from."""
        for node in self.nodes:
            if isinstance(node, ast.Return) and node.value and self.is_text(node.value):
                if (
                    not isinstance(self.scope, ast.Module)
                    and self.scope not in self.program.returns_text
                ):
                    self.program.returns_text.add(self.scope)
                    self.program.changed = True
            if not isinstance(node, ast.Call):
                continue
            for target in self._targets(node.func):
                params = _params(target)
                passed = [
                    *zip((p.arg for p in params), node.args, strict=False),
                    *((k.arg, k.value) for k in node.keywords if k.arg),
                ]
                for param, value in passed:
                    if self.is_text(value):
                        self.program.pass_on(target, param, exception=False)
                    elif self.is_exception(value):
                        self.program.pass_on(target, param, exception=True)

    def _prose_constant(self, node: ast.expr) -> bool:
        """A name bound to prose: this module's constant, or an imported one."""
        if isinstance(node, ast.Name):
            return self.program.is_prose(self.module, node.id)
        if not isinstance(node, ast.Attribute):
            return False
        owner = node.value
        if isinstance(owner, ast.Name) and owner.id in self.module.modules:
            return node.attr in self.module.modules[owner.id].prose
        return node.attr in self.module.prose

    def _prose(self, node: ast.expr) -> bool:
        """Prose: a literal, a collection holding one, or a module constant
        that does. A bare code such as ``not_found`` is not."""
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(self._prose(item) for item in node.elts)
        if isinstance(node, ast.Call) and _name(node.func) in STR_METHODS:
            node = getattr(node.func, "value", node)
        return _wording(node) or self._prose_constant(node)

    def _compares_wording(self, node: ast.Compare) -> bool:
        sides = [node.left, *node.comparators]
        for op, left, right in zip(node.ops, sides, sides[1:], strict=False):
            if isinstance(op, (ast.In, ast.NotIn)):
                # A needle in the text, or the text among whole messages.
                found = (self.is_text(right) and self._fixed(left)) or (
                    self.is_text(left) and self._prose(right)
                )
            elif isinstance(op, (ast.Eq, ast.NotEq)):
                found = (self.is_text(left) and self._prose(right)) or (
                    self.is_text(right) and self._prose(left)
                )
            else:
                continue
            if found:
                return True
        return False

    def _tests_wording(self, node: ast.Call) -> bool:
        func = node.func
        searched = any(self.is_text(arg) for arg in node.args[:2])
        if isinstance(func, ast.Name):  # ``from re import search``
            return func.id in self.module.regex and searched
        if not isinstance(func, ast.Attribute):
            return False
        return (func.attr in TEXT_TESTS and self.is_text(func.value)) or (
            func.attr in REGEX_TESTS and searched
        )

    def _matches_wording(self, node: ast.Match) -> bool:
        return self.is_text(node.subject) and any(
            _wording(value) for case in node.cases for value in _values(case.pattern)
        )

    def decisions(self) -> Iterator[ast.Compare | ast.Call | ast.Match]:
        for node in self.nodes:
            if isinstance(node, ast.Compare):
                if self._compares_wording(node):
                    yield node
            elif isinstance(node, ast.Match):
                if self._matches_wording(node):
                    yield node
            elif isinstance(node, ast.Call) and self._tests_wording(node):
                yield node


def _values(pattern: ast.pattern) -> list[ast.expr]:
    """The literals a ``case`` compares against."""
    if isinstance(pattern, ast.MatchValue):
        return [pattern.value]
    if isinstance(pattern, ast.MatchOr):
        return [value for each in pattern.patterns for value in _values(each)]
    return []


def _prose_constants(tree: ast.Module) -> set[str]:
    """Module-level names bound to prose or to a collection holding some."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value:
            targets, value = [node.target], node.value
        else:
            continue
        if any(
            isinstance(part, ast.expr) and _wording(part) for part in ast.walk(value)
        ):
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _wording(node: ast.expr) -> bool:
    """A literal that is prose, not a bare code such as ``not_found``."""
    if isinstance(node, ast.JoinedStr):
        return True
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and bool(node.value)
        and not CODE.fullmatch(node.value)
    )


def scan(sources: dict[str, str]) -> list[str]:
    """Return ``path:line: code`` for every wording decision in ``sources``."""
    program = Program(sources)
    program.changed = True
    flows: list[tuple[str, Flow]] = []
    while program.changed:
        program.changed = False
        flows = []
        for module in program.modules:
            built: dict[Scope, Flow] = {}
            for scope, outer in module.scopes:
                around = built[outer] if outer else None
                built[scope] = Flow(scope, module, program, around)
                flows.append((module.path, built[scope]))
        for _path, flow in flows:
            flow.spread()
    found = {
        (path, node.lineno, ast.unparse(node).replace("\n", " "))
        for path, flow in flows
        if path not in HOMES
        for node in flow.decisions()
    }
    return [f"{path}:{line}: {code[:120]}" for path, line, code in sorted(found)]


def main() -> int:
    sources = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
        for tree in TREES
        for path in sorted((ROOT / tree).rglob("*.py"))
        if "_vendor" not in path.parts
    }
    found = scan(sources)
    for line in found:
        print(line)
    if found:
        print(
            f"\n{len(found)} decision(s) by the wording of an error. Use the code, "
            f"status or exception type the sender provides; where it provides none, "
            "put the test in error_wording.py under the source that sends the text.",
            file=sys.stderr,
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
