"""Approval continuations wait for the existing UI decision, never authorize it."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, tzinfo
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools.base import InputRequiredToolResult
from ha_mcp._vendor.mcp_types import (
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitResult,
)
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.middleware import PolicyMiddleware
from ha_mcp.policy.model import Policy, Rule


def context(
    state: str | None = None,
    *,
    version: str = "2026-07-28",
    name: str = "ha_call_service",
    arguments: dict[str, Any] | None = None,
    elicitation: bool = True,
    answer: str | None = None,
    responses: dict[str, Any] | None = None,
    request_name: str | None = None,
    progress: Callable[..., Awaitable[None]] | None = None,
) -> SimpleNamespace:
    """A middleware context for one call of ``name``.

    ``request_name`` is the tool the client's own request called, when
    the gated call is nested inside another tool's run. ``progress`` receives
    the heartbeats a waiting call sends its client."""
    args = arguments if arguments is not None else {"domain": "light"}
    request_params = (
        {"name": name, "arguments": args}
        if request_name is None
        else {"name": request_name}
    )
    caps = ClientCapabilities(
        elicitation=ElicitationCapability() if elicitation else None
    )
    if answer is not None:
        responses = {"ha_mcp_approval": ElicitResult(action=answer)}
    return SimpleNamespace(
        message=SimpleNamespace(name=name, arguments=args),
        fastmcp_context=SimpleNamespace(
            request_context=SimpleNamespace(
                protocol_version=version,
                _srctx=SimpleNamespace(params=request_params),
                session=SimpleNamespace(client_capabilities=caps),
            ),
            request_state=state,
            input_responses=responses,
            report_progress=progress or AsyncMock(),
        ),
    )


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Control time and waits while retaining real queue and policy decisions.

    A round that waits before asking lasts a hundredth of a second; the
    ``blocking`` middleware gives a client it cannot ask a one-second window.
    """
    from ha_mcp.policy import approval_queue, mrtr

    monkeypatch.setattr(mrtr, "ROUND_WAIT_SECONDS", 0.01)
    clock = [2_000_000_000.0]

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(clock[0], tz or UTC)

    monkeypatch.setattr(approval_queue, "datetime", ClockDateTime)
    monkeypatch.setattr(mrtr, "time", lambda: clock[0])
    queue = ApprovalQueue()
    policy = Policy(rules=[Rule(tool_name="*")])
    middleware = PolicyMiddleware(policy_provider=lambda: policy, queue=queue)
    blocking = PolicyMiddleware(
        policy_provider=lambda: policy, queue=queue, wait_seconds=1
    )
    executed = []

    async def execute(ctx: SimpleNamespace) -> str:
        executed.append(ctx.message.name)
        return "saved"

    return SimpleNamespace(
        queue=queue,
        policy=policy,
        middleware=middleware,
        blocking=blocking,
        clock=clock,
        execute=execute,
        executed=executed,
    )


@pytest.mark.asyncio
async def test_denial_ends_continuation_without_dispatch(gate: SimpleNamespace) -> None:
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.queue.deny(gate.queue.list_pending()[0].token)
    with pytest.raises(ToolError, match="USER_DENIED"):
        await gate.middleware.on_call_tool(
            context(first.input_required.request_state), gate.execute
        )
    assert gate.executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["arguments", "tool", "policy", "removed", "expired"]
)
async def test_stale_or_repurposed_continuation_cannot_execute(
    gate: SimpleNamespace, change: str
) -> None:
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


@pytest.mark.asyncio
async def test_round_limit_returns_existing_manual_approval_fallback(
    gate: SimpleNamespace,
) -> None:
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


@pytest.mark.asyncio
async def test_dynamic_selector_resumes_only_its_original_row(
    gate: SimpleNamespace,
) -> None:
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
                answer="accept",
            ),
            gate.execute,
        )
        == "saved"
    )
    assert len(gate.queue.list_pending()) == 1
    assert gate.executed == ["ha_bulk_control"]


@pytest.mark.asyncio
async def test_abandoned_dynamic_continuation_expires_at_original_wait_deadline(
    gate: SimpleNamespace,
) -> None:
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


