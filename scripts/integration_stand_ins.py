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
until it reaches one of the helpers, also when a helper puts text around its
parameter. A name is read as it stood where it was bound: within one function
a later plain assignment replaces an earlier one, and a saved copy keeps what
it was given. ``zip`` stops at its shortest input when every length is known.
More than ``MAX_ALTERNATIVES`` possible names fail, since they are not checked,
and so does a name more than ``MAX_WRAPS`` helpers add text to, which is how a
helper calling itself looks. A name only known at runtime is left to review;
``--all`` lists where one reaches a helper, also through a function no call in
the trees reaches (a fixture, for example) or an argument passed with ``*`` or
``**``.

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
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

# The ratchet and the tests load this script by path, without its folder.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stand_in_allowlist import (  # type: ignore[import-not-found]
    ALLOWLIST,
    load_allowlist,
)
from stand_in_scopes import (  # type: ignore[import-not-found]
    AsOf,
    Bound,
    Chain,
    Each,
    Imported,
    ModuleRef,
    Scope,
    bind,
    bind_import,
    bind_loop,
    bind_parametrized,
    column_of,
    fixed_length,
)
from stand_in_syntax import (  # type: ignore[import-not-found]
    bound_argument,
    call_argument,
    name_parts,
    param_row,
)

REPO = Path(__file__).resolve().parent.parent
TREES = ("tests/src/unit", "tests/addon")
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
ENDLESS = "\5"  # text added by more than MAX_WRAPS helpers: reported
MAX_WRAPS = 8
MAX_ALTERNATIVES = 256
# The calls a ``for`` over takes the same elements from as from their argument.
_SAME_ITEMS = ("sorted", "list", "tuple", "set", "frozenset")
_NAME = "\x04"  # where the name goes in the text a helper puts around it
_MARKER = re.compile("\x02([0-9]+)\x03")
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEFINITIONS = (*_FUNCTIONS, ast.Lambda, ast.ClassDef)
_SEQUENCES = (ast.Tuple, ast.List, ast.Set)

FunctionKey = tuple[str, str, int, int]


class Mode(NamedTuple):
    """What of a helper's argument names integrations."""

    kind: str = "value"  # the argument; "item": a loop's items; "dict_value"
    column: int | None = None  # the element of each item that does
    around: tuple[str, str] | None = None  # text the helper puts around each


class Param(NamedTuple):
    function: FunctionKey
    name: str
    mode: Mode = Mode()


