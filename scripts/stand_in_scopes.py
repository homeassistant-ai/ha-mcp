"""Scopes for ``scripts/integration_stand_ins.py``: what each name in a module,
class or function is bound to, and the statements that bind it.

A scope maps a name to every value bound to it. A chain is the scopes a name
is looked up in, innermost last.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from typing import NamedTuple

_SEQUENCES = (ast.Tuple, ast.List, ast.Set)

Chain = list["Scope"]


# A value (an expression, a definition, or an ``Each``, ``Imported``,
# ``ModuleRef`` or ``Names``) and the scopes it resolves in: ``None`` for the
# scopes its name is found in.
Bound = tuple[object, "Chain | None"]


Scope = dict[str, list[Bound]]


class AsOf(list["Scope"]):
    """Scopes whose innermost one is read as it stood at ``at``, a line and
    column."""

    def __init__(self, scopes: Chain, at: tuple[int, int]) -> None:
        super().__init__(scopes)
        self.at = at


class Imported(NamedTuple):
    module: str
    name: str


class ModuleRef(NamedTuple):
    module: str


class Each(NamedTuple):
    """What a ``for`` takes from ``iterable``, or element ``column`` of it
    (from the end when negative), and from there the element each column of
    ``inner`` names in turn (``for (a, b), c in rows``)."""

    iterable: ast.expr
    column: int | None
    inner: tuple[int, ...] = ()


class Names(NamedTuple):
    """Strings already worked out, standing where an expression would."""

    names: frozenset[str]


def bind(scope: Scope, target: ast.expr, value: object, new: list[object]) -> None:
    """Bind ``target`` to ``value``; ``new`` collects each value bound."""
    if isinstance(target, ast.Name):
        scope.setdefault(target.id, []).append((value, None))
        new.append(value)
    elif isinstance(target, ast.Starred):
        # ``*rest`` holds a list only known at runtime.
        bind(scope, target.value, ast.Constant(None), new)
    elif isinstance(target, (ast.Tuple, ast.List)):
        values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
        stars = [isinstance(element, ast.Starred) for element in target.elts]
        named = len(stars) - any(stars)
        # An element for every target, and none a ``*`` of unknown length.
        readable = (
            len(values) >= named if any(stars) else len(values) == named
        ) and not any(isinstance(element, ast.Starred) for element in values)
        for index, element in enumerate(target.elts):
            # A target after ``*rest`` is counted from the end.
            column = index - len(stars) if any(stars[:index]) else index
            # ``*rest`` takes no element of its own.
            taken = readable and not stars[index]
            bind(scope, element, values[column] if taken else ast.Constant(None), new)


def bind_unread(scope: Scope, node: ast.AST, new: list[object]) -> list[ast.expr]:
    """Bind what ``with``, ``except`` and ``match`` name to a value only known
    at runtime; the targets of a ``with``."""
    targets: list[ast.expr] = []
    names: list[str | None] = []
    if isinstance(node, (ast.With, ast.AsyncWith)):
        targets = [item.optional_vars for item in node.items if item.optional_vars]
        for target in targets:
            bind(scope, target, ast.Constant(None), new)
    elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        names = [node.name]
    elif isinstance(node, ast.MatchMapping):
        names = [node.rest]
    for name in filter(None, names):
        value = ast.Constant(None)
        scope.setdefault(name, []).append((value, None))
        new.append(value)
    return targets


def bind_loop(
    scope: Scope,
    target: ast.expr,
    iterable: ast.expr,
    new: list[object],
    path: tuple[int, ...] = (),
) -> None:
    """Bind a loop's target; ``path`` is the columns that lead to it."""
    if isinstance(target, (ast.Tuple, ast.List)):
        stars = [isinstance(element, ast.Starred) for element in target.elts]
        for index, element in enumerate(target.elts):
            # A target after ``*rest`` is counted from the end of each row.
            column = index - len(stars) if any(stars[:index]) else index
            bind_loop(scope, element, iterable, new, (*path, column))
    else:
        first = path[0] if path else None
        bind(scope, target, Each(iterable, first, path[1:]), new)


