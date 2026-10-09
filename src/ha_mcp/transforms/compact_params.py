"""One-line renderings of a tool for compact search hits (#2633).

``compact_params`` names every parameter with its type on one line and
``summary`` is a description's first paragraph. The search transform renders
hits with them, and :class:`ha_mcp.llm_exposure.LlmExposureMiddleware` stamps
``params`` into each tool's ``_meta.ha_mcp`` so the custom component, which
cannot import this package, shows the same line in its own search hits.
"""

from __future__ import annotations

import json
from typing import Any


def _literal_labels(branch: dict[str, Any]) -> list[str | None]:
    """Labels of the ``enum``/``const`` values *branch* admits; ``None`` is
    kept as-is for the caller's nullable check."""
    values = [*(branch.get("enum") or [])]
    if "const" in branch:
        values.append(branch["const"])
    return [v if isinstance(v, str) or v is None else json.dumps(v) for v in values]


def _plain_type(branch: dict[str, Any], *, nested: bool) -> str:
    """Label of a branch without literal values."""
    kind = branch.get("type")
    if kind == "array":
        items = _param_type(branch.get("items"))
        return f"({items})[]" if "|" in items else f"{items}[]"
    if isinstance(kind, str) and kind:
        return kind
    if nested and (isinstance(kind, list) or "anyOf" in branch or "oneOf" in branch):
        return _param_type(branch)
    return "object" if {"$ref", "properties", "allOf"} & branch.keys() else "any"


def _param_type(node: Any) -> str:
    """Type label for one parameter: ``|`` joins union branches, enum and
    const values are spelled out inline, ``?`` marks nullable, ``T[]`` an
    array of ``T``."""
    if not isinstance(node, dict):
        return "any"
    kind = node.get("type")
    if isinstance(kind, list):
        branches: list[Any] = [{**node, "type": k} for k in kind]
    else:
        branches = node.get("anyOf") or node.get("oneOf") or [node]
    labels: dict[str, None] = {}
    nullable = False
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        if values := _literal_labels(branch):
            nullable = nullable or None in values
            labels.update(dict.fromkeys(v for v in values if v is not None))
        elif branch.get("type") == "null":
            nullable = True
        else:
            labels[_plain_type(branch, nested=branch is not node)] = None
    if not labels:
        return "null" if nullable else "any"
    return "|".join(labels) + ("?" if nullable else "")


def compact_params(schema: Any) -> str:
    """One line naming every parameter with its type and required marker;
    ``none`` for a tool without parameters."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return "none"
    required = schema.get("required")
    required = set(required) if isinstance(required, list) else set()
    return "; ".join(
        f"{name} ({_param_type(field)}{', required' if name in required else ''})"
        for name, field in props.items()
    )


def summary(description: str | None) -> str:
    """The first paragraph of a tool description, on one line.

    A docstring opens with its summary line; the paragraphs after it (and
    the BM25 keyword list ``SearchKeywordsTransform`` appends) come back
    with the full definition from ``tools=[...]``.
    """
    return " ".join((description or "").split("\n\n", 1)[0].split())
