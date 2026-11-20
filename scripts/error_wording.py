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
error's ``message``) field of a reply, and anything built from those. The text is followed through assignments, through
arguments into the functions they are passed to, and out of the functions that
return it. A decision is a membership, prefix, suffix or regex test on such
text, or its comparison with a literal that is not a bare code (``not_found``).
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

type Scope = ast.FunctionDef | ast.AsyncFunctionDef | ast.Module


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


class Program:
    """Every scanned function, and which of them take or return error text."""

    def __init__(self) -> None:
        self.text_params: dict[str, set[str]] = {}
        self.returns_text: set[str] = set()
        self.prose: set[str] = set()
        self.changed = False

    def pass_text(self, func: str, param: str) -> None:
        if param not in self.text_params.setdefault(func, set()):
            self.text_params[func].add(param)
            self.changed = True


class Flow:
    """Which names in one scope hold an exception and which hold error text."""

    def __init__(self, scope: Scope, program: Program, outer: Flow | None) -> None:
        self.scope = scope
        self.program = program
        # A nested function reads the names of the function around it.
        self.exceptions: set[str] = set(outer.exceptions) if outer else set()
        self.texts: set[str] = set(outer.texts) if outer else set()
        self.fixed: set[str] = set(outer.fixed) if outer else set()
        self.nodes = list(_body(scope))
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
        if self.scope.args.vararg:
            self.fixed.add(self.scope.args.vararg.arg)
        text_params = self.program.text_params.get(self.scope.name, ())
        for param in _params(self.scope):
            note = ast.unparse(param.annotation) if param.annotation else ""
            if param.arg in text_params:
                self.texts.add(param.arg)
            elif (
                "Exception" in note
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
                return self.is_text(receiver)
        return _name(node.func) in self.program.returns_text

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
            case ast.BoolOp():
                return any(self.is_text(value) for value in node.values)
            case ast.IfExp():
                return self.is_text(node.body) or self.is_text(node.orelse)
        return False

    def spread(self) -> None:
        """Follow error text into the functions it is passed to and returned from."""
        name = getattr(self.scope, "name", "")
        for node in self.nodes:
            if isinstance(node, ast.Return) and node.value and self.is_text(node.value):
                if name not in self.program.returns_text:
                    self.program.returns_text.add(name)
                    self.program.changed = True
            if not isinstance(node, ast.Call):
                continue
            for target in FUNCTIONS.get(_name(node.func), ()):
                params = _params(target)
                for index, arg in enumerate(node.args[: len(params)]):
                    if self.is_text(arg):
                        self.program.pass_text(target.name, params[index].arg)
                for keyword in node.keywords:
                    if keyword.arg and self.is_text(keyword.value):
                        self.program.pass_text(target.name, keyword.arg)

    def _prose(self, node: ast.expr) -> bool:
        """Prose: a literal, a collection holding one, or a module constant
        that does. A bare code such as ``not_found`` is not."""
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(self._prose(item) for item in node.elts)
        if isinstance(node, ast.Call) and _name(node.func) in STR_METHODS:
            node = getattr(node.func, "value", node)
        return _wording(node) or _name(node) in self.program.prose

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
        attr = _name(node.func)
        return (
            attr in TEXT_TESTS and self.is_text(getattr(node.func, "value", node))
        ) or (attr in REGEX_TESTS and any(self.is_text(arg) for arg in node.args[:2]))

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
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if self._tests_wording(node):
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


FUNCTIONS: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}


def scan(sources: dict[str, str]) -> list[str]:
    """Return ``path:line: code`` for every wording decision in ``sources``."""
    scopes: list[tuple[str, Scope, Scope | None]] = []
    FUNCTIONS.clear()

    def collect(path: str, scope: Scope, outer: Scope | None) -> None:
        scopes.append((path, scope, outer))
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                FUNCTIONS.setdefault(node.name, []).append(node)
                collect(path, node, scope)
            else:
                stack.extend(ast.iter_child_nodes(node))

    program = Program()
    for path, source in sources.items():
        tree = ast.parse(source, path)
        program.prose |= _prose_constants(tree)
        collect(path, tree, None)
    program.changed = True
    while program.changed:
        program.changed = False
        built: dict[Scope, Flow] = {}
        flows = []
        for path, scope, outer in scopes:
            built[scope] = Flow(scope, program, built.get(outer) if outer else None)
            flows.append((path, built[scope]))
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
