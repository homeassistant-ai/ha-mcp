"""Unit tests for process-wide Home Assistant tool-call concurrency control."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import anyio
import pytest

from ha_mcp.ha_request_queue import (
    HomeAssistantRequestQueueMiddleware,
    configure_ha_transport_concurrency,
    limit_ha_transport_request,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_context(name: str) -> MagicMock:
    message = MagicMock()
    message.name = name
    context = MagicMock()
    context.message = message
    return context


@pytest.mark.anyio
async def test_concurrent_outer_calls_are_serialized() -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)
    first_entered = anyio.Event()
    release_first = anyio.Event()
    second_attempted = anyio.Event()
    second_entered = anyio.Event()

    async def first_call_next(context: MagicMock) -> str:
        first_entered.set()
        await release_first.wait()
        return context.message.name

    async def second_call_next(context: MagicMock) -> str:
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


@pytest.mark.anyio
async def test_nested_redispatch_does_not_reacquire_the_queue() -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)

    async def inner_call_next(context: MagicMock) -> str:
        return context.message.name

    async def outer_call_next(_context: MagicMock) -> str:
        return await middleware.on_call_tool(
            make_context("ha_get_state"), inner_call_next
        )

    with anyio.fail_after(1):
        result = await middleware.on_call_tool(
            make_context("ha_call_read_tool"), outer_call_next
        )

    assert result == "ha_get_state"


@pytest.mark.anyio
async def test_proxy_envelope_defers_queue_slot_until_inner_dispatch() -> None:
    middleware = HomeAssistantRequestQueueMiddleware(max_concurrency=1)
    proxy_entered = anyio.Event()
    release_proxy = anyio.Event()
    normal_entered = anyio.Event()
    inner_entered = anyio.Event()

    async def inner_call_next(context: MagicMock) -> str:
        inner_entered.set()
        return context.message.name

    async def proxy_call_next(_context: MagicMock) -> str:
        proxy_entered.set()
        await release_proxy.wait()
        return await middleware.on_call_tool(
            make_context("ha_get_state"), inner_call_next
        )

    async def normal_call_next(context: MagicMock) -> str:
        normal_entered.set()
        return context.message.name

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            middleware.on_call_tool,
            make_context("ha_call_read_tool"),
            proxy_call_next,
        )
        await proxy_entered.wait()
        result = await middleware.on_call_tool(
            make_context("ha_get_overview"), normal_call_next
        )
        assert result == "ha_get_overview"
        assert normal_entered.is_set()
        release_proxy.set()

    assert inner_entered.is_set()


@pytest.mark.asyncio
async def test_transport_requests_share_process_wide_capacity() -> None:
    configure_ha_transport_concurrency(1)
    first_entered = anyio.Event()
    release_first = anyio.Event()
    second_entered = anyio.Event()

    async def first_request() -> None:
        async with limit_ha_transport_request():
            first_entered.set()
            await release_first.wait()

    async def second_request() -> None:
        await first_entered.wait()
        async with limit_ha_transport_request():
            second_entered.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(first_request)
        await first_entered.wait()
        task_group.start_soon(second_request)
        await anyio.lowlevel.checkpoint()
        assert not second_entered.is_set()
        release_first.set()

    assert second_entered.is_set()


def test_rest_and_websocket_transports_use_shared_limiter() -> None:
    root = Path(__file__).parents[3] / "src" / "ha_mcp" / "client"
    rest_source = (root / "rest_client.py").read_text(encoding="utf-8")
    websocket_source = (root / "websocket_client.py").read_text(encoding="utf-8")

    assert "async with limit_ha_transport_request():" in rest_source
    assert websocket_source.count("async with limit_ha_transport_request():") == 2


@pytest.mark.parametrize("max_concurrency", [0, 33])
def test_rejects_out_of_range_concurrency(max_concurrency: int) -> None:
    with pytest.raises(ValueError, match="max_concurrency must be between 1 and 32"):
        HomeAssistantRequestQueueMiddleware(max_concurrency=max_concurrency)


def test_server_registers_queue_after_policy_gates() -> None:
    server_source = (
        Path(__file__).parents[3] / "src" / "ha_mcp" / "server.py"
    ).read_text(encoding="utf-8")

    policy_index = server_source.index("self._apply_tool_security_policies()")
    queue_index = server_source.index("HomeAssistantRequestQueueMiddleware(")
    redaction_index = server_source.index("RedactSecretsMiddleware()")

    assert policy_index < queue_index < redaction_index
