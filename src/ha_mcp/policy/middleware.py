"""FastMCP on_call_tool middleware for per-tool user-approval gating (issue #966)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import anyio
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

from ..tools.helpers import safe_progress
from .approval_queue import ApprovalQueue, PendingApproval, compute_args_hash
from .evaluator import Verdict, evaluate, find_matching_rule
from .model import Policy

logger = logging.getLogger(__name__)

# Toolsearch proxy meta-tools — always pass through; the inner real-tool
# call re-enters the middleware via ctx.fastmcp.call_tool() and gets gated there.
PROXY_META_TOOLS = frozenset(
    {
        "ha_call_read_tool",
        "ha_call_write_tool",
        "ha_call_delete_tool",
        "ha_search_tools",
    }
)


class PolicyMiddleware(Middleware):
    """Gate tool calls against a Policy, blocking with progress heartbeats."""

    def __init__(
        self,
        *,
        policy_provider: Callable[[], Policy],
        queue: ApprovalQueue,
        approval_url_builder: Callable[[str], str] | None = None,
        wait_seconds: int | None = None,
    ) -> None:
        self._policy_provider = policy_provider
        self._queue = queue
        self._approval_url_builder = approval_url_builder or (
            lambda token: f"/api/policy/approve?token={token}"
        )
        self._wait_override = wait_seconds

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        try:
            policy = self._policy_provider()
        except ValueError as e:
            # Fail-closed: a corrupt or invalid tool_policy.json is a
            # security-relevant config error. Passing through would
            # silently bypass every rule the user configured. Raise a
            # structured ToolError so the LLM (and the user) sees what
            # to do, instead of crashing the call with an opaque trace.
            logger.exception("Tool security policy load failed; failing closed")
            raise ToolError(
                json.dumps(
                    {
                        "success": False,
                        "error": {
                            "code": "POLICY_LOAD_FAILED",
                            "message": (
                                "Tool security policy file is corrupt or "
                                f"invalid: {e}. Edit or delete tool_policy.json "
                                "and reload."
                            ),
                            "suggestions": [
                                "Open the Tool Security Policies tab in the "
                                "web UI to view/repair the policy.",
                            ],
                        },
                    }
                )
            ) from e
        name = context.message.name
        args = context.message.arguments or {}

        if not policy.enabled or name in PROXY_META_TOOLS:
            return await call_next(context)

        if evaluate(name, args, policy) != Verdict.REQUIRE_APPROVAL:
            return await call_next(context)

        rule = find_matching_rule(name, args, policy)
        args_hash = compute_args_hash(args)

        if self._queue.is_remembered(name, args_hash):
            return await call_next(context)

        existing = self._queue.find(name, args_hash)
        if existing and existing.decision == "approved":
            self._queue.consume_and_maybe_remember(
                existing,
                remember_minutes=rule.remember_minutes if rule else 0,
            )
            return await call_next(context)
        if existing and existing.decision == "denied":
            self._queue.remove(existing.token)
            raise self._denied_error()

        pending = existing or self._queue.create(
            name,
            args_hash,
            args,
            ttl_minutes=policy.approval_ttl_minutes,
        )
        approval_url = self._approval_url_builder(pending.token)

        wait = (
            self._wait_override
            if self._wait_override is not None
            else policy.wait_seconds
        )
        await self._wait_for_decision(context, pending, approval_url, wait)

        if pending.decision == "approved":
            self._queue.consume_and_maybe_remember(
                pending,
                remember_minutes=rule.remember_minutes if rule else 0,
            )
            return await call_next(context)
        if pending.decision == "denied":
            self._queue.remove(pending.token)
            raise self._denied_error()

        raise self._pending_error(pending, approval_url)

    async def _wait_for_decision(
        self,
        context: MiddlewareContext,
        pending: PendingApproval,
        approval_url: str,
        wait_seconds: int,
    ) -> None:
        deadline = anyio.current_time() + wait_seconds
        while anyio.current_time() < deadline and pending.decision == "pending":
            ctx = getattr(context, "fastmcp_context", None)
            await safe_progress(
                ctx,
                progress=0,
                total=0,
                message=f"Awaiting user approval — open {approval_url}",
            )
            remaining = deadline - anyio.current_time()
            if remaining <= 0:
                break
            with anyio.move_on_after(min(15, remaining)):
                await pending.event.wait()

    @staticmethod
    def _denied_error() -> ToolError:
        return ToolError(
            json.dumps(
                {
                    "success": False,
                    "error": {
                        "code": "USER_DENIED",
                        "message": "User explicitly denied this tool call.",
                        "suggestions": [
                            "Do not retry without confirming with the user first."
                        ],
                    },
                }
            )
        )

    def _pending_error(self, pending: PendingApproval, approval_url: str) -> ToolError:
        # Time-remaining, not total TTL: an LLM that re-calls a minute
        # before expiry should see "~60s left", not the original 300s.
        remaining = max(
            0, int((pending.expires_at - datetime.now(UTC)).total_seconds())
        )
        return ToolError(
            json.dumps(
                {
                    "success": False,
                    "error": {
                        "code": "USER_APPROVAL_REQUIRED",
                        "message": (
                            f"User approval required. Open {approval_url} to review the "
                            "exact call and approve. Re-call this tool with the same "
                            "arguments after the user approves."
                        ),
                        "context": {
                            "approve_url": approval_url,
                            "expires_in_seconds": remaining,
                        },
                        "suggestions": [
                            "Tell the user to click the approval link.",
                            "Re-call this tool with the same arguments after the user approves.",
                        ],
                    },
                }
            )
        )
