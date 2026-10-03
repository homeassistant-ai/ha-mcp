"""Bounded MRTR polling of the existing approval queue.

FastMCP seals requestState and binds it to the wire call and principal. This
module additionally binds the resolved leaf tool, policy and queue entry. The
state is a reference to a decision, never an approval supplied by the client.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from time import time
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ha_mcp._vendor.fastmcp.server.middleware.middleware import MiddlewareContext
from ha_mcp._vendor.fastmcp.tools.base import InputRequiredToolResult
from ha_mcp._vendor.mcp_types import InputRequiredResult
from ha_mcp._vendor.mcp_types.version import MODERN_PROTOCOL_VERSIONS

from ..errors import ErrorCode, create_error_response
from ..renamed_tools import current_tool_name
from ..tool_dispatch import CALL_PROXY_META_TOOLS
from ..tools.helpers import raise_tool_error
from .approval_queue import ApprovalQueue, PendingApproval, compute_args_hash
from .model import Policy

# Leave room below the SDK's default ten-round limit for a normal final error.
MAX_CONTINUATIONS = 8
ROUND_WAIT_SECONDS = 10


def supports_mrtr(context: MiddlewareContext) -> bool:
    """Use modern MRTR only where retrying resumes this single tool dispatch."""
    ctx = context.fastmcp_context
    request = getattr(ctx, "request_context", None)
    if getattr(request, "protocol_version", None) not in MODERN_PROTOCOL_VERSIONS:
        return False
    params = getattr(getattr(request, "_srctx", None), "params", None)
    if not isinstance(params, Mapping):
        return False
    name = current_tool_name(params.get("name", ""))
    if name in CALL_PROXY_META_TOOLS:
        args = params.get("arguments") or {}
        name = current_tool_name(args.get("name", ""))
    # Nested code-mode calls keep blocking approval: replaying their outer
    # script could repeat effects that ran before it reached the gated tool.
    return name == current_tool_name(context.message.name)


def _policy_hash(policy: Policy) -> str:
    return compute_args_hash(
        {
            "rule_effect": policy.rule_effect,
            "rules": [r.model_dump() for r in policy.rules],
        }
    )


def _cannot_resume() -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.USER_APPROVAL_REQUIRED,
            "This approval continuation is no longer valid. No tool was executed "
            "by this retry. Check the original call's outcome before issuing a "
            "fresh call; it may already have used the approval.",
            suggestions=[
                "Check the Tool Security Policies tab and the target's current state.",
                "A fresh call may require a new approval.",
            ],
        )
    )


class ApprovalContinuation(BaseModel):
    """Small, sealed reference retaining one logical call's deadline."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    kind: Literal["ha_mcp_approval"] = "ha_mcp_approval"
    token: str
    tool_name: str
    args_hash: str
    policy_hash: str
    deadline: float
    rounds: int = Field(default=0, ge=0, le=MAX_CONTINUATIONS)

    @classmethod
    def resume(
        cls,
        context: MiddlewareContext,
        queue: ApprovalQueue,
        name: str,
        args_hash: str,
        policy: Policy,
    ) -> tuple[ApprovalContinuation, PendingApproval] | None:
        """Resolve only the original live entry, including on concurrent retries."""
        ctx = context.fastmcp_context
        if ctx is None or not supports_mrtr(context):
            return None
        state = ctx.request_state
        if state is None:
            return None
        try:
            continuation = cls.model_validate_json(state)
        except ValidationError:
            _cannot_resume()
        pending = queue.get(continuation.token)
        if (
            pending is None
            or continuation.tool_name != name
            or continuation.args_hash != args_hash
            or pending.tool_name != name
            or pending.args_hash != args_hash
            or continuation.policy_hash != _policy_hash(policy)
            or continuation.deadline <= time()
        ):
            _cannot_resume()
        return continuation, pending

    @classmethod
    def start(
        cls,
        pending: PendingApproval,
        policy: Policy,
        wait_seconds: int,
        *,
        dynamic_targets: bool,
    ) -> ApprovalContinuation:
        deadline = min(time() + wait_seconds, pending.expires_at.timestamp())
        if dynamic_targets:
            # A client that never resumes must not leave an approvable dynamic
            # selector behind after its original invocation's wait window.
            pending.expires_at = datetime.fromtimestamp(deadline, UTC)
        return cls(
            token=pending.token,
            tool_name=pending.tool_name,
            args_hash=pending.args_hash,
            policy_hash=_policy_hash(policy),
            deadline=deadline,
        )

    def wait_seconds(self) -> float:
        return max(0, min(ROUND_WAIT_SECONDS, self.deadline - time()))

    def next_result(self, queue: ApprovalQueue) -> InputRequiredToolResult | None:
        """End a pending leg without granting, consuming or extending approval."""
        if (
            self.rounds >= MAX_CONTINUATIONS
            or time() >= self.deadline
            or queue.get(self.token) is None
        ):
            return None
        state = self.model_copy(update={"rounds": self.rounds + 1})
        return InputRequiredToolResult(
            InputRequiredResult(request_state=state.model_dump_json())
        )
