"""Test PolicyMiddleware by driving it directly with a fake call_next.

Avoids spinning up a full FastMCP server. The middleware sees
context.message.name + context.message.arguments and routes accordingly.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.policy.approval_queue import ApprovalQueue, compute_args_hash
from ha_mcp.policy.middleware import PROXY_META_TOOLS, PolicyMiddleware
from ha_mcp.policy.model import Policy, Predicate, Rule


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
async def test_empty_policy_passes_through(queue):
    mw = PolicyMiddleware(policy_provider=Policy, queue=queue)
    call_next = AsyncMock(return_value="real_result")
    result = await mw.on_call_tool(
        make_context("ha_call_service", {"domain": "lock"}), call_next
    )
    assert result == "real_result"


@pytest.mark.anyio
async def test_proxy_meta_tools_pass_through(queue):
    pol = Policy(rules=[Rule(tool_name="*")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue)
    call_next = AsyncMock(return_value="proxy_result")
    for name in PROXY_META_TOOLS:
        result = await mw.on_call_tool(make_context(name, {}), call_next)
        assert result == "proxy_result"


@pytest.mark.anyio
async def test_no_matching_rule_passes_through(queue):
    pol = Policy(rules=[Rule(tool_name="ha_other")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue)
    call_next = AsyncMock(return_value="ok")
    result = await mw.on_call_tool(make_context("ha_call_service"), call_next)
    assert result == "ok"


@pytest.mark.anyio
async def test_a_rule_gates_a_call_on_the_tools_retired_name(queue):
    """The gate is keyed on the current name; ``evaluate`` fails OPEN.

    The alias middleware normally rewrites the name before this gate sees it,
    so this pins the defence rather than the ordering: reading the raw name
    here means no rule matches, and an unmatched call is allowed.
    """
    pol = Policy(rules=[Rule(tool_name="ha_manage_app")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    call_next = AsyncMock()

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(make_context("ha_manage_addon", {"slug": "x"}), call_next)

    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    call_next.assert_not_called()


@pytest.mark.anyio
async def test_remembered_approval_passes_through(queue):
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    args = {"domain": "lock"}
    queue.remember("ha_call_service", compute_args_hash(args), minutes=5)
    call_next = AsyncMock(return_value="ok")
    result = await mw.on_call_tool(make_context("ha_call_service", args), call_next)
    assert result == "ok"


@pytest.mark.anyio
async def test_remembered_approval_never_bypasses_dynamic_bulk_selector(queue):
    """Selector membership changes require a fresh approval for every call."""
    args = {
        "selector": {"domain": "lock", "area_ids": ["entry"]},
        "action": "lock",
    }
    policy = Policy(rules=[Rule(tool_name="ha_bulk_control", remember_minutes=5)])
    queue.remember("ha_bulk_control", compute_args_hash(args), minutes=5)
    middleware = PolicyMiddleware(
        policy_provider=lambda: policy,
        queue=queue,
        wait_seconds=0,
    )
    call_next = AsyncMock()

    with pytest.raises(ToolError):
        await middleware.on_call_tool(make_context("ha_bulk_control", args), call_next)

    call_next.assert_not_awaited()


@pytest.mark.anyio
async def test_dynamic_bulk_selector_never_consumes_a_foreign_approval(queue):
    """A dynamic selector call must not pick up an approval it didn't wait on.

    Regression for a real gap: the pre-existing "existing = find(); consume if
    approved" fast path was not gated by ``dynamic_targets``, so ANY later
    identical selector call within ``approval_ttl_minutes`` could claim an
    approval created (and possibly already orphaned) by a prior call and
    dispatch against topology re-resolved at that later moment -- exactly
    the reuse-across-a-time-gap ``remember_minutes`` is disabled to prevent,
    just via the queue instead of the remember-cache. A dynamic call must
    always mint and wait on its own entry instead.
    """
    args = {
        "selector": {"domain": "lock", "area_ids": ["entry"]},
        "action": "lock",
    }
    args_hash = compute_args_hash(args)
    policy = Policy(rules=[Rule(tool_name="ha_bulk_control", remember_minutes=5)])
    # Simulate an orphaned approval: some earlier call created this entry
    # and (per the fix) is the only one entitled to consume it -- but that
    # earlier call is not the one running below.
    entry = queue.create("ha_bulk_control", args_hash, args, ttl_minutes=5)
    queue.approve(entry.token)
    middleware = PolicyMiddleware(
        policy_provider=lambda: policy,
        queue=queue,
        wait_seconds=0,
    )
    call_next = AsyncMock(return_value="ok")

    with pytest.raises(ToolError):
        await middleware.on_call_tool(make_context("ha_bulk_control", args), call_next)

    call_next.assert_not_awaited()
    # The foreign approval is untouched -- this call minted its own
    # independent entry rather than consuming it (proven by call_next
    # never firing despite the foreign entry being pre-approved). With
    # wait_seconds=0 this call times out immediately, and per
    # _finalize_timed_out_pending a timed-out DYNAMIC entry is removed
    # before it raises (an unconsumable entry left behind is a ghost row
    # the settings UI would still offer), so only the foreign entry
    # remains -- and it is untouched.
    assert queue.get(entry.token) is not None
    assert queue.get(entry.token).decision == "approved"
    assert queue.list_pending() == []


@pytest.mark.anyio
async def test_dynamic_selector_race_never_double_dispatches(queue):
    """Two concurrent calls, one approval: only the approved one dispatches.

    Regression for the exact race in the PR review: call A creates entry E
    and blocks; the user approves E; if call B's on_call_tool reaches the
    (now-removed-for-dynamic) "find an already-approved entry" fast path
    before A's own wait wakes, both A and B would see "approved" and both
    would call_next. With per-invocation entries, A and B never share one
    entry, so approving A's does not affect B's independent one.
    """
    args = {
        "selector": {"domain": "lock", "area_ids": ["entry"]},
        "action": "lock",
    }
    policy = Policy(rules=[Rule(tool_name="ha_bulk_control")])
    middleware = PolicyMiddleware(
        policy_provider=lambda: policy,
        queue=queue,
        wait_seconds=5,
    )
    call_next = AsyncMock(return_value="ok")
    outcomes: list[str] = []

    async def call():
        try:
            result = await middleware.on_call_tool(
                make_context("ha_bulk_control", dict(args)), call_next
            )
            outcomes.append(result)
        except ToolError:
            outcomes.append("denied_or_pending")

    async def approve_one_when_both_pending():
        for _ in range(50):
            pending = queue.list_pending()
            if len(pending) >= 2:
                break
            await anyio.sleep(0.02)
        assert len(pending) == 2
        queue.approve(pending[0].token)
        queue.deny(pending[1].token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
        tg.start_soon(approve_one_when_both_pending)

    assert sorted(outcomes) == ["denied_or_pending", "ok"]
    call_next.assert_awaited_once()


@pytest.mark.anyio
async def test_concurrent_dynamic_selector_calls_get_independent_approvals(
    queue, monkeypatch: pytest.MonkeyPatch
):
    """Two concurrent identical selector calls must not share one pending entry.

    Sharing (via ``find_or_create``'s dedup) would let a single approval
    click release both waiters, so one click could dispatch the selector
    twice — or against two different resolutions if topology changed
    between them. Each invocation must mint its own pending approval.

    Captures the minted tokens via a wrapped ``queue.create`` rather than
    inspecting ``queue.list_pending()`` after the calls return: with
    ``wait_seconds=0`` both calls time out immediately, and a timed-out
    DYNAMIC entry is now removed before its call raises (see
    ``_finalize_timed_out_pending`` -- an unconsumable entry left behind
    is a ghost row the settings UI would still offer), so the queue is
    correctly empty by the time either call has returned.
    """
    args = {
        "selector": {"domain": "lock", "area_ids": ["entry"]},
        "action": "lock",
    }
    policy = Policy(rules=[Rule(tool_name="ha_bulk_control")])
    middleware = PolicyMiddleware(
        policy_provider=lambda: policy,
        queue=queue,
        wait_seconds=0,
    )
    call_next = AsyncMock()

    real_create = queue.create
    created_tokens: list[str] = []

    def _create_and_capture(*args_: object, **kwargs: object):
        entry = real_create(*args_, **kwargs)
        created_tokens.append(entry.token)
        return entry

    monkeypatch.setattr(queue, "create", _create_and_capture)

    async def call():
        with pytest.raises(ToolError):
            await middleware.on_call_tool(
                make_context("ha_bulk_control", dict(args)), call_next
            )

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)

    assert len(created_tokens) == 2
    assert created_tokens[0] != created_tokens[1]
    assert queue.list_pending() == []
    call_next.assert_not_awaited()


@pytest.mark.anyio
async def test_stringified_selector_argument_still_gates_correctly(queue):
    """A JSON-stringified `selector` (Claude Desktop stdio-style) must still
    be gated by a rule targeting a nested selector field.

    Regression for a real gap found in review: PolicyMiddleware reads
    ``context.message.arguments`` before the tool's own Pydantic
    ``JSON_STRING_COERCION`` runs (that happens later, inside call_next), so
    without normalizing here, a rule scoped to ``args.selector.domain ==
    "lock"`` would silently never match a lock-domain selector sent as a
    string, allowing it straight through.
    """
    policy = Policy(
        rules=[
            Rule(
                tool_name="ha_bulk_control",
                when=[Predicate(path="args.selector.domain", op="eq", value="lock")],
            )
        ]
    )
    middleware = PolicyMiddleware(
        policy_provider=lambda: policy,
        queue=queue,
        wait_seconds=0,
    )
    call_next = AsyncMock()
    # Simulates a client that stringifies the nested `selector` parameter
    # instead of sending a real JSON object, exactly as
    # tools/util_helpers.py's JSON_STRING_COERCION comment describes.
    stringified_args = {
        "selector": '{"domain": "lock", "area_ids": ["entry"]}',
        "action": "lock",
    }

    with pytest.raises(ToolError) as ei:
        await middleware.on_call_tool(
            make_context("ha_bulk_control", stringified_args), call_next
        )

    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    call_next.assert_not_awaited()


@pytest.mark.anyio
async def test_pre_approved_entry_consumed_and_call_proceeds(queue):
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    args = {"domain": "lock"}
    entry = queue.create(
        "ha_call_service", compute_args_hash(args), args, ttl_minutes=5
    )
    queue.approve(entry.token)
    call_next = AsyncMock(return_value="ok")
    result = await mw.on_call_tool(make_context("ha_call_service", args), call_next)
    assert result == "ok"
    assert queue.find("ha_call_service", compute_args_hash(args)) is None


# --- appended for Task 3.2: block / deny / timeout / re-call coverage ---


@pytest.mark.anyio
async def test_block_then_approve_returns_real_result(queue):
    pol = Policy(wait_seconds=5, rules=[Rule(tool_name="ha_call_service")])
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
            make_context("ha_call_service", {"domain": "lock"}), call_next
        )
    assert result == "real_result"


@pytest.mark.anyio
async def test_block_then_deny_raises_denied(queue):
    pol = Policy(wait_seconds=5, rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue)
    call_next = AsyncMock()

    # anyio's task group wraps unhandled task-side exceptions in
    # ExceptionGroup (PEP 654, Python 3.11+). Putting pytest.raises
    # AROUND the task group misses bare ToolError. Schedule the denier
    # in the task group, but keep the middleware call (the one that
    # raises) OUTSIDE — directly under pytest.raises — so the
    # exception type matches exactly.
    async def denier():
        # Poll for the pending entry instead of a fixed sleep so the
        # test isn't sensitive to scheduling jitter on slow CI runners.
        for _ in range(50):
            pending = queue.list_pending()
            if pending:
                queue.deny(pending[0].token)
                return
            await anyio.sleep(0.02)

    async with anyio.create_task_group() as tg:
        tg.start_soon(denier)
        with pytest.raises(ToolError) as ei:
            await mw.on_call_tool(
                make_context("ha_call_service", {"domain": "lock"}), call_next
            )
    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "USER_DENIED"
    call_next.assert_not_called()


@pytest.mark.anyio
async def test_timeout_raises_pending_error_and_keeps_entry(queue):
    """A non-dynamic (static) timeout must keep the recover-by-re-calling
    promise: the entry survives (a later re-call's find() can still pick
    it up), and the message says so. Asserting the exact sentence, not
    just its absence on the dynamic side (see
    test_dynamic_selector_pending_error_does_not_promise_a_working_recall),
    is what would catch the dynamic wording leaking into this branch too
    and silently breaking approve-then-retry for every gated static tool.
    """
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    call_next = AsyncMock()

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), call_next
        )
    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    # create_error_response splays context fields at top level, alongside `error`.
    assert body["token"]
    assert "Tool Security Policies" in body["error"]["message"]
    full_text = body["error"]["message"] + " ".join(body["error"]["suggestions"])
    assert "Re-call this tool with the same arguments after the user approves" in (
        full_text
    )
    call_next.assert_not_called()
    # entry survives for re-call
    assert queue.list_pending()
    assert queue.get(body["token"]) is not None


@pytest.mark.anyio
async def test_recall_after_approval_executes(queue):
    """The crucial property: LLM re-calls same tool+args → middleware consumes
    the now-approved entry and proceeds. Strict args-hash binding ensures
    a re-call with mutated args would NOT pick up this approval."""
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
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
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    call_next = AsyncMock(return_value="ok")

    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), call_next
        )
    first_pending = queue.list_pending()[0]
    queue.approve(first_pending.token)

    # mutated args → different hash → new pending, NOT approved
    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "alarm_control_panel"}),
            call_next,
        )
    call_next.assert_not_called()


@pytest.mark.anyio
async def test_pending_error_reports_remaining_not_total_ttl(queue):
    """``expires_in_seconds`` MUST be time-remaining, not total TTL.

    Before the fix this was always `(expires_at - created_at)` ==
    the configured TTL (e.g. 300s for a 5-minute window). The LLM
    would see a stale "you have 5 minutes" hint even on a re-call
    issued one minute before expiry. Rewind ``created_at`` so the
    "now" gap is unambiguously smaller than the full TTL.
    """
    from datetime import timedelta

    pol = Policy(
        approval_ttl_minutes=5,
        rules=[Rule(tool_name="ha_call_service")],
    )
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    call_next = AsyncMock()

    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), call_next
        )
    pending = queue.list_pending()[0]
    # Rewind both created_at AND expires_at by 4 minutes so only ~1
    # minute remains until expiry. With the old (broken) logic this
    # would still report 300s (TTL); with the fix it must report <300.
    pending.created_at -= timedelta(minutes=4)
    pending.expires_at -= timedelta(minutes=4)

    # Force a second pass that hits the pending-error path.
    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), call_next
        )
    body = json.loads(ei.value.args[0])
    remaining = body["expires_in_seconds"]
    # Full TTL is 300s; remaining should be ~60s and definitely <300.
    assert 0 <= remaining < 300, f"expected <300s remaining, got {remaining}"


@pytest.mark.anyio
async def test_corrupt_policy_fails_closed_with_structured_error(queue):
    """A corrupt policy file must raise POLICY_LOAD_FAILED, not pass through.

    Fail-closed posture: a corrupt or schema-invalid tool_policy.json
    is a security-relevant config error. Silently allowing every call
    while the user's rules sit unparsed on disk would be the wrong
    default for a security feature.
    """

    def broken_provider() -> Policy:
        raise ValueError("tool_policy.json failed schema validation: ...")

    mw = PolicyMiddleware(policy_provider=broken_provider, queue=queue)
    call_next = AsyncMock(return_value="should_not_run")

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), call_next
        )
    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "POLICY_LOAD_FAILED"
    call_next.assert_not_called()


@pytest.mark.anyio
async def test_deeply_nested_args_fail_closed_not_silently_allowed(queue):
    """A RecursionError while normalizing args must fail the call closed,
    not silently evaluate (and hash) the unnormalized args and ALLOW.

    Before this fix, normalize_stringified_containers swallowed its own
    RecursionError and returned the args unrepaired -- a rule scoped to a
    nested selector-inspectable field (e.g. args.selector.domain) would
    then never match against the deeply-nested-but-otherwise-real value,
    landing on ALLOW instead of the REQUIRE_APPROVAL a shallower version
    of the identical call would have gotten. Mirrors
    test_corrupt_policy_fails_closed_with_structured_error's pattern: a
    security gate that cannot safely evaluate must degrade loudly, not
    silently pass calls through.
    """
    import sys

    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue)
    call_next = AsyncMock(return_value="should_not_run")

    nested: object = "leaf"
    for _ in range(sys.getrecursionlimit() + 100):
        nested = {"nested": nested}

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock", "data": nested}),
            call_next,
        )
    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "POLICY_ARGS_TOO_DEEPLY_NESTED"
    call_next.assert_not_called()


@pytest.mark.anyio
async def test_remember_minutes_caches_for_subsequent_calls(queue):
    pol = Policy(
        rules=[
            Rule(tool_name="ha_call_service", remember_minutes=10),
        ],
    )
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=5)
    call_next = AsyncMock(return_value="ok")
    args = {"domain": "lock"}

    async def approver():
        await anyio.sleep(0.05)
        queue.approve(queue.list_pending()[0].token)

    result1: object = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(approver)
        result1 = await mw.on_call_tool(
            make_context("ha_call_service", args), call_next
        )
    assert result1 == "ok"

    # second call with same args proceeds via remember-cache without any pending entry
    result2 = await mw.on_call_tool(make_context("ha_call_service", args), call_next)
    assert result2 == "ok"
    assert queue.list_pending() == []


# --- Wave 4B: wait-loop event-wake timing + multi-rule precedence ---


@pytest.mark.anyio
async def test_wait_loop_wakes_on_event_not_polling(queue):
    """Verify the wait-loop exits on ``event.set()``, not on the 15s polling tick.

    With ``wait_seconds=30``, an approval fired at t=0.05 should resolve
    well before t=15 (the polling-fallback inner-loop interval). If
    someone removes the ``pending.wait()`` call and leaves only
    the ``move_on_after(15)`` polling fallback, the loop would block for
    the full 15s before the next iteration noticed the decision flip;
    this test catches that regression.
    """
    import time

    pol = Policy(wait_seconds=30, rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue)
    call_next = AsyncMock(return_value="ok")

    async def approver():
        await anyio.sleep(0.05)
        queue.approve(queue.list_pending()[0].token)

    start = time.monotonic()
    async with anyio.create_task_group() as tg:
        tg.start_soon(approver)
        result = await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), call_next
        )
    elapsed = time.monotonic() - start

    assert result == "ok"
    assert elapsed < 1.0, (
        f"Approval took {elapsed:.2f}s — event.wait() may have been bypassed"
    )


@pytest.mark.anyio
async def test_multi_rule_first_match_wins_for_remember_minutes(queue):
    """Two overlapping rules for the same tool — first match wins for ``remember_minutes``.

    Catches a regression where ``evaluate()`` and ``find_matching_rule()``
    drift apart on precedence ordering (e.g. one walks the list head-first
    and the other tail-first, or one short-circuits on a later rule).
    """
    pol = Policy(
        rules=[
            Rule(tool_name="ha_call_service", remember_minutes=10),  # first match
            Rule(tool_name="ha_call_service", remember_minutes=999),
        ],
    )
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=5)
    call_next = AsyncMock(return_value="ok")
    args = {"domain": "lock"}

    async def approver():
        await anyio.sleep(0.05)
        queue.approve(queue.list_pending()[0].token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(approver)
        await mw.on_call_tool(make_context("ha_call_service", args), call_next)

    # is_remembered reflects the FIRST rule's 10-minute window, not 999.
    args_hash = compute_args_hash(args)
    assert queue.is_remembered("ha_call_service", args_hash) is True

    # Internal: remember-until should be ~10 min in the future, not 999.
    # Reaching into ``_remember`` is acceptable for this precedence test —
    # other tests in this file (e.g. the TTL-rewind test) take a similar
    # approach for properties not exposed on the public surface.
    from datetime import UTC, datetime, timedelta

    remember_until = queue._remember[("ha_call_service", args_hash)]
    delta = remember_until - datetime.now(UTC)
    assert delta < timedelta(minutes=20), (
        f"remember window was {delta}; expected ~10min (first rule), not 999min (second rule)"
    )


@pytest.mark.anyio
async def test_swept_pending_during_wait_is_reissued_with_fresh_token(queue):
    """If the queue sweeper evicts the pending entry while the
    middleware is in _wait_for_decision, the post-wait reissue branch
    must create a new entry so the LLM's next re-call has a real
    token to land on. Without this, the user would be told to
    re-call with a dead token."""
    from datetime import UTC, datetime, timedelta

    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
    call_next = AsyncMock()

    # Pre-create the entry so we can rewind its expiry — then the
    # wait-loop's exit will run _sweep_expired() via queue.find() and
    # remove the row, exercising the reissue branch.
    args = {"domain": "lock"}
    pre = queue.create("ha_call_service", compute_args_hash(args), args, ttl_minutes=5)
    original_token = pre.token
    pre.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(make_context("ha_call_service", args), call_next)
    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    # New token issued — the old one is gone and the body carries the
    # fresh one so the LLM's re-call won't hit a dead row.
    assert body["token"] != original_token
    assert queue.get(body["token"]) is not None, (
        "reissue branch must create a fresh, queryable entry"
    )
    assert queue.get(original_token) is None


@pytest.mark.anyio
async def test_dynamic_selector_pending_error_does_not_promise_a_working_recall(queue):
    """A dynamic selector's timeout error must not tell the agent to
    re-call after approval -- that instruction is false for a dynamic
    entry (creator-only binding means a re-call can never consume it) and
    following it produces an approve-and-be-asked-again loop: the user
    approves the row they can see, the re-call ignores it and mints a new
    one instead.

    Also pins the fix for the OTHER half of that same bug: the entry
    itself must actually be gone from the queue by the time this message
    is seen, not just described as gone -- an entry left behind is a row
    the settings UI still offers and ``ApprovalQueue.approve(token)``
    would still accept, doing nothing because nothing is left holding
    this call's ``pending`` reference to act on the decision. This is the
    NORMAL timeout path (this call's own wait window elapsing), not the
    sweeper-eviction path ``test_dynamic_selector_evicted_during_wait_is_not_reissued``
    covers.
    """
    args = {
        "selector": {"domain": "lock", "area_ids": ["entry"]},
        "action": "lock",
    }
    policy = Policy(rules=[Rule(tool_name="ha_bulk_control")])
    middleware = PolicyMiddleware(
        policy_provider=lambda: policy,
        queue=queue,
        wait_seconds=0,
    )
    call_next = AsyncMock()

    with pytest.raises(ToolError) as ei:
        await middleware.on_call_tool(make_context("ha_bulk_control", args), call_next)

    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    full_text = body["error"]["message"] + " ".join(body["error"]["suggestions"])
    assert "Re-call this tool with the same arguments after the user approves" not in (
        full_text
    )
    # Nor does it describe a still-open window: the wait already closed by
    # the time this message is built (the only call site is reached after
    # _wait_for_decision returns with no decision).
    assert "before this call's wait window closes" not in full_text
    assert queue.get(body["token"]) is None
    assert queue.list_pending() == []
    assert "single-use" in full_text or "bound to this specific call" in full_text
    # The static path's own remaining-TTL context key must not leak in
    # here: nothing is left with a live countdown for a removed, single-use
    # entry, and create_error_response splays context fields (including
    # this one) to the top level, outside the message/suggestions text the
    # assertions above already check -- a refactor hoisting `remaining`
    # back into the shared context would restore a misleading countdown
    # here while every text-based assertion above kept passing.
    assert "expires_in_seconds" not in body


@pytest.mark.anyio
async def test_dynamic_selector_evicted_during_wait_is_not_reissued(
    queue, monkeypatch: pytest.MonkeyPatch
):
    """Unlike a static tool's pending entry, a swept dynamic entry must NOT
    be reissued -- no future re-call can ever discover a dynamic entry by
    lookup (creator-only binding), so a reissued row would be just as
    unreachable as the one it replaced, silently burning a queue slot.

    Forces the eviction by wrapping the queue's own create() to immediately
    expire whatever it just created -- the middleware has no seam to
    pre-create its own dynamic entry the way the static-path sibling test
    (test_swept_pending_during_wait_is_reissued_with_fresh_token) does.
    """
    from datetime import UTC, datetime, timedelta

    args = {
        "selector": {"domain": "lock", "area_ids": ["entry"]},
        "action": "lock",
    }
    policy = Policy(rules=[Rule(tool_name="ha_bulk_control")])
    middleware = PolicyMiddleware(
        policy_provider=lambda: policy,
        queue=queue,
        wait_seconds=0,
    )
    call_next = AsyncMock()

    real_create = queue.create
    created_tokens: list[str] = []

    def _create_then_expire(*args_: object, **kwargs: object):
        entry = real_create(*args_, **kwargs)
        entry.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        created_tokens.append(entry.token)
        return entry

    monkeypatch.setattr(queue, "create", _create_then_expire)

    with pytest.raises(ToolError) as ei:
        await middleware.on_call_tool(make_context("ha_bulk_control", args), call_next)

    body = json.loads(ei.value.args[0])
    assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
    # Exactly one entry was ever created for this call -- the reissue
    # branch, if it fired, would have called create() a second time.
    assert len(created_tokens) == 1
    assert body["token"] == created_tokens[0]
    assert queue.get(created_tokens[0]) is None
    call_next.assert_not_awaited()


class TestApprovalManagementExemption:
    """Queue-management dev-tool actions must never themselves be gated.

    With a wildcard (or ha_dev_manage_server) rule, gating "approve" would
    create a SECOND pending entry instead of deciding the first — an
    MCP-only approval workflow would deadlock (Codex #1993 P1).
    """

    def test_queue_management_actions_exempt(self):
        from ha_mcp.tool_dispatch import is_approval_management_call

        for action in ("list_pending", "approve", "deny"):
            assert is_approval_management_call(
                "ha_dev_manage_server", {"action": action, "token": "t"}
            )

    def test_other_actions_and_tools_not_exempt(self):
        from ha_mcp.tool_dispatch import is_approval_management_call

        for action in ("info", "update_source", "restart"):
            assert not is_approval_management_call(
                "ha_dev_manage_server", {"action": action}
            )
        assert not is_approval_management_call(
            "ha_dev_manage_settings", {"action": "approve"}
        )
        assert not is_approval_management_call("ha_call_service", {"action": "approve"})
        assert not is_approval_management_call("ha_dev_manage_server", {})


class TestApprovalManagementExemptionMiddleware:
    """Drive the exemption through ``on_call_tool`` itself, not just the pure
    ``is_approval_management_call`` helper. Under a wildcard policy the queue-
    management actions must reach ``call_next`` (so an MCP-only approval flow
    can decide the first pending entry), while the high-stakes actions on the
    same tool stay gated (Codex #1993 P1)."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("action", ["list_pending", "approve", "deny"])
    async def test_queue_management_action_passes_through(self, queue, action):
        pol = Policy(rules=[Rule(tool_name="*")])
        mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
        call_next = AsyncMock(return_value="passed")
        result = await mw.on_call_tool(
            make_context("ha_dev_manage_server", {"action": action, "token": "t"}),
            call_next,
        )
        assert result == "passed"
        call_next.assert_awaited_once()

    @pytest.mark.anyio
    @pytest.mark.parametrize("action", ["update_source", "restart"])
    async def test_non_management_action_stays_gated(self, queue, action):
        pol = Policy(rules=[Rule(tool_name="*")])
        mw = PolicyMiddleware(policy_provider=lambda: pol, queue=queue, wait_seconds=0)
        call_next = AsyncMock(return_value="should_not_run")
        with pytest.raises(ToolError) as ei:
            await mw.on_call_tool(
                make_context("ha_dev_manage_server", {"action": action}), call_next
            )
        body = json.loads(ei.value.args[0])
        assert body["error"]["code"] == "USER_APPROVAL_REQUIRED"
        call_next.assert_not_called()


# --- Issue #2387: one approval must authorize exactly one execution ---


async def _attach_counting_find_or_create(queue, monkeypatch, counter: list[int]):
    """Wrap ``queue.find_or_create`` so a test can tell when both calls
    have attached to the shared pending entry.

    Approving on a timer instead would be racy: the approval could land
    before the second call reached ``find_or_create``, in which case it
    would take the already-approved fast path rather than the
    two-waiters-on-one-entry path these tests exist to cover.
    """
    real = queue.find_or_create

    async def counting(*args, **kwargs):
        entry = await real(*args, **kwargs)
        counter.append(1)
        return entry

    monkeypatch.setattr(queue, "find_or_create", counting)


async def _wait_until_attached(counter: list[int], expected: int) -> None:
    for _ in range(500):
        if len(counter) >= expected:
            return
        await anyio.sleep(0.01)
    raise AssertionError(
        f"only {len(counter)} of {expected} calls attached to the shared entry"
    )


@pytest.mark.anyio
async def test_concurrent_static_calls_consume_one_approval_once(
    queue, monkeypatch: pytest.MonkeyPatch
):
    """Two concurrent identical static calls share one row; one click runs one call.

    Regression for #2387. ``find_or_create`` deliberately folds identical
    static calls onto a single approval row so the user sees one prompt.
    Before the claim check, ``decide()`` woke every waiter and each one
    consumed the same entry and dispatched — one click, N executions, for
    a non-idempotent call such as ``button.press`` or ``script.turn_on``.
    """
    args = {"domain": "lock", "service": "unlock"}
    policy = Policy(rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: policy, queue=queue, wait_seconds=5)
    call_next = AsyncMock(return_value="ok")
    attached: list[int] = []
    outcomes: list[object] = []
    codes: list[str] = []
    approved_token: list[str] = []

    await _attach_counting_find_or_create(queue, monkeypatch, attached)

    async def call():
        try:
            outcomes.append(
                await mw.on_call_tool(
                    make_context("ha_call_service", dict(args)), call_next
                )
            )
        except ToolError as exc:
            codes.append(json.loads(exc.args[0])["error"]["code"])
            outcomes.append("error")

    async def approve_the_shared_row():
        await _wait_until_attached(attached, 2)
        pending = queue.list_pending()
        assert len(pending) == 1, "identical static calls must share one row"
        approved_token.append(pending[0].token)
        queue.approve(pending[0].token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
        tg.start_soon(approve_the_shared_row)

    call_next.assert_awaited_once()
    assert sorted(str(o) for o in outcomes) == ["error", "ok"]
    assert codes == ["USER_APPROVAL_REQUIRED"]
    # The loser gets its own approval to wait on rather than silently
    # riding the winner's click.
    leftover = queue.list_pending()
    assert len(leftover) == 1
    assert leftover[0].token != approved_token[0]


@pytest.mark.anyio
async def test_remembered_window_lets_the_losing_waiter_proceed(
    queue, monkeypatch: pytest.MonkeyPatch
):
    """A positive ``remember_minutes`` still authorizes every identical call.

    The claim check must not re-prompt here: the winner's consumption
    populates the remember-cache, and that window is exactly the user
    saying "don't ask me again for this call" — so the losing waiter
    proceeds instead of minting a second row.
    """
    args = {"domain": "lock", "service": "unlock"}
    policy = Policy(rules=[Rule(tool_name="ha_call_service", remember_minutes=10)])
    mw = PolicyMiddleware(policy_provider=lambda: policy, queue=queue, wait_seconds=5)
    call_next = AsyncMock(return_value="ok")
    attached: list[int] = []
    outcomes: list[object] = []

    await _attach_counting_find_or_create(queue, monkeypatch, attached)

    async def call():
        outcomes.append(
            await mw.on_call_tool(
                make_context("ha_call_service", dict(args)), call_next
            )
        )

    async def approve_the_shared_row():
        await _wait_until_attached(attached, 2)
        pending = queue.list_pending()
        assert len(pending) == 1
        queue.approve(pending[0].token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
        tg.start_soon(approve_the_shared_row)

    assert outcomes == ["ok", "ok"]
    assert call_next.await_count == 2
    assert queue.list_pending() == []


@pytest.mark.anyio
async def test_losing_waiters_share_one_replacement_row(
    queue, monkeypatch: pytest.MonkeyPatch
):
    """Three concurrent identical calls: one dispatch, one replacement row.

    A losing waiter mints its row through ``find_or_create`` like any
    other call, so a burst does not fan the queue out to one row per
    caller -- draining N identical calls costs N clicks across N rounds,
    never N simultaneous rows.

    Two callers cannot pin that bound: there is exactly one loser, so
    ``len(...) == 1`` holds whether losers share a row or each mint their
    own. Three callers discriminate, which is what keeps a later refactor
    from quietly routing the loser path around ``find_or_create``.
    """
    args = {"domain": "lock", "service": "unlock"}
    policy = Policy(rules=[Rule(tool_name="ha_call_service")])
    mw = PolicyMiddleware(policy_provider=lambda: policy, queue=queue, wait_seconds=5)
    call_next = AsyncMock(return_value="ok")
    attached: list[int] = []
    outcomes: list[object] = []
    codes: list[str] = []
    approved_token: list[str] = []

    await _attach_counting_find_or_create(queue, monkeypatch, attached)

    async def call():
        try:
            outcomes.append(
                await mw.on_call_tool(
                    make_context("ha_call_service", dict(args)), call_next
                )
            )
        except ToolError as exc:
            codes.append(json.loads(exc.args[0])["error"]["code"])
            outcomes.append("error")

    async def approve_the_shared_row():
        await _wait_until_attached(attached, 3)
        pending = queue.list_pending()
        assert len(pending) == 1, "identical static calls must share one row"
        approved_token.append(pending[0].token)
        queue.approve(pending[0].token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
        tg.start_soon(call)
        tg.start_soon(approve_the_shared_row)

    call_next.assert_awaited_once()
    assert sorted(str(o) for o in outcomes) == ["error", "error", "ok"]
    assert codes == ["USER_APPROVAL_REQUIRED"] * 2
    # Both losers land on ONE replacement row, not one row each.
    leftover = queue.list_pending()
    assert len(leftover) == 1
    assert leftover[0].token != approved_token[0]


# --- Home Assistant event surface ---


@pytest.mark.anyio
async def test_gated_call_announces_the_pending_request(queue):
    """The whole point of #2502: the queue must be visible outside the UI."""
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    client = AsyncMock()
    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=lambda: client,
    )

    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), AsyncMock()
        )

    client.fire_event.assert_awaited_once()
    event_type, payload = client.fire_event.await_args.args
    assert event_type == "ha_mcp_approval_requested"
    assert payload["tool_name"] == "ha_call_service"
    assert payload["args"] == {"domain": "lock"}
    assert payload["matched_rule"]["tool_name"] == "ha_call_service"
    assert payload["token"] == queue.list_pending()[0].token


@pytest.mark.anyio
async def test_a_shared_pending_row_is_announced_once(queue):
    """A later identical call joins the existing row (``find_or_create``).

    The user was already told about that request, so a second event would
    duplicate the notification while carrying the same token.
    """
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    client = AsyncMock()
    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=lambda: client,
    )

    for _ in range(2):
        with pytest.raises(ToolError):
            await mw.on_call_tool(
                make_context("ha_call_service", {"domain": "lock"}), AsyncMock()
            )

    assert len(queue.list_pending()) == 1
    client.fire_event.assert_awaited_once()


@pytest.mark.anyio
async def test_a_failing_announcement_still_gates_the_call(queue):
    """Delivery is best effort; the gate is not."""
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    client = AsyncMock()
    client.fire_event.side_effect = RuntimeError("HA unreachable")
    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=lambda: client,
    )
    call_next = AsyncMock()

    with pytest.raises(ToolError) as ei:
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), call_next
        )

    assert json.loads(ei.value.args[0])["error"]["code"] == "USER_APPROVAL_REQUIRED"
    call_next.assert_not_called()


