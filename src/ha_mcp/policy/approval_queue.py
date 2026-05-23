"""In-memory, per-process approval queue with args-hash binding and remember-cache."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
            datetime.now(timezone.utc) + timedelta(minutes=minutes)
        )

    def is_remembered(self, tool_name: str, args_hash: str) -> bool:
        until = self._remember.get((tool_name, args_hash))
        if until is None:
            return False
        if datetime.now(timezone.utc) >= until:
            self._remember.pop((tool_name, args_hash), None)
            return False
        return True
