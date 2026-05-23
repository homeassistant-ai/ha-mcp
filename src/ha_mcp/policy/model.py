"""Pydantic models for per-tool approval policy (issue #966)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PredicateOp = Literal[
    "eq", "neq", "in", "not_in", "regex", "contains", "exists", "gt", "lt"
]


class Predicate(BaseModel):
    """Single condition on a tool call's arguments (e.g. args.domain in [...])."""

    model_config = ConfigDict(extra="forbid")

    path: str
    op: PredicateOp
    value: Any | None = None


class Rule(BaseModel):
    """One policy rule: when this tool is called and all predicates match, require approval."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    when: list[Predicate] = Field(default_factory=list)
    remember_minutes: int = Field(default=0, ge=0)


class Policy(BaseModel):
    """Full per-tool approval policy, persisted to tool_policy.json."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    default_action: Literal["allow", "require_approval"] = "allow"
    wait_seconds: int = Field(default=60, ge=5, le=600)
    approval_ttl_minutes: int = Field(default=5, ge=1, le=60)
    rules: list[Rule] = Field(default_factory=list)
