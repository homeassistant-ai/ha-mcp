"""Pydantic models for tool security policies (issue #966)."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

PredicateOp = Literal[
    "eq", "neq", "in", "not_in", "regex", "contains", "exists", "gt", "lt"
]


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

    When this tool is called and all `when` predicates match, the call
    requires user approval. Use ``tool_name="*"`` to match any tool
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

    The system is always "allow unless a rule matches; rule = require
    approval". There is no global deny/require-approval default — rules
    grant approval gates, nothing else.

    ``extra="ignore"`` so policies persisted by an older version of this
    PR (which may carry removed fields like ``default_action``) load
    cleanly; the dropped fields are silently discarded on next save.
    Predicate/Rule keep ``extra="forbid"`` since those are constructed
    from UI / user-typed JSON where typos should fail loudly.
    """

    model_config = ConfigDict(extra="ignore")

    wait_seconds: int = Field(default=60, ge=5, le=600)
    approval_ttl_minutes: int = Field(default=5, ge=1, le=60)
    rules: list[Rule] = Field(default_factory=list)
    version: int = Field(default=0, ge=0)
