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
        try:
            args = normalize_stringified_containers(context.message.arguments or {})
        except RecursionError:
            # Fail-closed for the same reason the corrupt-policy branch
            # above is: silently evaluating (and hashing) unnormalized args
            # would let a rule scoped to a nested selector-inspectable field
            # (e.g. args.selector.domain) never match, and the call would
            # ALLOW. Reachable only via genuinely deep real nesting in the
            # caller's own args (a stringified container decoding into deep
            # nesting cannot reach this -- see the docstring), so this is
            # about the gate degrading loudly rather than a realistic
            # traffic pattern.
            logger.warning(
                "policy middleware: args normalization hit RecursionError "
                "for tool=%s; failing closed",
                name,
            )
            raise_tool_error(
                create_error_response(
                    ErrorCode.POLICY_LOAD_FAILED,
                    "This call's arguments are nested too deeply to "
                    "evaluate safely against the security policy.",
                    suggestions=[
                        "Reduce the nesting depth of the tool call arguments "
                        "and retry.",
                    ],
                )
            )

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

        pending = self._finalize_timed_out_pending(
            pending, dynamic_targets=dynamic_targets, policy=policy, name=name
        )
        self._raise_pending_error(pending, rule, dynamic_targets=dynamic_targets)
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    def _finalize_timed_out_pending(
        self,
        pending: PendingApproval,
        *,
        dynamic_targets: bool,
        policy: Policy,
        name: str,
    ) -> PendingApproval:
        """Resolve a pending entry that timed out without a decision.

        Two mutually exclusive outcomes, split by ``dynamic_targets`` for
        the same reason the entry was never shared or looked up in the
        first place (see ``on_call_tool``'s creator-only binding
        comments):

        - Static: the entry may have been evicted by the queue's sweeper
          (TTL elapsed during the wait). A future re-call's own ``find()``
          lookup is how it gets consumed, so a dead token would strand
          that re-call forever -- reissue a fresh entry so the next
          re-call wakes a real pending row.
        - Dynamic: no re-call can ever look this entry up (creator-only
          binding), so reissuing would only mint a second, equally
          unreachable row. Instead the entry is removed here, before
          ``_raise_pending_error`` builds its message -- leaving it in
          the queue past this point would offer the settings UI a row
          that looks approvable but isn't (nothing is left holding this
          ``pending`` reference to act on the decision).
        """
        if dynamic_targets:
            self._queue.remove(pending.token)
            return pending
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
        return pending

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
        created it (see the creator-only binding in ``on_call_tool``), was
        already removed from the queue by the caller just before this
        raises (nothing else could ever consume it), and can never be
        picked up by a later re-call -- so its wording must not promise the
        normal "re-call after the user approves" recovery path the
        non-dynamic case uses, and must not describe a still-open approval
        window: this is the ONLY call site, reached after
        ``_wait_for_decision`` returns with no decision, so the wait window
        has already closed by the time this message is built.
        """
        context: dict[str, Any] = {"token": pending.token}
        # Surface the matched rule so users (and the LLM) can tell at a
        # glance WHY the call was gated. Critical for "I added a
        # specific condition but every call is still gated" diagnostics.
        if rule is not None:
            context["matched_rule"] = {
                "tool_name": rule.tool_name,
                "when": [p.model_dump() for p in rule.when],
            }
        if dynamic_targets:
            # No expires_in_seconds here: the caller already removed this
            # entry from the queue before raising, so there is no live
            # countdown left to report -- a number here would read as
            # "there's still time", exactly the false impression this
            # message must not give.
            message = (
                "User approval required, and this specific request has "
                "already expired: it was single-use, bound to this one "
                "call only, and this call's wait window has now closed. "
                "The token above has already been removed and no longer "
                "exists -- there is nothing left to approve for this call. "
                "Re-call this tool to create a brand-new, independent "
                "request."
            )
            suggestions = [
                "Do not tell the user to approve the token above -- it no "
                "longer exists and there is nothing left to approve.",
                "Re-call this tool with the same arguments to create a new "
                "pending request, then have the user approve THAT one "
                "while the new call is still waiting on it.",
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
                + "the ha-mcp settings UI and approve the pending request.",
                "Re-call this tool with the same arguments after the user approves.",
            ]
            # Time-remaining, not total TTL: an LLM that re-calls a minute
            # before expiry should see "~60s left", not the original 300s.
            # Meaningful only here: this entry survives past this call (a
            # later re-call's find() can still consume it), so its TTL is
            # a live, actionable number -- unlike the dynamic case above.
            remaining = max(
                0, int((pending.expires_at - datetime.now(UTC)).total_seconds())
            )
            context["expires_in_seconds"] = remaining
        raise_tool_error(
            create_error_response(
                ErrorCode.USER_APPROVAL_REQUIRED,
                message,
                suggestions=suggestions,
                context=context,
            )
        )
