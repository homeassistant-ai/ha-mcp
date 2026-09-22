"""Test deciding a pending approval from the Home Assistant event bus.

Both gates are load-bearing and neither is checked at subscription time, so
each is exercised against a live-looking event: the toggle, and the PIN.
The limiter is what keeps the second one from being guessed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.decision_pin import PIN_FILENAME, set_pin
from ha_mcp.policy.decisions import (
    APPROVAL_RESPONSE_EVENT,
    ApprovalResponseListener,
    FailedAttemptLimiter,
)
from ha_mcp.policy.model import Policy

PIN = "2468"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ha_mcp.policy.decision_pin.HASH_ITERATIONS", 1000)


@pytest.fixture
def short_setup_budget(monkeypatch: pytest.MonkeyPatch) -> float:
    """Shrink the channel-setup budget so a stall test costs milliseconds.

    The production value is a user-facing delay, not a number under test:
    what is under test is that the cap exists and that the caller comes
    back from it.
    """
    budget = 0.2
    monkeypatch.setattr("ha_mcp.policy.decisions.SETUP_BUDGET_SECONDS", budget)
    return budget


@pytest.fixture
def queue() -> ApprovalQueue:
    return ApprovalQueue()


def make_ws_client() -> MagicMock:
    # spec'd on the real client: an attribute the production code reads but
    # the client does not have (is_ready vs is_connected) has to fail here,
    # not be invented by the mock and swallowed at runtime by the
    # best-effort handler around the subscribe.
    client = MagicMock(spec=HomeAssistantWebSocketClient)
    client.is_connected = True
    client.subscribe_events = AsyncMock(return_value=7)
    client.unsubscribe_events = AsyncMock()
    return client


def make_listener(
    tmp_path,
    queue: ApprovalQueue,
    *,
    enabled: bool = True,
    client: MagicMock | None = None,
    limiter: FailedAttemptLimiter | None = None,
    results: list[dict] | None = None,
) -> tuple[ApprovalResponseListener, MagicMock]:
    ws = client or make_ws_client()

    async def _record(
        token: str,
        decision: str,
        *,
        applied: bool,
        reason: str,
        tool_name: str | None = None,
    ) -> None:
        assert results is not None
        results.append(
            {
                "token": token,
                "decision": decision,
                "applied": applied,
                "reason": reason,
                "tool_name": tool_name,
            }
        )

    listener = ApprovalResponseListener(
        policy_provider=lambda: Policy(event_decisions_enabled=enabled),
        queue=queue,
        data_dir=tmp_path,
        get_ws_client=AsyncMock(return_value=ws),
        limiter=limiter,
        emit_result=_record if results is not None else None,
    )
    return listener, ws


def response_event(token: str, decision: str = "approve", pin: Any = PIN) -> dict:
    return {
        "event_type": APPROVAL_RESPONSE_EVENT,
        "data": {"token": token, "decision": decision, "pin": pin},
    }


@pytest.mark.anyio
async def test_a_correct_pin_approves_the_request(tmp_path, queue):
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue)

    await listener._handle_event(response_event(entry.token))

    assert queue.get(entry.token).decision == "approved"


@pytest.mark.anyio
async def test_a_correct_pin_denies_the_request(tmp_path, queue):
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue)

    await listener._handle_event(response_event(entry.token, decision="deny"))

    assert queue.get(entry.token).decision == "denied"


@pytest.mark.anyio
async def test_the_toggle_being_off_decides_nothing(tmp_path, queue):
    """Off is enforced per event, not by whether a subscription exists."""
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue, enabled=False)

    await listener._handle_event(response_event(entry.token))

    assert queue.get(entry.token).decision == "pending"


@pytest.mark.anyio
async def test_no_stored_pin_decides_nothing(tmp_path, queue):
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue)

    await listener._handle_event(response_event(entry.token))

    assert queue.get(entry.token).decision == "pending"


@pytest.mark.anyio
async def test_a_wrong_pin_decides_nothing(tmp_path, queue):
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue)

    await listener._handle_event(response_event(entry.token, pin="1111"))

    assert queue.get(entry.token).decision == "pending"


@pytest.mark.anyio
async def test_an_event_without_a_pin_is_not_a_guess(tmp_path, queue):
    """No PIN in the event is a badly written automation, not an attempt.

    Same reasoning as the malformed and unknown-token cases: an automation
    that forgets the field would otherwise spend the whole budget and close
    the channel on the user's own notification action.
    """
    set_pin(tmp_path, PIN)
    limiter = FailedAttemptLimiter(max_attempts=2)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue, limiter=limiter)

    for _ in range(5):
        await listener._handle_event(
            {"data": {"token": entry.token, "decision": "approve"}}
        )

    assert limiter.blocked() is False
    await listener._handle_event(response_event(entry.token))
    assert queue.get(entry.token).decision == "approved"


@pytest.mark.anyio
async def test_a_missing_pin_field_decides_nothing(tmp_path, queue):
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue)

    await listener._handle_event(
        {"data": {"token": entry.token, "decision": "approve"}}
    )

    assert queue.get(entry.token).decision == "pending"


@pytest.mark.anyio
async def test_the_pin_never_reaches_the_log(tmp_path, queue, caplog):
    """Refusals are logged; the secret in the refused event is not."""
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue)

    with caplog.at_level(logging.DEBUG):
        await listener._handle_event(response_event(entry.token, pin="wrong-pin-here"))
        await listener._handle_event(response_event(entry.token))

    assert "wrong-pin-here" not in caplog.text
    assert PIN not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "data",
    [
        None,
        "not-a-dict",
        {"decision": "approve", "pin": PIN},
        {"token": "", "decision": "approve", "pin": PIN},
        {"token": 17, "decision": "approve", "pin": PIN},
        {"token": "t", "decision": "maybe", "pin": PIN},
        {"token": "t", "pin": PIN},
    ],
    ids=[
        "no-data",
        "data-not-object",
        "no-token",
        "empty-token",
        "token-not-string",
        "bad-decision",
        "no-decision",
    ],
)
async def test_a_malformed_event_decides_nothing(tmp_path, queue, data):
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue)

    await listener._handle_event({"data": data})

    assert queue.get(entry.token).decision == "pending"


@pytest.mark.anyio
async def test_a_malformed_event_is_not_a_guess(tmp_path, queue):
    """Garbage from a broken automation must not spend the PIN budget."""
    set_pin(tmp_path, PIN)
    limiter = FailedAttemptLimiter(max_attempts=2)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue, limiter=limiter)

    for _ in range(5):
        await listener._handle_event({"data": {"token": "", "decision": "approve"}})

    assert limiter.blocked() is False
    await listener._handle_event(response_event(entry.token))
    assert queue.get(entry.token).decision == "approved"


@pytest.mark.anyio
async def test_an_unknown_token_with_the_right_pin_is_not_a_guess(tmp_path, queue):
    """The PIN was right, so nothing was guessed -- whatever the token was."""
    set_pin(tmp_path, PIN)
    limiter = FailedAttemptLimiter(max_attempts=2)
    listener, _ = make_listener(tmp_path, queue, limiter=limiter)

    for _ in range(5):
        await listener._handle_event(response_event("no-such-token"))

    assert limiter.blocked() is False


@pytest.mark.anyio
async def test_a_wrong_pin_counts_even_with_an_unknown_token(tmp_path, queue):
    """Authorisation runs before the token is looked up, so it counts.

    The token is the thing being guessed at. If an invented one bought a
    free attempt, the budget would bound nothing: an attacker would spend
    wrong PINs against tokens they made up and never be blocked.
    """
    set_pin(tmp_path, PIN)
    limiter = FailedAttemptLimiter(max_attempts=2)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue, limiter=limiter)

    for _ in range(2):
        await listener._handle_event(response_event("no-such-token", pin="1111"))

    assert limiter.blocked() is True
    # And the channel really is closed now, for the real token too.
    await listener._handle_event(response_event(entry.token))
    assert queue.get(entry.token).decision == "pending"


@pytest.mark.anyio
async def test_an_expired_entry_is_not_approved(tmp_path, queue):
    """The TTL binds a decision from the bus as it binds one from the tab.

    The settings UI reads the queue before approving, which expires the
    entry on the way in. An event does not, so without the sweep in
    ``approve`` a request whose window closed while its caller was
    retrying could still be approved -- waking that retry and dispatching
    the tool well after the entry should have been gone. Nothing polls the
    queue here, because a poll would expire the entry itself and hide the
    defect.
    """
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    entry.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    listener, _ = make_listener(tmp_path, queue)

    await listener._handle_event(response_event(entry.token))

    assert entry.decision == "pending"


@pytest.mark.anyio
async def test_an_expired_entry_is_not_denied(tmp_path, queue):
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    entry.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    listener, _ = make_listener(tmp_path, queue)

    await listener._handle_event(response_event(entry.token, decision="deny"))

    assert entry.decision == "pending"


@pytest.mark.anyio
async def test_an_unusable_stored_record_decides_nothing_and_costs_nothing(
    tmp_path, queue
):
    """A broken PIN file is a configuration problem, not a wrong guess.

    Nobody can type a PIN that matches ``{}``, so charging the budget for
    the attempt would close the channel over something no responder could
    have got right -- and the repair is to set the PIN again, which the
    refusal has to point at rather than at the PIN that was typed.
    """
    (tmp_path / PIN_FILENAME).write_text("{}", encoding="utf-8")
    limiter = FailedAttemptLimiter(max_attempts=2)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue, limiter=limiter)

    for _ in range(5):
        await listener._handle_event(response_event(entry.token))

    assert queue.get(entry.token).decision == "pending"
    assert limiter.blocked() is False
    # Setting a real PIN repairs it without any window to wait out.
    set_pin(tmp_path, PIN)
    await listener._handle_event(response_event(entry.token))
    assert queue.get(entry.token).decision == "approved"


@pytest.mark.anyio
async def test_a_missing_pin_is_not_a_guess(tmp_path, queue):
    """Nothing to guess against yet, so the budget stays whole.

    Otherwise a user switching the feature on would find the channel
    already locked out by the events their automation fired while they
    were still setting the PIN.
    """
    limiter = FailedAttemptLimiter(max_attempts=2)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue, limiter=limiter)

    for _ in range(5):
        await listener._handle_event(response_event(entry.token))

    assert limiter.blocked() is False
    set_pin(tmp_path, PIN)
    await listener._handle_event(response_event(entry.token))
    assert queue.get(entry.token).decision == "approved"


@pytest.mark.anyio
async def test_wrong_pins_close_the_channel(tmp_path, queue):
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(
        tmp_path, queue, limiter=FailedAttemptLimiter(max_attempts=3)
    )

    for _ in range(3):
        await listener._handle_event(response_event(entry.token, pin="1111"))
    # The right PIN now, and it still decides nothing: the budget is spent.
    await listener._handle_event(response_event(entry.token))

    assert queue.get(entry.token).decision == "pending"


@pytest.mark.anyio
async def test_a_correct_pin_clears_the_failures(tmp_path, queue):
    set_pin(tmp_path, PIN)
    limiter = FailedAttemptLimiter(max_attempts=3)
    first = queue.create("ha_call_service", "h1", {}, ttl_minutes=5)
    second = queue.create("ha_call_service", "h2", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue, limiter=limiter)

    for _ in range(2):
        await listener._handle_event(response_event(first.token, pin="1111"))
    await listener._handle_event(response_event(first.token))
    for _ in range(2):
        await listener._handle_event(response_event(second.token, pin="1111"))

    assert limiter.blocked() is False
    await listener._handle_event(response_event(second.token))
    assert queue.get(second.token).decision == "approved"


def test_the_failure_window_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("ha_mcp.policy.decisions.time.monotonic", lambda: now[0])
    limiter = FailedAttemptLimiter(max_attempts=2, window_seconds=60.0)

    limiter.record_failure()
    limiter.record_failure()
    assert limiter.blocked() is True

    now[0] += 61.0
    assert limiter.blocked() is False


@pytest.mark.anyio
async def test_it_subscribes_once_while_the_connection_holds(tmp_path, queue):
    listener, ws = make_listener(tmp_path, queue)

    await listener.ensure_subscribed()
    await listener.ensure_subscribed()

    ws.subscribe_events.assert_awaited_once_with(APPROVAL_RESPONSE_EVENT)
    ws.add_event_handler.assert_called_with(
        APPROVAL_RESPONSE_EVENT, listener._handle_event
    )


@pytest.mark.anyio
async def test_it_does_not_subscribe_while_the_feature_is_off(tmp_path, queue):
    listener, ws = make_listener(tmp_path, queue, enabled=False)

    await listener.ensure_subscribed()

    ws.subscribe_events.assert_not_awaited()


@pytest.mark.anyio
async def test_a_dropped_connection_is_resubscribed(tmp_path, queue):
    """A reconnect drops the server-side subscription; nothing tells us."""
    listener, ws = make_listener(tmp_path, queue)
    await listener.ensure_subscribed()

    ws.is_connected = False
    await listener.ensure_subscribed()

    assert ws.subscribe_events.await_count == 2


@pytest.mark.anyio
async def test_a_replaced_client_is_resubscribed(tmp_path, queue):
    first, second = make_ws_client(), make_ws_client()
    listener = ApprovalResponseListener(
        policy_provider=lambda: Policy(event_decisions_enabled=True),
        queue=queue,
        data_dir=tmp_path,
        get_ws_client=AsyncMock(side_effect=[first, second]),
    )

    await listener.ensure_subscribed()
    await listener.ensure_subscribed()

    first.subscribe_events.assert_awaited_once()
    second.subscribe_events.assert_awaited_once()


@pytest.mark.anyio
async def test_a_replaced_client_stops_receiving(tmp_path, queue):
    """Two live connections to one Home Assistant must not both deliver.

    The handler set is per client, so a second subscription is not
    deduplicated anywhere: each connection dispatches the same response
    event once, one wrong PIN is charged to the budget twice, and the user
    is locked out after fewer than five real guesses. Retiring the previous
    pairing is what keeps the single-active-listener model true when the
    effective credentials change.

    Order matters and is asserted: dropping the handler is synchronous and
    cannot fail, so once it has happened the duplicate cannot be charged
    even if the unsubscribe that follows is refused or abandoned. The
    reverse order would leave a window in which it can.
    """
    first, second = make_ws_client(), make_ws_client()
    first.subscribe_events = AsyncMock(return_value=11)
    order = MagicMock()
    order.attach_mock(first.remove_event_handler, "removed")
    order.attach_mock(first.unsubscribe_events, "unsubscribed")
    listener = ApprovalResponseListener(
        policy_provider=lambda: Policy(event_decisions_enabled=True),
        queue=queue,
        data_dir=tmp_path,
        get_ws_client=AsyncMock(side_effect=[first, second]),
    )

    await listener.ensure_subscribed()
    await listener.ensure_subscribed()

    assert first.remove_event_handler.call_args == first.add_event_handler.call_args
    first.unsubscribe_events.assert_awaited_once_with(11)
    assert [name for name, *_ in order.mock_calls] == ["removed", "unsubscribed"]
    # The socket is pooled and shared; retiring a subscription must not take
    # it down for whatever else is using it.
    first.disconnect.assert_not_called()
    second.unsubscribe_events.assert_not_awaited()


@pytest.mark.anyio
async def test_a_retirement_that_hangs_still_lets_the_new_one_open(
    tmp_path, queue, caplog, monkeypatch
):
    """The old connection must not be able to hold the new one hostage.

    Retirement runs inside the setup budget, which is the time before the
    user is told anything at all. An unresponsive previous connection --
    dropped without a FIN, say -- would otherwise spend that budget on
    tidying, and the request would be announced late or not at all. The
    bound is why the handler is dropped first: by the time this gives up,
    the duplicate delivery it was there to stop has already stopped.
    """
    monkeypatch.setattr("ha_mcp.policy.decisions.RETIRE_BUDGET_SECONDS", 0.05)
    first, second = make_ws_client(), make_ws_client()

    async def _never_returns(_subscription_id: int) -> None:
        await anyio.sleep(3600)

    first.unsubscribe_events = AsyncMock(side_effect=_never_returns)
    listener = ApprovalResponseListener(
        policy_provider=lambda: Policy(event_decisions_enabled=True),
        queue=queue,
        data_dir=tmp_path,
        get_ws_client=AsyncMock(side_effect=[first, second]),
    )

    await listener.ensure_subscribed()
    with caplog.at_level(logging.WARNING, logger="ha_mcp.policy.decisions"):
        await listener.ensure_subscribed()

    second.subscribe_events.assert_awaited_once()
    assert first.remove_event_handler.call_args == first.add_event_handler.call_args
    assert any(
        "gave up releasing subscription" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.anyio
async def test_returning_to_an_earlier_client_retires_the_one_in_between(
    tmp_path, queue
):
    """The switch is not one-way, and the second client is not special.

    Coming back to a connection that is still up does not match the stored
    pairing either, so it subscribes again -- and the subscription it
    replaces has to go the same way as the first one did.
    """
    first, second = make_ws_client(), make_ws_client()
    second.subscribe_events = AsyncMock(return_value=22)
    listener = ApprovalResponseListener(
        policy_provider=lambda: Policy(event_decisions_enabled=True),
        queue=queue,
        data_dir=tmp_path,
        get_ws_client=AsyncMock(side_effect=[first, second, first]),
    )

    await listener.ensure_subscribed()
    await listener.ensure_subscribed()
    await listener.ensure_subscribed()

    second.unsubscribe_events.assert_awaited_once_with(22)
    assert second.remove_event_handler.call_args == second.add_event_handler.call_args


@pytest.mark.anyio
async def test_a_failed_subscribe_is_not_fatal(tmp_path, queue, caplog):
    """The gate still works in the settings UI, so this may not raise."""
    ws = make_ws_client()
    ws.subscribe_events = AsyncMock(side_effect=RuntimeError("no websocket"))
    listener, _ = make_listener(tmp_path, queue, client=ws)

    with caplog.at_level(logging.WARNING):
        await listener.ensure_subscribed()

    assert "could not subscribe" in caplog.text


@pytest.mark.anyio
async def test_a_stalled_setup_gives_up_within_the_budget(
    tmp_path, queue, caplog, short_setup_budget
):
    """A connect that never returns must not hold up the notification.

    The subscription runs ahead of the announcement so a fast responder
    cannot lose the race, which puts connecting, authenticating and a
    round trip in front of the only signal the user gets. Each has its own
    timeout; this is the cap on their sum, and overshooting it costs the
    channel for one request rather than the notification.
    """

    async def never_returns() -> Any:
        await anyio.sleep(3600)

    listener = ApprovalResponseListener(
        policy_provider=lambda: Policy(event_decisions_enabled=True),
        queue=queue,
        data_dir=tmp_path,
        get_ws_client=never_returns,
    )

    with caplog.at_level(logging.WARNING):
        with anyio.fail_after(short_setup_budget + 5):
            await listener.ensure_subscribed()

    assert "gave up opening" in caplog.text


@pytest.mark.anyio
async def test_the_budget_covers_waiting_for_the_lock(
    tmp_path, queue, caplog, short_setup_budget
):
    """Waiting behind somebody else's setup costs the caller the same.

    A budget that started only once the lock was acquired would bound the
    wrong thing: the second request would sit in the queue for as long as
    the first one's stalled connect lasts, which is exactly the delay this
    cap exists to bound.
    """
    listener, _ = make_listener(tmp_path, queue)
    holder_may_release = anyio.Event()

    async def hold_the_lock() -> None:
        async with listener._lock:
            await holder_may_release.wait()

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold_the_lock)
        await anyio.sleep(0.05)
        with caplog.at_level(logging.WARNING):
            with anyio.fail_after(short_setup_budget + 5):
                # The holder is not bounded by anything here, so returning
                # at all is only possible if the budget covers the wait for
                # the lock. Letting the holder be another ensure_subscribed
                # would prove nothing: that one times out on its own budget
                # and releases, so the waiter gets in either way.
                await listener.ensure_subscribed()
        assert "gave up opening" in caplog.text
        holder_may_release.set()


@pytest.mark.anyio
async def test_an_abandoned_setup_leaves_no_subscription_behind(
    tmp_path, queue, short_setup_budget
):
    """A cancelled attempt must not read as a live subscription later.

    The next request checks the client/id pair to decide whether it still
    has a channel. If a timed-out attempt left the previous pair in place,
    that check would answer yes for a subscription that is gone.
    """
    ws = make_ws_client()
    listener, _ = make_listener(tmp_path, queue, client=ws)
    await listener.ensure_subscribed()
    assert listener._subscription_id == 7

    async def never_completes(*_args: Any, **_kwargs: Any) -> int:
        await anyio.sleep(3600)
        raise AssertionError("unreachable: the budget cancels this first")

    ws.is_connected = False
    ws.subscribe_events = AsyncMock(side_effect=never_completes)
    with anyio.fail_after(short_setup_budget + 5):
        await listener.ensure_subscribed()

    assert listener._subscription_id is None
    assert listener._client is None

    # The state that matters: the next attempt must actually re-subscribe.
    # A pairing left behind by the abandoned attempt would send this call
    # into the early return at the top of _subscribe_locked -- believing it
    # holds a subscription that was never opened.
    ws.is_connected = True
    ws.subscribe_events = AsyncMock(return_value=9)
    await listener.ensure_subscribed()

    ws.subscribe_events.assert_awaited_once_with(APPROVAL_RESPONSE_EVENT)
    assert listener._subscription_id == 9


@pytest.mark.anyio
async def test_an_applied_decision_is_reported_with_its_tool(tmp_path, queue):
    """The answer the responder has no other way of getting.

    Whoever decided from a phone notification is not watching the server
    log and may not have the settings tab open at all; without this they
    cannot tell an applied approval from one the server never received.
    """
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    results: list[dict] = []
    listener, _ = make_listener(tmp_path, queue, results=results)

    await listener._handle_event(response_event(entry.token))

    assert results == [
        {
            "token": entry.token,
            "decision": "approve",
            "applied": True,
            "reason": "applied",
            "tool_name": "ha_call_service",
        }
    ]


@pytest.mark.anyio
async def test_a_denial_reports_the_decision_that_was_asked_for(tmp_path, queue):
    """``decision`` is the request, ``applied`` is what became of it."""
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_write_file", "h", {}, ttl_minutes=5)
    results: list[dict] = []
    listener, _ = make_listener(tmp_path, queue, results=results)

    await listener._handle_event(response_event(entry.token, decision="deny"))

    assert results[0]["decision"] == "deny"
    assert results[0]["applied"] is True
    assert results[0]["reason"] == "applied"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("setup", "event", "reason"),
    [
        pytest.param(
            lambda tmp_path, queue: None,
            lambda token: response_event(token, pin="9999"),
            "wrong_pin",
            id="wrong-pin",
        ),
        pytest.param(
            lambda tmp_path, queue: None,
            lambda token: response_event(token, pin=None),
            "no_pin",
            id="no-pin",
        ),
        pytest.param(
            lambda tmp_path, queue: (tmp_path / PIN_FILENAME).unlink(),
            response_event,
            "pin_not_set",
            id="pin-absent",
        ),
        pytest.param(
            lambda tmp_path, queue: (tmp_path / PIN_FILENAME).write_text("{}"),
            response_event,
            "pin_unusable",
            id="pin-unusable",
        ),
    ],
)
async def test_every_refusal_says_which_one_it_was(
    tmp_path, queue, setup, event, reason
):
    """A responder has to be able to tell the repairs apart.

    "Nothing happened" covers a PIN that needs retyping, a PIN file that
    needs replacing, an automation that forgot the field, and a channel
    that is closed for the next few minutes -- four different things to
    do. The reasons are short tokens rather than the log sentences beside
    them, so an automation can branch on them and a reworded log does not
    move the contract.
    """
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    setup(tmp_path, queue)
    results: list[dict] = []
    listener, _ = make_listener(tmp_path, queue, results=results)

    await listener._handle_event(event(entry.token))

    assert [r["reason"] for r in results] == [reason]
    assert results[0]["applied"] is False
    assert queue.get(entry.token).decision == "pending"


@pytest.mark.anyio
async def test_a_closed_channel_says_so_rather_than_going_quiet(tmp_path, queue):
    """The one refusal that resolves by waiting."""
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    results: list[dict] = []
    listener, _ = make_listener(
        tmp_path, queue, limiter=FailedAttemptLimiter(max_attempts=1), results=results
    )

    await listener._handle_event(response_event(entry.token, pin="1111"))
    await listener._handle_event(response_event(entry.token))

    assert [r["reason"] for r in results] == ["wrong_pin", "rate_limited"]


@pytest.mark.anyio
async def test_the_feature_being_off_is_reported_as_such(tmp_path, queue):
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    results: list[dict] = []
    listener, _ = make_listener(tmp_path, queue, enabled=False, results=results)

    await listener._handle_event(response_event(entry.token))

    assert [r["reason"] for r in results] == ["feature_off"]


@pytest.mark.anyio
async def test_an_unknown_token_names_no_tool(tmp_path, queue):
    """Nothing to name, so nothing is named rather than guessed."""
    set_pin(tmp_path, PIN)
    results: list[dict] = []
    listener, _ = make_listener(tmp_path, queue, results=results)

    await listener._handle_event(response_event("not-a-token"))

    assert results[0]["reason"] == "unknown_token"
    assert results[0]["tool_name"] is None


@pytest.mark.anyio
async def test_an_expired_request_is_not_reported_as_unknown(tmp_path, queue):
    """Two different messages for the user: too late, versus never existed."""
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    entry.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    results: list[dict] = []
    listener, _ = make_listener(tmp_path, queue, results=results)

    await listener._handle_event(response_event(entry.token))

    assert results[0]["reason"] == "expired"


@pytest.mark.anyio
async def test_a_second_decision_is_reported_as_already_decided(tmp_path, queue):
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    results: list[dict] = []
    listener, _ = make_listener(tmp_path, queue, results=results)

    await listener._handle_event(response_event(entry.token))
    await listener._handle_event(response_event(entry.token, decision="deny"))

    assert [r["reason"] for r in results] == ["applied", "already_decided"]
    assert queue.get(entry.token).decision == "approved"


@pytest.mark.anyio
async def test_a_malformed_event_is_not_answered(tmp_path, queue):
    """Nothing to answer to, and answering would be the louder mistake.

    A malformed event carries no token, so a result could only name the
    event itself -- and a badly written automation firing in a loop would
    then have the bus answer every one of them.
    """
    set_pin(tmp_path, PIN)
    results: list[dict] = []
    listener, _ = make_listener(tmp_path, queue, results=results)

    await listener._handle_event({"data": {"decision": "approve"}})
    await listener._handle_event({"data": "not-an-object"})

    assert results == []


@pytest.mark.anyio
async def test_deciding_works_without_anywhere_to_report_to(tmp_path, queue):
    """The report is an extra, not a precondition."""
    set_pin(tmp_path, PIN)
    entry = queue.create("ha_call_service", "h", {}, ttl_minutes=5)
    listener, _ = make_listener(tmp_path, queue)

    await listener._handle_event(response_event(entry.token))

    assert queue.get(entry.token).decision == "approved"
