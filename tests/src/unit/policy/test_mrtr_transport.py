"""Verify approval continuations survive MCP serialization and proxy dispatch."""

from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.server.middleware import MiddlewareContext
from ha_mcp._vendor.mcp import MCPError
from ha_mcp._vendor.mcp_types import InputRequiredResult
from ha_mcp.policy.approval_queue import ApprovalQueue, PendingApproval
from ha_mcp.policy.middleware import PolicyMiddleware
from ha_mcp.policy.model import Policy, Rule
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

type ApprovalServer = tuple[FastMCP, ApprovalQueue, list[str], PolicyMiddleware]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> ApprovalServer:
    mcp = FastMCP("Approval test")
    queue = ApprovalQueue()
    executed = []

    @mcp.tool(name="ha_test_write", annotations={"readOnlyHint": False})
    async def write(value: str) -> str:
        executed.append(value)
        return "saved"

    middleware = PolicyMiddleware(
        policy_provider=lambda: Policy(rules=[Rule(tool_name="ha_test_write")]),
        queue=queue,
    )
    monkeypatch.setattr(middleware, "_wait_for_decision", AsyncMock())
    mcp.add_middleware(middleware)
    return mcp, queue, executed, middleware


@pytest.mark.anyio
@pytest.mark.parametrize("proxy", [False, True])
async def test_wire_continuation_is_bound_and_single_use(
    server: ApprovalServer, proxy: bool
) -> None:
    """Both direct and categorized calls carry sealed state, not JSON tool content."""
    mcp, queue, executed, _ = server
    name, args = "ha_test_write", {"value": "original"}
    if proxy:
        mcp.add_transform(CategorizedSearchTransform())
        name, args = "ha_call_write_tool", {"name": name, "arguments": args}

    async with Client(mcp) as client:
        first = await client.session.call_tool(name, args, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        assert first.request_state
        entry = queue.list_pending()[0]
        assert entry.token not in first.request_state
        assert executed == []

        # The production gate must participate in the SDK's state binding:
        # neither forged state nor another wire call may consume this approval.
        with pytest.raises(MCPError):
            await client.session.call_tool(
                name,
                args,
                request_state=first.request_state + "tampered",
                allow_input_required=True,
            )
        changed = {"value": "changed"}
        if proxy:
            changed = {"name": "ha_test_write", "arguments": changed}
        with pytest.raises(MCPError):
            await client.session.call_tool(
                name,
                changed,
                request_state=first.request_state,
                allow_input_required=True,
            )
        assert queue.list_pending() == [entry]
        queue.approve(entry.token)
        result = await client.session.call_tool(
            name, args, request_state=first.request_state, allow_input_required=True
        )
        assert not result.is_error
        replay = await client.session.call_tool(
            name, args, request_state=first.request_state, allow_input_required=True
        )
        assert replay.is_error
        assert executed == ["original"]


@pytest.mark.anyio
async def test_sdk_automatically_resumes_pending_approval(
    server: ApprovalServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, queue, executed, middleware = server
    waits = []

    async def decide_on_resume(
        ctx: MiddlewareContext, pending: PendingApproval, wait: float
    ) -> None:
        waits.append(pending.token)
        if len(waits) == 2:
            queue.approve(pending.token)

    monkeypatch.setattr(middleware, "_wait_for_decision", decide_on_resume)
    async with Client(mcp) as client:
        result = await client.call_tool("ha_test_write", {"value": "approved"})
    assert not result.is_error
    assert len(waits) == 2
    assert waits[0] == waits[1]
    assert executed == ["approved"]


@pytest.mark.anyio
async def test_legacy_wire_call_uses_normal_error_and_manual_retry(
    server: ApprovalServer,
) -> None:
    mcp, queue, executed, _ = server
    async with Client(mcp, mode="legacy") as client:
        result = await client.call_tool(
            "ha_test_write", {"value": "approved"}, raise_on_error=False
        )
        assert result.is_error
        assert "USER_APPROVAL_REQUIRED" in result.content[0].text
        assert executed == []
        queue.approve(queue.list_pending()[0].token)
        result = await client.call_tool("ha_test_write", {"value": "approved"})
        assert not result.is_error
        assert executed == ["approved"]
