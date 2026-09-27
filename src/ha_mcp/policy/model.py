"""Pydantic models for tool security policies."""

import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

PredicateOp = Literal[
    "eq", "neq", "in", "not_in", "regex", "contains", "exists", "gt", "lt"
]

# What a matching rule does. ``require_approval`` (the default) gates matching
# calls and lets everything else run; ``allow`` approves matching calls and
# gates everything else.
RuleEffect = Literal["require_approval", "allow"]

# Schema generation of the persisted policy file. Version 2 = ANY-match
# condition semantics (PR #1993): each UI condition is its own rule; a rule's
# predicates AND together (a condition with sub-parameters). Files WITHOUT the
# marker were written under the pre-#1993 editor, which packed every condition
# into one AND-ed rule — ``persistence.migrate_policy_any_semantics`` splits
# those once at startup and stamps the file.
POLICY_SCHEMA_VERSION = 2


class Predicate(BaseModel):
    """Single condition on a tool call's arguments (e.g. args.domain in [...])."""

    model_config = ConfigDict(extra="forbid")

    path: str
    op: PredicateOp
    value: Any | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if not v:
            raise ValueError("path must be non-empty")
        return v

    @field_validator("value")
    @classmethod
    def _validate_value(cls, v: Any, info: ValidationInfo) -> Any:
        # ``op`` runs before ``value`` because fields validate in
        # declaration order; if op failed its own validation, info.data
        # won't contain it and we skip — pydantic will already raise on
        # the op error.
        op = info.data.get("op")
        if op == "regex":
            if not isinstance(v, str):
                raise ValueError("op='regex' requires value: str")
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"Invalid regex: {e}") from e
        elif op in ("in", "not_in"):
            if not isinstance(v, (list, tuple, set)):
                raise ValueError(f"op={op!r} requires value: list")
        elif op in ("gt", "lt") and v is None:
            raise ValueError(f"op={op!r} requires a non-None comparable value")
        elif op == "exists":
            if v is not None:
                raise ValueError(
                    "op='exists' must not have a value (presence-only check)"
                )
        return v


class Rule(BaseModel):
    """One policy rule.

    When this tool is called and all `when` predicates match, the policy's
    ``rule_effect`` applies to the call: it requires user approval, or (in
    allow mode) it runs without one. Use ``tool_name="*"`` to match any tool
    (combine with predicates for cross-tool rules).
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    when: list[Predicate] = Field(default_factory=list)
    remember_minutes: int = Field(default=0, ge=0)

    @field_validator("tool_name")
    @classmethod
    def _validate_tool_name(cls, v: str) -> str:
        if not v:
            raise ValueError("tool_name must be non-empty (use '*' for wildcard)")
        return v


class Policy(BaseModel):
    """Full tool security policy, persisted to tool_policy.json.

    ``rule_effect`` decides how the rule list reads. With
    ``require_approval`` (the default) a matching rule gates the call and an
    unmatched call runs. With ``allow`` a matching rule approves the call and
    an unmatched call requires approval; ``evaluator.match_predicate`` also
    matches more strictly in that mode, so neither a case variant nor one
    listed value among unlisted ones can ride an approval.

    ``extra="ignore"`` so policy files written by older builds (which may
    carry fields since dropped from the schema) still load; dropped fields
    are silently discarded on next save. Predicate/Rule keep
    ``extra="forbid"`` since those are constructed from UI / user-typed
    JSON where typos should fail loudly.
    """

    model_config = ConfigDict(extra="ignore")

    wait_seconds: int = Field(default=60, ge=5, le=600)
    approval_ttl_minutes: int = Field(default=5, ge=1, le=60)
    rule_effect: RuleEffect = "require_approval"
    rules: list[Rule] = Field(default_factory=list)
    # Whether a PIN-carrying ha_mcp_approval_response event may decide a
    # pending approval (``policy.decisions``). Off by default, and off is
    # also what an older policy file means. The PIN itself is NOT part of
    # this document -- see ``policy.decision_pin`` for where it lives and
    # why. Enabling without a stored PIN decides nothing: every write path
    # refuses the combination and the listener re-checks it per event.
    event_decisions_enabled: bool = False
    version: int = Field(default=0, ge=0)
    # ANY-match schema marker (see POLICY_SCHEMA_VERSION). Detection of
    # unmigrated files reads the RAW json (this default would mask it).
    schema_version: int = Field(default=POLICY_SCHEMA_VERSION, ge=1)

    @model_validator(mode="after")
    def _wait_must_be_less_than_ttl(self) -> "Policy":
        # If the middleware's wait window can outlast the entry's TTL
        # the queue's sweeper evicts the entry mid-wait and the
        # reissue-pending path fires on every call — burning one
        # PENDING_CAP slot per retry. Force wait_seconds strictly less
        # than the TTL so a single approval window covers the wait.
        ttl_seconds = self.approval_ttl_minutes * 60
        if self.wait_seconds >= ttl_seconds:
            raise ValueError(
                f"wait_seconds ({self.wait_seconds}) must be less than "
                f"approval_ttl_minutes * 60 ({ttl_seconds}); otherwise the "
                "approval entry expires during the wait and every call "
                "issues a fresh pending row."
            )
        return self


ALLOW_LIST_OMITTED_MESSAGE = (
    "'rule_effect' is missing from the policy document, but the stored policy "
    "is an allow list (rule_effect='allow'). The whole document is replaced, "
    "so an omitted field would fall back to 'require_approval' and turn every "
    "approved call into a gated one and every other call into an ungated one. "
    'Send "rule_effect": "allow" to keep the allow list, or '
    '"rule_effect": "require_approval" to switch deliberately.'
)


def drops_allow_list(new: Policy, current: Policy) -> bool:
    """Whether writing ``new`` would leave allow mode only by omission.

    ``rule_effect`` defaults to ``require_approval``, so a whole-document
    write that omits it (an older settings page, a document copied from an
    example) would invert a stored allow list silently. Every write path
    refuses that instead of guessing.
    """
    return current.rule_effect == "allow" and "rule_effect" not in new.model_fields_set


def gates_differ(a: Policy, b: Policy) -> bool:
    """Whether a remembered approval may no longer hold after ``a`` -> ``b``."""
    return (a.rule_effect, a.rules) != (b.rule_effect, b.rules)


def bare_rule_gates(policy: Policy, tool: str) -> bool:
    """Whether ``tool`` is gated as far as its unconditional rule goes.

    What the per-tool "security gated" toggle shows and ``set_tool(gated=)``
    sets. A bare rule gates its tool in a require-approval list and approves
    it in an allow list, where a bare ``*`` rule approves every tool.
    Conditional rules are not considered.
    """
    bare = {rule.tool_name for rule in policy.rules if not rule.when}
    if policy.rule_effect == "allow":
        return tool not in bare and "*" not in bare
    return tool in bare


class PolicyWrite(Policy):
    """``Policy`` for incoming writes: unknown keys are refused.

    ``Policy`` ignores unknown keys so files from older builds still load,
    but a write is where a typo lands: ``"rule_efect": "allow"`` would store
    a require-approval list and turn the intended approvals into gates.
    """

    model_config = ConfigDict(extra="forbid")
