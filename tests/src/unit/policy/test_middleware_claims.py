"""Issue #2387: one approval authorizes exactly one execution.

Identical static calls share one queue row so the user sees one prompt;
these tests hold several such calls on that row and click once.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import anyio
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.middleware import PolicyMiddleware
from ha_mcp.policy.model import Policy, Rule

from ._middleware_core import QueueWatch, error_body, gate, tool_call

UNLOCK = {"domain": "lock", "service": "unlock"}


async def _race(
    mw: PolicyMiddleware,
    queue: ApprovalQueue,
    watch: QueueWatch,
    callers: int,
    call_next: AsyncMock,
) -> tuple[list[str], str]:
    """Hold ``callers`` identical calls on one row, approve it once.

    Returns each call's outcome ("ok" or its error code) and the approved token.
    """
    outcomes: list[str] = []
    approved: list[str] = []

    async def call() -> None:
        try:
            outcomes.append(
                await mw.on_call_tool(tool_call("ha_call_service", UNLOCK), call_next)
            )
        except ToolError as exc:
            outcomes.append(error_body(exc)["error"]["code"])

    async def approve_the_shared_row() -> None:
        await watch.until(callers)
        pending = queue.list_pending()
        assert len(pending) == 1, "identical static calls must share one row"
        approved.append(pending[0].token)
        queue.approve(pending[0].token)

    async with anyio.create_task_group() as tg:
        for _ in range(callers):
            tg.start_soon(call)
        tg.start_soon(approve_the_shared_row)
    return sorted(outcomes), approved[0]


async def test_concurrent_static_calls_consume_one_approval_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent identical calls share one row; one click runs one call.

    Before the claim check every waiter woken by the decision consumed the
    same entry and dispatched: one click, N executions, for a non-idempotent
    call such as ``button.press`` or ``script.turn_on``.
    """
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch)
    call_next = AsyncMock(return_value="ok")
    mw = gate(Policy(rules=[Rule(tool_name="ha_call_service")]), queue, wait_seconds=5)

    outcomes, approved = await _race(mw, queue, watch, 2, call_next)

    call_next.assert_awaited_once()
    assert outcomes == ["USER_APPROVAL_REQUIRED", "ok"]
    # The loser gets its own row to wait on rather than riding the click.
    (leftover,) = queue.list_pending()
    assert leftover.token != approved


async def test_remembered_window_lets_the_losing_waiter_proceed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A positive ``remember_minutes`` still authorizes every identical call.

    The winner's consumption arms the remember window, which is the user
    saying "don't ask me again for this call": the loser proceeds instead of
    minting a second row.
    """
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch)
    call_next = AsyncMock(return_value="ok")
    policy = Policy(rules=[Rule(tool_name="ha_call_service", remember_minutes=10)])
    mw = gate(policy, queue, wait_seconds=5)

    outcomes, _ = await _race(mw, queue, watch, 2, call_next)

    assert outcomes == ["ok", "ok"]
    assert call_next.await_count == 2
    assert queue.list_pending() == []


async def test_losing_waiters_share_one_replacement_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three concurrent identical calls: one dispatch, one replacement row.

    Two callers cannot pin this: with one loser, one leftover row holds
    whether losers share a row or each mint their own. Three discriminate,
    which keeps the loser path going through ``find_or_create``, so a burst
    of N calls never fans out to N simultaneous rows.
    """
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch)
    call_next = AsyncMock(return_value="ok")
    mw = gate(Policy(rules=[Rule(tool_name="ha_call_service")]), queue, wait_seconds=5)

    outcomes, approved = await _race(mw, queue, watch, 3, call_next)

    call_next.assert_awaited_once()
    assert outcomes == ["USER_APPROVAL_REQUIRED", "USER_APPROVAL_REQUIRED", "ok"]
    (leftover,) = queue.list_pending()
    assert leftover.token != approved