@pytest.mark.anyio
async def test_a_client_that_cannot_be_resolved_does_not_burn_the_announcement(queue):
    """A failed client lookup must leave the entry announceable.

    ``mark_notified`` is one-shot, so consuming it before the client is in
    hand would spend the entry's only announcement on a client that never
    materialised -- and the reissue path could not announce it either.
    """
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    client = AsyncMock()
    calls: list[int] = []

    def flaky_client():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("client not ready")
        return client

    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=flaky_client,
    )

    for _ in range(2):
        with pytest.raises(ToolError):
            await mw.on_call_tool(
                make_context("ha_call_service", {"domain": "lock"}), AsyncMock()
            )

    client.fire_event.assert_awaited_once()


@pytest.mark.anyio
async def test_a_reissued_entry_is_announced_with_its_new_token(queue):
    """An entry evicted during the wait is reissued -- under a new token.

    The token in the first event is dead at that point, so an automation
    holding it has nothing to act on; the replacement has to be announced
    or the notification path goes quiet for that request.
    """
    pol = Policy(
        rules=[Rule(tool_name="ha_call_service")],
        approval_ttl_minutes=1,
        wait_seconds=30,
    )
    client = AsyncMock()
    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=lambda: client,
    )

    # Expire the entry the call is waiting on, exactly as the queue's
    # sweeper would when the TTL elapses mid-wait.
    async def expire_during_wait(context, pending, wait_seconds):
        pending.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        queue._sweep_expired()

    mw._wait_for_decision = expire_during_wait  # type: ignore[method-assign]

    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), AsyncMock()
        )

    assert client.fire_event.await_count == 2
    first, second = (
        call.args[1]["token"] for call in client.fire_event.await_args_list
    )
    assert first != second
    assert second == queue.list_pending()[0].token


