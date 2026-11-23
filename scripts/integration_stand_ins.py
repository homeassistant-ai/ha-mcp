#!/usr/bin/env python3
"""Refuse Core's test helpers registered under a real integration's name.

``mock_component``, ``MockModule`` (the module ``mock_integration`` registers),
``mock_platform``, ``mock_config_flow`` and ``hass.config.components.add`` give
a unit or app test a stand-in under the name it passes. Under a real Home
Assistant integration's name the test runs against that stand-in while the
integration's own code never runs (#3178). Core's own tests register mock
platforms under a made-up domain and set a real integration up with
``async_setup_component`` or a config entry.

The real integrations are the packages with a manifest in the installed
``homeassistant.components``. A name counts when it is a string literal, a
constant (also one imported from another module or a class attribute), a loop
or ``parametrize`` value (a dict's keys or values, ``enumerate`` and ``zip``
columns included), an f-string, concatenation, ``%``, ``str.format`` or
``str.join`` of those, or a value passed on through the parameters of
functions in the test trees, or of methods called on ``self`` or ``cls``,
until it reaches one of the helpers. Within one function a later plain
assignment replaces an earlier one, and ``zip`` stops at its shortest input.
More than ``MAX_ALTERNATIVES`` possible names fail, since they are not checked.
A name only known at runtime is left to review; ``--all`` lists where one
reaches a helper, also through a parameter no call in the trees fills (a
fixture's, for example).

A kept case is an entry of ``ALLOWLIST`` naming the file, the integration and
the reason. An entry that matches nothing fails.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import itertools
import math
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import NamedTuple

import yaml  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stand_in_name_parts import (  # type: ignore[import-not-found]
    name_parts,
)

REPO = Path(__file__).resolve().parent.parent
TREES = ("tests/src/unit", "tests/addon")
ALLOWLIST = "tests/src/unit/integration_stand_in_allowlist.yml"
ENTRY_KEYS = ("file", "integration", "reason")
# Each helper and the argument that names the integration: (position, keyword).
SINKS: dict[str, tuple[int, str | None]] = {
    "mock_component": (1, "component"),
    "MockModule": (0, "domain"),
    "mock_platform": (1, "platform_path"),
    "mock_config_flow": (0, "domain"),
    "config.components.add": (0, None),
}
UNKNOWN = "\0"
TOO_MANY = "\1"  # more alternatives than are checked: reported, never dropped
MAX_ALTERNATIVES = 256
# The calls a ``for`` over takes the same elements from as from their argument.
_SAME_ITEMS = ("sorted", "list", "tuple", "set", "frozenset")
_AROUND = "\x04"  # where a wrapped parameter's name goes
_MARKER = re.compile("\x02([0-9]+)\x03")
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEFINITIONS = (*_FUNCTIONS, ast.Lambda, ast.ClassDef)
_SEQUENCES = (ast.Tuple, ast.List, ast.Set)

Chain = list["Scope"]
Bound = tuple[object, "Chain | None"]  # a value, and the scopes it resolves in
Scope = dict[str, list[Bound]]
FunctionKey = tuple[str, str, int, int]
# (function key, parameter, mode): the mode says what of the argument names
# integrations: "value" the argument, "item" what a ``for`` over it takes, and
# "dict_value" its values; "item:1" or "dict_value:1" takes element 1 of each;
# a "|prefix\x04suffix" ending wraps each name in text the helper adds.
Param = tuple[FunctionKey, str, str]


class _Imported(NamedTuple):
    module: str
    name: str


class _ModuleRef(NamedTuple):
    module: str


class _Each(NamedTuple):
    """What a ``for`` takes from ``iterable``, or element ``column`` of it."""

    iterable: ast.expr
    column: int | None


class _Module:
    def __init__(self, name: str, path: str, tree: ast.Module) -> None:
        self.name = name
        self.path = path
        self.tree = tree
        self.top: Scope = {}


class Finding(NamedTuple):
    path: str
    line: int
    integration: str
    via: str = ""


def real_integrations() -> frozenset[str]:
    """The integrations of the installed Home Assistant."""
    spec = importlib.util.find_spec("homeassistant")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("Home Assistant is not installed: run `uv sync --dev`")
    return frozenset(
        manifest.parent.name
        for location in spec.submodule_search_locations
        for manifest in Path(location, "components").glob("*/manifest.json")
    )


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> list[str]:
    args = node.args
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


def _key(module: str, node: ast.AST) -> tuple[str, str, int, int]:
    """A function's module, name and position."""
    name = node.name if isinstance(node, _FUNCTIONS) else "<lambda>"
    return (module, name, getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Nodes in one scope, leaving nested definitions' bodies to their own."""
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, _DEFINITIONS):
            pending += ast.iter_child_nodes(node)


