"""Evaluate a tool call against a Policy. Pure functions — no I/O, no state."""

import logging
import re
from collections.abc import Iterable, Iterator
from enum import StrEnum
from typing import Any

from ..tools.util_helpers import loads_if_json_container_str
from .model import Policy, Predicate, Rule

logger = logging.getLogger(__name__)


class Verdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"


def normalize_stringified_containers(value: Any) -> Any:
    """Recursively parse JSON-encoded object/array strings into real containers.

    Some MCP client stacks (Claude Desktop stdio among them — see
    ``tools/util_helpers.py``'s ``JSON_STRING_COERCION``) pass model-emitted
    stringified objects through unrepaired, e.g. sending
    ``{"selector": "{\\"domain\\": \\"light\\"}"}`` instead of a nested
    object. Pydantic's ``JSON_STRING_COERCION`` ``BeforeValidator`` repairs
    this, but only when the tool's own parameter validation runs — INSIDE
    ``call_next``, after policy evaluation. ``iter_path_values`` only
    descends into ``dict``/``list``, so a still-stringified value makes a
    predicate targeting a nested field (``args.selector.domain``,
    ``args.operations.*.entity_id``) silently yield nothing: no rule
    matches, and — for ``ha_bulk_control`` — ``args.get("selector")`` still
    sees a truthy string, so the selector fail-safe's own dynamic-call
    detection still fires, but nothing inside it can be inspected either.
    Applying the same repair here, once, before evaluation and hashing,
    closes that gap for both plain predicate rules and the fail-safe, and
    makes ``compute_args_hash`` key on the same logical value regardless of
    which wire shape the client sent.

    Malformed JSON that merely looks like a container is left as the raw
    string (not raised): policy evaluation is not the place to surface a
    JSON syntax error — the tool's own validation does that, with a
    properly attributed parameter name.

    Deeply-nested INPUT (real nested dicts/lists in the caller's own
    ``args`` -- NOT a stringified container decoding into deep nesting;
    ``loads_if_json_container_str`` already absorbs ``json.loads``'s own
    ``RecursionError``, so a still-stringified value can never reach this
    function's own recursion at all) can exceed the interpreter's
    recursion limit and raise ``RecursionError`` here. Unlike the
    malformed-JSON case, this is deliberately NOT swallowed: silently
    returning the unrepaired value would let this security gate evaluate
    (and hash) unnormalized args and, for a rule scoped to a nested
    selector-inspectable field, silently ALLOW a call that should have
    required approval -- the identical "fail open on a degraded gate"
    mistake ``PolicyMiddleware.on_call_tool`` already refuses to make for
    a corrupt policy file. The caller (``on_call_tool``) is responsible
    for catching this and failing the call closed with a structured,
    logged error, matching that same precedent.
    """
    return _normalize_stringified_containers(value)


def _normalize_stringified_containers(value: Any) -> Any:
    """Unbounded recursive worker for ``normalize_stringified_containers``.

    Split out so the public function's ``RecursionError`` guard wraps a
    single top-level call instead of needing a try/except at every
    recursive frame.
    """
    if isinstance(value, str):
        try:
            return loads_if_json_container_str(value)
        except ValueError:
            return value
    if isinstance(value, dict):
        return {key: _normalize_stringified_containers(v) for key, v in value.items()}
    if isinstance(value, list):
        return [_normalize_stringified_containers(v) for v in value]
    return value


def has_dynamic_selector_targets(name: str, args: dict[str, Any]) -> bool:
    """Return whether identical arguments can resolve to different targets later.

    Single source of truth for the ``ha_bulk_control`` selector-mode predicate:
    both ``PolicyMiddleware`` (approval-sharing/remembering gates) and
    ``evaluate()`` below (the operations fail-safe) must agree on exactly
    which calls are "dynamic", or a future rename/extension of this check in
    one place silently stops applying to the other.
    """
    return name == "ha_bulk_control" and args.get("selector") is not None


MISSING = object()
"""What ``iter_path_values(..., report_missing=True)`` yields for a dead end."""


