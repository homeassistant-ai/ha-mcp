"""Apply a JSON Patch subset to dashboard configs without mutating the input.

This stdlib-only module is shipped byte-for-byte in the standalone component too.
Keep both copies synchronized; the unit contract checks parity and behavior.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


def _pointer_tokens(path: Any) -> list[str]:
    """Decode an RFC 6901 pointer, rejecting invalid escape sequences."""
    if not isinstance(path, str):
        raise ValueError("path must be a JSON pointer string")
    if path == "":
        return []
    if not path.startswith("/") or re.search(r"~(?:[^01]|$)", path):
        raise ValueError("invalid JSON pointer")
    return [
        token.replace("~1", "/").replace("~0", "~") for token in path[1:].split("/")
    ]


def _array_index(token: str, length: int, *, adding: bool = False) -> int:
    """Resolve an array index; only add permits the end index or '-' token."""
    if adding and token == "-":
        return length
    if re.fullmatch(r"0|[1-9][0-9]*", token) is None:
        raise ValueError("invalid array index")
    # Bound before int conversion, including Python's oversized-integer limit.
    if len(token) > len(str(length)):
        raise ValueError("array index out of bounds")
    index = int(token)
    if index > length or (index == length and not adding):
        raise ValueError("array index out of bounds")
    return index


def _get_child(parent: Any, token: str) -> Any:
    if isinstance(parent, dict):
        if token not in parent:
            raise ValueError("path target does not exist")
        return parent[token]
    if isinstance(parent, list):
        return parent[_array_index(token, len(parent))]
    raise ValueError("path traverses a scalar value")


def _json_equal(actual: Any, expected: Any) -> bool:
    """RFC 6902 equality: numbers by value, booleans distinct from numbers."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return bool(actual == expected)
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(
            _json_equal(value, expected[key]) for key, value in actual.items()
        )
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(
            _json_equal(left, right)
            for left, right in zip(actual, expected, strict=True)
        )
    return bool(actual == expected)


def _validate_operation(operation: Any) -> tuple[str, list[str], Any]:
    if not isinstance(operation, dict):
        raise ValueError("operation must be an object")
    op = operation.get("op")
    if not isinstance(op, str) or op not in {"add", "remove", "replace", "test"}:
        raise ValueError("supported operations are add, remove, replace, and test")
    tokens = _pointer_tokens(operation.get("path"))
    if op in {"add", "replace", "test"} and "value" not in operation:
        raise ValueError("operation requires a value")
    return op, tokens, operation.get("value")


def _edit_child(parent: Any, token: str, op: str, value: Any) -> None:
    if isinstance(parent, dict):
        if op != "add" and token not in parent:
            raise ValueError("path target does not exist")
        if op == "remove":
            del parent[token]
        else:
            parent[token] = deepcopy(value)
        return
    if isinstance(parent, list):
        index = _array_index(token, len(parent), adding=op == "add")
        if op == "add":
            parent.insert(index, deepcopy(value))
        elif op == "remove":
            del parent[index]
        else:
            parent[index] = deepcopy(value)
        return
    raise ValueError("path traverses a scalar value")


def _apply_operation(document: Any, operation: Any) -> Any:
    op, tokens, value = _validate_operation(operation)
    if op == "test":
        target = document
        for token in tokens:
            target = _get_child(target, token)
        if not _json_equal(target, value):
            raise ValueError("test operation failed")
        return document
    if not tokens:
        if op == "remove":
            raise ValueError("dashboard root cannot be removed")
        return deepcopy(value)
    parent = document
    for token in tokens[:-1]:
        parent = _get_child(parent, token)
    _edit_child(parent, tokens[-1], op, value)
    return document


def apply_dashboard_patch(
    config: dict[str, Any], patch: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return an independent patched config or raise ValueError atomically.

    Supports add/remove/replace/test, with RFC 6901 paths and at most 100
    operations. Empty paths address the root; the result must be a dictionary.
    Strings, including templates, are copied literally and never evaluated.
    Errors identify the operation without disclosing config or patch values.
    """
    if not isinstance(config, dict):
        raise ValueError("dashboard config must be an object")
    if not isinstance(patch, list):
        raise ValueError("patch must be a list of operations")
    if len(patch) > 100:
        raise ValueError("patch must contain at most 100 operations")
    document: Any = deepcopy(config)
    for number, operation in enumerate(patch, start=1):
        try:
            document = _apply_operation(document, operation)
        except ValueError as error:
            raise ValueError(f"Patch operation {number}: {error}") from None
    if not isinstance(document, dict):
        raise ValueError("patched dashboard config must be an object")
    return document
