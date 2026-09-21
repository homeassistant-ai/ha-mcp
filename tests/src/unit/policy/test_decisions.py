"""Test deciding a pending approval from the Home Assistant event bus.

Both gates are load-bearing and neither is checked at subscription time, so
each is exercised against a live-looking event: the toggle, and the PIN.
The limiter is what keeps the second one from being guessed.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.decision_pin import set_pin
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
) -> tuple[ApprovalResponseListener, MagicMock]:
    ws = client or make_ws_client()
    listener = ApprovalResponseListener(
        policy_provider=lambda: Policy(event_decisions_enabled=enabled),
        queue=queue,
        data_dir=tmp_path,
        get_ws_client=AsyncMock(return_value=ws),
        limiter=limiter,
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
async def test_an_unknown_token_is_not_a_guess(tmp_path, queue):
    set_pin(tmp_path, PIN)
    limiter = FailedAttemptLimiter(max_attempts=2)
    listener, _ = make_listener(tmp_path, queue, limiter=limiter)

    for _ in range(5):
        await listener._handle_event(response_event("no-such-token"))

    assert limiter.blocked() is False


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
async def test_a_failed_subscribe_is_not_fatal(tmp_path, queue, caplog):
    """The gate still works in the settings UI, so this may not raise."""
    ws = make_ws_client()
    ws.subscribe_events = AsyncMock(side_effect=RuntimeError("no websocket"))
    listener, _ = make_listener(tmp_path, queue, client=ws)

    with caplog.at_level(logging.WARNING):
        await listener.ensure_subscribed()

    assert "could not subscribe" in caplog.text
