"""
WebSocket client for Home Assistant real-time communication.

This module handles WebSocket connections to Home Assistant for:
- Real-time state change monitoring
- Async device operation verification
- Live system updates
"""

import asyncio
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
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
    _is_ssl_error,
)

logger = logging.getLogger(__name__)


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

    def reset_connection(self) -> None:
        """Reset connection-specific state while preserving handlers."""
        self.connected = False
        self.authenticated = False
        self._message_id = 0

        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()

        for future in self._event_responses.values():
            if not future.done():
                future.cancel()
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

    def mark_disconnected(self) -> None:
        """Reset connection state when the socket is closed."""
        self.reset_connection()

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
                # Increase max message size to 20MB for large responses
                # (e.g., HACS repository list can be 2MB+)
                max_size=20 * 1024 * 1024,
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
                    raise Exception("Authentication failed: Invalid token")
                raise Exception("Authentication timeout")

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
            raise Exception("WebSocket not connected")
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
            raise Exception("WebSocket not connected")
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
        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket connection closed")
        except Exception as e:
            logger.error(f"WebSocket message handler error: {e}")
        finally:
            self._state.mark_disconnected()

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
                raise Exception("WebSocket not connected")
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
            raise HomeAssistantConnectionError("WebSocket not authenticated")

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
            self.cancel_pending_response(message_id)
            raise

        # Wait for response outside the lock.
        try:
            response = await asyncio.wait_for(future, timeout=wait_timeout)
            logger.debug(f"WebSocket response for id {message_id}: {response}")

            # Process standard Home Assistant WebSocket response
            if response.get("type") == "result":
                if response.get("success") is False:
                    error = response.get("error", {})
                    error_msg = (
                        error.get("message", str(error))
                        if isinstance(error, dict)
                        else str(error)
                    )
                    raise HomeAssistantCommandError(f"Command failed: {error_msg}")

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
        except TimeoutError:
            self.cancel_pending_response(message_id)
            self.cancel_event_response(message_id)
            raise

        if not result_response.get("success"):
            self.cancel_event_response(message_id)
            error = result_response.get("error", {})
            error_msg = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            raise HomeAssistantCommandError(f"Command failed: {error_msg}")

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

        error = response.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        raise HomeAssistantCommandError(f"subscribe_events failed: {error_msg}")

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
        error = response.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        raise HomeAssistantCommandError(
            f"subscribe_command({command_type!r}) failed: {error_msg}"
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
    _client_factory: Callable[[str, str], HomeAssistantWebSocketClient] | None = None

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
        client_factory: Callable[[str, str], HomeAssistantWebSocketClient]
        | None = None,
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

    async def get_client(
        self,
        url: str | None = None,
        token: str | None = None,
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
        """
        current_loop = asyncio.get_event_loop()

        self._ensure_lock()

        if not self._lock:
            raise Exception("Lock not initialized")
        async with self._lock:
            if self._current_loop is not None and self._current_loop != current_loop:
                # Event loop changed — disconnect all clients
                for client in self._clients.values():
                    try:
                        await client.disconnect()
                    except (OSError, asyncio.CancelledError):
                        # Best-effort cleanup — failure is expected when the
                        # event loop changed and connections are stale.
                        logger.debug(
                            "Ignoring error disconnecting stale WebSocket client",
                            exc_info=True,
                        )
                self._clients.clear()
                self._last_used.clear()

            self._current_loop = current_loop

            # Determine credentials to use
            if url and token:
                ws_url = url
                ws_token = token
            else:
                settings = get_global_settings()
                ws_url = settings.homeassistant_url
                ws_token = settings.homeassistant_token

            key = self._client_key(ws_url, ws_token)

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
            client = factory(ws_url, ws_token)

            connected = await client.connect()
            if not connected:
                reason = client.last_connect_error
                # Append only an actual string reason; the isinstance guard
                # keeps a non-str (e.g. a MagicMock in tests) from polluting
                # the message with a repr.
                detail = f": {reason}" if isinstance(reason, str) else ""
                raise Exception(
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
            except (OSError, asyncio.CancelledError):
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
            for client in self._clients.values():
                try:
                    await client.disconnect()
                except (OSError, asyncio.CancelledError):
                    logger.warning(
                        "Error disconnecting WebSocket client", exc_info=True
                    )
            self._clients.clear()
            self._last_used.clear()
            self._current_loop = None


# Global WebSocket manager instance
websocket_manager = WebSocketManager()


async def get_websocket_client(
    url: str | None = None,
    token: str | None = None,
) -> HomeAssistantWebSocketClient:
    """Get the global WebSocket client instance.

    Args:
        url: Optional HA URL for per-client credentials (OAuth mode).
        token: Optional HA token for per-client credentials (OAuth mode).
    """
    return await websocket_manager.get_client(url=url, token=token)