def _bind(scope: Scope, target: ast.expr, value: object) -> None:
    if isinstance(target, ast.Name):
        scope.setdefault(target.id, []).append((value, None))
    elif isinstance(target, (ast.Tuple, ast.List)):
        values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
        same_shape = len(values) == len(target.elts)
        for index, element in enumerate(target.elts):
            _bind(scope, element, values[index] if same_shape else ast.Constant(None))


def _bind_loop(scope: Scope, target: ast.expr, iterable: ast.expr) -> None:
    if isinstance(target, (ast.Tuple, ast.List)):
        for index, element in enumerate(target.elts):
            _bind(scope, element, _Each(iterable, index))
    else:
        _bind(scope, target, _Each(iterable, None))


def _bind_import(scope: Scope, node: ast.Import | ast.ImportFrom, package: str) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name if alias.asname else alias.name.split(".")[0]
            scope.setdefault(alias.asname or name, []).append((_ModuleRef(name), None))
        return
    base = node.module or ""
    if node.level:
        parent = package.split(".")[: len(package.split(".")) - node.level + 1]
        base = ".".join([*parent, *([base] if base else [])])
    for alias in node.names:
        scope.setdefault(alias.asname or alias.name, []).append(
            (_Imported(base, alias.name), None)
        )


def _parametrized(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    scope: Scope,
    text_of: Callable[[ast.expr], str | None],
) -> None:
    """Bind the values ``pytest.mark.parametrize`` gives each argument;
    ``text_of`` reads argument names given as a constant."""
    for decorator in node.decorator_list:
        if not (
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).endswith("parametrize")
            and len(decorator.args) >= 2
        ):
            continue
        names, rows = decorator.args[:2]
        if isinstance(names, ast.Constant) and isinstance(names.value, str):
            fields = [name.strip() for name in names.value.split(",")]
        elif isinstance(names, _SEQUENCES):
            fields = [str(e.value) for e in names.elts if isinstance(e, ast.Constant)]
        elif (text := text_of(names)) is not None:
            fields = [name.strip() for name in text.split(",")]
        else:
            continue
        for index, name in enumerate(fields):
            column = index if len(fields) > 1 else None
            scope.setdefault(name, []).append((_Each(rows, column), None))


def _column(found: Sequence[Bound], column: int | None) -> list[Bound]:
    """``found``, or element ``column`` of each of its tuples."""
    if column is None:
        return list(found)
    return [
        (element.elts[column], own)
        for element, own in found
        if isinstance(element, (ast.Tuple, ast.List)) and len(element.elts) > column
    ]


def _row(element: ast.expr, column: int | None) -> ast.expr:
    """A ``pytest.param(...)`` row as its values; any other element as it is."""
    if not (
        isinstance(element, ast.Call) and ast.unparse(element.func).endswith("param")
    ):
        return element
    if column is None:
        return element.args[0] if element.args else element
    return ast.Tuple(element.args)


