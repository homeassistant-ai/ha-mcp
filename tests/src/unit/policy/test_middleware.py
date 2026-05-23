"""Test PolicyMiddleware by driving it directly with a fake call_next.

Avoids spinning up a full FastMCP server. The middleware sees
context.message.name + context.message.arguments and routes accordingly.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.policy.approval_queue import ApprovalQueue, compute_args_hash
from ha_mcp.policy.middleware import PROXY_META_TOOLS, PolicyMiddleware
from ha_mcp.policy.model import Policy, Rule


def make_context(name: str, arguments: dict | None = None):
    msg = MagicMock()
    msg.name = name
    msg.arguments = arguments or {}
    ctx = MagicMock()
    ctx.message = msg
    ctx.fastmcp_context = MagicMock()
    ctx.fastmcp_context.report_progress = AsyncMock()
    return ctx


@pytest.fixture
def queue():
    return ApprovalQueue()


@pytest.mark.anyio
async def test_disabled_policy_passes_through(queue):
    mw = PolicyMiddleware(policy_provider=lambda: Policy(enabled=False), queue=queue)
    call_next = AsyncMock(return_value="real_result")
    result = await mw.on_call_tool(make_context("ha_call_service", {"domain": "lock"}), call_next)
    assert result == "real_result"


@pytest.mark.anyio
async def test_proxy_meta_tools_pass_through(queue):
    pol = Policy(enabled=True, rules=[Rule(tool_name="*")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue)
    call_next = AsyncMock(return_value="proxy_result")
    for name in PROXY_META_TOOLS:
        result = await mw.on_call_tool(make_context(name, {}), call_next)
        assert result == "proxy_result"


@pytest.mark.anyio
async def test_no_matching_rule_passes_through(queue):
    pol = Policy(enabled=True, rules=[Rule(tool_name="ha_other")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue)
    call_next = AsyncMock(return_value="ok")
    result = await mw.on_call_tool(make_context("ha_call_service"), call_next)
    assert result == "ok"


@pytest.mark.anyio
async def test_remembered_approval_passes_through(queue):
    pol = Policy(enabled=True, rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    args = {"domain": "lock"}
    queue.remember("ha_call_service", compute_args_hash(args), minutes=5)
    call_next = AsyncMock(return_value="ok")
    result = await mw.on_call_tool(make_context("ha_call_service", args), call_next)
    assert result == "ok"


@pytest.mark.anyio
async def test_pre_approved_entry_consumed_and_call_proceeds(queue):
    pol = Policy(enabled=True, rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    args = {"domain": "lock"}
    entry = queue.create("ha_call_service", compute_args_hash(args), args, ttl_minutes=5)
    queue.approve(entry.token)
    call_next = AsyncMock(return_value="ok")
    result = await mw.on_call_tool(make_context("ha_call_service", args), call_next)
    assert result == "ok"
    assert queue.find("ha_call_service", compute_args_hash(args)) is None


# --- appended for Task 3.2: block / deny / timeout / re-call coverage ---


@pytest.mark.anyio
async def test_block_then_approve_returns_real_result(queue):
    pol = Policy(enabled=True, wait_seconds=5,
                 rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue)
    call_next = AsyncMock(return_value="real_result")

    async def approver_after_short_delay():
        await anyio.sleep(0.05)
        pending = queue.list_pending()[0]
        queue.approve(pending.token)

    result: object = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(approver_after_short_delay)
        result = await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), call_next)
    assert result == "real_result"


@pytest.mark.anyio
async def test_block_then_deny_raises_denied(queue):
    pol = Policy(enabled=True, wait_seconds=5,
                 rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue)
    call_next = AsyncMock()

    async def denier():
        await anyio.sleep(0.05)
        pending = queue.list_pending()[0]
        queue.deny(pending.token)

    with pytest.raises(ToolError) as ei:
        async with anyio.create_task_group() as tg:
            tg.start_soon(denier)
            await mw.on_call_tool(
                make_context("ha_call_service", {"domain": "lock"}), call_next)
    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "USER_DENIED"
    call_next.assert_not_called()


@pytest.mark.anyio
async def test_timeout_raises_pending_error_and_keeps_entry(queue):
    pol = Policy(enabled=True, rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    call_next = AsyncMock()

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), call_next)
    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    assert "approve_url" in body["error"]["context"]
    call_next.assert_not_called()
    # entry survives for re-call
    assert queue.list_pending()


@pytest.mark.anyio
async def test_recall_after_approval_executes(queue):
    """The crucial property: LLM re-calls same tool+args → middleware consumes
    the now-approved entry and proceeds. Strict args-hash binding ensures
    a re-call with mutated args would NOT pick up this approval."""
    pol = Policy(enabled=True, rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    call_next = AsyncMock(return_value="ok")
    args = {"domain": "lock", "service": "unlock"}

    # 1st call: times out, leaves pending entry
    with pytest.raises(ToolError):
        await mw.on_call_tool(make_context("ha_call_service", args), call_next)
    pending = queue.list_pending()[0]

    # user approves out-of-band
    queue.approve(pending.token)

    # 2nd call (same args): proceeds
    result = await mw.on_call_tool(make_context("ha_call_service", args), call_next)
    assert result == "ok"
    call_next.assert_awaited_once()


@pytest.mark.anyio
async def test_recall_with_mutated_args_creates_new_pending(queue):
    pol = Policy(enabled=True, rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    call_next = AsyncMock(return_value="ok")

    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), call_next)
    first_pending = queue.list_pending()[0]
    queue.approve(first_pending.token)

    # mutated args → different hash → new pending, NOT approved
    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "alarm_control_panel"}),
            call_next)
    call_next.assert_not_called()


@pytest.mark.anyio
async def test_remember_minutes_caches_for_subsequent_calls(queue):
    pol = Policy(enabled=True, rules=[
        Rule(tool_name="ha_call_service", remember_minutes=10),
    ])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=5)
    call_next = AsyncMock(return_value="ok")
    args = {"domain": "lock"}

    async def approver():
        await anyio.sleep(0.05)
        queue.approve(queue.list_pending()[0].token)

    result1: object = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(approver)
        result1 = await mw.on_call_tool(make_context("ha_call_service", args), call_next)
    assert result1 == "ok"

    # second call with same args proceeds via remember-cache without any pending entry
    result2 = await mw.on_call_tool(make_context("ha_call_service", args), call_next)
    assert result2 == "ok"
    assert queue.list_pending() == []