def fixed_length(node: object) -> int | None:
    """How many elements a list or tuple literal has; a set's order and size,
    and a starred element's size, are only known at runtime."""
    if isinstance(node, (ast.Tuple, ast.List)) and not any(
        isinstance(element, ast.Starred) for element in node.elts
    ):
        return len(node.elts)
    return None


def bind_import(
    scope: Scope, node: ast.Import | ast.ImportFrom, package: str, new: list[object]
) -> None:
    """Bind what an import names; ``new`` collects each value bound."""
    value: Imported | ModuleRef
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name if alias.asname else alias.name.split(".")[0]
            value = ModuleRef(name)
            scope.setdefault(alias.asname or name, []).append((value, None))
            new.append(value)
        return
    base = node.module or ""
    if node.level:
        parent = package.split(".")[: len(package.split(".")) - node.level + 1]
        base = ".".join([*parent, *([base] if base else [])])
    for alias in node.names:
        value = Imported(base, alias.name)
        scope.setdefault(alias.asname or alias.name, []).append((value, None))
        new.append(value)


def _parametrized(
    decorator: ast.expr,
    text_of: Callable[[ast.expr], str | None],
    is_parametrize: Callable[[ast.expr], bool],
) -> tuple[list[str], ast.expr] | None:
    """The argument names a ``pytest.mark.parametrize`` decorator gives rows to
    (an empty name for one whose rows go to a fixture), and the rows."""
    if not (isinstance(decorator, ast.Call) and is_parametrize(decorator.func)):
        return None
    # pytest takes the names and the rows by position or by keyword.
    keywords = {k.arg: k.value for k in decorator.keywords}
    names = decorator.args[0] if decorator.args else keywords.get("argnames")
    rows = decorator.args[1] if len(decorator.args) > 1 else keywords.get("argvalues")
    if names is None or rows is None:
        return None
    if isinstance(names, _SEQUENCES):
        # One argument each: a name that cannot be read leaves the rows unread.
        read = [text_of(element) for element in names.elts]
        fields = [name for name in read if name is not None]
        if len(fields) != len(read):
            return None
    elif (text := text_of(names)) is not None:
        # As pytest does: a trailing comma names no argument.
        fields = [name.strip() for name in text.split(",") if name.strip()]
    else:
        return None
    # ``indirect`` hands rows to fixtures: what the test then gets is theirs.
    indirect = keywords.get("indirect")
    if isinstance(indirect, (ast.List, ast.Tuple)):
        fixtures = {e.value for e in indirect.elts if isinstance(e, ast.Constant)}
        fields = ["" if name in fixtures else name for name in fields]
    elif indirect is not None and getattr(indirect, "value", True) is not False:
        return None
    return fields, rows


def bind_parametrized(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    scope: Scope,
    text_of: Callable[[ast.expr], str | None],
    is_parametrize: Callable[[ast.expr], bool],
    around: Chain,
) -> None:
    """Bind the values ``pytest.mark.parametrize`` gives each argument;
    ``text_of`` reads argument names given through a name,
    ``is_parametrize`` says whether a decorator is pytest's own, and the rows
    are read in ``around``, the scopes the decorator stands in."""
    for decorator in node.decorator_list:
        found = _parametrized(decorator, text_of, is_parametrize)
        if found is None:
            continue
        fields, rows = found
        for index, name in enumerate(fields):
            column = index if len(fields) > 1 else None
            if name:
                scope.setdefault(name, []).append((Each(rows, column), around))


def column_of(found: Sequence[Bound], column: int | None) -> list[Bound]:
    """``found``, or element ``column`` of each of its rows. A row that is not
    a tuple or list written out, is too short or holds a ``*`` gives a value
    only known at runtime: no row is left out."""
    if column is None:
        return list(found)
    taken: list[Bound] = []
    for row, own in found:
        element: ast.expr = ast.Constant(None)
        if (
            isinstance(row, (ast.Tuple, ast.List))
            and -len(row.elts) <= column < len(row.elts)
            and not any(isinstance(each, ast.Starred) for each in row.elts)
        ):
            element = row.elts[column]
        taken.append((element, own))
    return taken
