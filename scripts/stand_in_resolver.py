"""Name resolution for ``scripts/integration_stand_ins.py``: every string an
expression can be, and the items a loop takes from a collection.

``Resolver`` builds on ``Reader`` (``stand_in_reader.py``), which reads the
modules and their scopes; the check's scan (``Checker``) builds on it.
"""

from __future__ import annotations

import ast
import itertools
import math

from stand_in_reader import (  # type: ignore[import-not-found]
    MARKER,
    MAX_ALTERNATIVES,
    MAX_STEPS,
    SEQUENCES,
    TOO_MANY,
    UNKNOWN,
    Mode,
    Reader,
)
from stand_in_scopes import (  # type: ignore[import-not-found]
    Bound,
    Chain,
    Each,
    Imported,
    Names,
    column_of,
    fixed_length,
)
from stand_in_syntax import (  # type: ignore[import-not-found]
    name_parts,
    param_row,
)

# Calls that give their argument's elements unchanged: ``for x in sorted(y)``
# loops over ``y``'s.
_SAME_ITEMS = ("sorted", "list", "tuple", "set", "frozenset")


def _from_start(column: int | None, width: int) -> int | None:
    """``column`` counted from the start of a row ``width`` long."""
    return column + width if column is not None and column < 0 else column


class Resolver(Reader):
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
