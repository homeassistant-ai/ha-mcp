"""A held call's life: waiting, approved, denied, timed out, re-called, remembered.

Each test drives ``PolicyMiddleware.on_call_tool`` with FastMCP's own call
context and decides the real approval queue the way the settings UI does.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import anyio
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.policy.approval_queue import ApprovalQueue, compute_args_hash
from ha_mcp.policy.model import Policy, Rule

from ._middleware_core import (
    START,
    QueueWatch,
    error_body,
    error_text,
    expire_on_arrival,
    gate,
    set_policy_clock,
    tool_call,
)

LOCK = {"domain": "lock"}
UNLOCK = {"domain": "lock", "service": "unlock"}
GATED = Policy(rules=[Rule(tool_name="ha_call_service")])
RECALL = "Re-call this tool with the same arguments after the user approves"


async def test_an_approval_releases_the_blocked_call_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The click wakes the waiting call, which then runs the real tool.

    The wait also polls every 15 s; a call that only noticed the approval on
    that tick would still pass a looser check, so the bound sits below it.
    """
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch)
    mw = gate(GATED, queue, wait_seconds=30)
    call_next = AsyncMock(return_value="real_result")

    async def approve() -> None:
        (entry,) = await watch.until(1)
        queue.approve(entry.token)

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(approve)
            result = await mw.on_call_tool(
                tool_call("ha_call_service", LOCK), call_next
            )

    assert result == "real_result"


async def test_block_then_deny_raises_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch)
    mw = gate(GATED, queue, wait_seconds=5)
    call_next = AsyncMock()

    async def deny() -> None:
        (entry,) = await watch.until(1)
        queue.deny(entry.token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(deny)
        with pytest.raises(ToolError) as ei:
            await mw.on_call_tool(tool_call("ha_call_service", LOCK), call_next)

    assert error_body(ei.value)["error"]["code"] == "USER_DENIED"
    call_next.assert_not_called()


async def test_timeout_raises_pending_error_and_keeps_entry() -> None:
    """A timed-out static call keeps its row and says to re-call after approval.

    The exact sentence is asserted, not only its absence on the dynamic side
    (``test_dynamic_selector_pending_error_does_not_promise_a_working_recall``):
    that is what catches the dynamic wording leaking into this branch and
    breaking approve-then-retry for every gated static tool.
    """
    queue = ApprovalQueue()
    call_next = AsyncMock()

    with pytest.raises(ToolError) as ei:
        await gate(GATED, queue).on_call_tool(
            tool_call("ha_call_service", LOCK), call_next
        )

    body = error_body(ei.value)
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    assert "Tool Security Policies" in body["error"]["message"]
    assert RECALL in error_text(body)
    call_next.assert_not_called()
    assert queue.get(body["token"]) is not None


async def test_recall_after_approval_executes_once() -> None:
    """The agent re-calls with the same args after the click: the call runs, once.

    The approval is consumed by that run, so a third identical call is held
    again rather than riding the same click.
    """
    queue = ApprovalQueue()
    mw = gate(GATED, queue)
    call_next = AsyncMock(return_value="ok")

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(tool_call("ha_call_service", UNLOCK), call_next)
    queue.approve(error_body(ei.value)["token"])

    assert (
        await mw.on_call_tool(tool_call("ha_call_service", UNLOCK), call_next) == "ok"
    )
    call_next.assert_awaited_once()
    with pytest.raises(ToolError):
        await mw.on_call_tool(tool_call("ha_call_service", UNLOCK), call_next)
    call_next.assert_awaited_once()


async def test_recall_with_mutated_args_creates_new_pending() -> None:
    """An approval is bound to its arguments: changed arguments need their own."""
    queue = ApprovalQueue()
    mw = gate(GATED, queue)
    call_next = AsyncMock(return_value="ok")

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(tool_call("ha_call_service", LOCK), call_next)
    queue.approve(error_body(ei.value)["token"])

    with pytest.raises(ToolError):
        await mw.on_call_tool(
            tool_call("ha_call_service", {"domain": "alarm_control_panel"}), call_next
        )
    call_next.assert_not_called()


async def test_pending_error_reports_remaining_not_total_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``expires_in_seconds`` is the time left, not the configured TTL.

    An agent re-calling a minute before expiry must not be told it still has
    the full five minutes.
    """
    set_policy_clock(monkeypatch, START)
    mw = gate(Policy(approval_ttl_minutes=5, rules=GATED.rules), ApprovalQueue())

    with pytest.raises(ToolError):
        await mw.on_call_tool(tool_call("ha_call_service", LOCK), AsyncMock())
    set_policy_clock(monkeypatch, START + timedelta(minutes=4))
    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(tool_call("ha_call_service", LOCK), AsyncMock())

    assert error_body(ei.value)["expires_in_seconds"] == 60


async def test_remember_minutes_caches_for_subsequent_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch)
    policy = Policy(rules=[Rule(tool_name="ha_call_service", remember_minutes=10)])
    mw = gate(policy, queue, wait_seconds=5)
    call_next = AsyncMock(return_value="ok")

    async def approve() -> None:
        (entry,) = await watch.until(1)
        queue.approve(entry.token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(approve)
        assert (
            await mw.on_call_tool(tool_call("ha_call_service", LOCK), call_next) == "ok"
        )

    assert await mw.on_call_tool(tool_call("ha_call_service", LOCK), call_next) == "ok"
    assert len(watch.entries) == 1
    assert queue.list_pending() == []


async def test_multi_rule_first_match_wins_for_remember_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two rules for one tool: the first one's ``remember_minutes`` applies.

    Catches ``evaluate()`` and ``find_matching_rule()`` drifting apart on
    precedence (one walking the list head-first, the other tail-first).
    """
    set_policy_clock(monkeypatch, START)
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch)
    policy = Policy(
        rules=[
            Rule(tool_name="ha_call_service", remember_minutes=10),
            Rule(tool_name="ha_call_service", remember_minutes=999),
        ],
    )
    mw = gate(policy, queue, wait_seconds=5)

    async def approve() -> None:
        (entry,) = await watch.until(1)
        queue.approve(entry.token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(approve)
        await mw.on_call_tool(tool_call("ha_call_service", LOCK), AsyncMock())

    args_hash = compute_args_hash(LOCK)
    set_policy_clock(monkeypatch, START + timedelta(minutes=9))
    assert queue.is_remembered("ha_call_service", args_hash)
    set_policy_clock(monkeypatch, START + timedelta(minutes=11))
    assert not queue.is_remembered("ha_call_service", args_hash)


async def test_swept_pending_during_wait_is_reissued_with_fresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row whose TTL ran out during the wait is reissued under a live token.

    The agent is told to re-call once the user approves; a token the sweeper
    already dropped would leave the user nothing to approve.
    """
    set_policy_clock(monkeypatch, START)
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch, after=expire_on_arrival(monkeypatch))

    with pytest.raises(ToolError) as ei:
        await gate(GATED, queue).on_call_tool(
            tool_call("ha_call_service", LOCK), AsyncMock()
        )

    body = error_body(ei.value)
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    (waited_on,) = watch.entries
    assert body["token"] != waited_on.token
    assert queue.get(body["token"]) is not None
    assert queue.get(waited_on.token) is None
