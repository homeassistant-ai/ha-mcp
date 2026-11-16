"""A bulk call by selector is approved for one dispatch, never reused.

The entities a selector resolves to can change between calls, so each such
call needs its own approval: no remembered window, no shared row, no row
another call created.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import anyio
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.policy.approval_queue import ApprovalQueue, compute_args_hash
from ha_mcp.policy.model import Policy, Rule

from ._middleware_core import (
    LOCK_SELECTOR_ARGS,
    START,
    QueueWatch,
    error_body,
    error_text,
    expire_on_arrival,
    gate,
    set_policy_clock,
    tool_call,
)

BULK = Policy(rules=[Rule(tool_name="ha_bulk_control", remember_minutes=5)])


async def test_remembered_approval_never_bypasses_dynamic_bulk_selector() -> None:
    """Selector membership changes, so a remembered window must not apply."""
    queue = ApprovalQueue()
    queue.remember("ha_bulk_control", compute_args_hash(LOCK_SELECTOR_ARGS), minutes=5)
    call_next = AsyncMock()

    with pytest.raises(ToolError):
        await gate(BULK, queue).on_call_tool(
            tool_call("ha_bulk_control", LOCK_SELECTOR_ARGS), call_next
        )

    call_next.assert_not_awaited()


async def test_dynamic_bulk_selector_never_consumes_a_foreign_approval() -> None:
    """A selector call must not pick up an approval it didn't wait on.

    Otherwise any later identical call within ``approval_ttl_minutes`` could
    claim an approval made for an earlier call and dispatch against the
    topology of that later moment: the reuse across a time gap that disabling
    ``remember_minutes`` exists to prevent, through the queue instead.
    """
    queue = ApprovalQueue()
    args_hash = compute_args_hash(LOCK_SELECTOR_ARGS)
    foreign = queue.create(
        "ha_bulk_control", args_hash, LOCK_SELECTOR_ARGS, ttl_minutes=5
    )
    queue.approve(foreign.token)
    call_next = AsyncMock(return_value="ok")

    with pytest.raises(ToolError):
        await gate(BULK, queue).on_call_tool(
            tool_call("ha_bulk_control", LOCK_SELECTOR_ARGS), call_next
        )

    call_next.assert_not_awaited()
    still_there = queue.get(foreign.token)
    assert still_there is not None
    assert still_there.decision == "approved"
    # This call's own single-use row was removed when its wait ended.
    assert queue.list_pending() == []


async def test_dynamic_selector_race_never_double_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent calls, one approval: only the approved one dispatches."""
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch, method="create")
    mw = gate(Policy(rules=[Rule(tool_name="ha_bulk_control")]), queue, wait_seconds=5)
    call_next = AsyncMock(return_value="ok")
    outcomes: list[str] = []

    async def call() -> None:
        try:
            outcomes.append(
                await mw.on_call_tool(
                    tool_call("ha_bulk_control", LOCK_SELECTOR_ARGS), call_next
                )
            )
        except ToolError as exc:
            outcomes.append(error_body(exc)["error"]["code"])

    async def approve_one_deny_other() -> None:
        first, second = await watch.until(2)
        queue.approve(first.token)
        queue.deny(second.token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
        tg.start_soon(approve_one_deny_other)

    assert sorted(outcomes) == ["USER_DENIED", "ok"]
    call_next.assert_awaited_once()


async def test_concurrent_dynamic_selector_calls_get_independent_approvals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent identical selector calls must not share one pending row.

    Sharing would let one click release both waiters and dispatch the
    selector twice, or against two different resolutions.
    """
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch, method="create")
    mw = gate(Policy(rules=[Rule(tool_name="ha_bulk_control")]), queue)
    call_next = AsyncMock()

    async def call() -> None:
        with pytest.raises(ToolError):
            await mw.on_call_tool(
                tool_call("ha_bulk_control", LOCK_SELECTOR_ARGS), call_next
            )

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)

    first, second = watch.entries
    assert first.token != second.token
    assert queue.list_pending() == []
    call_next.assert_not_awaited()


async def test_dynamic_selector_pending_error_does_not_promise_a_working_recall() -> (
    None
):
    """A selector call's timeout must not tell the agent to re-call after approval.

    A re-call can never consume that row, so following the advice loops:
    the user approves the row they see, the re-call mints a new one. The row
    itself must be gone too, or the settings UI offers a row whose approval
    does nothing.
    """
    queue = ApprovalQueue()

    with pytest.raises(ToolError) as ei:
        await gate(
            Policy(rules=[Rule(tool_name="ha_bulk_control")]), queue
        ).on_call_tool(tool_call("ha_bulk_control", LOCK_SELECTOR_ARGS), AsyncMock())

    body = error_body(ei.value)
    text = error_text(body)
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    assert "Re-call this tool with the same arguments after the user approves" not in (
        text
    )
    assert "before this call's wait window closes" not in text
    assert "single-use" in text
    assert queue.get(body["token"]) is None
    assert queue.list_pending() == []
    # No countdown for a row that no longer exists.
    assert "expires_in_seconds" not in body


async def test_dynamic_selector_evicted_during_wait_is_not_reissued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selector row whose TTL ran out during the wait is not reissued.

    No later call can look it up, so a reissued row would be as unreachable
    as the one it replaced, burning a queue slot.
    """
    set_policy_clock(monkeypatch, START)
    queue = ApprovalQueue()
    watch = QueueWatch(
        queue, monkeypatch, method="create", after=expire_on_arrival(monkeypatch)
    )
    call_next = AsyncMock()

    with pytest.raises(ToolError) as ei:
        await gate(
            Policy(rules=[Rule(tool_name="ha_bulk_control")]), queue
        ).on_call_tool(tool_call("ha_bulk_control", LOCK_SELECTOR_ARGS), call_next)

    body = error_body(ei.value)
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    (created,) = watch.entries
    assert body["token"] == created.token
    assert queue.get(created.token) is None
    call_next.assert_not_awaited()