@pytest.mark.anyio
async def test_each_replacement_row_in_a_burst_is_announced(
    queue, monkeypatch: pytest.MonkeyPatch
):
    """A waiter that loses the claim mints a new row -- and announces it.

    Without this the losers' replacement row exists in the settings UI but
    never reaches the event bus, so a user driving approvals from a
    notification sees the first request and nothing after it.
    """
    args = {"domain": "lock", "service": "unlock"}
    policy = Policy(rules=[Rule(tool_name="ha_call_service")])
    client = AsyncMock()
    mw = PolicyMiddleware(
        policy_provider=lambda: policy,
        queue=queue,
        wait_seconds=5,
        get_client=lambda: client,
    )
    call_next = AsyncMock(return_value="ok")
    attached: list[int] = []

    await _attach_counting_find_or_create(queue, monkeypatch, attached)

    async def call():
        try:
            await mw.on_call_tool(
                make_context("ha_call_service", dict(args)), call_next
            )
        except ToolError:
            # Expected on the caller that loses the claim: it raises
            # "approval pending" rather than dispatching. This test asserts
            # on what was announced, not on either call's own outcome.
            pass

    async def approve_the_shared_row():
        await _wait_until_attached(attached, 2)
        pending = queue.list_pending()
        assert len(pending) == 1
        queue.approve(pending[0].token)

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
        tg.start_soon(approve_the_shared_row)

    announced = [call.args[1]["token"] for call in client.fire_event.await_args_list]
    assert len(announced) == 2, "the replacement row was never announced"
    assert len(set(announced)) == 2
    assert announced[1] == queue.list_pending()[0].token


