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


def bind_parametrized(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    scope: Scope,
    text_of: Callable[[ast.expr], str | None],
) -> None:
    """Bind the values ``pytest.mark.parametrize`` gives each argument;
    ``text_of`` reads argument names given through a name."""
    for decorator in node.decorator_list:
        if not (
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).endswith("parametrize")
            and len(decorator.args) >= 2
        ):
            continue
        names, rows = decorator.args[:2]
        # ``indirect`` hands the rows to fixtures: what the test gets is theirs.
        indirect = next(
            (k.value for k in decorator.keywords if k.arg == "indirect"), None
        )
        to_fixtures: set[object] = set()
        if isinstance(indirect, (ast.List, ast.Tuple)):
            to_fixtures = {
                e.value for e in indirect.elts if isinstance(e, ast.Constant)
            }
        elif indirect is not None and getattr(indirect, "value", True) is not False:
            continue
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
            if name not in to_fixtures:
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
