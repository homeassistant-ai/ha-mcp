"""FastMCP on_call_tool middleware for tool security policies."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import anyio
from anyio.to_thread import run_sync as run_in_thread
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error, safe_progress
from .approval_queue import ApprovalQueue, PendingApproval, compute_args_hash
from .evaluator import Verdict, evaluate, find_matching_rule
from .model import Policy, Rule

logger = logging.getLogger(__name__)

# Toolsearch proxy meta-tools — always pass through. Gating the proxy
# directly would be wrong: rule predicates target the REAL tool's args
# (e.g. args.domain), but the proxy receives wrapped {"name": "...",
# "arguments": {...}} envelopes. The proxy re-dispatches via
# ctx.fastmcp.call_tool(name, arguments), which re-enters the middleware
# chain with the real tool name and args, so the inner call gets gated
# correctly there.
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
        wait_seconds: int | None = None,
    ) -> None:
        self._policy_provider = policy_provider
        self._queue = queue
        self._wait_override = wait_seconds

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        try:
            # Hoist sync file read off the event loop — a slow FS shouldn't
            # pause the fastmcp request handler's task.
            policy = await run_in_thread(self._policy_provider)
        except ValueError as e:
            # Fail-closed: a corrupt or invalid tool_policy.json is a
            # security-relevant config error. Passing through would
            # silently bypass every rule the user configured. Raise a
            # structured ToolError so the LLM (and the user) sees what
            # to do, instead of crashing the call with an opaque trace.
            logger.exception("Tool security policy load failed; failing closed")
            raise_tool_error(
                create_error_response(
                    ErrorCode.POLICY_LOAD_FAILED,
                    f"Tool security policy file is corrupt or invalid: {e}. "
                    "Edit or delete tool_policy.json and reload.",
                    suggestions=[
                        "Open the Tool Security Policies tab in the web UI "
                        "to view/repair the policy.",
                    ],
                )
            )
        name = context.message.name
        args = context.message.arguments or {}

        if name in PROXY_META_TOOLS:
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
            self._raise_denied_error()

        # find_or_create serialises the create — two concurrent calls with
        # the same args_hash share one pending entry, so the user only sees
        # one approval row and approving it releases every waiter.
        pending = await self._queue.find_or_create(
            name,
            args_hash,
            args,
            ttl_minutes=policy.approval_ttl_minutes,
        )

        wait = (
            self._wait_override
            if self._wait_override is not None
            else policy.wait_seconds
        )
        await self._wait_for_decision(context, pending, wait)

        if pending.decision == "approved":
            self._queue.consume_and_maybe_remember(
                pending,
                remember_minutes=rule.remember_minutes if rule else 0,
            )
            return await call_next(context)
        if pending.decision == "denied":
            self._queue.remove(pending.token)
            self._raise_denied_error()

        # The wait may have lasted long enough for the queue's sweeper to
        # evict this entry (TTL elapsed during the block). In that case
        # the pending row no longer exists in the UI so the LLM is being
        # told to re-call with a dead token. Issue a fresh entry so the
        # next re-call wakes a real pending row.
        if self._queue.get(pending.token) is None:
            old_token = pending.token
            pending = self._queue.create(
                pending.tool_name,
                pending.args_hash,
                pending.args,
                ttl_minutes=policy.approval_ttl_minutes,
            )
            logger.info(
                "policy middleware: pending token %s evicted during wait, "
                "reissued as %s for tool=%s",
                old_token,
                pending.token,
                name,
            )
        self._raise_pending_error(pending, rule)

    async def _wait_for_decision(
        self,
        context: MiddlewareContext,
        pending: PendingApproval,
        wait_seconds: int,
    ) -> None:
        deadline = anyio.current_time() + wait_seconds
        while anyio.current_time() < deadline and pending.decision == "pending":
            ctx = getattr(context, "fastmcp_context", None)
            await safe_progress(
                ctx,
                progress=0,
                total=0,
                message=(
                    "Awaiting user approval — open the ha-mcp settings UI, "
                    "go to the Tool Security Policies tab, and approve or deny "
                    "the pending request."
                ),
            )
            remaining = deadline - anyio.current_time()
            if remaining <= 0:
                break
            with anyio.move_on_after(min(15, remaining)):
                await pending.wait()

    @staticmethod
    def _raise_denied_error() -> None:
        raise_tool_error(
            create_error_response(
                ErrorCode.USER_DENIED,
                "User explicitly denied this tool call.",
                suggestions=[
                    "Do not retry without confirming with the user first.",
                ],
            )
        )

    def _raise_pending_error(
        self, pending: PendingApproval, rule: Rule | None = None
    ) -> None:
        # Time-remaining, not total TTL: an LLM that re-calls a minute
        # before expiry should see "~60s left", not the original 300s.
        remaining = max(
            0, int((pending.expires_at - datetime.now(UTC)).total_seconds())
        )
        context: dict[str, Any] = {
            "token": pending.token,
            "expires_in_seconds": remaining,
        }
        # Surface the matched rule so users (and the LLM) can tell at a
        # glance WHY the call was gated. Critical for "I added a
        # specific condition but every call is still gated" diagnostics.
        if rule is not None:
            context["matched_rule"] = {
                "tool_name": rule.tool_name,
                "when": [p.model_dump() for p in rule.when],
            }
        raise_tool_error(
            create_error_response(
                ErrorCode.USER_APPROVAL_REQUIRED,
                "User approval required. Tell the user to open the ha-mcp "
                "settings UI, go to the Tool Security Policies tab, and "
                "approve or deny the pending request. Re-call this tool "
                "with the same arguments after the user approves.",
                suggestions=[
                    "Tell the user to open the Tool Security Policies tab in "
                    "the ha-mcp settings UI and approve the pending request.",
                    "Re-call this tool with the same arguments after the user "
                    "approves.",
                ],
                context=context,
            )
        )