def iter_path_values(
    args: dict[str, Any], path: str, *, report_missing: bool = False
) -> Iterator[Any]:
    """Yield every value the dotted path resolves to.

    The leading ``args`` segment is implicit and stripped. A ``*`` segment
    fans out across the current node — across dict values for dicts,
    across items for lists — so ``args.*`` yields every top-level
    argument, ``args.config.*`` yields every leaf of the ``config``
    sub-dict, and so on. Empty iterator = no match.

    A branch that cannot continue (a missing key, a named segment on a value
    that is not a dict, or a ``*`` on a scalar) is skipped, unless
    ``report_missing``, which yields ``MISSING`` for it instead and also for a
    ``*`` over an empty container. Allow mode needs that: an operation without
    the constrained field is a value the predicate never saw.
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
            children = _children(cur)
            if report_missing and not children:
                # An empty container is a dead end too: the branch holds no
                # value the predicate could examine.
                children = None
        elif isinstance(cur, dict) and head in cur:
            children = [cur[head]]
        else:
            children = None
        if children is None:
            if report_missing:
                yield MISSING
            return
        for child in children:
            yield from walk(child, tail)

    yield from walk(args, parts)


def _children(node: Any) -> Iterable[Any] | None:
    """What a ``*`` segment fans out over; None for a scalar."""
    if isinstance(node, dict):
        return node.values()
    if isinstance(node, (list, tuple)):
        return node
    return None


def _ci(x: Any, strict: bool = False) -> Any:
    """Lower-case strings for case-insensitive comparison; pass other
    types through unchanged so type semantics (int != "1") survive.
    Used on both sides of the equality and membership ops — security gates
    should fire whether the caller wrote 'Lock' or 'LOCK' or 'lock'.

    ``strict`` keeps the case; ``match_predicate`` explains why allow mode
    compares both ways."""
    return x.lower() if isinstance(x, str) and not strict else x


def _contains_matches(val: Any, pv: Any, strict: bool) -> bool:
    if isinstance(val, str) and isinstance(pv, str):
        return _ci(pv, strict) in _ci(val, strict)
    # Outside strict mode, mirror the case-insensitive treatment that ``eq``
    # / ``in`` / ``not_in`` apply: a rule listing ``["light.kitchen"]``
    # must match an LLM passing ``"Light.Kitchen"``. Per-element
    # ``_ci`` guards non-string entries so mixed-type collections
    # (e.g. ``[1, "two"]``) keep their natural equality semantics.
    return isinstance(val, (list, tuple, set)) and any(
        _ci(pv, strict) == _ci(x, strict) for x in val
    )


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


def _op_matches(val: Any, op: str, pv: Any, strict: bool = False) -> bool:
    """Apply one op to one concrete value. Predicate dispatches over
    the candidate values (which may be many for wildcard paths).

    String comparisons are case-insensitive (security gates shouldn't
    care whether the LLM lowercased its args) unless ``strict`` (see
    ``_ci``). Non-string types preserve their natural comparison semantics.
    """
    match op:
        case "eq":
            return bool(_ci(val, strict) == _ci(pv, strict))
        case "neq":
            return bool(_ci(val, strict) != _ci(pv, strict))
        case "in":
            return _ci(val, strict) in [_ci(x, strict) for x in (pv or [])]
        case "not_in":
            return _ci(val, strict) not in [_ci(x, strict) for x in (pv or [])]
        case "regex":
            # `regex` is re.search (substring match). Anchor with ^...$
            # for full-match. re.IGNORECASE (unless strict) so '^light\.'
            # matches 'Light.x'.
            return (
                isinstance(val, str)
                and isinstance(pv, str)
                and re.search(pv, val, 0 if strict else re.IGNORECASE) is not None
            )
        case "contains":
            return _contains_matches(val, pv, strict)
        case "gt" | "lt":
            return _numeric_matches(val, op, pv)
    return False


def match_predicate(
    predicate: Predicate, args: dict[str, Any], *, strict: bool = False
) -> bool:
    """Whether ``predicate`` holds for ``args``.

    Outside allow mode a wildcard path matches when ANY value at the wildcard
    satisfies the op; for a non-wildcard path there is at most one value.

    ``strict`` is allow mode, where a match APPROVES the call, so every
    ambiguity must resolve towards "no match":

    - EVERY branch the path reaches must hold the field, ``exists``
      included, so ``args.operations.*.parameters.brightness`` does not
      approve a batch whose unlock operation carries no ``parameters`` (or
      an empty one), and ``args.*.entity_id`` does not approve a ``target``
      object that names only an area. A scalar argument lacks every field,
      so ``args.*.<field>`` approves no call that also has one. The
      remaining rules do not apply to ``exists``.
    - EVERY value must satisfy the op, and a list found at the path counts
      as its items, so an approval naming ``light.a`` covers neither an
      ``operations`` list nor an ``entity_id`` list that also names a lock.
    - A string with a comma or surrounding whitespace never matches: Home
      Assistant splits ``"light.a, lock.x"`` into two entity IDs and strips
      each, so the string would stand for values the op never saw.
    - A dict value (an object argument) can only be matched exactly, by
      ``eq`` or ``in``; ``neq "lock.x"`` says nothing about what the object
      holds, so it must not approve it.
    - The op must hold both case-sensitively and case-insensitively. A
      positive op (``eq``, ``in``) then needs the exact case, so
      ``home/bridge`` does not approve the different MQTT topic
      ``Home/Bridge``; a negated op (``neq``, ``not_in``) cannot be
      satisfied by a case variant, because Home Assistant lower-cases
      domains, services and entity IDs and would run ``LOCK`` as ``lock``.
    """
    values = list(iter_path_values(args, predicate.path, report_missing=strict))
    if not values or any(v is MISSING for v in values):
        return False
    if predicate.op == "exists":
        return True
    if not strict:
        return any(_op_matches(v, predicate.op, predicate.value) for v in values)
    values = list(_flatten_lists(values))
    if not values:
        return False
    return all(
        not _splits_into_other_values(v)
        and (not isinstance(v, dict) or predicate.op in ("eq", "in"))
        and _op_matches(v, predicate.op, predicate.value, strict=True)
        and _op_matches(v, predicate.op, predicate.value)
        for v in values
    )


def _splits_into_other_values(value: Any) -> bool:
    return isinstance(value, str) and ("," in value or value != value.strip())


def _flatten_lists(values: Iterable[Any]) -> Iterator[Any]:
    for value in values:
        if isinstance(value, (list, tuple)):
            yield from _flatten_lists(value)
        else:
            yield value


def match_rule(
    rule: Rule, tool_name: str, args: dict[str, Any], *, strict: bool = False
) -> bool:
    if rule.tool_name not in ("*", tool_name):
        return False
    return all(match_predicate(p, args, strict=strict) for p in rule.when)


def find_matching_rule(
    tool_name: str, args: dict[str, Any], policy: Policy
) -> Rule | None:
    """First rule gating this call under a require-approval list, else None."""
    if policy.rule_effect != "require_approval":
        return None
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
    literal head, so it yields no value against a list (only ``MISSING`` under
    ``report_missing``) and can never reach an operation row. Only a SECOND wildcard (``args.*.*...``, as in
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
    """Decide whether one tool call requires approval under ``policy``."""
    if policy.rule_effect == "require_approval":
        return _evaluate_require_approval_list(tool_name, args, policy)
    return _evaluate_allow_list(tool_name, args, policy)


def _evaluate_allow_list(
    tool_name: str, args: dict[str, Any], policy: Policy
) -> Verdict:
    """Allow mode: a call runs only when some rule approves it.

    Unmatched calls, a ``ws_command`` call included, already require
    approval here (a bare ``ha_call_service`` or ``*`` rule does approve
    one), so the require-approval list's first fail-safe has
    no counterpart. The selector one does: a rule that inspects
    ``args.operations`` cannot see the targets a selector call resolves to
    later, so it must not be what approves that call.
    """
    dynamic = has_dynamic_selector_targets(tool_name, args)
    for rule in policy.rules:
        if not match_rule(rule, tool_name, args, strict=True):
            continue
        if dynamic and _rule_needs_resolved_operations(rule):
            continue
        return Verdict.ALLOW
    return Verdict.REQUIRE_APPROVAL


def _evaluate_require_approval_list(
    tool_name: str, args: dict[str, Any], policy: Policy
) -> Verdict:
    """Require-approval mode (the default): gate matching calls.

    A normal rule match (``find_matching_rule``) decides most calls. Two
    fail-safes broaden approval beyond an exact predicate match, each only
    when the operator has SOME rule that could plausibly apply: an
    unmatched ``ha_call_service`` ``ws_command`` call (no ``domain``/
    ``service`` args for a domain/service-keyed rule to match), and an
    ``ha_bulk_control`` selector call whose matching rule needs fields
    (``args.operations.*``) that don't exist yet at gate time, resolved
    only later inside the tool. See the fail-safe blocks below for the
    full reasoning behind each.
    """
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
    if has_dynamic_selector_targets(tool_name, args) and any(
        rule.tool_name in ("ha_bulk_control", "*")
        and _rule_needs_resolved_operations(rule)
        for rule in policy.rules
    ):
        return Verdict.REQUIRE_APPROVAL
    return Verdict.ALLOW
