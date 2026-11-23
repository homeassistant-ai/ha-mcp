"""The pieces a string expression joins, read off its syntax.

``scripts/integration_stand_ins.py`` resolves each piece to the names it can
be: a concatenation, an f-string, ``%``, ``str.format`` or ``str.join`` of
string literals and other expressions.
"""

from __future__ import annotations

import ast
import string


def literal_text(node: ast.expr) -> str | None:
    """The string ``node`` is written as, if it is a string literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _interleave(pieces: list[str], args: list[ast.expr]) -> list[ast.expr]:
    """``pieces[0]``, ``args[0]``, ``pieces[1]``, ... as the parts of a string."""
    parts: list[ast.expr] = [ast.Constant(pieces[0])]
    for arg, piece in zip(args, pieces[1:], strict=True):
        parts += [arg, ast.Constant(piece)]
    return parts


def _format_parts(text: str, call: ast.Call) -> list[ast.expr] | None:
    """The parts ``text.format(...)`` joins, when every field is a plain one."""
    if any(isinstance(a, ast.Starred) for a in call.args) or any(
        k.arg is None for k in call.keywords
    ):
        return None
    keywords = {k.arg: k.value for k in call.keywords}
    parts: list[ast.expr] = []
    automatic = 0
    try:
        fields = list(string.Formatter().parse(text))
    except ValueError:
        return None
    for literal, field, spec, conversion in fields:
        parts.append(ast.Constant(literal))
        if field is None:
            continue
        if spec or conversion:
            return None
        key = field
        if not key:
            key, automatic = str(automatic), automatic + 1
        if key.isdigit() and int(key) < len(call.args):
            parts.append(call.args[int(key)])
        elif key in keywords:
            parts.append(keywords[key])
        else:
            return None
    return parts


def name_parts(node: object) -> list[ast.expr] | None:
    """The pieces a concatenation or formatting joins, in order."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [node.left, node.right]
    if isinstance(node, ast.JoinedStr):
        return list(node.values)
    if (
        isinstance(node, ast.FormattedValue)
        and node.conversion == -1
        and node.format_spec is None
    ):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        template = literal_text(node.left)
        pieces = [] if template is None else template.split("%s")
        if (
            len(pieces) != len(args) + 1
            or any("%" in piece for piece in pieces)
            or any(isinstance(a, ast.Starred) for a in args)
        ):
            return None
        return _interleave(pieces, args)
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    text = literal_text(node.func.value)
    if text is None:
        return None
    if node.func.attr == "format":
        return _format_parts(text, node)
    joined = node.args[0] if len(node.args) == 1 else None
    if (
        node.func.attr != "join"
        or node.keywords
        or not isinstance(joined, (ast.List, ast.Tuple))
        or not joined.elts
        or any(isinstance(e, ast.Starred) for e in joined.elts)
    ):
        return None
    return _interleave(["", *[text] * (len(joined.elts) - 1), ""], joined.elts)
