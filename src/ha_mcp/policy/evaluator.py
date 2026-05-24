"""Evaluate a tool call against a Policy. Pure functions — no I/O, no state."""

import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Any

from .model import Policy, Predicate, Rule


class Verdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"


def iter_path_values(args: dict[str, Any], path: str) -> Iterator[Any]:
    """Yield every value the dotted path resolves to.

    The leading ``args`` segment is implicit and stripped. A ``*`` segment
    fans out across the current node — across dict values for dicts,
    across items for lists — so ``args.*`` yields every top-level
    argument, ``args.config.*`` yields every leaf of the ``config``
    sub-dict, and so on. Empty iterator = no match.
    """
    parts = path.split(".")
    if parts[0] == "args":
        parts = parts[1:]

    def walk(cur: Any, rest: list[str]) -> Iterator[Any]:
        if not rest:
            yield cur
            return
        head, tail = rest[0], rest[1:]
        if head == "*":
            if isinstance(cur, dict):
                for v in cur.values():
                    yield from walk(v, tail)
            elif isinstance(cur, (list, tuple)):
                for v in cur:
                    yield from walk(v, tail)
            return
        if isinstance(cur, dict) and head in cur:
            yield from walk(cur[head], tail)

    yield from walk(args, parts)


def _ci(x: Any) -> Any:
    """Lower-case strings for case-insensitive comparison; pass other
    types through unchanged so type semantics (int != "1") survive.
    Used on both sides of every string op — security gates should fire
    whether the caller wrote 'Lock' or 'LOCK' or 'lock'."""
    return x.lower() if isinstance(x, str) else x


def _op_matches(val: Any, op: str, pv: Any) -> bool:
    """Apply one op to one concrete value. Predicate dispatches over
    the candidate values (which may be many for wildcard paths).

    String comparisons are case-insensitive (security gates shouldn't
    care whether the LLM lowercased its args). Non-string types
    preserve their natural comparison semantics.
    """
    match op:
        case "eq":
            return bool(_ci(val) == _ci(pv))
        case "neq":
            return bool(_ci(val) != _ci(pv))
        case "in":
            return _ci(val) in [_ci(x) for x in (pv or [])]
        case "not_in":
            return _ci(val) not in [_ci(x) for x in (pv or [])]
        case "regex":
            # `regex` is re.search (substring match). Anchor with ^...$
            # for full-match. re.IGNORECASE so '^light\.' matches 'Light.x'.
            return (
                isinstance(val, str)
                and isinstance(pv, str)
                and re.search(pv, val, re.IGNORECASE) is not None
            )
        case "contains":
            if isinstance(val, str) and isinstance(pv, str):
                return pv.lower() in val.lower()
            return isinstance(val, (list, tuple, set)) and pv in val
        case "gt":
            try:
                return bool(val > pv)
            except TypeError:
                return False
        case "lt":
            try:
                return bool(val < pv)
            except TypeError:
                return False
    return False


def match_predicate(predicate: Predicate, args: dict[str, Any]) -> bool:
    values = list(iter_path_values(args, predicate.path))
    if predicate.op == "exists":
        return bool(values)
    if not values:
        return False
    # Existential semantics: a wildcard path matches if ANY value at the
    # wildcard satisfies the op. For non-wildcard paths there's at most
    # one value so the any() collapses to a single check.
    return any(_op_matches(v, predicate.op, predicate.value) for v in values)


def match_rule(rule: Rule, tool_name: str, args: dict[str, Any]) -> bool:
    if rule.tool_name != "*" and rule.tool_name != tool_name:
        return False
    return all(match_predicate(p, args) for p in rule.when)


def find_matching_rule(
    tool_name: str, args: dict[str, Any], policy: Policy
) -> Rule | None:
    for rule in policy.rules:
        if match_rule(rule, tool_name, args):
            return rule
    return None


def evaluate(tool_name: str, args: dict[str, Any], policy: Policy) -> Verdict:
    if find_matching_rule(tool_name, args, policy) is not None:
        return Verdict.REQUIRE_APPROVAL
    return Verdict.ALLOW
