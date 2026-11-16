"""Drive ``PolicyMiddleware`` as FastMCP does, and watch its approval queue.

Shared by the ``test_middleware_*`` modules. Nothing here stands in for Home
Assistant: the call context is FastMCP's own type and the queue is the real one.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import anyio
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.server.middleware import MiddlewareContext
from ha_mcp._vendor.mcp_types import CallToolRequestParams
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.policy import approval_queue, middleware
from ha_mcp.policy.approval_queue import ApprovalQueue, PendingApproval
from ha_mcp.policy.middleware import PolicyMiddleware
from ha_mcp.policy.model import Policy

from .._module_clock import set_module_clock

START = datetime(2026, 1, 1, 12, tzinfo=UTC)

LOCK_SELECTOR_ARGS = {
    "selector": {"domain": "lock", "area_ids": ["entry"]},
    "action": "lock",
}


def tool_call(
    name: str, arguments: dict[str, Any] | None = None
) -> MiddlewareContext[CallToolRequestParams]:
    """The context FastMCP hands the middleware for one ``tools/call``."""
    return MiddlewareContext(
        message=CallToolRequestParams(name=name, arguments=arguments or {}),
        method="tools/call",
    )


def gate(
    policy: Policy | Callable[[], Policy],
    queue: ApprovalQueue,
    *,
    wait_seconds: int | None = 0,
    get_client: Callable[[], HomeAssistantClient] | None = None,
    ensure_decisions_listener: Callable[[], Awaitable[None]] | None = None,
) -> PolicyMiddleware:
    """The middleware on ``policy`` (or a provider), as the server wires it."""
    return PolicyMiddleware(
        policy_provider=policy if callable(policy) else lambda: policy,
        queue=queue,
        wait_seconds=wait_seconds,
        get_client=get_client,
        ensure_decisions_listener=ensure_decisions_listener,
    )


def error_body(error: ToolError) -> dict[str, Any]:
    """The structured error a held or refused call returns to the agent."""
    body: dict[str, Any] = json.loads(error.args[0])
    return body


def error_text(body: dict[str, Any]) -> str:
    """The message and every suggestion the agent reads."""
    return body["error"]["message"] + " ".join(body["error"]["suggestions"])


def set_policy_clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    """Set the time the queue and the gate read; the event loop keeps running."""
    set_module_clock(monkeypatch, approval_queue, now)
    set_module_clock(monkeypatch, middleware, now)


class QueueWatch:
    """Every entry the gate gets from one queue method, in order.

    A test acts once the calls it started hold their rows, instead of after
    a fixed sleep that could land before the second call joined. ``after``
    runs on each entry as the gate receives it.
    """

    def __init__(
        self,
        queue: ApprovalQueue,
        monkeypatch: pytest.MonkeyPatch,
        method: Literal["find_or_create", "create"] = "find_or_create",
        after: Callable[[PendingApproval], None] | None = None,
    ) -> None:
        self.entries: list[PendingApproval] = []
        self._grew = anyio.Event()
        self._after = after
        if method == "create":
            create = queue.create

            def watched_create(*args: Any, **kwargs: Any) -> PendingApproval:
                return self._record(create(*args, **kwargs))

            monkeypatch.setattr(queue, "create", watched_create)
        else:
            find_or_create = queue.find_or_create

            async def watched(*args: Any, **kwargs: Any) -> PendingApproval:
                return self._record(await find_or_create(*args, **kwargs))

            monkeypatch.setattr(queue, "find_or_create", watched)

    def _record(self, entry: PendingApproval) -> PendingApproval:
        if self._after is not None:
            self._after(entry)
        self.entries.append(entry)
        self._grew.set()
        self._grew = anyio.Event()
        return entry

    async def until(self, count: int) -> list[PendingApproval]:
        """Wait until ``count`` entries were handed out; fail rather than hang."""
        with anyio.fail_after(5):
            while len(self.entries) < count:
                await self._grew.wait()
        return self.entries


def expire_on_arrival(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[PendingApproval], None]:
    """Move the clock to an entry's expiry: its TTL runs out while the call waits."""

    def expire(entry: PendingApproval) -> None:
        set_policy_clock(monkeypatch, entry.expires_at)

    return expire
