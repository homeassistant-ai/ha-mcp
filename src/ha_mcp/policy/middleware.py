"""FastMCP on_call_tool middleware for tool security policies."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NoReturn

import anyio
from anyio.to_thread import run_sync as run_in_thread
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

from ..errors import ErrorCode, create_error_response
from ..renamed_tools import current_tool_name
from ..tools.helpers import raise_tool_error, safe_progress
from .approval_queue import ApprovalQueue, PendingApproval, compute_args_hash
from .evaluator import (
    Verdict,
    evaluate,
    find_matching_rule,
    has_dynamic_selector_targets,
    normalize_stringified_containers,
)
from .model import Policy, Rule

logger = logging.getLogger(__name__)

# Dispatching call proxies are a strict subset of the ungated tool-search
# meta-tools. Only these three execute their envelope's ``name``; search merely
# returns catalog metadata and must not be unwrapped by other middleware.
# Gating a call proxy directly would be wrong: rule predicates target the real
# tool's args (for example args.domain), while the proxy receives a wrapped
# {"name": "...", "arguments": {...}} envelope. Its dispatch re-enters the
# middleware chain with the real name and args, so the inner call is gated there.
CALL_PROXY_META_TOOLS = frozenset(
    {
        "ha_call_read_tool",
        "ha_call_write_tool",
        "ha_call_delete_tool",
    }
)
# Policy itself also leaves catalog search ungated; unlike the call proxies it
# never dispatches a tool named in client-supplied arguments.
PROXY_META_TOOLS = CALL_PROXY_META_TOOLS | {"ha_search_tools"}

# ha_dev_manage_server actions that MANAGE the approval queue itself.
# Gating these deadlocks by construction: with a wildcard (or
# ha_dev_manage_server) rule in place, an MCP-only "approve" call would
# itself require approval — creating a second pending entry instead of
# deciding the first, so nothing can ever be approved through the tool.
# Only the queue-management actions are exempt; update_source / restart
# remain gateable like any other high-stakes action. The exemption is not
# a free self-approval: approve/deny separately require the
# dev_tools_security_policy_access setting (off by default, issue #2141).
_APPROVAL_MANAGEMENT_TOOL = "ha_dev_manage_server"
_APPROVAL_MANAGEMENT_ACTIONS = frozenset({"list_pending", "approve", "deny"})


def _is_approval_management(name: str, args: dict[str, Any]) -> bool:
    """True for dev-tool calls that manage the approval queue itself."""
    return (
        name == _APPROVAL_MANAGEMENT_TOOL
        and args.get("action") in _APPROVAL_MANAGEMENT_ACTIONS
    )


def _passes_ungated(name: str, args: dict[str, Any]) -> bool:
    """Calls that must bypass gating: proxy meta-tools + queue management."""
    return name in PROXY_META_TOOLS or _is_approval_management(name, args)


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
        """Gate one tool call against the policy, blocking on approval if required.

        Loads the policy, evaluates the call, and -- when approval is
        required -- finds or creates a pending queue entry and waits up to
        the configured window for a decision. A dynamic selector call
        (``has_dynamic_selector_targets``) never shares or reuses another
        call's pending entry, and is never reissued on TTL eviction: see
        the inline comments above the ``existing =``, ``pending =``, and
        eviction-reissue assignments below for why binding it to this one
        invocation is a correctness requirement, not just extra caution.
        """
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
        # RenamedToolAliasMiddleware runs ahead of this one and normally
        # rewrites a retired name before it arrives. Resolving it again costs a
        # dict lookup and removes the ordering dependency, which here is the
        # difference between gated and ungated: rules are keyed on the current
        # name, and ``evaluate`` returns ALLOW when nothing matches, so a gate
        # reading a stale name lets the call through.
        name = current_tool_name(context.message.name)
        # Normalize stringified JSON containers (a client like Claude Desktop
        # stdio can send a nested parameter, e.g. `selector`, as a JSON
        # string rather than an object) before any evaluation/hashing below
        # — the tool's own Pydantic coercion happens only later, inside
        # call_next, which is too late for a predicate path or the selector
        # fail-safe to see the real structure. This only affects the local
        # copy used for gating; `context` itself is untouched, so the actual
        # tool call still receives the client's original wire shape.
        args = normalize_stringified_containers(context.message.arguments or {})

        if _passes_ungated(name, args):
            return await call_next(context)

        if evaluate(name, args, policy) != Verdict.REQUIRE_APPROVAL:
            return await call_next(context)

        rule = find_matching_rule(name, args, policy)
        args_hash = compute_args_hash(args)
        dynamic_targets = has_dynamic_selector_targets(name, args)
        remember_minutes = (
            0 if dynamic_targets else rule.remember_minutes if rule else 0
        )

        if not dynamic_targets and self._queue.is_remembered(name, args_hash):
            return await call_next(context)

        # A dynamic selector call must never consume an entry it did not
        # itself create and is not itself still waiting on. Two reachable
        # ways an unguarded lookup here breaks the "approve once, dispatch
        # once, against fresh topology" invariant: (1) a race -- decide()
        # flips a waiter's own PendingApproval.decision before the waiter's
        # task is rescheduled, so a second, concurrent call's find() can
        # observe "approved" and consume-and-dispatch before the original
        # waiter wakes and does the same from its own reference, executing
        # the click twice; (2) a later, non-racy call -- the original call
        # times out and is told to re-call, the user approves afterwards,
        # and ANY later identical call within approval_ttl_minutes claims
        # that stale approval and dispatches against topology re-resolved
        # at that later moment -- exactly the reuse-across-a-time-gap this
        # PR disables `remember_minutes` to prevent, just via a different
        # mechanism. Binding a dynamic entry to its creating invocation
        # closes both: only the call that created a pending entry ever
        # observes it (via its own `pending` reference after its own wait,
        # below), so every other call -- concurrent or a later retry --
        # unconditionally mints its own independent entry and wait window.
        # The accepted cost is that a retry never silently rides an earlier
        # approval: each blocked call gets its own approval row, and only
        # approving the row for the CURRENTLY-blocked call has any effect.
        existing = None if dynamic_targets else self._queue.find(name, args_hash)
        if existing and existing.decision == "approved":
            self._queue.consume_and_maybe_remember(
                existing,
                remember_minutes=remember_minutes,
            )
            return await call_next(context)
        if existing and existing.decision == "denied":
            self._queue.remove(existing.token)
            self._raise_denied_error()

        # find_or_create serialises the create — two concurrent calls with
        # the same args_hash share one pending entry, so the user only sees
        # one approval row and approving it releases every waiter. Dynamic
        # selector calls must NOT share a pending entry: two concurrent
        # identical calls can still resolve to different (or overlapping)
        # target sets by the time each one dispatches, so folding them onto
        # one approval would let a single click authorize more executions
        # than the user saw. Skip the sharing and always mint a fresh entry.
        pending = (
            self._queue.create(
                name, args_hash, args, ttl_minutes=policy.approval_ttl_minutes
            )
            if dynamic_targets
            else await self._queue.find_or_create(
                name,
                args_hash,
                args,
                ttl_minutes=policy.approval_ttl_minutes,
            )
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
                remember_minutes=remember_minutes,
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
        #
        # NOT for dynamic calls: a dynamic entry is only ever consumable by
        # the invocation that created it (see the creator-only binding
        # above), so no future re-call can ever "wake" a reissued row
        # either -- reissuing here would just mint a second, equally
        # unreachable row and burn a queue slot for nothing.
        if not dynamic_targets and self._queue.get(pending.token) is None:
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
        self._raise_pending_error(pending, rule, dynamic_targets=dynamic_targets)
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

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
    def _raise_denied_error() -> NoReturn:
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
        self,
        pending: PendingApproval,
        rule: Rule | None = None,
        *,
        dynamic_targets: bool = False,
    ) -> NoReturn:
        """Raise the structured USER_APPROVAL_REQUIRED error for one pending entry.

        ``dynamic_targets`` switches both the message and suggestions: a
        dynamic selector entry is bound to the single invocation that
        created it (see the creator-only binding in ``on_call_tool``) and
        can never be picked up by a later re-call, so its wording must not
        promise the normal "re-call after the user approves" recovery path
        the non-dynamic case uses.
        """
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
        if dynamic_targets:
            # "Re-call after the user approves" is false for a dynamic
            # entry: it is bound to THIS invocation only (see the
            # creator-only binding above the call site), so a re-call can
            # never consume this pending row -- it always mints its own,
            # independent one. Telling the agent to re-call anyway produces
            # an approve-and-be-asked-again loop: the user approves the row
            # they can see, the re-call ignores it and creates another. The
            # only window in which THIS approval can succeed is while this
            # call is still actively blocked (up to expires_in_seconds from
            # now); once it elapses, this specific request is dead and a
            # fresh call is a genuinely new request, not a retry of this one.
            message = (
                "User approval required. This approval request is single-use "
                "and bound to this specific call only (see token above) -- it "
                "cannot be approved after the fact by re-calling. Tell the "
                "user to approve or deny it in the ha-mcp settings UI, Tool "
                "Security Policies tab, right now, before this call's wait "
                "window closes. If it closes unapproved, re-calling starts a "
                "brand-new independent request rather than resuming this one."
            )
            suggestions = [
                "Tell the user to approve or deny THIS pending request (the "
                "token above) in the Tool Security Policies tab immediately "
                "-- it will not still work after this call has already timed "
                "out.",
                "Do not re-call expecting this approval to be picked up; a "
                "re-call creates its own separate request with its own "
                "approval window instead.",
            ]
        else:
            message = (
                "User approval required. Tell the user to open the ha-mcp "
                "settings UI, go to the Tool Security Policies tab, and "
                "approve or deny the pending request. Re-call this tool "
                "with the same arguments after the user approves."
            )
            suggestions = [
                "Tell the user to open the Tool Security Policies tab in "
                "the ha-mcp settings UI and approve the pending request.",
                "Re-call this tool with the same arguments after the user approves.",
            ]
        raise_tool_error(
            create_error_response(
                ErrorCode.USER_APPROVAL_REQUIRED,
                message,
                suggestions=suggestions,
                context=context,
            )
        )
