"""Approval continuations wait for the existing UI decision, never authorize it."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools.base import InputRequiredToolResult
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.middleware import PolicyMiddleware
from ha_mcp.policy.model import Policy, Rule


def context(
    state: str | None = None,
    *,
    version: str = "2026-07-28",
    name: str = "ha_call_service",
    arguments: dict | None = None,
):
    args = arguments if arguments is not None else {"domain": "light"}
    return SimpleNamespace(
        message=SimpleNamespace(name=name, arguments=args),
        fastmcp_context=SimpleNamespace(
            request_context=SimpleNamespace(
                protocol_version=version,
                _srctx=SimpleNamespace(params={"name": name, "arguments": args}),
            ),
            request_state=state,
            report_progress=AsyncMock(),
        ),
    )


@pytest.fixture
def gate(monkeypatch):
    """Control time and waits while retaining real queue and policy decisions."""
    from ha_mcp.policy import approval_queue, mrtr

    clock = [2_000_000_000.0]

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0], tz or UTC)

    monkeypatch.setattr(approval_queue, "datetime", ClockDateTime)
    monkeypatch.setattr(mrtr, "time", lambda: clock[0])
    queue = ApprovalQueue()
    policy = Policy(rules=[Rule(tool_name="*")])
    middleware = PolicyMiddleware(policy_provider=lambda: policy, queue=queue)
    monkeypatch.setattr(middleware, "_wait_for_decision", AsyncMock())
    executed = []

    async def execute(ctx):
        executed.append(ctx.message.name)
        return "saved"

    return SimpleNamespace(
        queue=queue,
        policy=policy,
        middleware=middleware,
        clock=clock,
        execute=execute,
        executed=executed,
    )


@pytest.mark.anyio
async def test_denial_ends_continuation_without_dispatch(gate):
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.queue.deny(gate.queue.list_pending()[0].token)
    with pytest.raises(ToolError, match="USER_DENIED"):
        await gate.middleware.on_call_tool(
            context(first.input_required.request_state), gate.execute
        )
    assert gate.executed == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change", ["arguments", "tool", "policy", "removed", "expired"]
)
async def test_stale_or_repurposed_continuation_cannot_execute(gate, change):
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    entry = gate.queue.list_pending()[0]
    gate.queue.approve(entry.token)
    ctx = context(first.input_required.request_state)
    if change == "arguments":
        ctx = context(first.input_required.request_state, arguments={"domain": "lock"})
    elif change == "tool":
        ctx = context(first.input_required.request_state, name="ha_restart")
    elif change == "policy":
        gate.policy.rules = []
    elif change == "removed":
        gate.queue.remove(entry.token)
    else:
        gate.clock[0] += 61
    with pytest.raises(ToolError, match="USER_APPROVAL_REQUIRED"):
        await gate.middleware.on_call_tool(ctx, gate.execute)
    assert gate.executed == []


@pytest.mark.anyio
async def test_round_limit_returns_existing_manual_approval_fallback(gate):
    """A client must reach an ordinary result before its ten-round SDK limit."""
    state = None
    for _ in range(10):
        try:
            result = await gate.middleware.on_call_tool(context(state), gate.execute)
        except ToolError as exc:
            assert "USER_APPROVAL_REQUIRED" in str(exc)
            break
        assert isinstance(result, InputRequiredToolResult)
        state = result.input_required.request_state
    else:
        pytest.fail("approval wait exhausted the client's MRTR budget")
    assert len(gate.queue.list_pending()) == 1
    assert gate.executed == []
    gate.queue.approve(gate.queue.list_pending()[0].token)
    assert await gate.middleware.on_call_tool(context(), gate.execute) == "saved"


@pytest.mark.anyio
async def test_dynamic_selector_resumes_only_its_original_row(gate):
    args = {"selector": {"domain": "light"}, "action": "turn_off"}
    first = await gate.middleware.on_call_tool(
        context(name="ha_bulk_control", arguments=args), gate.execute
    )
    original = gate.queue.list_pending()[0]
    await gate.middleware.on_call_tool(
        context(name="ha_bulk_control", arguments=args), gate.execute
    )
    assert len(gate.queue.list_pending()) == 2
    gate.queue.approve(original.token)
    assert (
        await gate.middleware.on_call_tool(
            context(
                first.input_required.request_state,
                name="ha_bulk_control",
                arguments=args,
            ),
            gate.execute,
        )
        == "saved"
    )
    assert len(gate.queue.list_pending()) == 1
    assert gate.executed == ["ha_bulk_control"]


@pytest.mark.anyio
async def test_abandoned_dynamic_continuation_expires_at_original_wait_deadline(gate):
    await gate.middleware.on_call_tool(
        context(
            name="ha_bulk_control",
            arguments={"selector": {"domain": "light"}, "action": "turn_off"},
        ),
        gate.execute,
    )
    entry = gate.queue.list_pending()[0]
    gate.clock[0] += 61
    assert not gate.queue.approve(entry.token)
    assert gate.queue.list_pending() == []
    assert gate.executed == []


@pytest.mark.anyio
async def test_concurrent_resumes_cannot_reuse_remembered_approval(gate, monkeypatch):
    gate.policy.rules[0].remember_minutes = 5
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    pending = gate.queue.list_pending()[0]
    arrived = 0
    both_waiting = asyncio.Event()

    async def wait_for_both(*args):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            gate.queue.approve(pending.token)
            both_waiting.set()
        await both_waiting.wait()

    monkeypatch.setattr(gate.middleware, "_wait_for_decision", wait_for_both)
    results = await asyncio.gather(
        *(
            gate.middleware.on_call_tool(
                context(first.input_required.request_state), gate.execute
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert results.count("saved") == 1
    assert sum(isinstance(result, ToolError) for result in results) == 1
    assert gate.executed == ["ha_call_service"]


@pytest.mark.anyio
async def test_nested_script_tool_uses_blocking_fallback(gate):
    """Continuing an outer script could replay earlier effects in that script."""
    ctx = context()
    ctx.fastmcp_context.request_context._srctx.params = {
        "name": "ha_manage_custom_tool"
    }
    with pytest.raises(ToolError, match="USER_APPROVAL_REQUIRED"):
        await gate.middleware.on_call_tool(ctx, gate.execute)
    assert gate.executed == []


@pytest.mark.anyio
async def test_continuation_resumes_only_after_ui_approval(monkeypatch):
    """A pending round cannot run the tool; the same approved row runs it once."""
    queue = ApprovalQueue()
    policy = Policy(rules=[Rule(tool_name="ha_call_service")])
    middleware = PolicyMiddleware(policy_provider=lambda: policy, queue=queue)
    # Avoid real waiting; the production decision/claim path remains intact.
    monkeypatch.setattr(middleware, "_wait_for_decision", AsyncMock())
    dispatched = []

    async def execute(ctx):
        dispatched.append(ctx.message.arguments)
        return "saved"

    first = await middleware.on_call_tool(context(), execute)
    assert isinstance(first, InputRequiredToolResult)
    assert not first.input_required.input_requests
    assert dispatched == []
    entry = queue.list_pending()[0]
    second = await middleware.on_call_tool(
        context(first.input_required.request_state), execute
    )
    assert isinstance(second, InputRequiredToolResult)
    assert queue.list_pending() == [entry]
    assert dispatched == []

    queue.approve(entry.token)
    state = second.input_required.request_state
    assert await middleware.on_call_tool(context(state), execute) == "saved"
    with pytest.raises(ToolError):
        await middleware.on_call_tool(context(state), execute)
    assert dispatched == [{"domain": "light"}]


@pytest.mark.anyio
async def test_legacy_client_keeps_approval_error_and_recall(monkeypatch):
    """Legacy clients never receive an unrepresentable input_required result."""
    queue = ApprovalQueue()
    middleware = PolicyMiddleware(
        policy_provider=lambda: Policy(rules=[Rule(tool_name="ha_call_service")]),
        queue=queue,
    )
    monkeypatch.setattr(middleware, "_wait_for_decision", AsyncMock())
    execute = AsyncMock(return_value="saved")
    with pytest.raises(ToolError, match="USER_APPROVAL_REQUIRED"):
        await middleware.on_call_tool(context(version="2025-11-25"), execute)
    execute.assert_not_called()
    queue.approve(queue.list_pending()[0].token)
    assert (
        await middleware.on_call_tool(context(version="2025-11-25"), execute) == "saved"
    )
