"""Evaluate a tool call against a Policy. Pure functions — no I/O, no state."""

import re
from enum import Enum
from typing import Any

from .model import Policy, Predicate, Rule

_MISSING = object()


class Verdict(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"


def extract_path(args: dict[str, Any], path: str) -> Any:
    """Walk a dotted path like 'args.config.alias' against the args dict.

    'args' is implicit — the leading 'args.' is stripped. Returns _MISSING if any
    intermediate key is absent.
    """
    parts = path.split(".")
    if parts[0] == "args":
        parts = parts[1:]
    cur: Any = args
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def match_predicate(predicate: Predicate, args: dict[str, Any]) -> bool:
    val = extract_path(args, predicate.path)
    if predicate.op == "exists":
        return val is not _MISSING
    if val is _MISSING:
        return False
    pv = predicate.value
    match predicate.op:
        case "eq":       return val == pv
        case "neq":      return val != pv
        case "in":       return val in (pv or [])
        case "not_in":   return val not in (pv or [])
        case "regex":    return isinstance(val, str) and re.search(pv, val) is not None
        case "contains": return pv in val if hasattr(val, "__contains__") else False
        case "gt":       return val > pv
        case "lt":       return val < pv
    return False


def match_rule(rule: Rule, tool_name: str, args: dict[str, Any]) -> bool:
    if rule.tool_name != "*" and rule.tool_name != tool_name:
        return False
    return all(match_predicate(p, args) for p in rule.when)


def find_matching_rule(tool_name: str, args: dict[str, Any],
                       policy: Policy) -> Rule | None:
    for rule in policy.rules:
        if match_rule(rule, tool_name, args):
            return rule
    return None


def evaluate(tool_name: str, args: dict[str, Any], policy: Policy) -> Verdict:
    if not policy.enabled:
        return Verdict.ALLOW
    if find_matching_rule(tool_name, args, policy) is not None:
        return Verdict.REQUIRE_APPROVAL
    return (Verdict.REQUIRE_APPROVAL
            if policy.default_action == "require_approval"
            else Verdict.ALLOW)
