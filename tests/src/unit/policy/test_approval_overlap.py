"""Approval cardinality at the MCP call/progress and public approval seams.

Reproduction contributed by @abdulla2010 in issue #2387; retained as a
regression test because it exercises the real FastMCP server and both the
direct and ``ha_call_write_tool`` routes, which the unit-level middleware
tests do not.

Real FastMCP and PolicyMiddleware; only the terminal action is synthetic.
No HA connection, queue internals, clock patching, or race-by-sleep assertions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.client import CallToolResult
from mcp.types import TextContent

from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.middleware import PolicyMiddleware
from ha_mcp.policy.model import Policy, Rule
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

ARGS = {"domain": "light", "service": "turn_on", "entity_id": "light.synthetic"}


def policy_server(
    queue: ApprovalQueue, *, remember_minutes: int = 0, wait_seconds: int = 5
) -> tuple[FastMCP, list[dict[str, Any]]]:
    server = FastMCP("single-use-approval-test")
    dispatched: list[dict[str, Any]] = []

    @server.tool(name="ha_call_service", annotations={"readOnlyHint": False})
    async def action(domain: str, service: str, entity_id: str) -> dict[str, bool]:
        dispatched.append(
            {"domain": domain, "service": service, "entity_id": entity_id}
        )
        return {"fake_dispatch": True}

    @server.tool(name="ha_bulk_control", annotations={"readOnlyHint": False})
    async def bulk(selector: dict[str, Any], action: str) -> dict[str, bool]:
        dispatched.append({"selector": selector, "action": action})
        return {"fake_dispatch": True}

    policy = Policy(rules=[Rule(tool_name="*", remember_minutes=remember_minutes)])
    server.add_middleware(
        PolicyMiddleware(
            policy_provider=lambda: policy, queue=queue, wait_seconds=wait_seconds
        )
    )
    server.add_transform(CategorizedSearchTransform())
    return server, dispatched


def envelope(route: str, args: dict[str, Any] = ARGS) -> tuple[str, dict[str, Any]]:
    if route == "direct":
        return "ha_call_service", dict(args)
    return "ha_call_write_tool", {"name": "ha_call_service", "arguments": dict(args)}


def assert_approval_required(result: CallToolResult) -> None:
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    assert '"USER_APPROVAL_REQUIRED"' in result.content[0].text


async def wait_for_approval_progress(
    *events: asyncio.Event,
) -> None:
    await asyncio.gather(*(event.wait() for event in events))


def progress_setter(
    event: asyncio.Event,
) -> Callable[[float, float | None, str | None], Awaitable[None]]:
    async def handler(
        progress: float, total: float | None, message: str | None
    ) -> None:
        assert message and "Awaiting user approval" in message
        event.set()

    return handler


@pytest.mark.parametrize("route", ["direct", "write-proxy"])
async def test_one_approval_dispatches_only_one_overlapping_static_call(
    route: str,
) -> None:
    queue = ApprovalQueue()
    server, dispatched = policy_server(queue)
    ready = [asyncio.Event(), asyncio.Event()]
    name, arguments = envelope(route)
    async with asyncio.timeout(10), Client(server) as first, Client(server) as second:
        async with asyncio.TaskGroup() as tasks:
            calls = [
                tasks.create_task(
                    first.call_tool(
                        name,
                        arguments,
                        progress_handler=progress_setter(ready[0]),
                        raise_on_error=False,
                    )
                ),
                tasks.create_task(
                    second.call_tool(
                        name,
                        arguments,
                        progress_handler=progress_setter(ready[1]),
                        raise_on_error=False,
                    )
                ),
            ]
            await wait_for_approval_progress(*ready)
            pending = queue.list_pending()
            assert len(pending) == 1
            assert dispatched == []
            assert queue.approve(pending[0].token)
        results = [call.result() for call in calls]

    assert dispatched == [ARGS], "one approval must authorize only one execution"
    assert sum(not result.is_error for result in results) == 1
    assert_approval_required(next(result for result in results if result.is_error))
    assert len(queue.list_pending()) == 1
    assert queue.list_pending()[0].token != pending[0].token
