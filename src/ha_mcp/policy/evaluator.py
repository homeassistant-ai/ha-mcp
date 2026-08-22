"""Evaluate a tool call against a Policy. Pure functions — no I/O, no state."""

import logging
import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Any

from .model import Policy, Predicate, Rule

logger = logging.getLogger(__name__)


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


def _contains_matches(val: Any, pv: Any) -> bool:
    if isinstance(val, str) and isinstance(pv, str):
        return pv.lower() in val.lower()
    # Mirror the case-insensitive treatment that ``eq`` / ``in`` /
    # ``not_in`` already apply: a rule listing ``["light.kitchen"]``
    # must match an LLM passing ``"Light.Kitchen"``. Per-element
    # ``_ci`` guards non-string entries so mixed-type collections
    # (e.g. ``[1, "two"]``) keep their natural equality semantics.
    return isinstance(val, (list, tuple, set)) and any(_ci(pv) == _ci(x) for x in val)


def _numeric_matches(val: Any, op: str, pv: Any) -> bool:
    try:
        return bool(val > pv) if op == "gt" else bool(val < pv)
    except TypeError:
        # Numeric rule against a non-numeric arg value — log so
        # users can tell their "temperature > 30" rule isn't
        # silently never firing because the arg is a string.
        logger.debug(
            "policy: %s type-mismatch (val=%r pv=%r) — predicate skipped",
            op,
            val,
            pv,
        )
        return False


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
            return _contains_matches(val, pv)
        case "gt" | "lt":
            return _numeric_matches(val, op, pv)
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
    if rule.tool_name not in ("*", tool_name):
        return False
    return all(match_predicate(p, args) for p in rule.when)


def find_matching_rule(
    tool_name: str, args: dict[str, Any], policy: Policy
) -> Rule | None:
    for rule in policy.rules:
        if match_rule(rule, tool_name, args):
            return rule
    return None


def _predicate_reaches_operations(path: str) -> bool:
    """Whether ``path`` can walk into ``args.operations`` under ``iter_path_values``.

    ``operations`` sits directly under ``args``, so only the first two
    segments after stripping the implicit ``args`` prefix matter. A literal
    ``operations`` first segment obviously reaches it. A leading ``*`` reaches
    it too — a wildcard segment fans out over EVERY value at that level (see
    ``iter_path_values``), landing on the `operations` list value exactly as
    readily as any other top-level key — but ``operations`` is a *list*, so a
    literal segment right after that wildcard (e.g. ``domain`` in
    ``args.*.domain``) can only ever match a *dict* value at that level (like
    ``selector``) — ``walk()`` requires ``isinstance(cur, dict)`` for a
    literal head, so it silently yields nothing against a list and can never
    reach an operation row. Only a SECOND wildcard (``args.*.*...``, as in
    ``args.*.*.entity_id``) or no further segment at all (bare ``args.*``,
    which yields the raw ``operations`` list value itself) can actually reach
    into the list. Any other concrete first segment (e.g. ``selector``) can
    only ever address selector-inspectable fields and is precisely excluded.
    """
    parts = path.split(".")
    if parts and parts[0] == "args":
        parts = parts[1:]
    if not parts:
        return False
    if parts[0] == "operations":
        return True
    return parts[0] == "*" and (len(parts) == 1 or parts[1] == "*")


def _rule_needs_resolved_operations(rule: Rule) -> bool:
    """Whether ``rule`` inspects fields only known after selector resolution.

    A selector-mode ``ha_bulk_control`` call carries ``args.selector``, not
    ``args.operations`` — the leaf targets don't exist yet, they're resolved
    inside the tool after this middleware runs. A rule predicate that can
    reach ``args.operations`` (exact, prefixed, or via a leading wildcard —
    see ``_predicate_reaches_operations``) can therefore never get a fair
    match attempt against a selector call and needs the fail-safe below. A
    rule whose predicates only ever address selector-inspectable fields
    (e.g. ``args.selector.domain``) already got a fair, precise match
    attempt in ``find_matching_rule`` and must not be broadened into an
    unconditional gate.
    """
    return any(_predicate_reaches_operations(p.path) for p in rule.when)


def evaluate(tool_name: str, args: dict[str, Any], policy: Policy) -> Verdict:
    if find_matching_rule(tool_name, args, policy) is not None:
        return Verdict.REQUIRE_APPROVAL
    # ha_call_service exposes a raw WebSocket escape hatch (``ws_command``) that
    # carries no ``domain``/``service`` argument, so a rule keyed on
    # ``args.domain``/``args.service`` cannot match it and it would otherwise slip
    # through the fail-open default. If the operator has ANY rule that applies to
    # ha_call_service -- one scoped to it by name, or a wildcard ``*`` rule, which
    # ``match_rule`` treats as applying to every tool -- treat an unmatched
    # ws_command call as require-approval (fail safe) so the escape hatch cannot
    # sneak past that oversight. Blocking the call (require-approval) rather than
    # silently allowing it is the safe error direction for a raw WS escape hatch,
    # especially since the write-command blocklist is a deliberately
    # non-exhaustive wrapper-bypass list that leans on this gate.
    # Operators who want finer control can add a rule keyed on ``args.ws_command``.
    if (
        tool_name == "ha_call_service"
        and args.get("ws_command")
        and any(rule.tool_name in ("ha_call_service", "*") for rule in policy.rules)
    ):
        return Verdict.REQUIRE_APPROVAL
    # Structural selectors are resolved inside the tool, after this middleware.
    # A pre-existing rule that inspects args.operations.* therefore cannot inspect
    # the eventual leaf targets. Fail safe only for rules that actually depend on
    # that unresolved data — a rule fully expressed over selector-inspectable
    # fields (e.g. args.selector.domain) already had its precise shot at matching
    # above, and broadening it here would defeat a deliberately conditional rule
    # (a rule scoped to selector.domain == "lock" must not gate a "light" call).
    if (
        tool_name == "ha_bulk_control"
        and args.get("selector") is not None
        and any(
            rule.tool_name in ("ha_bulk_control", "*")
            and _rule_needs_resolved_operations(rule)
            for rule in policy.rules
        )
    ):
        return Verdict.REQUIRE_APPROVAL
    return Verdict.ALLOW
