"""Under an allow list, only listed calls run; every other call waits for approval.

An allow list has no remember window, and nothing a require-approval list
left in the remember cache may approve a call it gates.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import anyio
import pytest
from anyio.abc import TaskGroup
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import async_capture_events

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.policy.approval_queue import ApprovalQueue, compute_args_hash
from ha_mcp.policy.events import APPROVAL_REQUESTED_EVENT
from ha_mcp.policy.model import Policy, Predicate, Rule

from ._middleware_core import QueueWatch, error_body, gate, tool_call

LOCK = {"domain": "lock"}
ALLOW_GET_STATE = Policy(rule_effect="allow", rules=[Rule(tool_name="ha_get_state")])
REMEMBERING = Policy(rules=[Rule(tool_name="ha_call_service", remember_minutes=10)])


async def test_allow_list_gates_unapproved_calls_and_passes_approved_ones(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Under an allow list nothing matched the gated call, so no rule is named."""
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    mw = gate(ALLOW_GET_STATE, ApprovalQueue(), get_client=lambda: core_client)
    call_next = AsyncMock(return_value="ok")

    assert await mw.on_call_tool(tool_call("ha_get_state"), call_next) == "ok"
    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(tool_call("ha_call_service", LOCK), call_next)
    await hass.async_block_till_done()

    assert "matched_rule" not in error_body(ei.value)
    (event,) = fired
    assert event.data["tool_name"] == "ha_call_service"
    assert "matched_rule" not in event.data
    call_next.assert_awaited_once()


async def test_allow_list_names_no_rule_and_remembers_nothing(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A case variant misses an approving rule that carries remember_minutes.

    Without the allow-list guard in ``find_matching_rule`` the gate would
    name that rule as the reason and arm its remember window.
    """
    fired = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    rule = Rule(
        tool_name="ha_call_service",
        when=[Predicate(path="args.data.topic", op="eq", value="home/bridge")],
        remember_minutes=5,
    )
    queue = ApprovalQueue()
    mw = gate(
        Policy(rule_effect="allow", rules=[rule]),
        queue,
        get_client=lambda: core_client,
    )
    args = {"data": {"topic": "Home/Bridge"}}
    call_next = AsyncMock(return_value="ok")

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(tool_call("ha_call_service", args), call_next)
    await hass.async_block_till_done()
    (event,) = fired
    assert "matched_rule" not in event.data

    queue.approve(error_body(ei.value)["token"])
    assert await mw.on_call_tool(tool_call("ha_call_service", args), call_next) == "ok"
    with pytest.raises(ToolError):
        await mw.on_call_tool(tool_call("ha_call_service", args), call_next)
    call_next.assert_awaited_once()


async def test_empty_allow_list_leaves_approval_management_ungated() -> None:
    """An empty allow list gates everything except deciding the queue itself."""
    mw = gate(Policy(rule_effect="allow"), ApprovalQueue())
    call_next = AsyncMock(return_value="ok")
    decide = tool_call("ha_dev_manage_server", {"action": "approve", "token": "t"})

    assert await mw.on_call_tool(decide, call_next) == "ok"
    assert await mw.on_call_tool(tool_call("ha_search_tools"), call_next) == "ok"
    with pytest.raises(ToolError):
        await mw.on_call_tool(tool_call("ha_get_state"), call_next)
    assert call_next.await_count == 2


async def test_switching_to_an_allow_list_mid_wait_remembers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call held under a rule with remember_minutes is approved after the
    switch to an allow list. Its approval arms the window it captured, and
    nothing clears the cache (a sidecar save cannot reach it), so the allow
    list must not read that window."""
    policy = [REMEMBERING]
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch)
    mw = gate(lambda: policy[0], queue, wait_seconds=5)

    async def switch_then_approve() -> None:
        (entry,) = await watch.until(1)
        policy[0] = ALLOW_GET_STATE
        queue.approve(entry.token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(switch_then_approve)
        result = await mw.on_call_tool(
            tool_call("ha_call_service", LOCK), AsyncMock(return_value="ok")
        )
    assert result == "ok"
    assert queue.is_remembered("ha_call_service", compute_args_hash(LOCK))

    later = AsyncMock()
    with pytest.raises(ToolError):
        await gate(lambda: policy[0], queue).on_call_tool(
            tool_call("ha_call_service", LOCK), later
        )
    later.assert_not_awaited()


async def test_a_waiter_under_an_allow_list_does_not_ride_the_winners_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two identical calls share one row across the switch to an allow list.

    The first, which started under a rule with remember_minutes, wins the
    approval and arms its window. The second joined under the allow list, so
    losing the claim must leave it without an approval rather than let it
    read that window: one click, one dispatch.
    """
    policy = [REMEMBERING]
    queue = ApprovalQueue()
    watch = QueueWatch(queue, monkeypatch)
    mw = gate(lambda: policy[0], queue, wait_seconds=5)
    call_next = AsyncMock(return_value="ok")
    outcomes: list[str] = []

    async def call() -> None:
        try:
            outcomes.append(
                await mw.on_call_tool(tool_call("ha_call_service", LOCK), call_next)
            )
        except ToolError as exc:
            outcomes.append(error_body(exc)["error"]["code"])

    async def switch_join_and_approve(tg: TaskGroup) -> None:
        await watch.until(1)
        policy[0] = ALLOW_GET_STATE
        tg.start_soon(call)
        first, second = await watch.until(2)
        assert first is second
        queue.approve(first.token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(switch_join_and_approve, tg)

    assert sorted(outcomes) == ["USER_APPROVAL_REQUIRED", "ok"]
    call_next.assert_awaited_once()
