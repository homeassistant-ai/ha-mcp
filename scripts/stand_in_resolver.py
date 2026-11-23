"""Name resolution for ``scripts/integration_stand_ins.py``: the modules of the
test trees, their scopes, and every string an expression can be.

``Resolver`` reads the modules and answers what a name is bound to and which
strings an expression can give; the check's scan (``Checker``) builds on it.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import math
import re
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from stand_in_scopes import (  # type: ignore[import-not-found]
    AsOf,
    Bound,
    Chain,
    Each,
    Imported,
    ModuleRef,
    Names,
    Scope,
    bind,
    bind_import,
    bind_loop,
    bind_parametrized,
    bind_unread,
    column_of,
    fixed_length,
)
from stand_in_syntax import (  # type: ignore[import-not-found]
    name_parts,
    param_row,
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
# Calls that give their argument's elements unchanged: ``for x in sorted(y)``
# loops over ``y``'s.
_SAME_ITEMS = ("sorted", "list", "tuple", "set", "frozenset")
NAME_HERE = "\x04"  # where the name goes in the text a helper puts around it
MARKER = re.compile("\x02([0-9]+)\x03")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
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


def _from_start(column: int | None, width: int) -> int | None:
    """``column`` counted from the start of a row ``width`` long."""
    return column + width if column is not None and column < 0 else column


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


class Resolver:
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
                if node.name.startswith("test") or any(
                    "fixture" in ast.unparse(d) for d in node.decorator_list
                ):
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
        body = (
            frozenset(id(statement) for statement in getattr(node, "body", []))
            if isinstance(node, FUNCTIONS)
            else frozenset()
        )
        for child in _scope_nodes(node):
            self._bind_node(scope, child, package, body)
            self._note_loop(scope, child)
        self._bind_outside(module, node, scope, outer or [module.top])
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
            body_end = (last.end_lineno or last.lineno, last.end_col_offset or 0)
            spans = self.loops.setdefault(id(scope), [])
            spans.append(
                ((node.lineno, node.col_offset), end, (first.lineno, 0), body_end)
            )

    def header_scopes(self, node: ast.AST, outer: Chain) -> Chain:
        """The scopes a definition's decorators and defaults are read in: for
        a method its class's body too, which its own body does not see."""
        owner = self.methods.get(id(node))
        body = self.scopes.get(id(owner)) if owner is not None else None
        return outer if body is None else [*outer, body]

    def _is_pytest(self, name: str, chain: Chain) -> bool:
        """Whether ``name`` is the pytest module: imported under that name
        (``import pytest as pt``), or ``pytest`` where nothing binds it."""
        bound = self.lookup(name, chain)
        if not bound:
            return name == "pytest"
        return all(
            isinstance(value, ModuleRef) and value.module == "pytest"
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
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bind_import(scope, node, package, new)
        elif isinstance(node, (*FUNCTIONS, ast.ClassDef)):
            scope.setdefault(node.name, []).append((node, None))
        else:
            targets = bind_unread(scope, node, new)
        if isinstance(node, (ast.stmt, ast.expr, ast.ExceptHandler, ast.pattern)):
            # A plain assignment, an import or a ``with`` in the function's own
            # body always runs, in order.
            named = bool(targets) and all(isinstance(t, ast.Name) for t in targets)
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
                start <= at <= end and start <= place <= end for start, end, *_ in loops
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
        """Every string ``node`` can be, with ``UNKNOWN`` for runtime parts."""
        if id(node) in seen:
            return {UNKNOWN}
        if len(seen) > MAX_STEPS:
            return {TOO_MANY}  # too many steps to follow: reported
        seen |= {id(node)}
        if isinstance(node, (Names, ast.Constant)):
            return self._written(node)
        if isinstance(node, (ast.Name, ast.Attribute)):
            found = self.objects(node, chain, seen)
            if not found:
                return {UNKNOWN}
            return set().union(*(self.values(v, c or chain, seen) for v, c in found))
        if isinstance(node, Each):
            # No names from a collection known to be empty.
            return set().union(
                *(
                    self.values(v, c or chain, seen)
                    for v, c in self.each(node, chain, seen)
                )
            )
        if isinstance(node, ast.IfExp):
            return self.values(node.body, chain, seen) | self.values(
                node.orelse, chain, seen
            )
        for inline in (self._named_join, self._named_format):
            if (inlined := inline(node, chain, seen)) is not None:
                return inlined
        return self._joined(node, chain, seen)

    @staticmethod
    def _written(node: Names | ast.Constant) -> set[str]:
        """The strings a literal is, or a set of names already worked out."""
        if isinstance(node, Names):
            return set(node.names)
        return {node.value} if isinstance(node.value, str) else {UNKNOWN}

    def _named_format(
        self, node: object, chain: Chain, seen: frozenset[int]
    ) -> set[str] | None:
        """``text % name`` where the name holds a tuple, read as if the tuple were
        written inline."""
        if not (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mod)
            and isinstance(node.right, (ast.Name, ast.Attribute))
        ):
            return None
        found: set[str] = set()
        for value, own in self.objects(node.right, chain, seen):
            if isinstance(value, ast.Tuple):
                inline = ast.BinOp(node.left, node.op, value)
                found |= self._joined(inline, own or chain, seen)
            else:
                found |= self._joined(node, chain, seen)
        return found or {UNKNOWN}

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
            if not isinstance(value, SEQUENCES):
                found.add(UNKNOWN)
                continue
            inline = ast.Call(func=node.func, args=[value], keywords=[])
            found |= self._joined(inline, own or chain, seen)
        return found or {UNKNOWN}

    def _joined(self, node: object, chain: Chain, seen: frozenset[int]) -> set[str]:
        parts = name_parts(node)
        if parts is None:
            return {UNKNOWN}
        return self._join([(part, chain) for part in parts], seen)

    def _join(self, parts: list[Bound], seen: frozenset[int]) -> set[str]:
        """Every string the parts give joined in order. Parts taken from one
        loop row stay together: no string mixes two rows."""
        joined: set[str] = set()
        count = 0
        for row in self._rows(parts, seen):
            alternatives = [sorted(self.values(v, c or [], seen)) for v, c in row]
            count += math.prod(len(a) for a in alternatives)
            # A part already over the limit makes the whole expression over it.
            if any(TOO_MANY in a for a in alternatives) or count > MAX_ALTERNATIVES:
                return {TOO_MANY}
            joined |= {"".join(combo) for combo in itertools.product(*alternatives)}
        return joined

    def _rows(self, parts: list[Bound], seen: frozenset[int]) -> list[list[Bound]]:
        """``parts`` once for each row of a loop that binds two or more of them
        (``for a, b in rows``), with those parts replaced by the row's elements;
        ``parts`` as they are when no loop does or its rows cannot be read."""
        loops: dict[int, list[tuple[int, Each, Chain]]] = {}
        for index, (node, chain) in enumerate(parts):
            if isinstance(node, ast.Name) and chain:
                bound = self.lookup(node.id, chain)
                if len(bound) == 1 and isinstance(bound[0][0], Each):
                    each, own = bound[0]
                    if each.column is not None:
                        loops.setdefault(id(each.iterable), []).append(
                            (index, each, own or chain)
                        )
        shared = next((uses for uses in loops.values() if len(uses) > 1), None)
        if shared is None:
            return [parts]
        _, first, scopes = shared[0]
        aligned: list[list[Bound]] = []
        for row, own in self.items(first.iterable, scopes, seen):
            cells = {
                index: self._cell((row, own or scopes), each, seen)
                for index, each, _ in shared
            }
            if None in cells.values():
                return [parts]
            aligned.append(
                [cells.get(index) or part for index, part in enumerate(parts)]
            )
        return aligned

    def _cell(self, row: Bound, each: Each, seen: frozenset[int]) -> Bound | None:
        """The element of ``row`` that ``each`` names, when it can be read."""
        found = [row]
        for column in (each.column, *each.inner):
            found = self._column(found, column, seen)
        if len(found) != 1:
            return None
        value = found[0][0]
        unread = isinstance(value, ast.Constant) and value.value is None
        return None if unread else found[0]

    def each(self, node: Each, chain: Chain, seen: frozenset[int]) -> list[Bound]:
        found = self.items(node.iterable, chain, seen, node.column)
        for column in node.inner:
            rows: list[Bound] = [(row, own or chain) for row, own in found]
            found = self._column(rows, column, seen)
        return found

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
            taken = self._call_items(node, chain, seen, column)
            return taken or self._unread(node, chain)
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._named(node, chain, seen, column, "item")
        if isinstance(node, SEQUENCES):
            rows: list[Bound] = [
                (
                    param_row(element, column)
                    if isinstance(element, ast.Call)
                    and self._pytest_param(element, chain)
                    else element,
                    chain,
                )
                for element in node.elts
            ]
            return self._column(rows, column, seen)
        if isinstance(node, ast.Dict):
            keys: list[Bound] = []
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    keys += self._spread(self.items(value, chain, seen), chain)
                else:
                    keys.append((key, chain))
            return self._column(keys, column, seen)
        return self._other_items(node, chain, seen, column)

    def _other_items(
        self, node: ast.expr, chain: Chain, seen: frozenset[int], column: int | None
    ) -> list[Bound]:
        """``items`` of two collections added or chosen between; a runtime
        name for anything else, which cannot be read."""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            sides = [node.left, node.right]
        elif isinstance(node, ast.IfExp):
            sides = [node.body, node.orelse]
        else:
            return [(ast.Constant(None), chain)]
        return [b for side in sides for b in self.items(side, chain, seen, column)]

    def _pytest_param(self, call: ast.Call, chain: Chain) -> bool:
        """Whether ``call`` calls pytest's own ``param``: read off the pytest
        module, or a bare ``param`` imported from pytest."""
        func = call.func
        if isinstance(func, ast.Name):
            return any(
                isinstance(value, Imported)
                and (value.module, value.name) == ("pytest", "param")
                for value, _ in self.lookup(func.id, chain)
            )
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "param"
            and isinstance(func.value, ast.Name)
            and self._is_pytest(func.value.id, chain)
        )

    def _column(
        self, found: list[Bound], column: int | None, seen: frozenset[int]
    ) -> list[Bound]:
        """``found``, or element ``column`` of each row; a row given by a name
        is the tuple the name holds."""
        if column is None:
            return found
        rows: list[Bound] = []
        for row, own in found:
            held: list[Bound] = []
            if isinstance(row, (ast.Name, ast.Attribute)) and own:
                held = self.objects(row, own, seen)
            rows += held or [(row, own)]
        taken: list[Bound] = column_of(rows, column)
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
            return self._column(held, column, seen)
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
        held_by = self.objects(node, chain, seen)
        if not held_by:
            # Nothing the check reads binds the name: its items are runtime names.
            return [(ast.Constant(None), chain)]
        found: list[Bound] = []
        circular = False
        for value, own in held_by:
            taken = self._taken(value, own or chain, seen, column, mode)
            circular = circular or taken is None
            found += taken or []
        # A name that only leads back to itself holds nothing that can be read.
        return found or ([(ast.Constant(None), chain)] if circular else [])

    def _taken(
        self,
        value: object,
        chain: Chain,
        seen: frozenset[int],
        column: int | None,
        mode: str,
    ) -> list[Bound] | None:
        """What ``_named`` takes from one value its name holds: nothing only
        from a collection known to be empty, and ``None`` from a value that is
        already being read (a name bound through itself)."""
        unread: list[Bound] = [(ast.Constant(None), chain)]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            match = MARKER.fullmatch(value.value)
            if not match:
                return unread  # a string's characters
            function, param, _ = self.params[int(match.group(1))]
            marker = self.marker(function, param, Mode(mode, column))
            return [(ast.Constant(marker), chain)]
        if not isinstance(value, (ast.expr, Each)):
            return unread  # a function, a class or a module
        if id(value) in seen:
            return None
        inner = seen | {id(value)}
        collect = self.items if mode == "item" else self.dict_values
        # A loop variable looped over in turn: the items of each value it takes.
        elements = (
            self.each(value, chain, inner)
            if isinstance(value, Each)
            else [(value, chain)]
        )
        found: list[Bound] = []
        for element, own in elements:
            scopes = own or chain
            held = collect(element, scopes, inner, column)
            found += held or self._unread(element, scopes)
        return found

    @staticmethod
    def _unread(node: object, chain: Chain) -> list[Bound]:
        """A runtime name for a collection that cannot be read; nothing for a
        literal one, which is then known to be empty."""
        if isinstance(node, (*SEQUENCES, ast.Dict)):
            return []
        return [(ast.Constant(None), chain)]

    def _call_items(
        self, node: ast.Call, chain: Chain, seen: frozenset[int], column: int | None
    ) -> list[Bound]:
        """``items`` of a call: a dict's views, ``enumerate``, ``zip``, ``sorted``..."""
        func, args = node.func, node.args
        if any(isinstance(a, ast.Starred) for a in args):
            return []
        if isinstance(func, ast.Attribute) and not args:
            return self._view_items(func, chain, seen, column)
        if isinstance(func, ast.Name) and self.lookup(func.id, chain):
            return []  # the tests' own function under a built-in's name
        name = ast.unparse(func)
        if name == "enumerate" and args:
            item = _from_start(column, 2) == 1
            return self.items(args[0], chain, seen) if item else []
        if name == "zip":
            column = _from_start(column, len(args))
            if column is None or not 0 <= column < len(args):
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
        pair = _from_start(column, 2)
        if func.attr == "items" and pair == 0:
            return self.items(func.value, chain, seen)
        if func.attr == "items" and pair == 1:
            return self.dict_values(func.value, chain, seen)
        return []
