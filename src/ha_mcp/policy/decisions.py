"""Accepting approve/deny decisions from the Home Assistant event bus.

The request side (``events.py``) announces a pending approval so a user's
own automation can notify them. This is the way back: a PIN-carrying
``ha_mcp_approval_response`` event decides that request, so an approval can
be granted from a phone notification instead of the settings UI.

**What the PIN does and does not buy.** The event bus cannot tell a
response fired by the user's automation from one fired by an agent that
can author automations, so the channel itself carries no authority. The
PIN is the authority, and an agent with enough access can obtain it --
by prompting for it, or by writing an automation that reads it out of a
response event it triggers. Users enabling this accept that; the settings
UI says so next to the toggle. What the PIN plus the limiter below does
buy is that the channel is closed to anything that has not been told the
PIN, which is the difference between "anyone who can fire an event" and
"someone who has the secret".

Two independent gates therefore guard every decision, both checked here
rather than at subscription time so that turning the feature off takes
effect on the next event rather than on the next restart:

1. ``Policy.event_decisions_enabled`` -- off by default.
2. A PIN that matches the stored digest (``decision_pin.py``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from anyio.to_thread import run_sync as run_in_thread

from .approval_queue import ApprovalQueue
from .decision_pin import PIN_ABSENT, PIN_INVALID, pin_state, verify_pin
from .model import Policy

if TYPE_CHECKING:
    from ..client.websocket_client import HomeAssistantWebSocketClient

logger = logging.getLogger(__name__)

APPROVAL_RESPONSE_EVENT = "ha_mcp_approval_response"

# Failed-PIN budget. Small, because a legitimate responder types the PIN
# they configured; the window is what turns a wrong PIN into a delay
# instead of a lockout somebody has to clear by hand.
MAX_FAILED_ATTEMPTS = 5
FAILURE_WINDOW_SECONDS = 300.0

# How long opening the response channel may delay the announcement it runs
# ahead of. Connecting, authenticating and the subscription round trip each
# carry their own timeout, and the lock can already be held by another
# request doing the same; this is the only cap on the sum. Small on purpose:
# every second here is a second the user has not yet been notified, and it
# is spent out of the request's own TTL. Overshooting it costs the channel
# for this one request, which the settings UI already covers.
SETUP_BUDGET_SECONDS = 5.0


class FailedAttemptLimiter:
    """Bounds PIN guessing against the live server.

    Counts only wrong PINs -- but every wrong PIN, whatever token came
    with it. Authorisation runs before the token is looked up, so a guess
    carrying a token the queue never knew is still a guess and still
    spends budget; that is what stops an attacker buying attempts with
    invented tokens. What is never counted is an event with no PIN to be
    wrong about: a malformed event, one without a PIN field, and one that
    arrives before a PIN was configured are badly written automations
    rather than guesses, and counting them would let such an automation
    lock out the user's real notification action.

    The block is global rather than per token: the tokens are the thing
    being guessed at, so per-token counting would hand an attacker a fresh
    budget with every new pending request.

    Global means global to this process, matching the approval queue it
    guards: both live in memory, both are documented single-process, and a
    multi-worker deployment would hand each worker its own budget -- the
    same unsupported configuration in which an approval created on one
    worker is invisible to the next.
    """

    def __init__(
        self,
        *,
        max_attempts: int = MAX_FAILED_ATTEMPTS,
        window_seconds: float = FAILURE_WINDOW_SECONDS,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._failures: list[float] = []

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds
        self._failures = [at for at in self._failures if at > cutoff]

    def blocked(self) -> bool:
        """Whether the budget is spent. Monotonic clock: a host clock step
        must not hand out a fresh budget mid-window."""
        now = time.monotonic()
        self._prune(now)
        return len(self._failures) >= self._max_attempts

    def record_failure(self) -> int:
        """Count one wrong PIN. Returns the number of failures in the window."""
        now = time.monotonic()
        self._prune(now)
        self._failures.append(now)
        return len(self._failures)

    def reset(self) -> None:
        """Forget the failures. Called on a PIN that verified: the user is
        demonstrably present, and their typo should not follow them into
        the next request."""
        self._failures.clear()


class ApprovalResponseListener:
    """Subscribes to ``ha_mcp_approval_response`` and applies what it carries.

    There is no tear-down counterpart. Switching the feature off is
    enforced per event in ``_authorised`` rather than by dropping the
    subscription, so an open channel with the toggle off decides nothing;
    at shutdown the WebSocket manager closes the connection and the
    subscription goes with it.

    Subscription is lazy and self-healing rather than started once at
    boot: ``ensure_subscribed`` runs each time a request is announced, so
    the toggle takes effect without a restart, an install that never uses
    the feature never opens a subscription, and a WebSocket that dropped
    and reconnected between two approvals is re-subscribed at the moment
    the next request needs it -- a reconnect silently drops server-side
    subscriptions, and the only thing worse than no channel is one the
    user believes is listening.
    """

    def __init__(
        self,
        *,
        policy_provider: Callable[[], Policy],
        queue: ApprovalQueue,
        data_dir: Path,
        get_ws_client: Callable[[], Awaitable[HomeAssistantWebSocketClient]],
        limiter: FailedAttemptLimiter | None = None,
    ) -> None:
        self._policy_provider = policy_provider
        self._queue = queue
        self._data_dir = data_dir
        self._get_ws_client = get_ws_client
        self._limiter = limiter or FailedAttemptLimiter()
        self._client: HomeAssistantWebSocketClient | None = None
        self._subscription_id: int | None = None
        self._lock = anyio.Lock()

    async def ensure_subscribed(self) -> None:
        """Open (or re-open) the response subscription if the feature is on.

        Best effort by the same reasoning as the announcement it
        accompanies: a gate that cannot be decided from a notification is
        still a working gate in the settings UI, so a failure here is
        logged and the call it was announcing proceeds.

        Bounded, for the same reason. This runs ahead of the announcement
        so a fast responder cannot answer a request that has no channel to
        answer on, which puts connecting, authenticating, waiting for the
        lock another announcement holds, and a subscription round trip in
        front of the notification the user is waiting for. Each of those
        has its own timeout, and together they can still add up to tens of
        seconds of silence. ``SETUP_BUDGET_SECONDS`` caps the lot: past it
        the attempt is abandoned and the caller announces anyway, with the
        subscription left to the next request that comes through here. The
        budget deliberately covers lock acquisition, because waiting for
        somebody else's setup costs the user exactly what doing it here
        would.
        """
        try:
            policy = await run_in_thread(self._policy_provider)
        except ValueError:
            # A corrupt policy file is the middleware's business -- it
            # fails the call closed. Here it only means "cannot tell
            # whether the channel is wanted", so leave it shut.
            logger.warning(
                "policy decisions: cannot read the policy; the %s "
                "subscription stays closed",
                APPROVAL_RESPONSE_EVENT,
                exc_info=True,
            )
            return
        if not policy.event_decisions_enabled:
            return
        with anyio.move_on_after(SETUP_BUDGET_SECONDS) as scope:
            async with self._lock:
                try:
                    await self._subscribe_locked()
                except Exception:
                    logger.warning(
                        "policy decisions: could not subscribe to %s; approvals "
                        "can still be decided in the settings UI",
                        APPROVAL_RESPONSE_EVENT,
                        exc_info=True,
                    )
        if scope.cancelled_caught:
            logger.warning(
                "policy decisions: gave up opening the %s channel after %.0f "
                "seconds; announcing the request anyway. It can be decided in "
                "the settings UI, and the next request retries the channel.",
                APPROVAL_RESPONSE_EVENT,
                SETUP_BUDGET_SECONDS,
            )

    async def _subscribe_locked(self) -> None:
        client = await self._get_ws_client()
        if client is self._client and self._subscription_id is not None:
            if client.is_connected:
                return
            # Same client object, dead connection: the server-side
            # subscription went with it, so the id we hold names nothing.
            self._subscription_id = None
        # Forget the previous pairing BEFORE subscribing. The call below can
        # be abandoned mid-flight when the setup budget runs out, and a
        # cancellation there must not leave a client/id pair behind that the
        # check above would read as a live subscription. Both are set again
        # only once the round trip has actually returned an id.
        self._client = None
        self._subscription_id = None
        subscription_id = await client.subscribe_events(APPROVAL_RESPONSE_EVENT)
        # Handlers live in a set keyed on the bound method's identity, so
        # re-registering the same one after a reconnect does not double it.
        client.add_event_handler(APPROVAL_RESPONSE_EVENT, self._handle_event)
        self._client = client
        self._subscription_id = subscription_id
        logger.info(
            "policy decisions: listening for %s (subscription %s)",
            APPROVAL_RESPONSE_EVENT,
            self._subscription_id,
        )

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Apply one ``ha_mcp_approval_response`` event.

        The event payload is untrusted input from the bus: every field is
        checked before it reaches the queue, and the PIN it carries is
        never logged -- not here, and not by the WebSocket reader that
        decodes the event, which redacts it before its own debug line.
        Home Assistant's own logs are not where a user's secret should end
        up. The token and the decision ARE logged: they are what ties an
        applied decision to the bus rather than to a click in the tab, and
        neither is a secret -- the token names a request the settings UI
        lists in full, and it dies with that request.
        """
        data = event.get("data")
        if not isinstance(data, dict):
            logger.warning(
                "policy decisions: ignoring a %s event with no data object",
                APPROVAL_RESPONSE_EVENT,
            )
            return
        token = data.get("token")
        decision = data.get("decision")
        if (
            decision not in ("approve", "deny")
            or not isinstance(token, str)
            or not token
        ):
            logger.warning(
                "policy decisions: ignoring a malformed %s event "
                "(decision must be 'approve' or 'deny' and token a non-empty "
                "string; got decision=%r, token of type %s)",
                APPROVAL_RESPONSE_EVENT,
                decision if isinstance(decision, str) else type(decision).__name__,
                type(token).__name__,
            )
            return
        if not await self._authorised(data.get("pin")):
            return
        applied = (
            self._queue.approve(token)
            if decision == "approve"
            else self._queue.deny(token)
        )
        if applied:
            logger.info(
                "policy decisions: %s from the event bus applied to token %s",
                decision,
                token,
            )
        else:
            # The queue logs the why (unknown token / already decided) at
            # its own level; this line is what ties that to the bus rather
            # than to a click in the settings UI.
            logger.info(
                "policy decisions: %s from the event bus did not apply to "
                "token %s (unknown or already decided)",
                decision,
                token,
            )

    async def _authorised(self, pin: Any) -> bool:
        """Both gates, in the order that costs least when it says no."""
        try:
            policy = await run_in_thread(self._policy_provider)
        except ValueError:
            logger.warning(
                "policy decisions: refusing a %s event, the policy file is unreadable",
                APPROVAL_RESPONSE_EVENT,
                exc_info=True,
            )
            return False
        if not policy.event_decisions_enabled:
            logger.info(
                "policy decisions: refusing a %s event, deciding from the "
                "event bus is switched off",
                APPROVAL_RESPONSE_EVENT,
            )
            return False
        if self._limiter.blocked():
            logger.warning(
                "policy decisions: refusing a %s event, %d wrong PINs within "
                "%.0f seconds -- the channel is closed until that window "
                "passes. Decide in the settings UI meanwhile.",
                APPROVAL_RESPONSE_EVENT,
                MAX_FAILED_ATTEMPTS,
                FAILURE_WINDOW_SECONDS,
            )
            return False
        state = await run_in_thread(pin_state, self._data_dir)
        if state == PIN_ABSENT:
            logger.warning(
                "policy decisions: refusing a %s event, no PIN is configured. "
                "Set one on the Tool Security Policies tab.",
                APPROVAL_RESPONSE_EVENT,
            )
            return False
        if state == PIN_INVALID:
            # A stored record this build cannot verify against is a broken
            # configuration, not a guess: charging the budget for it would
            # close the channel over something no responder can get right,
            # and the repair is to set the PIN again.
            logger.warning(
                "policy decisions: refusing a %s event, the stored approval "
                "PIN is unusable. Set the PIN again on the Tool Security "
                "Policies tab.",
                APPROVAL_RESPONSE_EVENT,
            )
            return False
        if not isinstance(pin, str) or not pin:
            # Carries no PIN to be wrong about, so it is the same class as a
            # missing token: an automation written without reading the FAQ.
            # Charging it would let that automation close the channel on the
            # user's own notification action.
            logger.warning(
                "policy decisions: refusing a %s event, it carries no PIN",
                APPROVAL_RESPONSE_EVENT,
            )
            return False
        if not await run_in_thread(verify_pin, self._data_dir, pin):
            failures = self._limiter.record_failure()
            logger.warning(
                "policy decisions: refusing a %s event, wrong PIN (%d of %d "
                "allowed within %.0f seconds)",
                APPROVAL_RESPONSE_EVENT,
                failures,
                MAX_FAILED_ATTEMPTS,
                FAILURE_WINDOW_SECONDS,
            )
            return False
        self._limiter.reset()
        return True