@pytest.mark.anyio
async def test_a_selector_call_is_announced_as_single_use(queue):
    """A dynamic-selector gate announces no expiry it cannot honour."""
    pol = Policy(rules=[Rule(tool_name="ha_bulk_control")])
    client = AsyncMock()
    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=lambda: client,
    )

    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_bulk_control", {"selector": {"domain": "light"}}),
            AsyncMock(),
        )

    client.fire_event.assert_awaited_once()
    payload = client.fire_event.await_args.args[1]
    assert payload["single_use"] is True
    assert "expires_at" not in payload


@pytest.mark.anyio
async def test_a_removed_selector_entry_is_never_announced(queue):
    """The single-use entry is gone by the time the retry branch runs.

    Announcing there would hand an automation a token the queue no longer
    knows — reachable whenever the first announcement found no client and
    so left the one-shot unspent.
    """
    pol = Policy(rules=[Rule(tool_name="ha_bulk_control")])
    client = AsyncMock()
    calls: list[int] = []

    def client_missing_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("client not ready")
        return client

    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=client_missing_once,
    )

    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_bulk_control", {"selector": {"domain": "light"}}),
            AsyncMock(),
        )

    client.fire_event.assert_not_awaited()
    # Nothing survives the call on this path, so there is no token an
    # announcement could have pointed a listener at.
    assert queue.list_pending() == []