@pytest.mark.asyncio
async def test_concurrent_resumes_cannot_reuse_remembered_approval(
    gate: SimpleNamespace,
) -> None:
    gate.policy.rules[0].remember_minutes = 5
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    pending = gate.queue.list_pending()[0]
    waiting = 0

    async def approve_once_both_wait(**_progress: object) -> None:
        # The user approves in the tab while both resumed calls wait for it.
        nonlocal waiting
        waiting += 1
        if waiting == 2:
            gate.queue.approve(pending.token)

    results = await asyncio.gather(
        *(
            gate.middleware.on_call_tool(
                context(
                    first.input_required.request_state,
                    answer="accept",
                    progress=approve_once_both_wait,
                ),
                gate.execute,
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert results.count("saved") == 1
    assert sum(isinstance(result, ToolError) for result in results) == 1
    assert gate.executed == ["ha_call_service"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("ha_bulk_control", {"selector": {"domain": "light"}, "action": "turn_off"}),
        (
            "ha_config_set_scene",
            {"scene_id": "movie", "config": {}, "capture_devices": ["tv"]},
        ),
    ],
)
async def test_a_resume_that_loses_a_single_use_approval_leaves_no_row(
    gate: SimpleNamespace, name: str, arguments: dict[str, Any]
) -> None:
    """Two resumes of one single-use request wait on its row; one runs. The
    other gets no new row, whose approval no call could ever use."""
    first = await gate.middleware.on_call_tool(
        context(name=name, arguments=arguments), gate.execute
    )
    pending = gate.queue.list_pending()[0]
    waiting = 0

    async def approve_once_both_wait(**_progress: object) -> None:
        nonlocal waiting
        waiting += 1
        if waiting == 2:
            gate.queue.approve(pending.token)

    results = await asyncio.gather(
        *(
            gate.middleware.on_call_tool(
                context(
                    first.input_required.request_state,
                    name=name,
                    arguments=arguments,
                    answer="accept",
                    progress=approve_once_both_wait,
                ),
                gate.execute,
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert results.count("saved") == 1
    (lost,) = [r for r in results if isinstance(r, ToolError)]
    assert pending.token in str(lost)
    # The approval was used, not timed out: the action already ran once.
    assert "another resume of this request used its approval" in str(lost)
    assert "Re-call this tool" not in str(lost)
    assert gate.queue.list_pending() == []
    assert gate.executed == [name]


@pytest.mark.asyncio
async def test_nested_script_tool_uses_blocking_fallback(gate: SimpleNamespace) -> None:
    """Continuing an outer script could replay earlier effects in that script."""
    ctx = context(request_name="ha_manage_custom_tool")
    with pytest.raises(ToolError, match="USER_APPROVAL_REQUIRED"):
        await gate.blocking.on_call_tool(ctx, gate.execute)
    assert gate.executed == []


@pytest.mark.asyncio
async def test_continuation_resumes_only_after_ui_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending round cannot run the tool; the same approved row runs it once."""
    from ha_mcp.policy import mrtr

    queue = ApprovalQueue()
    policy = Policy(rules=[Rule(tool_name="ha_call_service")])
    middleware = PolicyMiddleware(policy_provider=lambda: policy, queue=queue)
    monkeypatch.setattr(mrtr, "ROUND_WAIT_SECONDS", 0.01)
    dispatched = []

    async def execute(ctx: SimpleNamespace) -> str:
        dispatched.append(ctx.message.arguments)
        return "saved"

    first = await middleware.on_call_tool(context(), execute)
    assert isinstance(first, InputRequiredToolResult)
    ask = first.input_required.input_requests["ha_mcp_approval"]
    assert isinstance(ask, ElicitRequest)
    assert "Tool Security Policies" in ask.params.message
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
    assert (
        await middleware.on_call_tool(context(state, answer="accept"), execute)
        == "saved"
    )
    with pytest.raises(ToolError):
        await middleware.on_call_tool(context(state, answer="accept"), execute)
    assert dispatched == [{"domain": "light"}]


@pytest.mark.asyncio
async def test_legacy_client_keeps_approval_error_and_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy clients never receive an unrepresentable input_required result."""
    from ha_mcp.policy import mrtr

    # Short enough that a client that can be asked is asked well inside the
    # one-second window.
    monkeypatch.setattr(mrtr, "ROUND_WAIT_SECONDS", 0.01)
    queue = ApprovalQueue()
    middleware = PolicyMiddleware(
        policy_provider=lambda: Policy(rules=[Rule(tool_name="ha_call_service")]),
        queue=queue,
        wait_seconds=1,
    )
    execute = AsyncMock(return_value="saved")
    with pytest.raises(ToolError, match="USER_APPROVAL_REQUIRED"):
        await middleware.on_call_tool(context(version="2025-11-25"), execute)
    execute.assert_not_called()
    queue.approve(queue.list_pending()[0].token)
    assert (
        await middleware.on_call_tool(context(version="2025-11-25"), execute) == "saved"
    )


@pytest.mark.asyncio
async def test_modern_client_without_elicitation_gets_blocking_approval_error(
    gate: SimpleNamespace,
) -> None:
    """A state-only continuation gives such a client nothing to show its user,
    so it waits the whole window, telling it so, and gets the
    approval-required error."""
    ctx = context(elicitation=False)
    with pytest.raises(ToolError, match="USER_APPROVAL_REQUIRED"):
        await gate.blocking.on_call_tool(ctx, gate.execute)
    ctx.fastmcp_context.report_progress.assert_awaited()
    assert gate.executed == []


@pytest.mark.asyncio
async def test_confirmed_retry_waits_out_the_window_without_asking_again(
    gate: SimpleNamespace,
) -> None:
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    # Half a second of the 60 s window is left.
    gate.clock[0] += 59.5
    ctx = context(first.input_required.request_state, answer="accept")
    with pytest.raises(ToolError, match="USER_APPROVAL_REQUIRED"):
        await gate.middleware.on_call_tool(ctx, gate.execute)
    ctx.fastmcp_context.report_progress.assert_awaited()
    assert gate.executed == []


def approve_later(queue: ApprovalQueue) -> Callable[..., Awaitable[None]]:
    """A progress callback: the user approves in the tab a tenth of a second
    after the call starts waiting, ten rounds' worth past ``ROUND_WAIT_SECONDS``."""

    async def progress(**_progress: object) -> None:
        token = queue.list_pending()[0].token
        asyncio.get_running_loop().call_later(0.1, queue.approve, token)

    return progress


@pytest.mark.asyncio
async def test_confirmed_retry_runs_when_approved_inside_the_rest_of_the_window(
    gate: SimpleNamespace,
) -> None:
    """The retry waits for the rest of the window, not one round."""
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    # Five seconds of the 60 s window are left.
    gate.clock[0] += 55
    ctx = context(
        first.input_required.request_state,
        answer="accept",
        progress=approve_later(gate.queue),
    )
    assert await gate.middleware.on_call_tool(ctx, gate.execute) == "saved"
    assert gate.executed == ["ha_call_service"]


@pytest.mark.asyncio
async def test_client_without_elicitation_runs_when_approved_inside_the_window(
    gate: SimpleNamespace,
) -> None:
    """Such a client waits the whole window, so a late approval still runs it."""
    blocking = PolicyMiddleware(
        policy_provider=lambda: gate.policy, queue=gate.queue, wait_seconds=5
    )
    ctx = context(elicitation=False, progress=approve_later(gate.queue))
    assert await blocking.on_call_tool(ctx, gate.execute) == "saved"
    assert gate.executed == ["ha_call_service"]


@pytest.mark.asyncio
async def test_confirmed_retry_runs_once_approved(gate: SimpleNamespace) -> None:
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.queue.approve(gate.queue.list_pending()[0].token)
    assert (
        await gate.middleware.on_call_tool(
            context(first.input_required.request_state, answer="accept"),
            gate.execute,
        )
        == "saved"
    )
    assert gate.executed == ["ha_call_service"]


@pytest.mark.asyncio
async def test_declined_ask_denies_the_call(gate: SimpleNamespace) -> None:
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    with pytest.raises(ToolError, match="USER_DENIED"):
        await gate.middleware.on_call_tool(
            context(first.input_required.request_state, answer="decline"),
            gate.execute,
        )
    assert gate.queue.list_pending() == []
    assert gate.executed == []


@pytest.mark.asyncio
async def test_declined_ask_cannot_run_an_already_approved_call(
    gate: SimpleNamespace,
) -> None:
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.queue.approve(gate.queue.list_pending()[0].token)
    with pytest.raises(ToolError, match="USER_DENIED"):
        await gate.middleware.on_call_tool(
            context(first.input_required.request_state, answer="decline"),
            gate.execute,
        )
    assert gate.executed == []


@pytest.mark.asyncio
async def test_cancelled_ask_returns_approval_error_and_keeps_the_row(
    gate: SimpleNamespace,
) -> None:
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    ctx = context(first.input_required.request_state, answer="cancel")
    with pytest.raises(ToolError, match="USER_APPROVAL_REQUIRED"):
        await gate.middleware.on_call_tool(ctx, gate.execute)
    # Answered at once: the call never sat waiting for the tab.
    ctx.fastmcp_context.report_progress.assert_not_awaited()
    assert len(gate.queue.list_pending()) == 1
    gate.queue.approve(gate.queue.list_pending()[0].token)
    assert await gate.middleware.on_call_tool(context(), gate.execute) == "saved"


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_seconds", [30, 120])
async def test_ask_states_the_configured_approval_window(
    gate: SimpleNamespace, wait_seconds: int
) -> None:
    """The form cannot count down, so it relays the window the user set."""
    gate.policy.wait_seconds = wait_seconds
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    message = first.input_required.input_requests["ha_mcp_approval"].params.message
    assert f"within {wait_seconds} s" in message


@pytest.mark.asyncio
async def test_dismissed_form_cannot_run_an_already_approved_call(
    gate: SimpleNamespace,
) -> None:
    """Continuing needs both the approval and an accepted form."""
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.queue.approve(gate.queue.list_pending()[0].token)
    with pytest.raises(ToolError, match="USER_APPROVAL_REQUIRED"):
        await gate.middleware.on_call_tool(
            context(first.input_required.request_state, answer="cancel"),
            gate.execute,
        )
    assert gate.executed == []


@pytest.mark.asyncio
async def test_unanswered_retry_is_asked_again_even_once_approved(
    gate: SimpleNamespace,
) -> None:
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.queue.approve(gate.queue.list_pending()[0].token)
    again = await gate.middleware.on_call_tool(
        context(first.input_required.request_state), gate.execute
    )
    assert isinstance(again, InputRequiredToolResult)
    assert "ha_mcp_approval" in again.input_required.input_requests
    assert gate.executed == []


@pytest.mark.asyncio
async def test_unreadable_answer_is_logged_and_asked_again(
    gate: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.queue.approve(gate.queue.list_pending()[0].token)
    with caplog.at_level("WARNING", logger="ha_mcp.policy.mrtr"):
        again = await gate.middleware.on_call_tool(
            context(
                first.input_required.request_state,
                responses={"other_key": ElicitResult(action="accept")},
            ),
            gate.execute,
        )
    assert isinstance(again, InputRequiredToolResult)
    assert "unreadable answer" in caplog.text
    assert gate.executed == []


@pytest.mark.asyncio
async def test_late_accept_on_an_approved_call_runs_instead_of_a_false_reuse_error(
    gate: SimpleNamespace,
) -> None:
    """Answering after the window is user latency, not a reused state."""
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.queue.approve(gate.queue.list_pending()[0].token)
    gate.clock[0] += 70
    assert (
        await gate.middleware.on_call_tool(
            context(first.input_required.request_state, answer="accept"),
            gate.execute,
        )
        == "saved"
    )


@pytest.mark.asyncio
async def test_late_accept_without_approval_gets_the_normal_recall_error(
    gate: SimpleNamespace,
) -> None:
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.clock[0] += 70
    with pytest.raises(ToolError) as exc:
        await gate.middleware.on_call_tool(
            context(first.input_required.request_state, answer="accept"),
            gate.execute,
        )
    assert "USER_APPROVAL_REQUIRED" in str(exc.value)
    assert "may already have used the approval" not in str(exc.value)
    assert gate.executed == []


@pytest.mark.asyncio
async def test_late_accept_on_an_expired_selector_row_says_the_window_ended(
    gate: SimpleNamespace,
) -> None:
    args = {"selector": {"domain": "light"}, "action": "turn_off"}
    first = await gate.middleware.on_call_tool(
        context(name="ha_bulk_control", arguments=args), gate.execute
    )
    gate.clock[0] += 70
    with pytest.raises(ToolError, match="approval window for this call has ended"):
        await gate.middleware.on_call_tool(
            context(
                first.input_required.request_state,
                name="ha_bulk_control",
                arguments=args,
                answer="accept",
            ),
            gate.execute,
        )
    assert gate.executed == []


@pytest.mark.asyncio
async def test_dismissed_form_on_a_selector_call_says_it_was_dismissed(
    gate: SimpleNamespace,
) -> None:
    args = {"selector": {"domain": "light"}, "action": "turn_off"}
    first = await gate.middleware.on_call_tool(
        context(name="ha_bulk_control", arguments=args), gate.execute
    )
    with pytest.raises(ToolError) as exc:
        await gate.middleware.on_call_tool(
            context(
                first.input_required.request_state,
                name="ha_bulk_control",
                arguments=args,
                answer="cancel",
            ),
            gate.execute,
        )
    assert "approval form for this call was dismissed" in str(exc.value)
    assert "window has now closed" not in str(exc.value)


@pytest.mark.asyncio
async def test_accepted_call_denied_in_the_tab_is_denied(gate: SimpleNamespace) -> None:
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.queue.deny(gate.queue.list_pending()[0].token)
    with pytest.raises(ToolError, match="USER_DENIED"):
        await gate.middleware.on_call_tool(
            context(first.input_required.request_state, answer="accept"),
            gate.execute,
        )
    assert gate.queue.list_pending() == []
    assert gate.executed == []


@pytest.mark.asyncio
async def test_replaying_a_used_approval_after_the_window_does_not_invite_a_rerun(
    gate: SimpleNamespace,
) -> None:
    """The row is gone either way, so the message must not say nothing ran."""
    first = await gate.middleware.on_call_tool(context(), gate.execute)
    gate.queue.approve(gate.queue.list_pending()[0].token)
    state = first.input_required.request_state
    assert (
        await gate.middleware.on_call_tool(
            context(state, answer="accept"), gate.execute
        )
        == "saved"
    )
    gate.clock[0] += 70
    with pytest.raises(ToolError) as exc:
        await gate.middleware.on_call_tool(
            context(state, answer="accept"), gate.execute
        )
    assert "check its outcome" in str(exc.value)
    assert gate.executed == ["ha_call_service"]
