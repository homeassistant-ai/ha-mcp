"""Unit tests for process-wide Home Assistant tool-call concurrency control."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import anyio
import pytest

import ha_mcp.ha_request_queue as queue_module
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.server.middleware import MiddlewareContext
from ha_mcp._vendor.mcp_types import CallToolRequestParams
from ha_mcp.client.rest_client import HomeAssistantClient

HomeAssistantRequestQueueMiddleware = queue_module.HomeAssistantRequestQueueMiddleware


def make_context(name: str) -> MiddlewareContext[CallToolRequestParams]:
    return MiddlewareContext(message=CallToolRequestParams(name=name, arguments={}))


@pytest.mark.asyncio
async def test_concurrent_outer_calls_are_serialized() -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)
    first_entered = anyio.Event()
    release_first = anyio.Event()
    second_attempted = anyio.Event()
    second_entered = anyio.Event()

    async def first_call_next(context: MiddlewareContext[CallToolRequestParams]) -> str:
        first_entered.set()
        await release_first.wait()
        return context.message.name

    async def second_call_next(
        context: MiddlewareContext[CallToolRequestParams],
    ) -> str:
        second_entered.set()
        return context.message.name

    async def run_first() -> None:
        result = await middleware.on_call_tool(
            make_context("ha_get_history"), first_call_next
        )
        assert result == "ha_get_history"

    async def run_second() -> None:
        second_attempted.set()
        result = await middleware.on_call_tool(
            make_context("ha_get_overview"), second_call_next
        )
        assert result == "ha_get_overview"

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_first)
        await first_entered.wait()
        task_group.start_soon(run_second)
        await second_attempted.wait()
        await anyio.lowlevel.checkpoint()
        assert not second_entered.is_set()
        release_first.set()

    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_nested_redispatch_does_not_reacquire_the_queue() -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)

    async def inner_call_next(context: MiddlewareContext[CallToolRequestParams]) -> str:
        return context.message.name

    async def outer_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> str:
        return await middleware.on_call_tool(
            make_context("ha_get_state"), inner_call_next
        )

    with anyio.fail_after(1):
        result = await middleware.on_call_tool(
            make_context("ha_get_overview"), outer_call_next
        )

    assert result == "ha_get_state"


@pytest.mark.asyncio
@pytest.mark.parametrize("outer_name", ["ha_call_read_tool", "ha_manage_custom_tool"])
async def test_dispatch_envelope_defers_queue_slot_until_inner_dispatch(
    outer_name: str,
) -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)
    blocker_entered = anyio.Event()
    release_blocker = anyio.Event()
    proxy_entered = anyio.Event()
    inner_attempted = anyio.Event()
    inner_entered = anyio.Event()

    async def blocker_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> None:
        blocker_entered.set()
        await release_blocker.wait()

    async def inner_call_next(context: MiddlewareContext[CallToolRequestParams]) -> str:
        inner_entered.set()
        return context.message.name

    async def proxy_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> str:
        proxy_entered.set()
        inner_attempted.set()
        return await middleware.on_call_tool(
            make_context("ha_get_state"), inner_call_next
        )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            middleware.on_call_tool,
            make_context("ha_get_overview"),
            blocker_call_next,
        )
        await blocker_entered.wait()
        task_group.start_soon(
            middleware.on_call_tool,
            make_context(outer_name),
            proxy_call_next,
        )
        await proxy_entered.wait()
        await inner_attempted.wait()
        await anyio.lowlevel.checkpoint()
        assert not inner_entered.is_set()
        release_blocker.set()

    assert inner_entered.is_set()


@pytest.mark.asyncio
async def test_slot_and_depth_are_released_when_call_raises() -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)

    async def failing_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> None:
        raise RuntimeError("simulated tool failure")

    async def succeeding_call_next(
        context: MiddlewareContext[CallToolRequestParams],
    ) -> str:
        return context.message.name

    with pytest.raises(RuntimeError, match="simulated tool failure"):
        await middleware.on_call_tool(make_context("ha_get_history"), failing_call_next)

    with anyio.fail_after(1):
        result = await middleware.on_call_tool(
            make_context("ha_get_overview"), succeeding_call_next
        )
    assert result == "ha_get_overview"


@pytest.mark.asyncio
async def test_nested_depth_is_restored_when_inner_call_raises() -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)

    async def failing_inner(_context: MiddlewareContext[CallToolRequestParams]) -> None:
        raise RuntimeError("simulated nested failure")

    async def outer_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> str:
        with pytest.raises(RuntimeError, match="simulated nested failure"):
            await middleware.on_call_tool(make_context("ha_get_state"), failing_inner)
        return await middleware.on_call_tool(
            make_context("ha_get_state"), succeeding_inner
        )

    async def succeeding_inner(
        context: MiddlewareContext[CallToolRequestParams],
    ) -> str:
        return context.message.name

    start_blocker = anyio.Event()
    blocker_entered = anyio.Event()
    release_blocker = anyio.Event()
    recovered_attempted = anyio.Event()
    recovered_entered = anyio.Event()

    async def hold_slot(_context: MiddlewareContext[CallToolRequestParams]) -> None:
        blocker_entered.set()
        await release_blocker.wait()

    async def block_from_clean_context() -> None:
        await start_blocker.wait()
        await middleware.on_call_tool(make_context("ha_get_history"), hold_slot)

    async def release_after_contention() -> None:
        await recovered_attempted.wait()
        await anyio.lowlevel.checkpoint()
        try:
            assert not recovered_entered.is_set()
        finally:
            release_blocker.set()

    async def recovered_call_next(
        context: MiddlewareContext[CallToolRequestParams],
    ) -> str:
        recovered_entered.set()
        return context.message.name

    with anyio.fail_after(1):
        async with anyio.create_task_group() as tasks:
            # Start before the failure so this task inherits a clean context,
            # even if the caller's depth-reset logic regresses.
            tasks.start_soon(block_from_clean_context)
            tasks.start_soon(release_after_contention)
            result = await middleware.on_call_tool(
                make_context("ha_get_overview"), outer_call_next
            )
            assert result == "ha_get_state"
            start_blocker.set()
            await blocker_entered.wait()
            recovered_attempted.set()
            assert (
                await middleware.on_call_tool(
                    make_context("ha_get_overview"), recovered_call_next
                )
                == "ha_get_overview"
            )
    assert recovered_entered.is_set()


@pytest.mark.asyncio
async def test_queue_wait_reports_progress_warns_and_times_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)
    blocker_entered = anyio.Event()
    release_blocker = anyio.Event()
    progress = AsyncMock()
    monkeypatch.setattr(queue_module, "safe_progress", progress)
    monkeypatch.setattr(queue_module, "QUEUE_WAIT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(queue_module, "QUEUE_WAIT_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(queue_module, "QUEUE_WAIT_WARNING_SECONDS", 0.01)

    async def blocker_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> None:
        blocker_entered.set()
        await release_blocker.wait()

    async def queued_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> None:
        pytest.fail("timed-out queued call must not execute")

    caplog.set_level(logging.DEBUG, logger=queue_module.__name__)
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            middleware.on_call_tool,
            make_context("ha_get_history"),
            blocker_call_next,
        )
        await blocker_entered.wait()
        with pytest.raises(ToolError, match="queue admission"):
            await middleware.on_call_tool(
                make_context("ha_get_overview"), queued_call_next
            )
        release_blocker.set()

    progress.assert_awaited()
    assert "still waiting for slot tool=ha_get_overview" in caplog.text
    assert "timed out waiting for slot tool=ha_get_overview" in caplog.text


@pytest.mark.asyncio
async def test_internal_fanout_is_not_throttled_by_outer_queue() -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)
    both_entered = anyio.Event()
    release_workers = anyio.Event()
    entered = 0

    async def worker() -> None:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await release_workers.wait()

    async def call_next(_context: MiddlewareContext[CallToolRequestParams]) -> None:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(worker)
            task_group.start_soon(worker)
            with anyio.fail_after(1):
                await both_entered.wait()
            release_workers.set()

    await middleware.on_call_tool(make_context("ha_search"), call_next)
    assert entered == 2


@pytest.mark.asyncio
async def test_local_tool_search_bypasses_outer_queue() -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)
    first_entered = anyio.Event()
    release_first = anyio.Event()
    local_entered = anyio.Event()

    async def first_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> None:
        first_entered.set()
        await release_first.wait()

    async def local_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> str:
        local_entered.set()
        return "found"

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            middleware.on_call_tool,
            make_context("ha_get_history"),
            first_call_next,
        )
        await first_entered.wait()
        result = await middleware.on_call_tool(
            make_context("ha_search_tools"), local_call_next
        )
        assert result == "found"
        assert local_entered.is_set()
        release_first.set()


@pytest.mark.asyncio
async def test_approval_management_bypasses_outer_queue() -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)
    first_entered = anyio.Event()
    release_first = anyio.Event()
    approval_entered = anyio.Event()

    async def first_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> None:
        first_entered.set()
        await release_first.wait()

    async def approval_call_next(
        _context: MiddlewareContext[CallToolRequestParams],
    ) -> str:
        approval_entered.set()
        return "approved"

    approval_context = make_context("ha_dev_manage_server")
    approval_context.message.arguments = {"action": "approve"}

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            middleware.on_call_tool,
            make_context("ha_get_history"),
            first_call_next,
        )
        await first_entered.wait()
        result = await middleware.on_call_tool(approval_context, approval_call_next)
        assert result == "approved"
        assert approval_entered.is_set()
        release_first.set()


@pytest.mark.parametrize("max_concurrency", [0, 33])
def test_rejects_out_of_range_concurrency(max_concurrency: int) -> None:
    with pytest.raises(
        ValueError,
        match="ha_tool_concurrency middleware requires a value between 1 and 32",
    ):
        HomeAssistantRequestQueueMiddleware(max_concurrency=max_concurrency)


@pytest.mark.parametrize("max_concurrency, expected_count", [(0, 0), (2, 1)])
async def test_server_registers_queue_only_when_enabled(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    max_concurrency: int,
    expected_count: int,
) -> None:
    from ha_mcp.config import get_global_settings
    from ha_mcp.server import HomeAssistantSmartMCPServer

    monkeypatch.setattr(get_global_settings(), "ha_tool_concurrency", max_concurrency)
    server = HomeAssistantSmartMCPServer(client=core_client)
    queues = [
        m
        for m in server.mcp.middleware
        if isinstance(m, HomeAssistantRequestQueueMiddleware)
    ]
    assert len(queues) == expected_count


async def test_server_registers_queue_after_policy_gate(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ha_mcp.config import get_global_settings
    from ha_mcp.policy.middleware import PolicyMiddleware
    from ha_mcp.server import HomeAssistantSmartMCPServer

    monkeypatch.setattr(get_global_settings(), "ha_tool_concurrency", 1)
    monkeypatch.setattr(get_global_settings(), "enable_tool_security_policies", True)
    server = HomeAssistantSmartMCPServer(client=core_client)
    kinds = [type(m) for m in server.mcp.middleware]
    assert kinds.index(PolicyMiddleware) < kinds.index(
        HomeAssistantRequestQueueMiddleware
    )
