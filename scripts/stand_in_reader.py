"""Reading for ``scripts/integration_stand_ins.py``: the modules of the test
trees, their scopes, and what a name is bound to where it is read.

``Reader`` reads the modules and answers what a name or an attribute is;
``Resolver`` (``stand_in_resolver.py``) builds on it to say which strings an
expression can be.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from stand_in_scopes import (  # type: ignore[import-not-found]
    Added,
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
    bind_unread,
    fixed_length,
)

TREES = ("tests/src/unit", "tests/addon")
UNKNOWN = "\0"
TOO_MANY = "\1"  # more alternatives than are checked: reported, never dropped
ENDLESS = "\5"  # text added by more than MAX_WRAPS helpers: reported
MAX_WRAPS = 8
MAX_ALTERNATIVES = 256
# Steps from a name to its value, one for each name or expression on the way:
# reported past it.
MAX_STEPS = 100
NAME_HERE = "\x04"  # where the name goes in the text a helper puts around it
MARKER = re.compile("\x02([0-9]+)\x03")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
# The decorators that make a function a fixture: (module, name).
FIXTURES = (("pytest", "fixture"), ("pytest_asyncio", "fixture"))
DEFINITIONS = (*FUNCTIONS, ast.Lambda, ast.ClassDef)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
SEQUENCES = (ast.Tuple, ast.List, ast.Set)

FunctionKey = tuple[str, str, int, int]
Place = tuple[int, int]  # a line and a column
# Where a loop begins and ends, and where its body does.
Loop = tuple[Place, Place, Place, Place]


class Mode(NamedTuple):
    """What of a helper's argument names integrations."""

    # "value": the argument itself; "item": the items a loop takes from it;
    # "dict_value": its values, as a dict.
    kind: str = "value"
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


class Module:
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


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> list[str]:
    args = node.args
    named = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    # ``*names`` and ``**options`` hold what a call passes beyond the named ones.
    return named + [a.arg for a in (args.vararg, args.kwarg) if a]