@pytest.mark.anyio
async def test_the_decision_channel_opens_before_the_request_is_announced(queue):
    """Order matters: a response to an event nobody is subscribed to is lost.

    The listener is what turns the announcement into something answerable,
    so it has to be up before the announcement goes out -- an automation
    that fires its response the moment the notification lands would
    otherwise race the subscription and lose.
    """
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    order: list[str] = []
    client = AsyncMock()
    client.fire_event.side_effect = lambda *a, **k: order.append("announced")

    async def ensure() -> None:
        order.append("subscribed")

    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=lambda: client,
        ensure_decisions_listener=ensure,
    )

    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), AsyncMock()
        )

    # Further "subscribed" entries may follow: with wait_seconds=0 this call
    # passes the announce path twice, and the channel attempt is deliberately
    # not tied to the one-shot that suppresses the second event.
    assert order[:2] == ["subscribed", "announced"]
    assert order.count("announced") == 1


@pytest.mark.anyio
async def test_a_failing_decision_channel_still_announces(queue, caplog):
    """A request answerable only in the settings UI beats no request at all."""
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    client = AsyncMock()

    async def ensure() -> None:
        raise RuntimeError("no websocket")

    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=lambda: client,
        ensure_decisions_listener=ensure,
    )

    with caplog.at_level(logging.WARNING), pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), AsyncMock()
        )

    client.fire_event.assert_awaited_once()
    assert "approval-response channel" in caplog.text


