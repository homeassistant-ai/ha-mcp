"""
WebSocket client for Home Assistant real-time communication.

This module handles WebSocket connections to Home Assistant for:
- Real-time state change monitoring
- Async device operation verification
- Live system updates
"""

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import ssl
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import websockets

from ..config import get_global_settings
from .rest_client import (
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
    _is_ssl_error,
)

logger = logging.getLogger(__name__)

# Matches the Supervisor's own Core-connection receive limit
# (MAX_MESSAGE_SIZE_FROM_CORE in home-assistant/supervisor, see supervisor
# issue #4392). On the add-on path frames above that limit die at the
# Supervisor proxy anyway, so a larger client-side cap adds nothing there.
# Registry list responses scale with entity count and arrive as ONE frame
# (the HA WebSocket API has no pagination); a ~6.4k-entity instance
# overflowed the previous 20MB cap (#1721).
MAX_WS_MESSAGE_BYTES = 64 * 1024 * 1024


def _extract_ws_error(error: Any) -> tuple[str, str | None]:
    """Split an HA WebSocket ``error`` payload into ``(message, code)``.

    HA replies to a failed command with ``{"error": {"code": ..., "message":
    ...}}``. Dict payloads yield both fields; a bare string (or other shape)
    yields ``str(error)`` as the message and ``None`` for the code. The code is
    threaded onto ``HomeAssistantCommandError`` so callers route on the stable
    ``code`` (e.g. ``unknown_command``) instead of the message text.
    """
    if isinstance(error, dict):
        return error.get("message", str(error)), error.get("code")
    return str(error), None


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
        """Cancel a pending request future if it exists."""
        future = self._pending_requests.pop(message_id, None)
        if future and not future.done():
            future.cancel()

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
        """Cancel a stored event future."""
        future = self._event_responses.pop(message_id, None)
        if future and not future.done():
            future.cancel()

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

        # Drop any subscription queues — readers wake on the close signal
        # we push, then a ``QueueShutDown`` (3.13) tells them the source
        # is gone. Using ``shutdown`` rather than just clearing the dict
        # so blocked ``queue.get()`` awaiters unblock instead of hanging.
        for queue in self._subscription_queues.values():
            queue.shutdown(immediate=True)
        self._subscription_queues.clear()

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