class Site(NamedTuple):
    path: str
    line: int
    callee: str


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
        self.sink_sites: dict[Param, set[Site]] = {}
        self.depths: dict[Param, int] = {}  # helpers adding text to each name
        self.called: set[FunctionKey] = set()  # functions a call in the trees fills
        # Parameters with values of their own: a default or ``parametrize``.
        self.given: set[tuple[FunctionKey, str]] = set()
        # Where each value is bound: its statement's line and column.
        self.assigned: dict[object, tuple[int, int]] = {}
        self.straight: set[object] = set()  # values a function always binds
        self.findings: set[Finding] = set()
        self.unresolved: set[Site] = set()
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

    def marker(self, function: FunctionKey, param: str, mode: Mode = Mode()) -> str:
        key = Param(function, param, mode)
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
                scope[name] = [(ast.Constant(self.marker(key, name)), None)]
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
            bind_parametrized(
                node, scope, lambda e: self._one_text(e, outer or [module.top])
            )
            key = _key(module.name, node)
            self.given |= {
                (key, name) for name, bound in scope.items() if len(bound) > 1
            }
        if isinstance(node, (*_FUNCTIONS, ast.Lambda)):
            # A parameter holds its value from the definition's line on.
            for bound in scope.values():
                for value, _ in bound:
                    self._bound_at(value, (node.lineno, node.col_offset), straight=True)
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
        new: list[object] = []
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
            for target in targets:
                bind(scope, target, node.value, new)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value:
            targets = [node.target]
            bind(scope, node.target, node.value, new)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            # ``name += text``: the name's earlier value with the text added.
            targets = [node.target]
            earlier = ast.Name(id=node.target.id, ctx=ast.Load())
            bind(scope, node.target, ast.BinOp(earlier, node.op, node.value), new)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            bind_loop(scope, node.target, node.iter, new)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bind_import(scope, node, package)
        elif isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            scope.setdefault(node.name, []).append((node, None))
        if isinstance(node, (ast.stmt, ast.expr)):
            # A plain assignment in the function's own body always runs, in order.
            straight = (
                id(node) in body
                and bool(targets)
                and all(isinstance(target, ast.Name) for target in targets)
            )
            for value in new:
                self._bound_at(value, (node.lineno, node.col_offset), straight=straight)

    def _bound_at(self, value: object, at: tuple[int, int], *, straight: bool) -> None:
        self.assigned[value] = at
        if straight:
            self.straight.add(value)

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
        """What ``name`` is bound to. In scopes read as of a position, the
        innermost scope gives what the name held there, and each value goes on
        being read as of where it was bound."""
        at = chain.at if isinstance(chain, AsOf) else None
        for depth in range(len(chain) - 1, -1, -1):
            if name not in chain[depth]:
                continue
            inner = chain[: depth + 1]
            bound = chain[depth][name]
            if at is None or depth != len(chain) - 1:
                return [(v, own if own is not None else inner) for v, own in bound]
            return [
                (v, AsOf(inner, self.assigned.get(v, at)) if own is None else own)
                for v, own in self._latest(bound, at)
            ]
        return []

    def _latest(self, bound: list[Bound], at: tuple[int, int]) -> list[Bound]:
        """Of a name's parameter value and plain assignments in a function body,
        the last one before ``at``; every binding when any of them may not
        run in order."""
        if not all(value in self.straight for value, _ in bound):
            return bound
        places = [self.assigned[value] for value, _ in bound]
        before = [place for place in places if place < at]
        if not before:
            return bound
        # A parameter's default and its argument share the definition's place.
        return [
            b for b, place in zip(bound, places, strict=True) if place == max(before)
        ]

    def resolve(self, value: object, chain: Chain, seen: frozenset[int]) -> list[Bound]:
        """What an import names: a value in another module, or a module."""
        if not isinstance(value, Imported):
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
            return [(ModuleRef(f"{value.module}.{value.name}"), None)]
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
        if isinstance(owner, ModuleRef):
            return [(Imported(owner.module, attr), None)]
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
        if isinstance(node, Each):
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
                found.add(UNKNOWN)
                continue
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

    def each(self, node: Each, chain: Chain, seen: frozenset[int]) -> list[Bound]:
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
            rows: list[Bound] = [
                (param_row(element, column), chain) for element in node.elts
            ]
            return self._column(rows, column)
        if isinstance(node, ast.Dict):
            keys: list[Bound] = []
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    keys += self._spread(self.items(value, chain, seen), chain)
                else:
                    keys.append((key, chain))
            return self._column(keys, column)
        return []

    @staticmethod
    def _column(found: list[Bound], column: int | None) -> list[Bound]:
        taken: list[Bound] = column_of(found, column)
        return taken

    def _held(
        self, node: ast.Dict, chain: Chain, seen: frozenset[int]
    ) -> dict[object, Bound]:
        """What a dict literal ends up holding, by key: a later entry replaces
        an earlier one of the same constant key, also one a ``**`` brought in."""
        held: dict[object, Bound] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant):
                held[key.value] = (value, chain)
            elif key is not None:
                held[("expression", id(key))] = (value, chain)
            else:
                spread = self.objects(value, chain, seen)
                other = spread[0][0] if len(spread) == 1 else None
                if isinstance(other, ast.Dict) and id(other) not in seen:
                    own = spread[0][1] or chain
                    held.update(self._held(other, own, seen | {id(other)}))
                    continue
                unread = self._spread(self.dict_values(value, chain, seen), chain)
                held.update((("spread", id(v)), (v, own)) for v, own in unread)
        return held

    @staticmethod
    def _spread(found: list[Bound], chain: Chain) -> list[Bound]:
        """What a ``**other`` entry adds; a runtime name when it cannot be read."""
        return found or [(ast.Constant(None), chain)]

    def dict_values(
        self,
        node: ast.expr,
        chain: Chain,
        seen: frozenset[int],
        column: int | None = None,
    ) -> list[Bound]:
        """The values of the dict ``node`` is, or element ``column`` of each."""
        if isinstance(node, ast.Dict):
            held = list(self._held(node, chain, seen).values())
            return self._column(held, column)
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
                    taken = self.marker(function, param, Mode(mode, column))
                    found.append((ast.Constant(taken), own))
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
        """How many elements a list or tuple literal, or a name holding one, has."""
        if isinstance(node, (ast.Name, ast.Attribute)):
            found = self.objects(node, chain, seen)
            held = found[0][0] if len(found) == 1 else None
        else:
            held = node
        length: int | None = fixed_length(held)
        return length

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
        # each round only adds parameters, and MAX_WRAPS bounds the ones with
        # text around them, so this ends.
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
                    if param.function not in self.called
                    and (param.function, param.name) not in self.given
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
            self.record(module, call, call_argument(call, position, keyword), chain, "")
        # Resolve the callee when a helper passes names on: under its own name,
        # or under any name an import gives it (a re-export can rename it).
        if not self.sinks or (
            name not in {sink.function[1] for sink in self.sinks}
            and not self._imported(call.func, chain)
        ):
            return
        for target, _ in self.objects(call.func, chain, frozenset()):
            key = self.functions.get(id(target))
            if key is None or not isinstance(target, _FUNCTIONS):
                continue
            self.called.add(key)
            bound = self.fills_first_parameter(call, target)
            for sink in tuple(self.sinks):
                if sink.function == key:
                    argument = bound_argument(call, target, sink.name, bound=bound)
                    self.record(
                        module,
                        call,
                        argument,
                        chain,
                        target.name,
                        sink,
                        (target, bound),
                    )

    def _imported(self, func: ast.expr, chain: Chain) -> bool:
        """Whether the callee's name, or the module it is read off, is imported."""
        while isinstance(func, ast.Attribute):
            func = func.value
        return isinstance(func, ast.Name) and any(
            isinstance(value, (Imported, ModuleRef))
            for value, _ in self.lookup(func.id, chain)
        )

    def _names(self, argument: ast.expr, chain: Chain, mode: Mode) -> set[str]:
        """The names ``argument`` gives, read as ``mode`` says."""
        if mode.kind == "value":
            return self.values(argument, chain)
        collect = self.items if mode.kind == "item" else self.dict_values
        names: set[str] = set().union(
            *(
                self.values(v, c or chain)
                for v, c in collect(argument, chain, frozenset(), mode.column)
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
            if isinstance(value, Imported):
                name: str = value.name
                return name
        return func.id

    def record(
        self,
        module: _Module,
        call: ast.Call,
        argument: ast.expr | None,
        chain: Chain,
        via: str,
        sink: Param | None = None,
        helper: tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool] | None = None,
    ) -> None:
        """Check the names ``call`` gives a stand-in, directly or as the argument
        of the parameter ``sink`` of ``helper`` (the function, and whether
        Python passes its first parameter)."""
        site = Site(module.path, call.lineno, ast.unparse(call.func))
        if argument is None:
            # ``*`` or ``**`` may carry the name where it cannot be read.
            if any(isinstance(a, ast.Starred) for a in call.args) or any(
                keyword.arg is None for keyword in call.keywords
            ):
                self.unresolved.add(site)
            return
        mode = sink.mode if sink else Mode()
        here = AsOf(chain, (call.lineno, call.col_offset))
        names = self._names(argument, here, mode)
        if mode.around and sink and helper:
            names = {
                n if n in (UNKNOWN, TOO_MANY) else n.join(text.split(_NAME))
                for text in self._around(sink, call, helper, here)
                for n in ([TOO_MANY] if text == TOO_MANY else names)
            }
        endless = Finding(module.path, call.lineno, ENDLESS, via)
        for name in names:
            domain = name.partition(".")[0]
            if match := _MARKER.search(domain):
                self._follow(match, site, sink, endless)
            elif domain == TOO_MANY or domain in self.real:
                self.findings.add(Finding(module.path, call.lineno, domain, via))
            elif UNKNOWN in domain:
                self.unresolved.add(site)

    def _around(
        self,
        sink: Param,
        call: ast.Call,
        helper: tuple[ast.FunctionDef | ast.AsyncFunctionDef, bool],
        chain: Chain,
    ) -> set[str]:
        """The text ``sink``'s helper puts around a name, ``_NAME`` standing for
        the name, with the helper's other parameters filled in from this same
        call: the parts of one name never come from two calls."""
        function, bound = helper
        before, after = sink.mode.around or ("", "")
        choices: list[list[str]] = []
        # ``split`` gives text, parameter number, text, ... in turn.
        for index, part in enumerate(_MARKER.split(f"{before}{_NAME}{after}")):
            param = self.params[int(part)] if index % 2 else None
            if param is None or param.function != sink.function:
                choices.append([f"\x02{part}\x03" if param else part])
                continue
            argument = bound_argument(call, function, param.name, bound=bound)
            choices.append(
                [UNKNOWN]
                if argument is None
                else sorted(self._names(argument, chain, param.mode))
            )
        if any(TOO_MANY in choice for choice in choices) or (
            math.prod(len(choice) for choice in choices) > MAX_ALTERNATIVES
        ):
            return {TOO_MANY}
        return {"".join(combo) for combo in itertools.product(*choices)}

    def _follow(
        self, match: re.Match[str], site: Site, sink: Param | None, endless: Finding
    ) -> None:
        """Make a sink of the helper parameter ``match`` found in a name, with
        the text the helpers put around it; that text may hold further
        parameters, followed the same way. ``endless`` is the finding when more
        than ``MAX_WRAPS`` helpers have added text."""
        domain = match.string
        around = (domain[: match.start()], domain[match.end() :])
        if UNKNOWN in "".join(around) or TOO_MANY in "".join(around):
            self.unresolved.add(site)
            return
        param = self.params[int(match.group(1))]
        if any(around):
            param = param._replace(mode=param.mode._replace(around=around))
            depth = self.depths.get(sink, 0) + 1 if sink else 1
            if param not in self.sinks and depth > MAX_WRAPS:
                self.findings.add(endless)
                return
            self.depths.setdefault(param, depth)
        self.sinks.add(param)
        self.sink_sites.setdefault(param, set()).add(site)


def _describe(finding: Finding) -> str:
    through = f" through {finding.via}()" if finding.via else ""
    if finding.integration == ENDLESS:
        return (
            f"{finding.path}:{finding.line}: more than {MAX_WRAPS} helpers add "
            f"text to the name{through}, as a helper calling itself does; name "
            "the integration directly"
        )
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
    # A list with a bad entry exempts nothing until it is fixed.
    exempt = {} if problems else allowed
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
        _describe(f) for f in findings if (f.path, f.integration) not in exempt
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