@pytest.mark.anyio
async def test_an_identical_retry_tries_the_channel_again(queue):
    """A failed subscription has to be recoverable by the retry that follows.

    Identical calls share one queue row, and that row is announced once.
    While channel recovery hung off the same one-shot, a subscription that
    failed on the first call could never be retried for the request that is
    actually waiting: the retry reuses the entry, finds it already
    announced, and used to return before trying. Recovery then depended on
    some future, different request turning up.
    """
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    client = AsyncMock()
    attempts: list[str] = []

    async def ensure() -> None:
        attempts.append("tried")
        if len(attempts) == 1:
            raise RuntimeError("no websocket")

    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=lambda: client,
        ensure_decisions_listener=ensure,
    )

    for _ in range(2):
        with pytest.raises(ToolError):
            await mw.on_call_tool(
                make_context("ha_call_service", {"domain": "lock"}), AsyncMock()
            )

    assert len(attempts) > 1
    # The dedup it must not break: one request, one notification, however
    # many times the same call comes back.
    assert client.fire_event.await_count == 1


@pytest.mark.anyio
async def test_a_channel_lost_after_the_announcement_is_reopened(queue):
    """A reconnect drops the subscription; the entry stays announced.

    The two failures are independent, so the announcement latch must not
    decide whether the channel is looked at again. The listener itself
    re-subscribes when it sees a dead connection -- it only ever gets the
    chance if something calls it.
    """
    pol = Policy(rules=[Rule(tool_name="ha_call_service")])
    client = AsyncMock()
    attempts: list[str] = []

    async def ensure() -> None:
        attempts.append("tried")

    mw = PolicyMiddleware(
        policy_provider=lambda: pol,
        queue=queue,
        wait_seconds=0,
        get_client=lambda: client,
        ensure_decisions_listener=ensure,
    )

    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), AsyncMock()
        )
    announced = len(attempts)

    # The connection dies here, invisibly. The same call comes back.
    with pytest.raises(ToolError):
        await mw.on_call_tool(
            make_context("ha_call_service", {"domain": "lock"}), AsyncMock()
        )

    assert len(attempts) > announced
    assert client.fire_event.await_count == 1
