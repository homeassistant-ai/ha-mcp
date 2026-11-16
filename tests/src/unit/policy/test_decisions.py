"""Test deciding a pending approval from the Home Assistant event bus.

Both gates are load-bearing and neither is checked at subscription time, so
each is exercised against a live event: the toggle, and the PIN. The limiter
is what keeps the second one from being guessed.

Every response is fired on a real Home Assistant's bus and reaches the
listener through the server's own WebSocket client; every result goes back
through the server's REST client and is read off the same bus.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import async_capture_events

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.policy import approval_queue
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.policy.decision_pin import PIN_FILENAME, set_pin
from ha_mcp.policy.decisions import (
    APPROVAL_RESPONSE_EVENT,
    ApprovalResponseListener,
    FailedAttemptLimiter,
)
from ha_mcp.policy.events import APPROVAL_RESULT_EVENT, emit_approval_result
from ha_mcp.policy.model import Policy
from ha_mcp.policy.persistence import POLICY_FILENAME, load_policy, save_policy
from ha_mcp.policy.server_channels import approval_ws_client

from .._command_faults import received
from .._held_command import held_command
from .._module_clock import set_module_clock
from .._ws_transport import abort_transport, connected_ws, until

PIN = "2468"
WRONG_PIN = "1111"

WsClientSource = Callable[[], Awaitable[HomeAssistantWebSocketClient]]


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
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


class DecisionBus:
    """A real Home Assistant, the server's clients on it, and one approval queue.

    The feature starts switched on with ``PIN`` set, as a user who has
    finished setting it up has it.
    """

    def __init__(
        self, hass: HomeAssistant, client: HomeAssistantClient, data_dir: Path
    ) -> None:
        self.hass = hass
        self.client = client
        self.data_dir = data_dir
        self.queue = ApprovalQueue()
        self.results = async_capture_events(hass, APPROVAL_RESULT_EVENT)
        self.switch(enabled=True)
        set_pin(data_dir, PIN)

    def switch(self, *, enabled: bool) -> None:
        save_policy(self.data_dir, Policy(event_decisions_enabled=enabled))

    def pooled_socket(
        self, client: HomeAssistantClient | None = None
    ) -> WsClientSource:
        """How the server opens the channel: as its own client, on the pool."""
        return partial(
            approval_ws_client, SimpleNamespace(client=client or self.client)
        )

    def listener(
        self,
        *,
        limiter: FailedAttemptLimiter | None = None,
        get_ws_client: WsClientSource | None = None,
        report: bool = True,
    ) -> ApprovalResponseListener:
        return ApprovalResponseListener(
            policy_provider=partial(load_policy, self.data_dir),
            queue=self.queue,
            data_dir=self.data_dir,
            get_ws_client=get_ws_client or self.pooled_socket(),
            limiter=limiter,
            emit_result=partial(emit_approval_result, self.client) if report else None,
        )

    async def listening(self, **kwargs: Any) -> ApprovalResponseListener:
        listener = self.listener(**kwargs)
        await listener.ensure_subscribed()
        return listener

    def request(self, tool_name: str = "ha_call_service") -> str:
        return self.queue.create(tool_name, "h", {}, ttl_minutes=5).token

    def decision(self, token: str) -> str:
        entry = self.queue.get(token)
        assert entry is not None
        return entry.decision

    def reasons(self) -> list[str]:
        return [event.data["reason"] for event in self.results]

    async def fire(self, data: dict[str, Any]) -> dict[str, Any]:
        """Fire one response event; the result event it was answered with."""
        seen = len(self.results)
        self.hass.bus.async_fire(APPROVAL_RESPONSE_EVENT, data)
        await until(lambda: len(self.results) > seen)
        return dict(self.results[seen].data)

    async def respond(
        self, token: str, decision: str = "approve", pin: Any = PIN
    ) -> dict[str, Any]:
        return await self.fire({"token": token, "decision": decision, "pin": pin})

    async def settle(self) -> None:
        """Fire a response the listener answers without deciding or counting.

        It carries no PIN, so it never charges or clears the budget; once it
        is answered, everything fired before it has been handled.
        """
        result = await self.respond("settle", pin=None)
        assert result["token"] == "settle"


@pytest.fixture
async def bus(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> DecisionBus:
    return DecisionBus(hass, core_client, tmp_path)


def taking_turns(*clients: HomeAssistantWebSocketClient) -> WsClientSource:
    """Each channel setup gets the next client, as when credentials change."""
    turns = iter(clients)

    async def next_client() -> HomeAssistantWebSocketClient:
        return next(turns)

    return next_client


def subscriptions(seen: dict[str, list[Any]]) -> int:
    return sum(
        msg.get("event_type") == APPROVAL_RESPONSE_EVENT
        for msg in seen["subscribe_events"]
    )


async def test_a_correct_pin_approves_the_request(bus: DecisionBus) -> None:
    token = bus.request()
    await bus.listening()

    await bus.respond(token)

    assert bus.decision(token) == "approved"


async def test_a_correct_pin_denies_the_request(bus: DecisionBus) -> None:
    token = bus.request()
    await bus.listening()

    await bus.respond(token, decision="deny")

    assert bus.decision(token) == "denied"


async def test_switching_the_toggle_off_decides_nothing_and_says_so(
    bus: DecisionBus,
) -> None:
    """Off is enforced per event, not by whether a subscription exists."""
    token = bus.request()
    await bus.listening()
    bus.switch(enabled=False)

    result = await bus.respond(token)

    assert result["reason"] == "feature_off"
    assert bus.decision(token) == "pending"


async def test_an_event_without_a_pin_is_not_a_guess(bus: DecisionBus) -> None:
    """No PIN in the event is a badly written automation, not an attempt.

    Same reasoning as the malformed and unknown-token cases: an automation
    that forgets the field would otherwise spend the whole budget and close
    the channel on the user's own notification action.
    """
    limiter = FailedAttemptLimiter(max_attempts=2)
    token = bus.request()
    await bus.listening(limiter=limiter)

    for _ in range(5):
        await bus.fire({"token": token, "decision": "approve"})

    assert limiter.blocked() is False
    await bus.respond(token)
    assert bus.decision(token) == "approved"


async def test_the_pin_never_reaches_the_log(
    bus: DecisionBus, caplog: pytest.LogCaptureFixture
) -> None:
    """Refusals are logged; the secret in the refused event is not.

    That covers the WebSocket reader that decodes the event as much as the
    listener that refuses it.
    """
    token = bus.request()
    await bus.listening()

    with caplog.at_level(logging.DEBUG, logger="ha_mcp"):
        await bus.respond(token, pin="wrong-pin-here")
        await bus.respond(token)

    ours = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("ha_mcp")
    )
    assert APPROVAL_RESPONSE_EVENT in ours
    assert "wrong-pin-here" not in ours
    assert PIN not in ours


@pytest.mark.parametrize(
    "data",
    [
        {"token": "", "decision": "approve", "pin": PIN},
        {"token": 17, "decision": "approve", "pin": PIN},
        {"decision": "maybe", "pin": PIN},
    ],
    ids=["empty-token", "token-not-string", "bad-decision"],
)
async def test_a_malformed_event_decides_nothing_and_is_not_answered(
    bus: DecisionBus, data: dict[str, Any]
) -> None:
    """Nothing to answer to, and answering would be the louder mistake.

    A malformed event carries no usable token, so a result could only name
    the event itself -- and a badly written automation firing in a loop
    would then have the bus answer every one of them.
    """
    token = bus.request()
    await bus.listening()

    bus.hass.bus.async_fire(APPROVAL_RESPONSE_EVENT, {"token": token, **data})
    await bus.settle()

    assert bus.reasons() == ["no_pin"]
    assert bus.decision(token) == "pending"


async def test_a_malformed_event_is_not_a_guess(bus: DecisionBus) -> None:
    """Garbage from a broken automation must not spend the PIN budget."""
    limiter = FailedAttemptLimiter(max_attempts=2)
    token = bus.request()
    await bus.listening(limiter=limiter)

    for _ in range(5):
        bus.hass.bus.async_fire(
            APPROVAL_RESPONSE_EVENT, {"token": "", "decision": "approve"}
        )
    await bus.settle()

    assert limiter.blocked() is False
    await bus.respond(token)
    assert bus.decision(token) == "approved"


async def test_an_unknown_token_with_the_right_pin_is_not_a_guess(
    bus: DecisionBus,
) -> None:
    """The PIN was right, so nothing was guessed -- whatever the token was."""
    limiter = FailedAttemptLimiter(max_attempts=2)
    await bus.listening(limiter=limiter)

    for _ in range(5):
        await bus.respond("no-such-token")

    assert limiter.blocked() is False


async def test_a_wrong_pin_counts_even_with_an_unknown_token(bus: DecisionBus) -> None:
    """Authorisation runs before the token is looked up, so it counts.

    The token is the thing being guessed at. If an invented one bought a
    free attempt, the budget would bound nothing: an attacker would spend
    wrong PINs against tokens they made up and never be blocked.
    """
    limiter = FailedAttemptLimiter(max_attempts=2)
    token = bus.request()
    await bus.listening(limiter=limiter)

    for _ in range(2):
        await bus.respond("no-such-token", pin=WRONG_PIN)

    assert limiter.blocked() is True
    # And the channel really is closed now, for the real token too.
    await bus.respond(token)
    assert bus.decision(token) == "pending"


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_an_expired_request_is_not_decided_and_says_so(
    bus: DecisionBus, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    """The TTL binds a decision from the bus as it binds one from the tab.

    The settings UI reads the queue before deciding, which expires the entry
    on the way in. An event does not, so without the sweep in the queue a
    request whose window closed while its caller was retrying could still be
    decided -- waking that retry well after the entry should have been gone.
    Nothing polls the queue here, because a poll would expire the entry
    itself and hide the defect. And the responder is told it was too late,
    not that the request never existed.
    """
    created = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    set_module_clock(monkeypatch, approval_queue, created)
    token = bus.request()
    await bus.listening()
    set_module_clock(monkeypatch, approval_queue, created + timedelta(minutes=6))

    result = await bus.respond(token, decision=decision)

    assert result["reason"] == "expired"
    assert result["applied"] is False


async def test_an_unusable_stored_record_decides_nothing_and_costs_nothing(
    bus: DecisionBus,
) -> None:
    """A broken PIN file is a configuration problem, not a wrong guess.

    Nobody can type a PIN that matches ``{}``, so charging the budget for
    the attempt would close the channel over something no responder could
    have got right -- and the repair is to set the PIN again, which the
    refusal has to point at rather than at the PIN that was typed.
    """
    (bus.data_dir / PIN_FILENAME).write_text("{}", encoding="utf-8")
    limiter = FailedAttemptLimiter(max_attempts=2)
    token = bus.request()
    await bus.listening(limiter=limiter)

    for _ in range(5):
        await bus.respond(token)

    assert bus.decision(token) == "pending"
    assert limiter.blocked() is False
    # Setting a real PIN repairs it without any window to wait out.
    set_pin(bus.data_dir, PIN)
    await bus.respond(token)
    assert bus.decision(token) == "approved"


async def test_a_missing_pin_is_not_a_guess(bus: DecisionBus) -> None:
    """Nothing to guess against yet, so the budget stays whole.

    Otherwise a user switching the feature on would find the channel
    already locked out by the events their automation fired while they
    were still setting the PIN.
    """
    (bus.data_dir / PIN_FILENAME).unlink()
    limiter = FailedAttemptLimiter(max_attempts=2)
    token = bus.request()
    await bus.listening(limiter=limiter)

    for _ in range(5):
        await bus.respond(token)

    assert limiter.blocked() is False
    set_pin(bus.data_dir, PIN)
    await bus.respond(token)
    assert bus.decision(token) == "approved"


async def test_wrong_pins_close_the_channel_and_say_so(bus: DecisionBus) -> None:
    """The one refusal that resolves by waiting, and it is reported as such."""
    token = bus.request()
    await bus.listening(limiter=FailedAttemptLimiter(max_attempts=3))

    for _ in range(3):
        await bus.respond(token, pin=WRONG_PIN)
    # The right PIN now, and it still decides nothing: the budget is spent.
    await bus.respond(token)

    assert bus.reasons() == ["wrong_pin"] * 3 + ["rate_limited"]
    assert bus.decision(token) == "pending"


async def test_a_correct_pin_clears_the_failures(bus: DecisionBus) -> None:
    limiter = FailedAttemptLimiter(max_attempts=3)
    first = bus.request()
    second = bus.request("ha_write_file")
    await bus.listening(limiter=limiter)

    for _ in range(2):
        await bus.respond(first, pin=WRONG_PIN)
    await bus.respond(first)
    for _ in range(2):
        await bus.respond(second, pin=WRONG_PIN)

    assert limiter.blocked() is False
    await bus.respond(second)
    assert bus.decision(second) == "approved"


def test_the_failure_window_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("ha_mcp.policy.decisions.time.monotonic", lambda: now[0])
    limiter = FailedAttemptLimiter(max_attempts=2, window_seconds=60.0)

    limiter.record_failure()
    limiter.record_failure()
    assert limiter.blocked() is True

    now[0] += 61.0
    assert limiter.blocked() is False


async def test_it_subscribes_once_while_the_connection_holds(
    hass: HomeAssistant, bus: DecisionBus
) -> None:
    token = bus.request()

    with received(hass, "subscribe_events") as seen:
        listener = await bus.listening()
        await listener.ensure_subscribed()

    assert subscriptions(seen) == 1
    assert (await bus.respond(token))["reason"] == "applied"
    assert bus.reasons() == ["applied"]


async def test_it_does_not_subscribe_while_the_feature_is_off(
    hass: HomeAssistant, bus: DecisionBus
) -> None:
    bus.switch(enabled=False)

    with received(hass, "subscribe_events") as seen:
        await bus.listening()

    assert subscriptions(seen) == 0


async def test_a_dropped_connection_is_resubscribed(
    hass: HomeAssistant, bus: DecisionBus
) -> None:
    """A reconnect drops the server-side subscription; nothing tells us."""
    token = bus.request()
    listener = await bus.listening()
    socket = await bus.pooled_socket()()

    abort_transport(socket)
    await until(lambda: not socket.is_connected)
    with received(hass, "subscribe_events") as seen:
        await listener.ensure_subscribed()

    assert subscriptions(seen) == 1
    await bus.respond(token)
    assert bus.decision(token) == "approved"


async def test_a_replaced_client_is_resubscribed(
    bus: DecisionBus, core_client: HomeAssistantClient
) -> None:
    """Decisions keep arriving once the channel moves to another connection."""
    token = bus.request()
    async with connected_ws(core_client) as first, connected_ws(core_client) as second:
        listener = await bus.listening(get_ws_client=taking_turns(first, second))
        await listener.ensure_subscribed()
        await first.disconnect()

        await bus.respond(token)

    assert bus.decision(token) == "approved"


async def test_a_replaced_client_stops_receiving(
    hass: HomeAssistant, bus: DecisionBus, core_client: HomeAssistantClient
) -> None:
    """Two live connections to one Home Assistant must not both deliver.

    The handler set is per client, so a second subscription is not
    deduplicated anywhere: each connection dispatches the same response
    event once, one wrong PIN is charged to the budget twice, and the user
    is locked out after fewer than five real guesses. Retiring the previous
    pairing is what keeps the single-active-listener model true when the
    effective credentials change.
    """
    limiter = FailedAttemptLimiter(max_attempts=2)
    token = bus.request()
    async with connected_ws(core_client) as first, connected_ws(core_client) as second:
        listener = await bus.listening(
            limiter=limiter, get_ws_client=taking_turns(first, second)
        )
        with received(hass, "unsubscribe_events") as seen:
            await listener.ensure_subscribed()

        await bus.respond(token, pin=WRONG_PIN)
        await bus.settle()

        assert len(seen["unsubscribe_events"]) == 1
        assert bus.reasons() == ["wrong_pin", "no_pin"]
        assert limiter.blocked() is False
        # The socket is pooled and shared; retiring a subscription must not
        # take it down for whatever else is using it.
        assert first.is_connected


async def test_a_retirement_that_hangs_still_lets_the_new_one_open(
    hass: HomeAssistant,
    bus: DecisionBus,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old connection must not be able to hold the new one hostage.

    Retirement runs inside the setup budget, which is the time before the
    user is told anything at all. An unresponsive previous connection would
    otherwise spend that budget on tidying, and the request would be
    announced late or not at all. The handler is dropped first, so while
    Home Assistant still holds the old subscription its deliveries reach
    nothing and cannot be charged twice.
    """
    monkeypatch.setattr("ha_mcp.policy.decisions.RETIRE_BUDGET_SECONDS", 0.05)
    limiter = FailedAttemptLimiter(max_attempts=2)
    token = bus.request()
    async with connected_ws(core_client) as first, connected_ws(core_client) as second:
        listener = await bus.listening(
            limiter=limiter, get_ws_client=taking_turns(first, second)
        )
        async with held_command(hass, "unsubscribe_events") as held:
            with caplog.at_level(logging.WARNING, logger="ha_mcp.policy.decisions"):
                await listener.ensure_subscribed()
            assert held.arrived.is_set()

            await bus.respond(token, pin=WRONG_PIN)
            await bus.settle()

            assert bus.reasons() == ["wrong_pin", "no_pin"]
            assert limiter.blocked() is False

    assert any(
        "gave up releasing subscription" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


async def test_returning_to_an_earlier_client_retires_the_one_in_between(
    hass: HomeAssistant, bus: DecisionBus, core_client: HomeAssistantClient
) -> None:
    """The switch is not one-way, and the second client is not special.

    Coming back to a connection that is still up does not match the stored
    pairing either, so it subscribes again -- and the subscription it
    replaces has to go the same way as the first one did.
    """
    limiter = FailedAttemptLimiter(max_attempts=2)
    token = bus.request()
    async with connected_ws(core_client) as first, connected_ws(core_client) as second:
        listener = await bus.listening(
            limiter=limiter, get_ws_client=taking_turns(first, second, first)
        )
        with received(hass, "unsubscribe_events") as seen:
            await listener.ensure_subscribed()
            await listener.ensure_subscribed()

        await bus.respond(token, pin=WRONG_PIN)
        await bus.settle()

    assert len(seen["unsubscribe_events"]) == 2
    assert bus.reasons() == ["wrong_pin", "no_pin"]
    assert limiter.blocked() is False


async def test_a_refused_subscribe_is_not_fatal(
    bus: DecisionBus,
    read_only_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gate still works in the settings UI, so this may not raise.

    Home Assistant refuses a non-administrator's subscription to this event.
    """
    with caplog.at_level(logging.WARNING, logger="ha_mcp.policy.decisions"):
        await bus.listening(get_ws_client=bus.pooled_socket(read_only_client))

    assert "could not subscribe" in caplog.text


async def test_a_stalled_setup_gives_up_within_the_budget(
    hass: HomeAssistant,
    bus: DecisionBus,
    caplog: pytest.LogCaptureFixture,
    short_setup_budget: float,
) -> None:
    """A subscription Home Assistant never answers must not hold up the
    notification.

    The subscription runs ahead of the announcement so a fast responder
    cannot lose the race, which puts connecting, authenticating and a round
    trip in front of the only signal the user gets. Each has its own
    timeout; this is the cap on their sum, and overshooting it costs the
    channel for one request rather than the notification.
    """
    async with held_command(hass, "subscribe_events"):
        with caplog.at_level(logging.WARNING, logger="ha_mcp.policy.decisions"):
            with anyio.fail_after(short_setup_budget + 5):
                await bus.listening()

    assert "gave up opening" in caplog.text


async def test_the_budget_covers_waiting_for_the_lock(
    hass: HomeAssistant,
    bus: DecisionBus,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    short_setup_budget: float,
) -> None:
    """Waiting behind somebody else's setup costs the caller the same.

    A budget that started only once the lock was acquired would bound the
    wrong thing: the second request would sit in the queue for as long as
    the first one's stalled setup lasts, which is exactly the delay this cap
    exists to bound. The first one keeps the lock past its own budget while
    it releases what it may have opened, and Home Assistant answers neither
    its subscribe nor that release until the second has returned.
    """
    monkeypatch.setattr(websocket_client, "CLEANUP_TIMEOUT_SECONDS", 60.0)
    listener = bus.listener()

    async with (
        held_command(hass, "subscribe_events") as subscribe,
        held_command(hass, "unsubscribe_events") as release,
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(listener.ensure_subscribed)
        await subscribe.arrived.wait()
        try:
            with caplog.at_level(logging.WARNING, logger="ha_mcp.policy.decisions"):
                with anyio.fail_after(short_setup_budget + 5):
                    await listener.ensure_subscribed()
            assert "gave up opening" in caplog.text
        finally:
            subscribe.release.set()
            release.release.set()


async def test_an_abandoned_setup_leaves_no_subscription_behind(
    hass: HomeAssistant, bus: DecisionBus, short_setup_budget: float
) -> None:
    """A cancelled attempt must not read as a live subscription later.

    The next request checks the client/id pair to decide whether it still
    has a channel. If a timed-out attempt left a pair in place, that check
    would answer yes for a subscription that was never opened, and no
    decision would ever arrive.
    """
    token = bus.request()
    listener = bus.listener()
    async with held_command(hass, "subscribe_events", first_only=True):
        with anyio.fail_after(short_setup_budget + 5):
            await listener.ensure_subscribed()

    with received(hass, "subscribe_events") as seen:
        await listener.ensure_subscribed()

    assert subscriptions(seen) == 1
    await bus.respond(token)
    assert bus.decision(token) == "approved"


async def test_an_applied_decision_is_reported_with_its_tool(bus: DecisionBus) -> None:
    """The answer the responder has no other way of getting.

    Whoever decided from a phone notification is not watching the server
    log and may not have the settings tab open at all; without this they
    cannot tell an applied approval from one the server never received.
    """
    token = bus.request()
    await bus.listening()

    result = await bus.respond(token)

    assert result == {
        "token": token,
        "decision": "approve",
        "applied": True,
        "reason": "applied",
        "tool_name": "ha_call_service",
    }


async def test_a_denial_reports_the_decision_that_was_asked_for(
    bus: DecisionBus,
) -> None:
    """``decision`` is the request, ``applied`` is what became of it."""
    token = bus.request("ha_write_file")
    await bus.listening()

    result = await bus.respond(token, decision="deny")

    assert result["decision"] == "deny"
    assert result["applied"] is True
    assert result["reason"] == "applied"


@pytest.mark.parametrize(
    ("setup", "pin", "reason"),
    [
        pytest.param(lambda data_dir: None, "9999", "wrong_pin", id="wrong-pin"),
        pytest.param(lambda data_dir: None, None, "no_pin", id="no-pin"),
        pytest.param(
            lambda data_dir: (data_dir / PIN_FILENAME).unlink(),
            PIN,
            "pin_not_set",
            id="pin-absent",
        ),
        pytest.param(
            lambda data_dir: (data_dir / PIN_FILENAME).write_text("{}"),
            PIN,
            "pin_unusable",
            id="pin-unusable",
        ),
    ],
)
async def test_every_refusal_says_which_one_it_was(
    bus: DecisionBus, setup: Callable[[Path], object], pin: str | None, reason: str
) -> None:
    """A responder has to be able to tell the repairs apart.

    "Nothing happened" covers a PIN that needs retyping, a PIN file that
    needs replacing, an automation that forgot the field, and a channel
    that is closed for the next few minutes -- four different things to
    do. The reasons are short tokens rather than the log sentences beside
    them, so an automation can branch on them and a reworded log does not
    move the contract.
    """
    token = bus.request()
    setup(bus.data_dir)
    await bus.listening()

    result = await bus.respond(token, pin=pin)

    assert bus.reasons() == [reason]
    assert result["applied"] is False
    assert bus.decision(token) == "pending"


async def test_a_policy_that_cannot_be_read_is_reported_as_such(
    bus: DecisionBus,
) -> None:
    """The refusal a responder cannot fix by retyping anything.

    An unreadable policy file leaves the channel unable to say whether it
    is wanted, so the event is refused -- and saying so is the difference
    between an automation author retrying a PIN and an operator repairing
    a file.
    """
    token = bus.request()
    await bus.listening()
    (bus.data_dir / POLICY_FILENAME).write_text("{not json", encoding="utf-8")

    result = await bus.respond(token)

    assert result["reason"] == "policy_unreadable"
    assert result["applied"] is False
    assert bus.decision(token) == "pending"


async def test_an_unknown_token_names_no_tool(bus: DecisionBus) -> None:
    """Nothing to name, so nothing is named rather than guessed."""
    await bus.listening()

    result = await bus.respond("not-a-token")

    assert result["reason"] == "unknown_token"
    assert "tool_name" not in result


async def test_a_second_decision_is_reported_as_already_decided(
    bus: DecisionBus,
) -> None:
    token = bus.request()
    await bus.listening()

    await bus.respond(token)
    await bus.respond(token, decision="deny")

    assert bus.reasons() == ["applied", "already_decided"]
    assert bus.decision(token) == "approved"


async def test_deciding_works_without_anywhere_to_report_to(bus: DecisionBus) -> None:
    """The report is an extra, not a precondition."""
    token = bus.request()
    await bus.listening(report=False)

    bus.hass.bus.async_fire(
        APPROVAL_RESPONSE_EVENT, {"token": token, "decision": "approve", "pin": PIN}
    )
    await until(lambda: bus.decision(token) == "approved")

    assert bus.results == []
