"""Verify approval continuations survive MCP serialization and proxy dispatch."""

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.client.elicitation import ElicitResult
from ha_mcp._vendor.mcp import MCPError
from ha_mcp._vendor.mcp_types import ElicitResult as MCPElicitResult
from ha_mcp._vendor.mcp_types import InputRequiredResult
from ha_mcp.policy import mrtr
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.middleware import PolicyMiddleware
from ha_mcp.policy.model import Policy, Rule
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

type ApprovalServer = tuple[FastMCP, ApprovalQueue, list[str], PolicyMiddleware]


async def accept(*_args: object) -> ElicitResult:
    return ElicitResult(action="accept", content={"decided": True})


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
        wait_seconds=2,
    )
    monkeypatch.setattr(mrtr, "ROUND_WAIT_SECONDS", 0)
    mcp.add_middleware(middleware)
    return mcp, queue, executed, middleware


@pytest.mark.usefixtures("tool_search_default")
@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", [False, True])
async def test_wire_continuation_is_bound_and_single_use(
    server: ApprovalServer, proxy: bool
) -> None:
    """Both direct and categorized calls carry sealed state, not JSON tool content."""
    mcp, queue, executed, _ = server
    name = "ha_test_write"
    args: dict[str, object] = {"value": "original"}
    if proxy:
        mcp.add_transform(CategorizedSearchTransform())
        name, args = "ha_call_write_tool", {"name": name, "arguments": args}

    async with Client(mcp, elicitation_handler=accept) as client:
        first = await client.session.call_tool(name, args, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        assert first.request_state
        # Through the proxy too, the form names the gated tool, not the proxy.
        assert "ha_test_write" in first.input_requests["ha_mcp_approval"].params.message
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
        changed: dict[str, object] = {"value": "changed"}
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
        accepted = {
            "ha_mcp_approval": MCPElicitResult(
                action="accept", content={"decided": True}
            )
        }
        result = await client.session.call_tool(
            name,
            args,
            request_state=first.request_state,
            input_responses=accepted,
            allow_input_required=True,
        )
        assert not result.is_error
        replay = await client.session.call_tool(
            name,
            args,
            request_state=first.request_state,
            input_responses=accepted,
            allow_input_required=True,
        )
        assert replay.is_error
        assert executed == ["original"]


@pytest.mark.asyncio
async def test_sdk_automatically_resumes_pending_approval(
    server: ApprovalServer,
) -> None:
    mcp, queue, executed, _ = server
    prompts = []

    async def approve_pending(*_args: object) -> ElicitResult:
        pending = queue.list_pending()
        assert len(pending) == 1
        assert executed == []
        prompts.append(pending[0].token)
        queue.approve(pending[0].token)
        return ElicitResult(action="accept", content={"decided": True})

    async with Client(mcp, elicitation_handler=approve_pending) as client:
        result = await client.call_tool("ha_test_write", {"value": "approved"})
    assert not result.is_error
    assert len(prompts) == 1
    assert executed == ["approved"]


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_modern_client_without_elicitation_gets_normal_error(
    server: ApprovalServer,
) -> None:
    """Without a declared elicitation capability the client cannot be asked,
    so a 2026-07-28 call gets the approval error rather than input_required."""
    mcp, queue, executed, _ = server
    async with Client(mcp) as client:
        first = await client.session.call_tool(
            "ha_test_write", {"value": "approved"}, allow_input_required=True
        )
        assert not isinstance(first, InputRequiredResult)
        assert first.is_error
        assert "USER_APPROVAL_REQUIRED" in first.content[0].text
        assert executed == []