def function_key(module: str, node: ast.AST) -> tuple[str, str, int, int]:
    """A function's module, name and position."""
    name = node.name if isinstance(node, FUNCTIONS) else "<lambda>"
    return (module, name, getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _only_names(target: ast.expr) -> bool:
    """Whether an assignment to ``target`` binds names and nothing else: a
    name, or names unpacked into (``a, *b = row``), not ``rows[0]``."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return all(_only_names(element) for element in target.elts)
    if isinstance(target, ast.Starred):
        return _only_names(target.value)
    return isinstance(target, ast.Name)


def _scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Nodes in one scope, leaving nested definitions' bodies, and the ``for``
    clauses of comprehensions inside it (which bind their targets), to their
    own."""
    own = {id(clause) for clause in getattr(scope, "generators", ())}
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if not isinstance(node, ast.comprehension) or id(node) in own:
            yield node
        if not isinstance(node, DEFINITIONS):
            pending += ast.iter_child_nodes(node)


class Reader:
    def __init__(self, repo: Path, real: frozenset[str]) -> None:
        self.repo = repo
        self.real = real
        self.modules: dict[str, Module | None] = {}
        self.local: list[Module] = []
        self.markers: dict[Param, str] = {}
        self.params: list[Param] = []
        self.scopes: dict[int, Scope] = {}
        self.owners: dict[int, ast.AST] = {}  # the definition each scope is of
        self.comprehensions: set[int] = set()  # the scopes of comprehensions
        self.functions: dict[int, FunctionKey] = {}
        self.methods: dict[int, ast.ClassDef] = {}  # each method's class
        # Each parameter's default, and the scopes it is read in.
        self.defaults: dict[tuple[FunctionKey, str], tuple[ast.expr, Chain]] = {}
        # Where each scope's loops begin and end: a later binding reaches an
        # earlier read only round a loop.
        self.loops: dict[int, list[Loop]] = {}
        # What pytest calls itself, with fixtures for parameters: tests, fixtures.
        self.collected: set[FunctionKey] = set()
        self.sinks: set[Param] = set()
        self.sink_sites: dict[Param, set[Site]] = {}
        self.depths: dict[Param, int] = {}  # helpers adding text to each name
        self.called: set[FunctionKey] = set()  # functions a call in the trees fills
        # Parameters with values of their own: a default or ``parametrize``.
        self.given: set[tuple[FunctionKey, str]] = set()
        # Where each value is bound, by its id (two imports of one name are
        # equal, and bound in two places): its statement's line and column.
        self.assigned: dict[int, tuple[int, int]] = {}
        self.straight: set[int] = set()  # the values a function always binds
        self.class_bodies: dict[int, Scope] = {}  # what each class body binds
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

    def _read(self, name: str, path: Path, shown: str) -> Module:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=shown)
        module = Module(name, shown, tree)
        self.modules[name] = module
        for node in ast.walk(tree):
            if isinstance(node, FUNCTIONS):
                self.functions[id(node)] = function_key(name, node)
                if node.name.startswith("test"):
                    self.collected.add(function_key(name, node))
            elif isinstance(node, ast.ClassDef):
                self.methods.update(
                    (id(s), node) for s in node.body if isinstance(s, FUNCTIONS)
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

    def module(self, name: str) -> Module | None:
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
        self, module: Module, node: ast.AST, outer: Chain | None = None
    ) -> Scope:
        scope: Scope = {}
        # Defaults and decorators are read where the definition stands.
        around = self.header_scopes(node, outer or [module.top])
        here = AsOf(
            around, (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        )
        if isinstance(node, (*FUNCTIONS, ast.Lambda)):
            key = function_key(module.name, node)
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
            # A default applies where a call leaves the argument out.
            for arg, default in defaults:
                self.defaults[(key, arg.arg)] = (default, here)
        if isinstance(node, FUNCTIONS):
            bind_parametrized(
                node,
                scope,
                lambda e: self._one_text(e, here),
                lambda f: self._pytest_parametrize(f, here),
                here,
            )
            self._note_fixture(module, node, here)
            key = function_key(module.name, node)
            self.given |= {
                (key, name) for name, bound in scope.items() if len(bound) > 1
            }
        if isinstance(node, (*FUNCTIONS, ast.Lambda)):
            # A parameter holds its value from the definition's line on.
            for bound in scope.values():
                for value, _ in bound:
                    self._bound_at(value, (node.lineno, node.col_offset), straight=True)
        package = module.name
        if not module.path.endswith("__init__.py"):
            package = module.name.rpartition(".")[0]
        # The statements that always run, in order: a function's or a module's own.
        body = (
            frozenset(id(statement) for statement in getattr(node, "body", []))
            if isinstance(node, (*FUNCTIONS, ast.Module))
            else frozenset()
        )
        for child in _scope_nodes(node):
            self._bind_node(scope, child, package, body)
            self._note_loop(scope, child)
        self._bind_outside(module, node, scope, outer or [module.top])
        self._added_outside(scope, outer or [])
        self._first_iterable_outside(node, scope, here)
        return scope

    @staticmethod
    def _first_iterable_outside(node: ast.AST, scope: Scope, around: Chain) -> None:
        """A comprehension reads its first iterable in the scopes around it."""
        if not isinstance(node, COMPREHENSIONS):
            return
        first = node.generators[0].iter
        for bounds in scope.values():
            bounds[:] = [
                (v, around if isinstance(v, Each) and v.iterable is first else own)
                for v, own in bounds
            ]

    def _added_outside(self, scope: Scope, outer: Chain) -> None:
        """Settle the writes into collections once the scope is read. A write
        through an alias (``alias = rows``, ``alias.append(...)``) is a write
        into what it is an alias of. A name a scope only writes into is not
        its own: the write is bound where the name lives, at no fixed point
        there."""
        loops = self.loops.get(id(scope), [])
        for bounds in list(scope.values()):
            held = [(v, own) for v, own in bounds if not isinstance(v, Added)]
            for written, own in [b for b in bounds if isinstance(b[0], Added)]:
                # What the name can hold where the write is made.
                place = self.assigned.get(id(written))
                there = self._latest(held, place, loops) if place and held else held
                for other in [v.id for v, _ in there if isinstance(v, ast.Name)]:
                    again = Added(
                        ast.Name(id=other, ctx=ast.Load()), ast.Add(), written.right
                    )
                    scope.setdefault(other, []).append((again, own))
                    if id(written) in self.assigned:
                        self.assigned[id(again)] = self.assigned[id(written)]
        written_only = [
            name
            for name, bounds in scope.items()
            if all(isinstance(value, Added) for value, _ in bounds)
        ]
        for name in written_only:
            home = next((s for s in outer[::-1] if name in s), None)
            if home is None:
                continue
            for value, own in scope.pop(name):
                self.assigned.pop(id(value), None)
                home.setdefault(name, []).append((value, own or [*outer, scope]))

    def _bind_outside(
        self, module: Module, node: ast.AST, scope: Scope, outer: Chain
    ) -> None:
        """What a function binds to a name it declares ``global`` or
        ``nonlocal`` is bound where the name lives, at no fixed point there."""
        if not isinstance(node, FUNCTIONS):
            return
        for child in _scope_nodes(node):
            if not isinstance(child, (ast.Global, ast.Nonlocal)):
                continue
            for name in child.names:
                home = module.top
                if isinstance(child, ast.Nonlocal):
                    home = next((s for s in outer[:0:-1] if name in s), outer[-1])
                for value, own in scope.pop(name, []):
                    self.assigned.pop(id(value), None)
                    self.straight.discard(id(value))
                    home.setdefault(name, []).append((value, own or [*outer, scope]))

    def _note_loop(self, scope: Scope, node: ast.AST) -> None:
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            end = (node.end_lineno or node.lineno, node.end_col_offset or 0)
            first, last = node.body[0], node.body[-1]
            start = (node.lineno, node.col_offset)
            # What each run of the loop reads again: a ``for``'s body, and a
            # ``while``'s test too. A ``for``'s iterable is read once.
            again = (
                start
                if isinstance(node, ast.While)
                else (
                    first.lineno,
                    first.col_offset,
                )
            )
            body_end = (last.end_lineno or last.lineno, last.end_col_offset or 0)
            self.loops.setdefault(id(scope), []).append((start, end, again, body_end))

    def header_scopes(self, node: ast.AST, outer: Chain) -> Chain:
        """The scopes a definition's decorators and defaults are read in: for
        a method its class's body too, which its own body does not see."""
        owner = self.methods.get(id(node))
        body = self.scopes.get(id(owner)) if owner is not None else None
        return outer if body is None else [*outer, body]

    def _is_pytest(self, name: str, chain: Chain) -> bool:
        """Whether ``name`` is the pytest module: imported under that name
        (``import pytest as pt``), or ``pytest`` where nothing binds it."""
        return self._is_module(name, "pytest", chain)

    def _note_fixture(
        self,
        module: Module,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        chain: Chain,
    ) -> None:
        """Count ``node`` among what pytest calls when a decorator of it is
        pytest's ``fixture`` (or pytest-asyncio's), read off the module under
        whatever name or imported by name, with or without arguments."""
        for decorator in node.decorator_list:
            func = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(func, ast.Name):
                found = any(
                    isinstance(value, Imported)
                    and (value.module, value.name) in FIXTURES
                    for value, _ in self.lookup(func.id, chain)
                )
            else:
                found = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "fixture"
                    and isinstance(func.value, ast.Name)
                    and any(
                        self._is_module(func.value.id, name, chain)
                        for name, _ in FIXTURES
                    )
                )
            if found:
                self.collected.add(function_key(module.name, node))

    def _is_module(self, name: str, module: str, chain: Chain) -> bool:
        """Whether ``name`` is the module ``module``: imported under that name,
        or its own name where nothing binds it."""
        bound = self.lookup(name, chain)
        if not bound:
            return name == module
        return all(
            isinstance(value, ModuleRef) and value.module == module
            for value, _ in bound
        )

    def _pytest_parametrize(self, func: ast.expr, chain: Chain) -> bool:
        """Whether ``func`` is pytest's own ``mark.parametrize``, read off the
        pytest module or an imported ``mark`` under whatever name, not a
        decorator the tests define under a similar one."""
        if not (isinstance(func, ast.Attribute) and func.attr == "parametrize"):
            return False
        mark = func.value
        if isinstance(mark, ast.Name):
            return any(
                isinstance(value, Imported)
                and (value.module, value.name) == ("pytest", "mark")
                for value, _ in self.lookup(mark.id, chain)
            )
        return (
            isinstance(mark, ast.Attribute)
            and mark.attr == "mark"
            and isinstance(mark.value, ast.Name)
            and self._is_pytest(mark.value.id, chain)
        )

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
            # A loop over a list written out, with something in it, runs.
            ran = isinstance(node, ast.For) and bool(fixed_length(node.iter))
            targets = [node.target] if ran else []
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bind_import(scope, node, package, new)
        elif isinstance(node, (*FUNCTIONS, ast.ClassDef)):
            scope.setdefault(node.name, []).append((node, None))
        else:
            targets = bind_unread(scope, node, new)
        if isinstance(node, (ast.stmt, ast.expr, ast.ExceptHandler, ast.pattern)):
            # A plain assignment (also one that unpacks into names), an import,
            # a ``with`` or a loop that runs, among the statements that always
            # run, binds its names every time.
            named = bool(targets) and all(_only_names(t) for t in targets)
            imported = isinstance(node, (ast.Import, ast.ImportFrom))
            straight = id(node) in body and (named or imported)
            for value in new:
                self._bound_at(value, (node.lineno, node.col_offset), straight=straight)

    def _bound_at(self, value: object, at: tuple[int, int], *, straight: bool) -> None:
        self.assigned[id(value)] = at
        if straight:
            self.straight.add(id(value))

    def scope_of(self, module: Module, node: ast.AST, outer: Chain) -> Scope:
        if id(node) not in self.scopes:
            self.scopes[id(node)] = self._scope(module, node, outer)
            self.owners[id(self.scopes[id(node)])] = node
            if isinstance(node, COMPREHENSIONS):
                self.comprehensions.add(id(self.scopes[id(node)]))
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
        innermost scope (the one around it too, for a comprehension) gives
        what the name held there, and each value goes on being read as of
        where it was bound."""
        at = chain.at if isinstance(chain, AsOf) else None
        for depth in range(len(chain) - 1, -1, -1):
            if name not in chain[depth]:
                # Only a comprehension reads the scope around it where it stands.
                if id(chain[depth]) not in self.comprehensions:
                    at = None
                continue
            inner = chain[: depth + 1]
            bound = chain[depth][name]
            if at is None:
                return [(v, own if own is not None else inner) for v, own in bound]
            loops = self.loops.get(id(chain[depth]), [])
            return [
                (v, AsOf(inner, self.assigned.get(id(v), at)) if own is None else own)
                for v, own in self._latest(bound, at, loops)
            ]
        return []

    def _latest(self, bound: list[Bound], at: Place, loops: list[Loop]) -> list[Bound]:
        """What can hold at ``at``. Not what came before the last binding
        always made before it (a plain assignment in the function's body, a
        parameter's value, the target of the loop whose body ``at`` is in),
        and not a later binding, unless a loop holding both brings it round
        again."""
        placed = [
            (self.assigned.get(id(value)), id(value) in self.straight)
            for value, _ in bound
        ]
        always = [
            place
            for (value, _), (place, straight) in zip(bound, placed, strict=True)
            if place
            and place < at
            and (straight or self._its_loops_body(value, place, at, loops))
        ]
        cutoff = max(always, default=None)

        def holds(place: Place | None, straight: bool) -> bool:
            if place is None:
                return True
            if place < at:
                return cutoff is None or place >= cutoff
            if straight and cutoff is not None:
                return False  # the function has not got there yet
            return place == at or any(
                again <= at <= body_end and start <= place <= body_end
                for start, _, again, body_end in loops
            )

        kept = [
            b
            for b, (place, straight) in zip(bound, placed, strict=True)
            if holds(place, straight)
        ]
        return kept or bound

    @staticmethod
    def _its_loops_body(
        value: object, place: Place, at: Place, loops: list[Loop]
    ) -> bool:
        """Whether ``value`` is the target of a loop (bound at ``place``, anew
        before each run of the body) and ``at`` is in that loop's body."""
        return isinstance(value, Each) and any(
            start == place and body <= at <= body_end
            for start, _, body, body_end in loops
        )

    def resolve(self, value: object, chain: Chain, seen: frozenset[int]) -> list[Bound]:
        """What an import names (a value in another module, or a module); any
        other value as it is."""
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
            # Read once: a value's place is kept by the object the scope holds.
            body = self.class_bodies.setdefault(id(owner), {})
            if not body:
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
        """Every string ``node`` can be: ``Resolver`` says."""
        raise NotImplementedError