class Checker:
    def __init__(self, repo: Path, real: frozenset[str]) -> None:
        self.repo = repo
        self.real = real
        self.modules: dict[str, _Module | None] = {}
        self.local: list[_Module] = []
        self.markers: dict[Param, str] = {}
        self.params: list[Param] = []
        self.scopes: dict[int, Scope] = {}
        self.owners: dict[int, ast.AST] = {}  # the definition each scope is of
        self.functions: dict[int, FunctionKey] = {}
        self.methods: dict[int, ast.ClassDef] = {}  # each method's class
        self.sinks: set[Param] = set()
        # Each sink parameter's call sites.
        self.sink_sites: dict[Param, set[tuple[str, int, str]]] = {}
        self.called: set[FunctionKey] = set()  # functions a call in the trees fills
        # Parameters with values of their own: a default or ``parametrize``.
        self.given: set[tuple[FunctionKey, str]] = set()
        self.straight: dict[int, int] = {}  # straight-line assignment value -> line
        # The line and scope of the call being recorded.
        self.at: tuple[int, int] | None = None
        self.findings: set[Finding] = set()
        self.unresolved: set[tuple[str, int, str]] = set()
        for tree in TREES:
            for path in sorted((repo / tree).rglob("*.py")):
                relative = path.relative_to(repo)
                parts = relative.with_suffix("").parts
                name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
                module = self._read(name, path, relative.as_posix())
                self.local.append(module)

    # --- modules -------------------------------------------------------------

    def _read(self, name: str, path: Path, shown: str) -> _Module:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=shown)
        module = _Module(name, shown, tree)
        self.modules[name] = module
        for node in ast.walk(tree):
            if isinstance(node, _FUNCTIONS):
                self.functions[id(node)] = _key(name, node)
            elif isinstance(node, ast.ClassDef):
                self.methods.update(
                    (id(s), node) for s in node.body if isinstance(s, _FUNCTIONS)
                )
        module.top = self._scope(module, tree)
        return module

    def _source(self, name: str) -> Path | None:
        top, *rest = name.split(".")
        roots = [self.repo, self.repo / "src"]
        try:
            spec = importlib.util.find_spec(top) if top.isidentifier() else None
        except ImportError, ValueError:
            spec = None
        if spec is not None and spec.submodule_search_locations:
            roots += [Path(p).parent for p in spec.submodule_search_locations]
        for root in roots:
            base = root.joinpath(top, *rest)
            for candidate in (base / "__init__.py", base.with_suffix(".py")):
                if candidate.is_file():
                    return candidate
        return None

    def module(self, name: str) -> _Module | None:
        """A module outside the test trees, read (never imported) on first use."""
        if name not in self.modules:
            self.modules[name] = None
            path = self._source(name)
            if path is not None:
                self._read(name, path, str(path))
        return self.modules[name]

    # --- scopes --------------------------------------------------------------

    def marker(self, function: FunctionKey, param: str, mode: str) -> str:
        key = (function, param, mode)
        if key not in self.markers:
            self.markers[key] = f"\x02{len(self.params)}\x03"
            self.params.append(key)
        return self.markers[key]

    def _scope(
        self, module: _Module, node: ast.AST, outer: Chain | None = None
    ) -> Scope:
        scope: Scope = {}
        if isinstance(node, (*_FUNCTIONS, ast.Lambda)):
            key = _key(module.name, node)
            for name in _parameters(node):
                scope[name] = [(ast.Constant(self.marker(key, name, "value")), None)]
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            defaults = list(zip(positional[::-1], args.defaults[::-1], strict=False))
            defaults += [
                (a, d)
                for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                if d
            ]
            for arg, default in defaults:
                scope[arg.arg].append((default, outer))
        if isinstance(node, _FUNCTIONS):
            _parametrized(
                node, scope, lambda e: self._one_text(e, outer or [module.top])
            )
            key = _key(module.name, node)
            self.given |= {
                (key, name) for name, bound in scope.items() if len(bound) > 1
            }
        package = module.name
        if not module.path.endswith("__init__.py"):
            package = module.name.rpartition(".")[0]
        body = (
            frozenset(id(statement) for statement in getattr(node, "body", []))
            if isinstance(node, _FUNCTIONS)
            else frozenset()
        )
        for child in _scope_nodes(node):
            self._bind_node(scope, child, package, body)
        return scope

    def _one_text(self, node: ast.expr, chain: Chain) -> str | None:
        """The one string ``node`` can be, if it can be only one."""
        found = self.values(node, chain)
        if len(found) != 1:
            return None
        (text,) = found
        return None if UNKNOWN in text or TOO_MANY in text else text

    def _bind_node(
        self,
        scope: Scope,
        node: ast.AST,
        package: str,
        body: frozenset[int] = frozenset(),
    ) -> None:
        if isinstance(node, ast.Assign):
            if id(node) in body and all(isinstance(t, ast.Name) for t in node.targets):
                # A statement of the function's own body runs every time, in order.
                self.straight[id(node.value)] = node.lineno
            for target in node.targets:
                _bind(scope, target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value:
            _bind(scope, node.target, node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            _bind_loop(scope, node.target, node.iter)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            _bind_import(scope, node, package)
        elif isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            scope.setdefault(node.name, []).append((node, None))

    def scope_of(self, module: _Module, node: ast.AST, outer: Chain) -> Scope:
        if id(node) not in self.scopes:
            self.scopes[id(node)] = self._scope(module, node, outer)
            self.owners[id(self.scopes[id(node)])] = node
        return self.scopes[id(node)]

    def enclosing_class(self, chain: Chain) -> list[Bound]:
        """The class of the innermost method ``chain`` is in, with its scopes."""
        for depth in range(len(chain) - 1, 0, -1):
            owner = self.owners.get(id(chain[depth]))
            if owner is not None and id(owner) in self.methods:
                return [(self.methods[id(owner)], chain[:depth])]
        return []

    # --- what an expression can be --------------------------------------------

    def lookup(self, name: str, chain: Chain) -> list[Bound]:
        for depth in range(len(chain) - 1, -1, -1):
            if name in chain[depth]:
                inner = chain[: depth + 1]
                bound = chain[depth][name]
                if self.at is not None and id(chain[depth]) == self.at[1]:
                    bound = self._latest(bound, self.at[0])
                return [
                    (value, own if own is not None else inner) for value, own in bound
                ]
        return []

    def _latest(self, bound: list[Bound], line: int) -> list[Bound]:
        """Of a name's plain assignments in a function body, the last one before
        ``line``; every binding when any of them may not run in order."""
        if not all(id(value) in self.straight for value, _ in bound):
            return bound
        before = [b for b in bound if self.straight[id(b[0])] < line]
        if not before:
            return bound
        return [max(before, key=lambda b: self.straight[id(b[0])])]

    def resolve(self, value: object, chain: Chain, seen: frozenset[int]) -> list[Bound]:
        """What an import names: a value in another module, or a module."""
        if not isinstance(value, _Imported):
            return [(value, chain)]
        if id(value) in seen:
            return []
        seen |= {id(value)}
        module = self.module(value.module)
        if module is not None and value.name in module.top:
            return [
                bound
                for inner, own in module.top[value.name]
                for bound in self.resolve(inner, own or [module.top], seen)
            ]
        if self.module(f"{value.module}.{value.name}") is not None:
            return [(_ModuleRef(f"{value.module}.{value.name}"), None)]
        return []

    def objects(
        self, node: ast.expr, chain: Chain, seen: frozenset[int]
    ) -> list[Bound]:
        """The values, classes, functions or modules a name or attribute is."""
        if isinstance(node, ast.Name):
            found = self.lookup(node.id, chain)
        elif isinstance(node, ast.Attribute):
            owners = self.objects(node.value, chain, seen)
            if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
                owners += self.enclosing_class(chain)
            found = [
                member
                for owner, owner_chain in owners
                for member in self.member(owner, owner_chain, node.attr, seen)
            ]
        else:
            return [(node, chain)]
        return [
            b for value, own in found for b in self.resolve(value, own or chain, seen)
        ]

    def member(
        self, owner: object, chain: Chain | None, attr: str, seen: frozenset[int]
    ) -> list[Bound]:
        if isinstance(owner, ast.ClassDef):
            body: Scope = {}
            for statement in owner.body:
                self._bind_node(body, statement, "")
            if attr in body:
                return [(v, own or [*(chain or []), body]) for v, own in body[attr]]
            if id(owner) in seen:
                return []
            seen |= {id(owner)}
            return [
                found
                for base_node in owner.bases
                for base, base_chain in self.objects(base_node, chain or [], seen)
                for found in self.member(base, base_chain, attr, seen)
            ]
        if isinstance(owner, _ModuleRef):
            return [(_Imported(owner.module, attr), None)]
        return []

    def values(
        self, node: object, chain: Chain, seen: frozenset[int] = frozenset()
    ) -> set[str]:
        """Every string ``node`` can be, with ``UNKNOWN`` for runtime parts."""
        if id(node) in seen:
            return {UNKNOWN}
        seen |= {id(node)}
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else {UNKNOWN}
        if isinstance(node, (ast.Name, ast.Attribute)):
            found = self.objects(node, chain, seen)
            return set().union(
                *(self.values(v, c or chain, seen) for v, c in found)
            ) or {UNKNOWN}
        if isinstance(node, _Each):
            return set().union(
                *(
                    self.values(v, c or chain, seen)
                    for v, c in self.each(node, chain, seen)
                )
            ) or {UNKNOWN}
        if isinstance(node, ast.IfExp):
            return self.values(node.body, chain, seen) | self.values(
                node.orelse, chain, seen
            )
        if (joined := self._named_join(node, chain, seen)) is not None:
            return joined
        return self._joined(node, chain, seen)

    def _named_join(
        self, node: object, chain: Chain, seen: frozenset[int]
    ) -> set[str] | None:
        """``str.join`` over a sequence a name holds, read as if written inline."""
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], (ast.Name, ast.Attribute))
        ):
            return None
        found: set[str] = set()
        for value, own in self.objects(node.args[0], chain, seen):
            if not isinstance(value, _SEQUENCES):
                return {UNKNOWN}
            inline = ast.Call(func=node.func, args=[value], keywords=[])
            found |= self._joined(inline, own or chain, seen)
        return found or {UNKNOWN}

    def _joined(self, node: object, chain: Chain, seen: frozenset[int]) -> set[str]:
        parts = name_parts(node)
        if parts is None:
            return {UNKNOWN}
        alternatives = [sorted(self.values(part, chain, seen)) for part in parts]
        # A part already over the limit makes the whole expression over it.
        if any(TOO_MANY in a for a in alternatives) or (
            math.prod(len(a) for a in alternatives) > MAX_ALTERNATIVES
        ):
            return {TOO_MANY}
        return {"".join(combo) for combo in itertools.product(*alternatives)}

    def each(self, node: _Each, chain: Chain, seen: frozenset[int]) -> list[Bound]:
        return self.items(node.iterable, chain, seen, node.column)

    def items(
        self,
        node: ast.expr,
        chain: Chain,
        seen: frozenset[int],
        column: int | None = None,
    ) -> list[Bound]:
        """The elements a ``for`` over ``node`` takes (a dict's keys), or element
        ``column`` of each."""
        if isinstance(node, ast.Call):
            return self._call_items(node, chain, seen, column)
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._named(node, chain, seen, column, "item")
        if isinstance(node, _SEQUENCES):
            rows = [(_row(element, column), chain) for element in node.elts]
            return _column(rows, column)
        if isinstance(node, ast.Dict):
            keys = [(key, chain) for key in node.keys if key is not None]
            return _column(keys, column)
        return []

    def dict_values(
        self,
        node: ast.expr,
        chain: Chain,
        seen: frozenset[int],
        column: int | None = None,
    ) -> list[Bound]:
        """The values of the dict ``node`` is, or element ``column`` of each."""
        if isinstance(node, ast.Dict):
            pairs = zip(node.keys, node.values, strict=True)
            return _column([(v, chain) for k, v in pairs if k is not None], column)
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._named(node, chain, seen, column, "dict_value")
        return []

    def _named(
        self,
        node: ast.Name | ast.Attribute,
        chain: Chain,
        seen: frozenset[int],
        column: int | None,
        mode: str,
    ) -> list[Bound]:
        """``items`` (mode "item") or ``dict_values`` of what ``node`` names; a
        parameter's are its marker in that mode and column."""
        found: list[Bound] = []
        for value, own in self.objects(node, chain, seen):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if match := _MARKER.fullmatch(value.value):
                    function, param, _ = self.params[int(match.group(1))]
                    taken = mode if column is None else f"{mode}:{column}"
                    found.append(
                        (ast.Constant(self.marker(function, param, taken)), own)
                    )
            elif isinstance(value, ast.expr) and id(value) not in seen:
                inner = seen | {id(value)}
                collect = self.items if mode == "item" else self.dict_values
                found += collect(value, own or chain, inner, column)
        return found

    def _call_items(
        self, node: ast.Call, chain: Chain, seen: frozenset[int], column: int | None
    ) -> list[Bound]:
        """``items`` of a call: a dict's views, ``enumerate``, ``zip``, ``sorted``..."""
        func, args = node.func, node.args
        if any(isinstance(a, ast.Starred) for a in args):
            return []
        if isinstance(func, ast.Attribute) and not args:
            return self._view_items(func, chain, seen, column)
        name = ast.unparse(func)
        if name == "enumerate" and args:
            return self.items(args[0], chain, seen) if column == 1 else []
        if name == "zip":
            if column is None or column >= len(args):
                return []
            rows = self.items(args[column], chain, seen)
            lengths = [self._length(a, chain, seen) for a in args]
            known = [n for n in lengths if n is not None]
            return rows[: min(known)] if len(known) == len(args) else rows
        if name in _SAME_ITEMS and len(args) == 1:
            return self.items(args[0], chain, seen, column)
        return []

    def _length(self, node: ast.expr, chain: Chain, seen: frozenset[int]) -> int | None:
        """How many elements a literal sequence, or a name holding one, has."""
        if isinstance(node, _SEQUENCES):
            return len(node.elts)
        if isinstance(node, (ast.Name, ast.Attribute)):
            found = self.objects(node, chain, seen)
            if len(found) == 1 and isinstance(found[0][0], _SEQUENCES):
                return len(found[0][0].elts)
        return None

    def _view_items(
        self,
        func: ast.Attribute,
        chain: Chain,
        seen: frozenset[int],
        column: int | None,
    ) -> list[Bound]:
        """``items`` of a dict's ``keys()``, ``values()`` or ``items()``."""
        if func.attr == "keys":
            return self.items(func.value, chain, seen, column)
        if func.attr == "values":
            return self.dict_values(func.value, chain, seen, column)
        if func.attr == "items" and column == 0:
            return self.items(func.value, chain, seen)
        if func.attr == "items" and column == 1:
            return self.dict_values(func.value, chain, seen)
        return []

    # --- the scan ------------------------------------------------------------

    def run(self) -> list[Finding]:
        # Repeat until helpers passing names on to other helpers add no more;
        # each round can only add parameters, so this ends.
        while True:
            before = set(self.sinks)
            self.findings.clear()
            self.unresolved.clear()
            for module in self.local:
                self.visit(module, module.tree, [module.top])
            if self.sinks == before:
                self.unresolved |= {
                    site
                    for param, sites in self.sink_sites.items()
                    for site in sites
                    if param[0] not in self.called
                    and (param[0], param[1]) not in self.given
                }
                return sorted(
                    self.findings, key=lambda f: (f.path, f.line, f.integration)
                )

    def visit(self, module: _Module, node: ast.AST, chain: Chain) -> None:
        if isinstance(node, _DEFINITIONS):
            self.visit_definition(module, node, chain)
            return
        if isinstance(node, ast.Call):
            self.check_call(module, node, chain)
        for child in ast.iter_child_nodes(node):
            self.visit(module, child, chain)

    def visit_definition(
        self,
        module: _Module,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef,
        chain: Chain,
    ) -> None:
        if isinstance(node, ast.ClassDef):
            header: list[ast.AST] = [*node.decorator_list, *node.bases, *node.keywords]
        else:
            header = [*node.args.defaults, *filter(None, node.args.kw_defaults)]
            header += node.decorator_list if isinstance(node, _FUNCTIONS) else []
        for child in header:
            self.visit(module, child, chain)
        inner = [*chain, self.scope_of(module, node, chain)]
        body = node.body if isinstance(node.body, list) else [node.body]
        for statement in body:
            # A method does not see the names its class body binds.
            method = isinstance(node, ast.ClassDef) and isinstance(
                statement, _DEFINITIONS
            )
            self.visit(module, statement, chain if method else inner)

    def check_call(self, module: _Module, call: ast.Call, chain: Chain) -> None:
        name = self.callee_name(call, chain)
        if name in SINKS:
            position, keyword = SINKS[name]
            self.record(
                module, call, _argument(call, position, keyword), chain, "value", ""
            )
        # Resolve the callee when a helper passes names on: under its own name,
        # or under any name an import gives it (a re-export can rename it).
        if not self.sinks or (
            name not in {key[1] for key, _, _ in self.sinks}
            and not self._imported(call.func, chain)
        ):
            return
        for target, _ in self.objects(call.func, chain, frozenset()):
            key = self.functions.get(id(target))
            if key is None or not isinstance(target, _FUNCTIONS):
                continue
            self.called.add(key)
            bound = self.fills_first_parameter(call, target)
            for function, param, mode in sorted(self.sinks):
                if function == key:
                    argument = _bound_argument(call, target, param, bound=bound)
                    self.record(module, call, argument, chain, mode, target.name)

    def _imported(self, func: ast.expr, chain: Chain) -> bool:
        """Whether the callee's name, or the module it is read off, is imported."""
        while isinstance(func, ast.Attribute):
            func = func.value
        return isinstance(func, ast.Name) and any(
            isinstance(value, (_Imported, _ModuleRef))
            for value, _ in self.lookup(func.id, chain)
        )

    def _names(self, argument: ast.expr, chain: Chain, mode: str) -> set[str]:
        """The names ``argument`` gives, read as ``mode`` says."""
        if mode == "value":
            return self.values(argument, chain)
        kind, _, taken = mode.partition(":")
        column = int(taken) if taken else None
        collect = self.items if kind == "item" else self.dict_values
        names: set[str] = set().union(
            *(
                self.values(v, c or chain)
                for v, c in collect(argument, chain, frozenset(), column)
            )
        )
        if not names and not isinstance(argument, (*_SEQUENCES, ast.Dict)):
            names = {UNKNOWN}
        return names

    def fills_first_parameter(
        self, call: ast.Call, function: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> bool:
        """Whether ``call`` leaves a method's ``self`` or ``cls`` to Python."""
        if id(function) not in self.methods:
            return False
        decorators = {ast.unparse(d) for d in function.decorator_list}
        if "staticmethod" in decorators:
            return False
        func = call.func
        return "classmethod" in decorators or (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")
        )

    def callee_name(self, call: ast.Call, chain: Chain) -> str | None:
        func = call.func
        if isinstance(func, ast.Attribute):
            owner = ast.unparse(func.value).split(".")
            if func.attr == "add" and owner[-2:] == ["config", "components"]:
                return "config.components.add"
            return func.attr
        if not isinstance(func, ast.Name):
            return None
        for value, _ in self.lookup(func.id, chain):
            if isinstance(value, _Imported):
                return value.name
        return func.id

    def record(
        self,
        module: _Module,
        call: ast.Call,
        argument: ast.expr | None,
        chain: Chain,
        mode: str,
        via: str,
    ) -> None:
        if argument is None:
            return
        base, _, template = mode.partition("|")
        self.at = (call.lineno, id(chain[-1]))
        try:
            names = self._names(argument, chain, base)
        finally:
            self.at = None
        if template:
            prefix, _, suffix = template.partition(_AROUND)
            names = {
                n if n in (UNKNOWN, TOO_MANY) else f"{prefix}{n}{suffix}" for n in names
            }
        site = (module.path, call.lineno, ast.unparse(call.func))
        for name in names:
            domain = name.partition(".")[0]
            if match := _MARKER.fullmatch(domain):
                param = self.params[int(match.group(1))]
                self.sinks.add(param)
                self.sink_sites.setdefault(param, set()).add(site)
            elif (match := _MARKER.search(domain)) and not _MARKER.search(
                domain, match.end()
            ):
                # The helper builds the name around its parameter.
                function, param_name, taken = self.params[int(match.group(1))]
                wrap = f"{domain[: match.start()]}{_AROUND}{domain[match.end() :]}"
                if UNKNOWN in wrap or TOO_MANY in wrap:
                    self.unresolved.add(site)
                    continue
                param = (function, param_name, f"{taken}|{wrap}")
                self.sinks.add(param)
                self.sink_sites.setdefault(param, set()).add(site)
            elif domain == TOO_MANY or domain in self.real:
                self.findings.add(Finding(module.path, call.lineno, domain, via))
            elif UNKNOWN in domain:
                self.unresolved.add((module.path, call.lineno, ast.unparse(call.func)))


def _argument(call: ast.Call, index: int, keyword: str | None) -> ast.expr | None:
    leading = call.args[: index + 1]
    if len(leading) > index and not any(isinstance(a, ast.Starred) for a in leading):
        return call.args[index]
    if keyword is None:
        return None
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def _bound_argument(
    call: ast.Call,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    param: str,
    *,
    bound: bool,
) -> ast.expr | None:
    """The argument ``call`` passes for ``param`` of ``function``; ``bound``
    when Python passes the first parameter itself."""
    positional = [a.arg for a in (*function.args.posonlyargs, *function.args.args)]
    positional = positional[1:] if bound else positional
    if param in positional:
        found = _argument(call, positional.index(param), param)
        if found is not None:
            return found
    return next((k.value for k in call.keywords if k.arg == param), None)


# --- the allowlist ------------------------------------------------------------


def load_allowlist(path: Path, problems: list[str]) -> dict[tuple[str, str], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        problems.append(f"{ALLOWLIST}: cannot be read: {err}")
        return {}
    return parse_allowlist(text, problems)


def _repeated_key(node: yaml.Node | None) -> yaml.ScalarNode | None:
    """A key repeated in one mapping, which loading would settle silently by
    keeping the last value."""
    if isinstance(node, yaml.MappingNode):
        keys = [key.value for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
        for index, (key, _) in enumerate(node.value):
            if isinstance(key, yaml.ScalarNode) and key.value in keys[:index]:
                return key
        children = [value for _, value in node.value]
    elif isinstance(node, yaml.SequenceNode):
        children = list(node.value)
    else:
        return None
    return next((k for c in children if (k := _repeated_key(c)) is not None), None)


def parse_allowlist(text: str, problems: list[str]) -> dict[tuple[str, str], str]:
    """Each entry's reason by (file, integration); a bad entry is a problem.
    ``scripts/stand_in_lists_ratchet.py`` reads the allowlist with this too."""
    try:
        repeated = _repeated_key(yaml.compose(text, Loader=yaml.SafeLoader))
        entries = yaml.safe_load(text) or []
    except yaml.YAMLError as err:
        problems.append(f"{ALLOWLIST}: cannot be read: {err}")
        return {}
    if repeated is not None:
        line = repeated.start_mark.line + 1
        problems.append(f"{ALLOWLIST}:{line}: repeats the key {repeated.value!r}")
        return {}
    if not isinstance(entries, list):
        problems.append(f"{ALLOWLIST}: must be a list of entries")
        return {}
    allowed: dict[tuple[str, str], str] = {}
    for number, entry in enumerate(entries, 1):
        valid = isinstance(entry, dict) and sorted(entry) == sorted(ENTRY_KEYS)
        if not valid or not all(
            isinstance(entry[k], str) and entry[k] for k in ENTRY_KEYS
        ):
            problems.append(
                f"{ALLOWLIST}: entry {number} needs exactly a non-empty "
                + ", ".join(ENTRY_KEYS)
            )
            continue
        key = (entry["file"], entry["integration"])
        if key in allowed:
            problems.append(f"{ALLOWLIST}: entry {number} repeats {key[0]} {key[1]}")
        allowed[key] = entry["reason"]
    return allowed


def _describe(finding: Finding) -> str:
    through = f" through {finding.via}()" if finding.via else ""
    if finding.integration == TOO_MANY:
        return (
            f"{finding.path}:{finding.line}: more than {MAX_ALTERNATIVES} possible "
            f"names to check{through}; name the integration directly"
        )
    return (
        f"{finding.path}:{finding.line}: a stand-in registered under Home "
        f"Assistant's {finding.integration!r} integration{through}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument(
        "--all",
        action="store_true",
        help="list every case, allowlisted or not, and every name left to review",
    )
    args = parser.parse_args(argv)
    problems: list[str] = []
    allowed = load_allowlist(args.repo / ALLOWLIST, problems)
    try:
        checker = Checker(args.repo, real_integrations())
        findings = checker.run()
    except SyntaxError as error:
        print(f"{error.filename}:{error.lineno}: {error.msg}")
        return 1
    if args.all:
        listed = [_describe(f) for f in findings] + [
            f"{path}:{line}: {callee}() gets a name only known at runtime"
            for path, line, callee in sorted(checker.unresolved)
        ]
        if listed:
            print("\n".join(listed))
    problems += [
        _describe(f) for f in findings if (f.path, f.integration) not in allowed
    ]
    matched = {(f.path, f.integration) for f in findings}
    problems += [
        f"{ALLOWLIST}: {path} no longer registers {name!r}; remove the entry"
        for path, name in sorted(set(allowed) - matched)
    ]
    if problems:
        print("\n".join(problems))
        print(
            "Set the real integration up as Core's own tests do, or register the "
            "stand-in under a made-up domain (tests/AGENTS.md)."
        )
        return 1
    print(f"No unlisted stand-in for a real integration; {len(allowed)} allowlisted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
