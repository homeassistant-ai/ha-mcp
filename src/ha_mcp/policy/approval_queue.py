"""In-memory, per-process approval queue with args-hash binding and remember-cache."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import anyio

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
    args_preview: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    decision: Decision = "pending"
    event: anyio.Event = field(default_factory=anyio.Event)


class ApprovalQueue:
    """In-memory store. Per-process. Token-keyed for HTTP lookup,
    (tool, hash)-indexed for re-call lookup."""

    def __init__(self) -> None:
        self._by_token: dict[str, PendingApproval] = {}
        self._remember: dict[tuple[str, str], datetime] = {}

    # --- remember cache ---
    def remember(self, tool_name: str, args_hash: str, *, minutes: int) -> None:
        if minutes <= 0:
            return
        self._remember[(tool_name, args_hash)] = (
            datetime.now(UTC) + timedelta(minutes=minutes)
        )

    def is_remembered(self, tool_name: str, args_hash: str) -> bool:
        until = self._remember.get((tool_name, args_hash))
        if until is None:
            return False
        if datetime.now(UTC) >= until:
            self._remember.pop((tool_name, args_hash), None)
            return False
        return True

    # --- pending entries lifecycle ---
    def create(
        self,
        tool_name: str,
        args_hash: str,
        args_preview: dict[str, Any],
        *,
        ttl_minutes: int,
    ) -> PendingApproval:
        now = datetime.now(UTC)
        entry = PendingApproval(
            token=secrets.token_urlsafe(24),
            tool_name=tool_name,
            args_hash=args_hash,
            args_preview=args_preview,
            created_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
        )
        self._by_token[entry.token] = entry
        return entry

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

    def approve(self, token: str) -> None:
        entry = self._by_token.get(token)
        if entry and entry.decision == "pending":
            entry.decision = "approved"
            entry.event.set()

    def deny(self, token: str) -> None:
        entry = self._by_token.get(token)
        if entry and entry.decision == "pending":
            entry.decision = "denied"
            entry.event.set()

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
