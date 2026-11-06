"""Request, event and subscription bookkeeping for one WebSocket connection."""

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from .rest_client import HomeAssistantConnectionError


class WebSocketConnectionState:
    """Encapsulates mutable state used by the WebSocket client."""

    def __init__(self) -> None:
        self.connected = False
        self.authenticated = False
        self._message_id = 0
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._auth_messages: dict[str, dict[str, Any]] = {}
        self._event_responses: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: dict[
            str, set[Callable[[dict[str, Any]], Awaitable[None]]]
        ] = defaultdict(set)
        # Continuous-subscription queues keyed by the subscribe command's
        # message_id. Long-lived subscriptions (e.g. HACS' ``hacs/subscribe``)
        # deliver many events sharing one id; the one-shot
        # ``_event_responses`` future can't handle that. When a queue is
        # registered for a given id, every event with that id is pushed
        # into it instead of going to ``event_type``-keyed handlers.
        self._subscription_queues: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        # Ids of subscribe commands whose caller gave up before Home Assistant
        # acknowledged them. See ``take_abandoned_subscription``.
        self._abandoned_subscriptions: set[int] = set()

    def next_message_id(self) -> int:
        """Reserve the next available WebSocket message identifier."""
        self._message_id += 1
        return self._message_id

    def register_pending_request(
        self, message_id: int
    ) -> asyncio.Future[dict[str, Any]]:
        """Create and register a future for a pending command response."""
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending_requests[message_id] = future
        return future

    def resolve_pending_request(
        self, message_id: int
    ) -> asyncio.Future[dict[str, Any]] | None:
        """Resolve and remove a pending request future."""
        return self._pending_requests.pop(message_id, None)

    def cancel_pending_request(self, message_id: int) -> None:
        """Remove a pending request future, cancelling it or draining its exception."""
        future = self._pending_requests.pop(message_id, None)
        if future and not future.done():
            future.cancel()
        elif future and not future.cancelled():
            # A done future may hold the exception reset_connection set on it;
            # retrieve it here so dropping the last reference doesn't log an
            # ERROR-level "Future exception was never retrieved" at GC.
            future.exception()

    def register_event_response(
        self, message_id: int
    ) -> asyncio.Future[dict[str, Any]]:
        """Create and register a future for a follow-up event."""
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._event_responses[message_id] = future
        return future

    def resolve_event_response(
        self, message_id: int
    ) -> asyncio.Future[dict[str, Any]] | None:
        """Resolve a stored event future."""
        return self._event_responses.pop(message_id, None)

    def cancel_event_response(self, message_id: int) -> None:
        """Remove a pending event future, cancelling it or draining its exception."""
        future = self._event_responses.pop(message_id, None)
        if future and not future.done():
            future.cancel()
        elif future and not future.cancelled():
            # Same GC guard as cancel_pending_request above.
            future.exception()

    def mark_abandoned_subscription(self, message_id: int) -> None:
        """Record a subscribe command whose acknowledgement nobody awaits."""
        self._abandoned_subscriptions.add(message_id)

    def take_abandoned_subscription(self, message_id: int) -> bool:
        """Forget an abandoned subscribe command; True if it was one.

        Home Assistant may register a subscription only after the caller has
        given up and the immediate release answered ``not_found`` — a
        ``render_template`` setup waits out the template's own timeout first.
        Its late acknowledgement then arrives with no pending request, and
        this is how the dispatch recognises that it still has to release it.
        """
        if message_id in self._abandoned_subscriptions:
            self._abandoned_subscriptions.discard(message_id)
            return True
        return False

    def store_auth_message(self, message_type: str, data: dict[str, Any]) -> None:
        """Store an authentication handshake message."""
        self._auth_messages[message_type] = data

    def consume_auth_message(self, message_type: str) -> dict[str, Any] | None:
        """Retrieve and remove an authentication message if present."""
        return self._auth_messages.pop(message_type, None)

    def reset_connection(self, close_reason: str | None = None) -> None:
        """Reset connection-specific state while preserving handlers.

        Args:
            close_reason: Human-readable description of why the connection
                went away (e.g. a close code/reason pair), included in the
                error surfaced to any request still awaiting a response.
        """
        self.connected = False
        self.authenticated = False
        self._message_id = 0

        # ``future.cancel()`` makes awaiters see ``asyncio.CancelledError``,
        # a BaseException that skips every ``except Exception`` handler in
        # the tool layer and reaches the MCP SDK, which treats it as a
        # client-initiated cancellation, suppresses the response entirely,
        # and leaves the MCP client hanging until its own timeout (#1721).
        # ``set_exception`` with a normal exception propagates through
        # ``except Exception`` as expected instead.
        message = (
            "WebSocket connection to Home Assistant closed while waiting for a response"
        )
        if close_reason:
            message = f"{message} ({close_reason})"

        for future in self._pending_requests.values():
            if not future.done():
                # A fresh instance per future: sharing one exception object
                # across multiple ``set_exception`` calls means the second
                # raise attaches a traceback to an exception already
                # associated with another future's stack.
                future.set_exception(HomeAssistantConnectionError(message))
        self._pending_requests.clear()

        for future in self._event_responses.values():
            if not future.done():
                future.set_exception(HomeAssistantConnectionError(message))
        self._event_responses.clear()

        # Drop any subscription queues. ``shutdown`` rather than just clearing
        # the dict, so blocked ``queue.get()`` awaiters unblock instead of
        # hanging; not ``immediate``, so a reader still gets the events that
        # arrived before the close, then ``QueueShutDown`` (3.13).
        for queue in self._subscription_queues.values():
            queue.shutdown()
        self._subscription_queues.clear()
        # Subscriptions die with the socket, and ids restart on the next one.
        self._abandoned_subscriptions.clear()

        self._auth_messages.clear()

    def mark_connected(self) -> None:
        """Mark the socket as connected but not yet authenticated."""
        self.connected = True
        self.authenticated = False

    def mark_authenticated(self) -> None:
        """Mark the socket as authenticated and ready for commands."""
        self.authenticated = True

    def mark_disconnected(self, close_reason: str | None = None) -> None:
        """Reset connection state when the socket is closed."""
        self.reset_connection(close_reason)

    @property
    def is_ready(self) -> bool:
        """Whether the connection is active and authenticated."""
        return self.connected and self.authenticated

    def add_event_handler(
        self, event_type: str, handler: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Register an async handler for a Home Assistant event type."""
        self._event_handlers[event_type].add(handler)

    def remove_event_handler(
        self, event_type: str, handler: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Remove an event handler and prune empty handler sets."""
        if event_type in self._event_handlers:
            self._event_handlers[event_type].discard(handler)
            if not self._event_handlers[event_type]:
                self._event_handlers.pop(event_type, None)

    def get_event_handlers(
        self, event_type: str
    ) -> tuple[Callable[[dict[str, Any]], Awaitable[None]], ...]:
        """Return registered handlers for a given event type."""
        if event_type not in self._event_handlers:
            return ()
        return tuple(self._event_handlers[event_type])

    def register_subscription_queue(
        self, message_id: int
    ) -> asyncio.Queue[dict[str, Any]]:
        """Register a queue for continuous-subscription event delivery."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscription_queues[message_id] = queue
        return queue

    def get_subscription_queue(
        self, message_id: int
    ) -> asyncio.Queue[dict[str, Any]] | None:
        """Return the queue for a subscription id, or None."""
        return self._subscription_queues.get(message_id)

    def unregister_subscription_queue(self, message_id: int) -> None:
        """Drop a subscription queue and wake any blocked readers."""
        queue = self._subscription_queues.pop(message_id, None)
        if queue is not None:
            # ``shutdown(immediate=True)`` raises ``QueueShutDown`` in any
            # pending ``get()`` so a waiter doesn't deadlock when the
            # caller decides to stop listening.
            queue.shutdown(immediate=True)
