"""MCP safety annotations for tool decorators.

Every tool declares all four hints. Home Assistant 2026.10+ copies them into
its LLM tool metadata and fills an omitted hint with its least-safe default, so
a read-only tool that leaves one out reaches Core as destructive.

``scripts/extract_tools.py`` and ``tests/src/unit/test_tool_annotations.py``
load this file by path and evaluate its calls on literal arguments, so it must
import nothing outside the standard library.
"""

import ast
from typing import Any


def read_only_hints(title: str, *, open_world: bool) -> dict[str, Any]:
    """Annotations for a tool that changes nothing."""
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": open_world,
    }


def write_hints(
    title: str, *, destructive: bool, idempotent: bool, open_world: bool
) -> dict[str, Any]:
    """Annotations for a tool that changes state."""
    return {
        "title": title,
        "readOnlyHint": False,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


_BUILDERS = {"read_only_hints": read_only_hints, "write_hints": write_hints}


def annotations_from_ast(node: ast.expr) -> dict[str, Any]:
    """What an ``annotations=`` value evaluates to, read from source.

    Handles a dict literal and a call to one of the builders above with literal
    arguments; anything else yields ``{}``.
    """
    if isinstance(node, ast.Dict):
        return {
            k.value: v.value
            for k, v in zip(node.keys, node.values, strict=True)
            if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
        }
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) in _BUILDERS:
        return _BUILDERS[node.func.id](  # type: ignore[attr-defined]
            *(ast.literal_eval(arg) for arg in node.args),
            **{kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg},
        )
    return {}