class HomeAssistantWebSocketClient:
    """WebSocket client for Home Assistant real-time communication."""

    def __init__(self, url: str, token: str, verify_ssl: bool | None = None):
        """Initialize WebSocket client.

        Args:
            url: Home Assistant URL (e.g., 'https://homeassistant.local:8123')
            token: Home Assistant long-lived access token
            verify_ssl: Whether to verify the HA server's TLS certificate
                for ``wss://`` connections. Defaults to
                ``settings.verify_ssl``. Pass False to allow self-signed
                certs or hostname mismatches.
        """
        self.base_url = url.rstrip("/")
        self.token = token
        if verify_ssl is None:
            try:
                verify_ssl = get_global_settings().verify_ssl
            except Exception as e:
                # A bad env var elsewhere should not silently flip TLS off:
                # log which key tripped and fall back to the secure default.
                logger.warning(
                    "Could not load settings while resolving verify_ssl "
                    "(%s); falling back to verify_ssl=True.",
                    e,
                )
                verify_ssl = True
        self.verify_ssl = verify_ssl
        self._warned_verify_disabled = False
        self.websocket: websockets.ClientConnection | None = None
        self.background_task: asyncio.Task | None = None
        self._send_lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._state = WebSocketConnectionState()
        # Reason the most recent connect() attempt failed (exception text),
        # or None. Surfaced by callers so the agent sees *why* a WebSocket
        # connection failed instead of an opaque "Failed to connect" string.
        self._last_connect_error: str | None = None

        # Parse URL to get WebSocket endpoint
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"

        # Handle Supervisor proxy case: http://supervisor/core -> ws://supervisor/core/websocket
        # For regular HA URLs: http://ha.local:8123 -> ws://ha.local:8123/api/websocket
        if parsed.path and parsed.path != "/":
            # Supervisor proxy or URL with path - use path + /websocket
            base_path = parsed.path.rstrip("/")
            self.ws_url = f"{scheme}://{parsed.netloc}{base_path}/websocket"
        else:
            # Standard Home Assistant URL - use /api/websocket
            self.ws_url = f"{scheme}://{parsed.netloc}/api/websocket"

    async def connect(self) -> bool:
        """Connect to Home Assistant WebSocket API.

        Returns:
            True if connection and authentication successful
        """
        try:
            logger.info(f"Connecting to Home Assistant WebSocket: {self.ws_url}")
            self._state.reset_connection()
            self._last_connect_error = None

            # Only configure an SSLContext for wss://; ws:// (Supervisor
            # proxy) doesn't use TLS and gets ssl=None.
            ssl_ctx: ssl.SSLContext | None = None
            if self.ws_url.startswith("wss://"):
                ssl_ctx = ssl.create_default_context()
                if not self.verify_ssl:
                    if not self._warned_verify_disabled:
                        # Once per client — pool reconnects/HA restarts
                        # otherwise flood logs with the same warning.
                        logger.warning(
                            "TLS verification disabled for Home Assistant "
                            "WebSocket (HA_VERIFY_SSL=false). Connecting to "
                            "%s with hostname/cert checks off.",
                            self.ws_url,
                        )
                        self._warned_verify_disabled = True
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE

            # Connect to WebSocket
            # Include Authorization header for Supervisor proxy compatibility
            # (required when connecting via http://supervisor/core/websocket)
            self.websocket = await websockets.connect(
                self.ws_url,
                ping_interval=30,
                ping_timeout=10,
                additional_headers={"Authorization": f"Bearer {self.token}"},
                ssl=ssl_ctx,
                max_size=MAX_WS_MESSAGE_BYTES,
            )
            self._state.mark_connected()

            # Start message handling task
            self.background_task = asyncio.create_task(self._message_handler())

            # Wait for auth_required message
            auth_msg = await self._wait_for_auth_message(
                message_type="auth_required", timeout=5
            )
            if not auth_msg:
                raise HomeAssistantConnectionError(
                    "Did not receive auth_required message"
                )

            # Send authentication
            await self._send_auth()

            # Wait for auth response
            auth_response = await self._wait_for_auth_message(
                message_type="auth_ok", timeout=5
            )
            if not auth_response:
                auth_invalid = await self._wait_for_auth_message(
                    message_type="auth_invalid", timeout=1
                )
                if auth_invalid:
                    raise HomeAssistantAuthError("Authentication failed: Invalid token")
                raise HomeAssistantConnectionError("Authentication timeout")

            self._state.mark_authenticated()
            logger.info("WebSocket connected and authenticated successfully")
            return True

        except Exception as e:
            self._last_connect_error = f"{type(e).__name__}: {e}"
            if _is_ssl_error(e) and self.verify_ssl:
                logger.error(
                    "WebSocket TLS verification failed for %s: %s. "
                    "If this is a self-signed certificate or hostname "
                    "mismatch, set HA_VERIFY_SSL=false to skip verification.",
                    self.ws_url,
                    e,
                )
            else:
                logger.error(f"WebSocket connection failed: {e}")
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        if self.background_task:
            self.background_task.cancel()
            try:
                await self.background_task
            except asyncio.CancelledError:
                # Expected: we just cancelled the task above; swallow the
                # propagated CancelledError so disconnect can finish cleanly.
                pass
            finally:
                self.background_task = None

        if self.websocket:
            await self.websocket.close()
            self.websocket = None

        self._state.mark_disconnected()
        logger.info("WebSocket disconnected")

    async def _send_auth(self) -> None:
        """Send authentication message."""
        if not self.websocket:
            raise HomeAssistantConnectionError("WebSocket not connected")
        auth_message = {"type": "auth", "access_token": self.token}
        await self.websocket.send(json.dumps(auth_message))

    async def _wait_for_auth_message(
        self, message_type: str, timeout: float = 5.0
    ) -> dict[str, Any] | None:
        """Wait for an authentication message type with timeout."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            message = self._state.consume_auth_message(message_type)
            if message:
                return message
            await asyncio.sleep(0.01)  # Small delay to prevent busy waiting

        return None

    async def _message_handler(self) -> None:
        """Background task to handle incoming WebSocket messages."""
        if not self.websocket:
            raise HomeAssistantConnectionError("WebSocket not connected")
        # None means a clean exit (the async-for loop simply ended); the
        # except blocks below fill this in so pending futures — and the
        # log — carry *why* the connection went away instead of a bare
        # "WebSocket connection closed" that gave no lead on #1721.
        close_reason: str | None = None
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    logger.debug(f"WebSocket received: {data}")
                    await self._process_message(data)
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON received: {e}")
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            # Prefer the frame we received (the peer closed on us); fall
            # back to the frame we sent (we failed the connection
            # ourselves, e.g. an over-max_size frame produces a sent
            # Close(1009, ...) with no frame received at all).
            close = e.rcvd if e.rcvd is not None else e.sent
            if close is not None:
                verb = "received" if e.rcvd is not None else "sent"
                close_reason = f"{verb} close code {close.code}"
                if close.reason:
                    close_reason = f"{close_reason} ({close.reason})"
            else:
                close_reason = "connection dropped without a close frame"

            log_message = f"WebSocket connection closed ({close_reason})"
            if close is None or close.code not in (1000, 1001):
                logger.warning(log_message)
            else:
                logger.info(log_message)
        except Exception as e:
            close_reason = str(e)
            logger.error(f"WebSocket message handler error: {e}")
        finally:
            self._state.mark_disconnected(close_reason)

    async def _process_message(self, data: dict[str, Any]) -> None:
        """Process incoming WebSocket message."""
        message_type = data.get("type")
        message_id = data.get("id")

        # Handle authentication messages (store for auth sequence)
        if message_type in ["auth_required", "auth_ok", "auth_invalid"]:
            self._state.store_auth_message(message_type, data)
            return

        # Handle command responses
        if message_id is not None:
            future = self._state.resolve_pending_request(message_id)
            if future:
                if not future.cancelled():
                    future.set_result(data)
                return

        # Handle events
        if message_type == "event":
            await self._handle_event_message(data, message_id)

    async def _handle_event_message(
        self, data: dict[str, Any], message_id: int | None
    ) -> None:
        """Handle an incoming event message."""
        if message_id is not None:
            # Continuous subscriptions take priority: a single ``hacs/subscribe``
            # can deliver many events sharing one id and the one-shot
            # ``_event_responses`` future would only catch the first.
            # Events delivered to a subscription queue do NOT also
            # fan out to ``add_event_handler`` listeners below — the
            # ``return`` here is intentional; subscribe-via-queue and
            # the legacy event-type registry are mutually exclusive
            # routes for a given message id.
            queue = self._state.get_subscription_queue(message_id)
            if queue is not None:
                try:
                    queue.put_nowait(data)
                except asyncio.QueueShutDown:
                    # Caller unsubscribed between dispatch and delivery —
                    # drop the event quietly rather than logging an
                    # error for an expected lifecycle race.
                    pass
                return

            render_future = self._state.resolve_event_response(message_id)
            if render_future:
                if not render_future.cancelled():
                    render_future.set_result(data)
                return

        event_type = data.get("event", {}).get("event_type")
        if event_type:
            for handler in self._state.get_event_handlers(event_type):
                try:
                    await handler(data["event"])
                except Exception as e:
                    # ``exc_info=True`` so handler bugs (AttributeError /
                    # KeyError / TypeError from schema-drift on the
                    # incoming event payload) leave a traceback rather
                    # than a one-line obscured error. Without this the
                    # dispatch loop keeps a single buggy handler from
                    # killing the WS, but the bug itself becomes
                    # invisible — handlers wired to ``asyncio.Event``
                    # nudges (see ``util_helpers._ws_wait_for_condition``)
                    # silently stop nudging and the calling waiter times
                    # out reporting "not found." #1395 silent-failure
                    # audit.
                    logger.error("Error in event handler: %s", e, exc_info=True)

    def _ensure_send_lock(self) -> None:
        """Ensure the send lock belongs to the current event loop."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if (
            self._send_lock is not None
            and self._lock_loop is not None
            and self._lock_loop != current_loop
        ):
            logger.debug("Event loop changed, resetting WebSocket send lock")
            self._send_lock = None

        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
            self._lock_loop = current_loop

    async def send_json_message(self, message: dict[str, Any]) -> None:
        """Send a raw JSON message over the WebSocket connection."""
        self._ensure_send_lock()
        if not self._send_lock:
            raise Exception("Send lock not initialized")

        async with self._send_lock:
            if not self.websocket:
                raise HomeAssistantConnectionError("WebSocket not connected")
            logger.debug(f"WebSocket sending: {message}")
            await self.websocket.send(json.dumps(message))

    def get_next_message_id(self) -> int:
        """Expose the next WebSocket message ID for external callers."""
        return self._state.next_message_id()

    def register_pending_response(
        self, message_id: int
    ) -> asyncio.Future[dict[str, Any]]:
        """Register a future that will resolve when the response arrives."""
        return self._state.register_pending_request(message_id)

    def cancel_pending_response(self, message_id: int) -> None:
        """Cancel and drop a pending response future."""
        self._state.cancel_pending_request(message_id)

    def register_event_response(
        self, message_id: int
    ) -> asyncio.Future[dict[str, Any]]:
        """Register a future for a follow-up event."""
        return self._state.register_event_response(message_id)

    def cancel_event_response(self, message_id: int) -> None:
        """Cancel and drop a stored event future."""
        self._state.cancel_event_response(message_id)

    async def send_command(self, command_type: str, **kwargs: Any) -> dict[str, Any]:
        """Send command and wait for response.

        Args:
            command_type: Type of command to send
            _wait_timeout: Seconds to wait for the response (consumed from
                ``kwargs``, not forwarded to Home Assistant). Defaults to 30s,
                which suits fast commands; long-running ones (e.g. a
                ``supervisor/api`` add-on install) must raise this so the
                client doesn't give up before Home Assistant replies.
            **kwargs: Command parameters (merged into the outgoing message)

        Returns:
            Response from Home Assistant
        """
        if not self._state.is_ready:
            # PRE-SEND and the ONLY provably-never-sent site: nothing is transmitted at
            # this entry guard. Raise the never-sent subtype so an at-most-once write
            # consumer can fall back to legacy safely (a subclass of
            # HomeAssistantConnectionError, so every existing broad handler is
            # unaffected). A later send() failure is NOT never-sent (see below).
            raise HomeAssistantCommandNotSent("WebSocket not authenticated")

        # Pull the wait timeout out of kwargs rather than making it a positional
        # parameter: callers unpack a ``dict[str, object]`` via
        # ``send_command(cmd, **message)``, and a typed positional param would
        # break that call shape under mypy. The leading underscore keeps it out
        # of the HA message namespace — HA WebSocket fields never start with
        # one — so it can never shadow a real command field when popped.
        wait_timeout: float = kwargs.pop("_wait_timeout", 30.0)

        message_id = self.get_next_message_id()
        message = {"id": message_id, "type": command_type, **kwargs}

        # Create future for response
        future = self.register_pending_response(message_id)

        try:
            await self.send_json_message(message)
        except Exception:
            # AMBIGUOUS, not never-sent: websocket.send() raising (e.g. a
            # ConnectionClosed detected mid-write) does NOT prove the frame was not
            # transmitted — bytes may already be on the socket when the close surfaces.
            # Re-raise the ORIGINAL exception unchanged so an at-most-once write
            # consumer treats it like a post-send drop (ambiguous -> partial, never
            # re-fired), NOT as never-sent; only the readiness guard above is provably
            # never-sent. Still cancel the pending future so it cannot leak.
            self.cancel_pending_response(message_id)
            raise

        # Wait for response outside the lock.
        try:
            response = await asyncio.wait_for(future, timeout=wait_timeout)
            logger.debug(f"WebSocket response for id {message_id}: {response}")

            # Process standard Home Assistant WebSocket response
            if response.get("type") == "result":
                if response.get("success") is False:
                    error_msg, error_code = _extract_ws_error(response.get("error", {}))
                    raise HomeAssistantCommandError(
                        f"Command failed: {error_msg}", error_code
                    )

                # Return success response according to HA WebSocket format
                return {
                    "success": response.get("success", True),
                    "result": response.get("result"),
                }
            elif response.get("type") == "pong":
                # Pong responses are normal keep-alive messages, handle silently
                return {"success": True, "type": "pong"}
            else:
                # Log unexpected response format
                logger.warning(
                    f"Unexpected WebSocket response type: {response.get('type')}"
                )
                return {"success": True, **response}

        except TimeoutError as e:
            self.cancel_pending_response(message_id)
            raise HomeAssistantCommandTimeout("Command timeout") from e
        except Exception:
            self.cancel_pending_response(message_id)
            raise

    async def send_command_with_event(
        self,
        command_type: str,
        wait_timeout: float = 10.0,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Send a command that returns a result followed by an event response.

        Some HA WebSocket commands (e.g. system_health/info, render_template)
        reply with an immediate result message and then deliver the actual data
        in a subsequent event message sharing the same message ID.

        Args:
            command_type: Type of command to send.
            wait_timeout: Seconds to wait for each response phase.
            **kwargs: Additional fields merged into the outgoing message.

        Returns:
            A (result_response, event_response) tuple.
        """
        if not self._state.is_ready:
            raise HomeAssistantConnectionError("WebSocket not authenticated")

        message_id = self.get_next_message_id()
        message = {"id": message_id, "type": command_type, **kwargs}

        result_future = self.register_pending_response(message_id)
        event_future = self.register_event_response(message_id)

        try:
            await self.send_json_message(message)
        except Exception:
            self.cancel_pending_response(message_id)
            self.cancel_event_response(message_id)
            raise

        try:
            result_response = await asyncio.wait_for(
                result_future, timeout=wait_timeout
            )
        except BaseException:
            self.cancel_pending_response(message_id)
            self.cancel_event_response(message_id)
            # A connection drop fails BOTH futures via reset_connection;
            # only result_future gets awaited on this path, so retrieve
            # event_future's exception too or asyncio logs an ERROR-level
            # "Future exception was never retrieved" when it is GC'd.
            if event_future.done() and not event_future.cancelled():
                event_future.exception()
            raise

        if not result_response.get("success"):
            self.cancel_event_response(message_id)
            error_msg, error_code = _extract_ws_error(result_response.get("error", {}))
            raise HomeAssistantCommandError(f"Command failed: {error_msg}", error_code)

        try:
            event_response = await asyncio.wait_for(event_future, timeout=wait_timeout)
        except TimeoutError:
            self.cancel_event_response(message_id)
            raise

        return result_response, event_response

    async def subscribe_events(self, event_type: str | None = None) -> int:
        """Subscribe to Home Assistant events.

        HA's WebSocket API identifies a subscription by the ``id`` of the
        original ``subscribe_events`` command — not by a field inside the
        ``result`` payload. ``handle_subscribe_events`` in HA Core
        (``websocket_api/commands.py``) ends with
        ``connection.send_result(msg["id"])``, and ``send_result(msg_id)``
        emits ``{"id": N, "type": "result", "success": true, "result": null}``.
        Subsequent event deliveries arrive as
        ``{"id": N, "type": "event", "event": {...}}`` with the same ``id``.

        The previous implementation called ``send_command`` (which discards
        the message_id it generated) and then looked for
        ``response["result"]["subscription"]``, a field HA does not send.
        That branch never matched, so this function always raised
        ``"Failed to get subscription ID"`` — even though the underlying
        subscription on HA's side WAS established. The ``WebSocketListenerService``
        treated the raised exception as a startup failure and left
        ``_listener_started = False``, so every device-control call
        repeatedly retried (and re-failed) and ``OperationManager.process_state_change``
        was never invoked, leaving every async operation in PENDING until
        ``OperationManager.get_operation`` flipped it to TIMEOUT after
        the 10s ``timeout_ms`` budget. Surfaced during PR #1375 HAOS log
        audit (3x "Failed to get subscription ID" per bulk-control test).

        Args:
            event_type: Specific event type to subscribe to (None for all)

        Returns:
            Subscription ID (the message_id used when subscribing)
        """
        if not self._state.is_ready:
            raise HomeAssistantConnectionError("WebSocket not authenticated")

        message_id = self.get_next_message_id()
        message: dict[str, Any] = {"id": message_id, "type": "subscribe_events"}
        if event_type:
            message["event_type"] = event_type

        future = self.register_pending_response(message_id)
        try:
            await self.send_json_message(message)
        except Exception:
            self.cancel_pending_response(message_id)
            raise

        try:
            response = await asyncio.wait_for(future, timeout=30.0)
        except TimeoutError:
            self.cancel_pending_response(message_id)
            raise

        if response.get("type") == "result" and response.get("success"):
            return message_id

        error_msg, error_code = _extract_ws_error(response.get("error", {}))
        raise HomeAssistantCommandError(
            f"subscribe_events failed: {error_msg}", error_code
        )

    async def unsubscribe_events(self, subscription_id: int) -> None:
        """Release a subscription previously returned by ``subscribe_events``.

        Used by short-lived waiters (``util_helpers.wait_for_*``) that need
        to drop the subscription as soon as their event arrives so the
        shared socket doesn't accumulate stale ``state_changed`` listeners.

        Exception policy (narrow, distinct log levels — Gemini #1382):

        - Transport-level loss (``OSError``): subscription is implicitly
          gone with the connection. Logged at ``debug`` so HA-mid-restart
          cleanup doesn't spam warnings.
        - HA-side rejection (``HomeAssistantCommandError``, e.g. "Subscription
          not found" after a server-side reset): unexpected during normal
          cleanup. Logged at ``warning`` so a real subscription leak is
          discoverable.
        - Everything else: propagates to the caller's ``finally`` so a
          programming bug (TypeError, AttributeError) fails loudly instead
          of being buried under a broad ``except``.
        """
        if not self._state.is_ready:
            logger.debug(
                "unsubscribe_events(%s) skipped: WebSocket not ready",
                subscription_id,
            )
            return
        try:
            await self.send_command("unsubscribe_events", subscription=subscription_id)
        except OSError as e:
            logger.debug(
                "unsubscribe_events(%s): transport lost during cleanup: %s",
                subscription_id,
                e,
            )
        except HomeAssistantCommandError as e:
            logger.warning(
                "unsubscribe_events(%s) rejected by HA: %s",
                subscription_id,
                e,
            )

    async def subscribe_command(
        self,
        command_type: str,
        *,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> tuple[int, asyncio.Queue[dict[str, Any]]]:
        """Send a subscribe-style command and return (subscription_id, queue).

        For commands that establish a long-lived stream sharing the
        command's ``id`` for every event (HA's ``subscribe_events``,
        HACS' ``hacs/subscribe`` with a ``signal`` field, etc.).
        ``subscribe_events`` has its own dedicated entrypoint above
        because it has additional callers wired to the legacy
        event-type handler registry; use this method for everything
        else.

        Returns:
            (subscription_id, queue) — ``await queue.get()`` yields each
            incoming ``{"id": N, "type": "event", "event": ...}`` payload.
            Cancel the subscription via :meth:`unsubscribe_command`.
        """
        if not self._state.is_ready:
            raise HomeAssistantConnectionError("WebSocket not authenticated")

        message_id = self.get_next_message_id()
        message: dict[str, Any] = {"id": message_id, "type": command_type, **kwargs}

        # Register the queue BEFORE sending so we never miss an event
        # that arrives between the result and the first ``get()``.
        queue = self._state.register_subscription_queue(message_id)
        result_future = self.register_pending_response(message_id)

        try:
            await self.send_json_message(message)
        except Exception:
            self._state.unregister_subscription_queue(message_id)
            self.cancel_pending_response(message_id)
            raise

        try:
            response = await asyncio.wait_for(result_future, timeout=timeout)
        except TimeoutError:
            self._state.unregister_subscription_queue(message_id)
            self.cancel_pending_response(message_id)
            raise

        if response.get("type") == "result" and response.get("success"):
            return message_id, queue

        self._state.unregister_subscription_queue(message_id)
        error_msg, error_code = _extract_ws_error(response.get("error", {}))
        raise HomeAssistantCommandError(
            f"subscribe_command({command_type!r}) failed: {error_msg}", error_code
        )

    async def unsubscribe_command(
        self,
        subscription_id: int,
        *,
        unsubscribe_type: str = "unsubscribe_events",
    ) -> None:
        """Tear down a subscription opened via :meth:`subscribe_command`.

        HACS' ``hacs/subscribe`` slots into HA's standard
        ``connection.subscriptions`` map, so ``unsubscribe_events`` cancels
        it the same way it cancels a native ``subscribe_events`` stream
        — ``unsubscribe_type`` is exposed only for the rare case a
        protocol introduces its own teardown command.
        """
        # Always drop the local queue first so any in-flight ``get()``
        # call wakes immediately, even if the HA-side teardown errors.
        self._state.unregister_subscription_queue(subscription_id)

        if not self._state.is_ready:
            logger.debug(
                "unsubscribe_command(%s) skipped: WebSocket not ready",
                subscription_id,
            )
            return
        try:
            await self.send_command(unsubscribe_type, subscription=subscription_id)
        except OSError as e:
            logger.debug(
                "unsubscribe_command(%s): transport lost during cleanup: %s",
                subscription_id,
                e,
            )
        except HomeAssistantCommandError as e:
            logger.warning(
                "unsubscribe_command(%s) rejected by HA: %s",
                subscription_id,
                e,
            )

    def add_event_handler(
        self,
        event_type: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Add event handler for specific event type.

        Args:
            event_type: Event type to handle (e.g., 'state_changed')
            handler: Async function to handle events
        """
        self._state.add_event_handler(event_type, handler)

    def remove_event_handler(
        self,
        event_type: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Remove event handler."""
        self._state.remove_event_handler(event_type, handler)

    async def get_states(self) -> dict[str, Any]:
        """Get all entity states via WebSocket."""
        return await self.send_command("get_states")

    async def get_config(self) -> dict[str, Any]:
        """Get Home Assistant configuration via WebSocket."""
        return await self.send_command("get_config")

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call Home Assistant service via WebSocket.

        Args:
            domain: Service domain (e.g., 'light')
            service: Service name (e.g., 'turn_on')
            service_data: Service parameters
            target: Service target (entity_id, area_id, etc.)

        Returns:
            Service call response
        """
        kwargs: dict[str, Any] = {"domain": domain, "service": service}

        if service_data:
            kwargs["service_data"] = service_data
        if target:
            kwargs["target"] = target

        return await self.send_command("call_service", **kwargs)

    async def ping(self) -> bool:
        """Ping Home Assistant to check connection health.

        Returns:
            True if ping successful
        """
        try:
            response = await self.send_command("ping")
            return response.get("type") == "pong"
        except Exception:
            return False

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected and authenticated."""
        return self._state.is_ready

    @property
    def last_connect_error(self) -> str | None:
        """Reason the most recent ``connect()`` attempt failed, or ``None``.

        Captured from the underlying exception (e.g. an auth timeout, a
        handshake HTTP/TLS error, or "Did not receive auth_required") so
        callers can surface *why* the connection failed instead of an
        opaque "Failed to connect to Home Assistant WebSocket".
        """
        return self._last_connect_error


MAX_POOL_SIZE = 50


def _log_stale_disconnect(future: "concurrent.futures.Future[None]") -> None:
    """Report how a disconnect scheduled on a stale event loop ended.

    Nothing awaits that future, so without this the outcome would be invisible
    at every log level: a ``concurrent.futures.Future`` never warns about an
    exception no one retrieved.
    """
    if future.cancelled():
        logger.debug("Stale WebSocket disconnect was cancelled with its loop")
        return
    error = future.exception()
    if error is not None:
        logger.debug("Stale WebSocket disconnect failed: %s", error)


class WebSocketManager:
    """Singleton manager for Home Assistant WebSocket connections.

    Maintains a pool of WebSocket connections keyed by (url, token) so that
    multiple OAuth users can have concurrent connections without interfering
    with each other.  The pool is bounded to ``MAX_POOL_SIZE`` entries; when
    this limit is exceeded the least-recently-used connection is evicted.
    """

    _instance = None
    _clients: dict[str, HomeAssistantWebSocketClient]
    _last_used: dict[str, float]
    _current_loop: asyncio.AbstractEventLoop | None = None
    _lock: asyncio.Lock | None = None
    _lock_loop: asyncio.AbstractEventLoop | None = None
    _client_factory: Callable[..., HomeAssistantWebSocketClient] | None = None

    def __new__(cls) -> "WebSocketManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._clients = {}
            cls._instance._last_used = {}
            cls._instance._lock = None
            cls._instance._lock_loop = None
            cls._instance._client_factory = HomeAssistantWebSocketClient
        return cls._instance

    def configure(
        self,
        *,
        client_factory: Callable[..., HomeAssistantWebSocketClient] | None = None,
    ) -> None:
        """Configure the manager with injectable dependencies."""
        if client_factory is not None:
            self._client_factory = client_factory

    def _ensure_lock(self) -> None:
        """Ensure lock is created in the current event loop."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if (
            self._lock is not None
            and self._lock_loop is not None
            and self._lock_loop != current_loop
        ):
            logger.debug("Event loop changed, resetting WebSocketManager lock")
            self._lock = None

        if self._lock is None:
            self._lock = asyncio.Lock()
            self._lock_loop = current_loop
            logger.debug("Created new WebSocketManager lock for current event loop")

    @staticmethod
    def _client_key(url: str, token: str) -> str:
        """Create a cache key from credentials."""
        return hashlib.sha256(f"{url.rstrip('/')}:{token}".encode()).hexdigest()

    @staticmethod
    def _effective_verify_ssl(verify_ssl: bool | None) -> bool:
        """Resolve the effective TLS-verification mode for the pool key."""
        if verify_ssl is not None:
            return verify_ssl
        try:
            return bool(get_global_settings().verify_ssl)
        except Exception as e:
            # Mirror HomeAssistantWebSocketClient.__init__: a bad env var
            # elsewhere should not crash pooling or silently flip TLS off.
            logger.warning(
                "Could not load settings while resolving the pool verify_ssl "
                "key (%s); falling back to verify_ssl=True.",
                e,
            )
            return True

    @staticmethod
    def _release_stale_clients(
        clients: list[HomeAssistantWebSocketClient],
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Best-effort cleanup of clients orphaned by an event-loop change.

        Never awaits: the connections belong to ``loop``, and awaiting them
        from the loop that replaced it is exactly the cross-loop failure this
        avoids. Each disconnect is scheduled on the owning loop and
        deliberately not waited for. A merely stopped loop is scheduled too:
        it still owns live transports and still accepts callbacks, so the
        disconnect runs once that loop is resumed, and otherwise stays an
        unrun callback like any other the abandoned loop still holds. Only a
        closed or unknown loop has nothing left to schedule on; its
        connection is abandoned and the socket closes when the orphaned
        transport is garbage-collected (with a ``ResourceWarning``), because
        closing a loop does not close the transports it carried. Either way
        the caller has already detached the pool, so a failure here cannot
        make it stale again.
        """
        if not clients:
            return
        if loop is None or loop.is_closed():
            logger.debug(
                "Abandoning %d stale WebSocket client(s): the owning event "
                "loop is gone",
                len(clients),
            )
            return
        for client in clients:
            coro = client.disconnect()
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:
                # The loop closed between the check above and now. Closing the
                # coroutine keeps it from surfacing as "never awaited". The
                # narrower window stays open by construction: a loop that
                # accepts the callback and then stops before draining it never
                # runs the coroutine at all, and ``run_coroutine_threadsafe``
                # needs the coroutine object up front, so there is nothing left
                # to reclaim from this side.
                coro.close()
                logger.debug(
                    "Could not schedule disconnect of a stale WebSocket client",
                    exc_info=True,
                )
                continue
            future.add_done_callback(_log_stale_disconnect)

    async def get_client(
        self,
        url: str | None = None,
        token: str | None = None,
        verify_ssl: bool | None = None,
    ) -> HomeAssistantWebSocketClient:
        """Get WebSocket client, creating connection if needed.

        Maintains a pool of connections keyed by credentials. In OAuth mode,
        each user gets their own connection. In non-OAuth mode, the global
        settings are used as the key.

        Args:
            url: Optional HA URL. If provided with token, uses these
                 credentials instead of global settings. This is required
                 for OAuth mode where each request has its own credentials.
            token: Optional HA token. Must be provided with url.
            verify_ssl: TLS verification override, keyed into the pool so a
                 ``HomeAssistantClient(verify_ssl=False)`` caller never shares
                 a connection built with default verification. ``None`` keeps
                 the client's own settings-based default.
        """
        current_loop = asyncio.get_event_loop()

        self._ensure_lock()

        if not self._lock:
            raise Exception("Lock not initialized")
        async with self._lock:
            previous_loop = self._current_loop
            stale_clients: list[HomeAssistantWebSocketClient] = []
            if previous_loop is not None and previous_loop is not current_loop:
                # Event loop changed: detach the pool BEFORE cleaning it up.
                # The pooled clients' futures belong to ``previous_loop``, so
                # awaiting their ``disconnect()`` here raises ``RuntimeError:
                # ... attached to a different loop``, which used to escape the
                # best-effort catch and leave both the pool and the loop
                # reference stale for every later call (issue #1994).
                stale_clients = list(self._clients.values())
                self._clients.clear()
                self._last_used.clear()

            self._current_loop = current_loop
            self._release_stale_clients(stale_clients, previous_loop)

            # Determine credentials to use
            if url and token:
                ws_url = url
                ws_token = token
            else:
                settings = get_global_settings()
                ws_url = settings.homeassistant_url
                ws_token = settings.homeassistant_token

            # Key on the EFFECTIVE verification mode: a caller passing the
            # resolved settings default (send_websocket_message) must share
            # the pooled connection with callers that omit the argument
            # (listener, HACS, installer) — only a genuine override such as
            # verify_ssl=False gets its own isolated connection.
            effective_verify_ssl = self._effective_verify_ssl(verify_ssl)
            key = (
                f"{self._client_key(ws_url, ws_token)}"
                f"|verify_ssl={effective_verify_ssl}"
            )

            # Return existing connected client for these credentials
            existing = self._clients.get(key)
            if existing and existing.is_connected:
                self._last_used[key] = time.monotonic()
                return existing

            # Remove stale client if present
            if existing:
                self._clients.pop(key, None)
                self._last_used.pop(key, None)

            factory = self._client_factory or HomeAssistantWebSocketClient
            client = (
                factory(ws_url, ws_token)
                if verify_ssl is None
                else factory(ws_url, ws_token, verify_ssl=verify_ssl)
            )

            connected = await client.connect()
            if not connected:
                reason = client.last_connect_error
                # Append only an actual string reason; the isinstance guard
                # keeps a non-str (e.g. a MagicMock in tests) from polluting
                # the message with a repr.
                detail = f": {reason}" if isinstance(reason, str) else ""
                raise HomeAssistantConnectionError(
                    "Failed to connect to Home Assistant WebSocket" + detail
                )

            self._clients[key] = client
            self._last_used[key] = time.monotonic()

            await self._evict_lru_if_needed()

            return client

    async def _evict_lru_if_needed(self) -> None:
        """Evict the least-recently-used connection if pool exceeds limit."""
        if len(self._clients) <= MAX_POOL_SIZE:
            return
        oldest_key = min(self._last_used, key=lambda k: self._last_used[k])
        stale = self._clients.pop(oldest_key, None)
        self._last_used.pop(oldest_key, None)
        if stale:
            try:
                await stale.disconnect()
            except (OSError, RuntimeError, asyncio.CancelledError):
                logger.warning(
                    "Error disconnecting evicted WebSocket client",
                    exc_info=True,
                )

    async def disconnect(self) -> None:
        """Disconnect all WebSocket clients."""
        self._ensure_lock()

        if not self._lock:
            raise Exception("Lock not initialized")
        async with self._lock:
            # Detach before disconnecting, for the same reason as the
            # loop-change branch in ``get_client``: a disconnect that raises
            # (a cross-loop ``RuntimeError`` when this runs on a loop other
            # than the pool's) must not leave the pool populated.
            clients = list(self._clients.values())
            self._clients.clear()
            self._last_used.clear()
            self._current_loop = None
            for client in clients:
                try:
                    await client.disconnect()
                except (OSError, RuntimeError, asyncio.CancelledError):
                    logger.warning(
                        "Error disconnecting WebSocket client", exc_info=True
                    )


# Global WebSocket manager instance
websocket_manager = WebSocketManager()


async def get_websocket_client(
    url: str | None = None,
    token: str | None = None,
    verify_ssl: bool | None = None,
) -> HomeAssistantWebSocketClient:
    """Get the global WebSocket client instance.

    Args:
        url: Optional HA URL for per-client credentials (OAuth mode).
        token: Optional HA token for per-client credentials (OAuth mode).
        verify_ssl: Optional TLS-verification override propagated into the
            pool key and client construction (None = settings default).
    """
    return await websocket_manager.get_client(
        url=url, token=token, verify_ssl=verify_ssl
    )
