"""In-memory, per-process approval queue with args-hash binding and remember-cache."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import anyio

logger = logging.getLogger(__name__)

Decision = Literal["pending", "approved", "denied"]


def compute_args_hash(args: dict[str, Any]) -> str:
    """Canonical sha256 of args. Same hash function used at insert and lookup."""
    payload = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class PendingApproval:
    token: str
    tool_name: str
    args_hash: str
    args: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    _decision: Decision = "pending"
    _event: anyio.Event = field(default_factory=anyio.Event)

    @property
    def decision(self) -> Decision:
        return self._decision

    def decide(self, outcome: Literal["approved", "denied"]) -> bool:
        """Transition pending -> outcome exactly once. Returns False if already decided."""
        if self._decision != "pending":
            return False
        self._decision = outcome
        self._event.set()
        return True

    async def wait(self) -> Decision:
        """Block until decided; return the final Decision."""
        await self._event.wait()
        return self._decision

    def __post_init__(self) -> None:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")


class ApprovalQueue:
    """In-memory per-process approval queue with args-hash binding.

    Pending approvals are bound to (tool_name, sha256(canonical_args)).
    A re-call with mutated args produces a different hash and a new
    pending entry, so an approval cannot be silently repurposed.

    **Single-process only.** Multi-worker deployments (e.g.
    ``uvicorn --workers N``) are unsupported — approvals created on
    worker A do NOT propagate to worker B, so a re-call routed to a
    different worker will look like a brand-new approval request.
    The standard ha-mcp deployments (stdio, addon, ha-mcp-web) all
    run single-worker.

    **Restart loses pending tokens.** The persistent ``tool_policy.json``
    rules survive a restart, but any in-flight approval tokens do not.
    Users will need to re-issue an approval click after a restart.
    """

    # Hard cap on pending entries to prevent memory exhaustion if an LLM
    # in a retry loop with mutated args creates a new entry every call.
    # Hit = oldest non-resolved entries are evicted FIFO. 1000 is well
    # above any realistic interactive use (the UI shows them all at once);
    # an attacker probing past the cap just churns the queue.
    PENDING_CAP = 1000

    def __init__(self) -> None:
        self._by_token: dict[str, PendingApproval] = {}
        self._remember: dict[tuple[str, str], datetime] = {}
        # Serialises find_or_create against concurrent on_call_tool
        # invocations with identical (tool_name, args_hash) — without it
        # two coroutines could both find() == None then both create()
        # separate pending entries, and approving one would leave the
        # other waiter blocked forever.
        self._create_lock = anyio.Lock()

    # --- remember cache ---
    def remember(self, tool_name: str, args_hash: str, *, minutes: int) -> None:
        if minutes <= 0:
            return
        self._remember[(tool_name, args_hash)] = datetime.now(UTC) + timedelta(
            minutes=minutes
        )

    def is_remembered(self, tool_name: str, args_hash: str) -> bool:
        until = self._remember.get((tool_name, args_hash))
        if until is None:
            return False
        if datetime.now(UTC) >= until:
            self._remember.pop((tool_name, args_hash), None)
            return False
        return True

    def clear_remember_cache(self) -> None:
        """Drop every remembered approval. Called when the policy is
        saved so a tightened rule takes effect immediately instead of
        being silently bypassed by an in-flight remembered approval
        until its window expires."""
        self._remember.clear()

    # --- pending entries lifecycle ---
    def create(
        self,
        tool_name: str,
        args_hash: str,
        args: dict[str, Any],
        *,
        ttl_minutes: int,
    ) -> PendingApproval:
        # Enforce PENDING_CAP — evict oldest already-resolved entries
        # first (the middleware should have removed them but the UI may
        # not have called approve/deny). If still over cap, evict oldest
        # pending too.
        if len(self._by_token) >= self.PENDING_CAP:
            self._sweep_expired()
            if len(self._by_token) >= self.PENDING_CAP:
                overflow = len(self._by_token) - self.PENDING_CAP + 1
                ordered = sorted(self._by_token.values(), key=lambda e: e.created_at)
                for stale in ordered[:overflow]:
                    self._by_token.pop(stale.token, None)
        now = datetime.now(UTC)
        entry = PendingApproval(
            token=secrets.token_urlsafe(24),
            tool_name=tool_name,
            args_hash=args_hash,
            args=args,
            created_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
        )
        self._by_token[entry.token] = entry
        return entry

    async def find_or_create(
        self,
        tool_name: str,
        args_hash: str,
        args: dict[str, Any],
        *,
        ttl_minutes: int,
    ) -> PendingApproval:
        """Atomic find-then-create: prevents two concurrent on_call_tool
        coroutines with identical (tool_name, args_hash) from creating
        two separate pending entries that would then each block their
        own waiter independently."""
        async with self._create_lock:
            existing = self.find(tool_name, args_hash)
            if existing is not None:
                return existing
            return self.create(tool_name, args_hash, args, ttl_minutes=ttl_minutes)

    def find(self, tool_name: str, args_hash: str) -> PendingApproval | None:
        self._sweep_expired()
        for entry in self._by_token.values():
            if entry.tool_name == tool_name and entry.args_hash == args_hash:
                return entry
        return None

    def get(self, token: str) -> PendingApproval | None:
        self._sweep_expired()
        return self._by_token.get(token)

    def list_pending(self) -> list[PendingApproval]:
        self._sweep_expired()
        return [e for e in self._by_token.values() if e.decision == "pending"]

    def approve(self, token: str) -> bool:
        """Mark the entry approved. Returns False if unknown or already decided."""
        entry = self._by_token.get(token)
        if entry is None:
            logger.info("approval_queue.approve: unknown token %s", token)
            return False
        ok = entry.decide("approved")
        if not ok:
            logger.info(
                "approval_queue.approve: token %s already decided as %s",
                token,
                entry.decision,
            )
        return ok

    def deny(self, token: str) -> bool:
        """Mark the entry denied. Returns False if unknown or already decided."""
        entry = self._by_token.get(token)
        if entry is None:
            logger.info("approval_queue.deny: unknown token %s", token)
            return False
        ok = entry.decide("denied")
        if not ok:
            logger.info(
                "approval_queue.deny: token %s already decided as %s",
                token,
                entry.decision,
            )
        return ok

    def remove(self, token: str) -> None:
        self._by_token.pop(token, None)

    def consume_and_maybe_remember(
        self, entry: PendingApproval, *, remember_minutes: int
    ) -> None:
        self.remove(entry.token)
        if remember_minutes > 0:
            self.remember(entry.tool_name, entry.args_hash, minutes=remember_minutes)

    def _sweep_expired(self) -> None:
        now = datetime.now(UTC)
        stale = [t for t, e in self._by_token.items() if e.expires_at <= now]
        for t in stale:
            self._by_token.pop(t, None)
