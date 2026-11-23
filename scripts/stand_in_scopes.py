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


Bound = tuple[object, "Chain | None"]  # a value, and the scopes it resolves in


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
    """What a ``for`` takes from ``iterable``, or element ``column`` of it."""

    iterable: ast.expr
    column: int | None


class Names(NamedTuple):
    """Strings already worked out, standing where an expression would."""

    names: frozenset[str]


def bind(scope: Scope, target: ast.expr, value: object, new: list[object]) -> None:
    """Bind ``target`` to ``value``; ``new`` collects each value bound."""
    if isinstance(target, ast.Name):
        scope.setdefault(target.id, []).append((value, None))
        new.append(value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
        same_shape = len(values) == len(target.elts)
        for index, element in enumerate(target.elts):
            part = values[index] if same_shape else ast.Constant(None)
            bind(scope, element, part, new)


def bind_loop(
    scope: Scope, target: ast.expr, iterable: ast.expr, new: list[object]
) -> None:
    if isinstance(target, (ast.Tuple, ast.List)):
        for index, element in enumerate(target.elts):
            bind(scope, element, Each(iterable, index), new)
    else:
        bind(scope, target, Each(iterable, None), new)


def fixed_length(node: object) -> int | None:
    """How many elements a list or tuple literal has; a set's order and size,
    and a starred element's size, are only known at runtime."""
    if isinstance(node, (ast.Tuple, ast.List)) and not any(
        isinstance(element, ast.Starred) for element in node.elts
    ):
        return len(node.elts)
    return None


def bind_import(scope: Scope, node: ast.Import | ast.ImportFrom, package: str) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name if alias.asname else alias.name.split(".")[0]
            scope.setdefault(alias.asname or name, []).append((ModuleRef(name), None))
        return
    base = node.module or ""
    if node.level:
        parent = package.split(".")[: len(package.split(".")) - node.level + 1]
        base = ".".join([*parent, *([base] if base else [])])
    for alias in node.names:
        scope.setdefault(alias.asname or alias.name, []).append(
            (Imported(base, alias.name), None)
        )


def _parametrized(
    decorator: ast.expr, text_of: Callable[[ast.expr], str | None]
) -> tuple[list[str], ast.expr] | None:
    """The argument names a ``pytest.mark.parametrize`` decorator gives rows to
    (an empty name for one whose rows go to a fixture), and the rows."""
    if not (
        isinstance(decorator, ast.Call)
        and ast.unparse(decorator.func).endswith("parametrize")
    ):
        return None
    # pytest takes the names and the rows by position or by keyword.
    keywords = {k.arg: k.value for k in decorator.keywords}
    names = decorator.args[0] if decorator.args else keywords.get("argnames")
    rows = decorator.args[1] if len(decorator.args) > 1 else keywords.get("argvalues")
    if names is None or rows is None:
        return None
    if isinstance(names, ast.Constant) and isinstance(names.value, str):
        fields = [name.strip() for name in names.value.split(",")]
    elif isinstance(names, _SEQUENCES):
        fields = [str(e.value) for e in names.elts if isinstance(e, ast.Constant)]
    elif (text := text_of(names)) is not None:
        fields = [name.strip() for name in text.split(",")]
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
) -> None:
    """Bind the values ``pytest.mark.parametrize`` gives each argument;
    ``text_of`` reads argument names given through a name."""
    for decorator in node.decorator_list:
        found = _parametrized(decorator, text_of)
        if found is None:
            continue
        fields, rows = found
        for index, name in enumerate(fields):
            column = index if len(fields) > 1 else None
            if name:
                scope.setdefault(name, []).append((Each(rows, column), None))


def column_of(found: Sequence[Bound], column: int | None) -> list[Bound]:
    """``found``, or element ``column`` of each of its tuples."""
    if column is None:
        return list(found)
    return [
        (element.elts[column], own)
        for element, own in found
        if isinstance(element, (ast.Tuple, ast.List)) and len(element.elts) > column
    ]
