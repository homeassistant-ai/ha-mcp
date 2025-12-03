"""
WebSocket client for Home Assistant real-time communication.

This module handles WebSocket connections to Home Assistant for:
- Real-time state change monitoring
- Async device operation verification
- Live system updates
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import websockets

from ..config import get_global_settings

logger = logging.getLogger(__name__)


class WebSocketConnectionState:
    """Encapsulates mutable state used by the WebSocket client."""

    def __init__(self) -> None:
        self.connected = False
        self.authenticated = False
        self._message_id = 0
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._auth_messages: dict[str, dict[str, Any]] = {}
        self._render_template_events: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: dict[
            str, set[Callable[[dict[str, Any]], Awaitable[None]]]
        ] = defaultdict(set)

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

    def register_render_template_event(
        self, message_id: int
    ) -> asyncio.Future[dict[str, Any]]:
        """Create and register a future for a render_template follow-up event."""
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._render_template_events[message_id] = future
        return future

    def resolve_render_template_event(
        self, message_id: int
    ) -> asyncio.Future[dict[str, Any]] | None:
        """Resolve a stored render_template event future."""
        return self._render_template_events.pop(message_id, None)

    def cancel_render_template_event(self, message_id: int) -> None:
        """Cancel a stored render_template event future."""
        future = self._render_template_events.pop(message_id, None)
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

        for future in self._render_template_events.values():
            if not future.done():
                future.cancel()
        self._render_template_events.clear()

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


class HomeAssistantWebSocketClient:
    """WebSocket client for Home Assistant real-time communication."""

    def __init__(self, url: str, token: str):
        """Initialize WebSocket client.

        Args:
            url: Home Assistant URL (e.g., 'https://homeassistant.local:8123')
            token: Home Assistant long-lived access token
        """
        self.base_url = url.rstrip("/")
        self.token = token
        self.websocket: websockets.ClientConnection | None = None
        self.background_task: asyncio.Task | None = None
        self._send_lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._state = WebSocketConnectionState()

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

            # Connect to WebSocket
            # Include Authorization header for Supervisor proxy compatibility
            # (required when connecting via http://supervisor/core/websocket)
            self.websocket = await websockets.connect(
                self.ws_url,
                ping_interval=30,
                ping_timeout=10,
                additional_headers={"Authorization": f"Bearer {self.token}"},
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
                raise Exception("Did not receive auth_required message")

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
            if message_id is not None:
                render_future = self._state.resolve_render_template_event(message_id)
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
                        logger.error(f"Error in event handler: {e}")

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

    def register_render_template_event(
        self, message_id: int
    ) -> asyncio.Future[dict[str, Any]]:
        """Register a future for a render_template follow-up event."""
        return self._state.register_render_template_event(message_id)

    def cancel_render_template_event(self, message_id: int) -> None:
        """Cancel and drop a stored render_template event future."""
        self._state.cancel_render_template_event(message_id)

    async def send_command(self, command_type: str, **kwargs: Any) -> dict[str, Any]:
        """Send command and wait for response.

        Args:
            command_type: Type of command to send
            **kwargs: Command parameters

        Returns:
            Response from Home Assistant
        """
        if not self._state.is_ready:
            raise Exception("WebSocket not authenticated")

        message_id = self.get_next_message_id()
        message = {"id": message_id, "type": command_type, **kwargs}

        # Create future for response
        future = self.register_pending_response(message_id)

        try:
            await self.send_json_message(message)
        except Exception:
            self.cancel_pending_response(message_id)
            raise

        # Wait for response outside the lock (30 second timeout)
        try:
            response = await asyncio.wait_for(future, timeout=30.0)
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
                    raise Exception(f"Command failed: {error_msg}")

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

        except asyncio.TimeoutError:
            self.cancel_pending_response(message_id)
            raise Exception("Command timeout")
        except Exception:
            self.cancel_pending_response(message_id)
            raise

    async def subscribe_events(self, event_type: str | None = None) -> int:
        """Subscribe to Home Assistant events.

        Args:
            event_type: Specific event type to subscribe to (None for all)

        Returns:
            Subscription ID
        """
        kwargs = {}
        if event_type:
            kwargs["event_type"] = event_type

        response = await self.send_command("subscribe_events", **kwargs)
        result = response.get("result")
        if isinstance(result, dict):
            subscription_id = result.get("subscription")
            if isinstance(subscription_id, int):
                return subscription_id

        raise Exception("Failed to get subscription ID")

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



class WebSocketManager:
    """Singleton manager for Home Assistant WebSocket connections."""

    _instance = None
    _client = None
    _current_loop: asyncio.AbstractEventLoop | None = None
    _lock: asyncio.Lock | None = None
    _lock_loop: asyncio.AbstractEventLoop | None = None
    _client_factory: Callable[[str, str], HomeAssistantWebSocketClient] | None = None

    def __new__(cls) -> "WebSocketManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._lock = None
            cls._instance._lock_loop = None
            cls._instance._client_factory = HomeAssistantWebSocketClient
        return cls._instance

    def configure(
        self,
        *,
        client_factory: Callable[[str, str], HomeAssistantWebSocketClient] | None = None,
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

    async def get_client(self) -> HomeAssistantWebSocketClient:
        """Get WebSocket client, creating connection if needed."""
        current_loop = asyncio.get_event_loop()

        self._ensure_lock()

        if not self._lock:
            raise Exception("Lock not initialized")
        async with self._lock:
            if self._current_loop is not None and self._current_loop != current_loop:
                if self._client:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                    self._client = None

            self._current_loop = current_loop

            if self._client and self._client.is_connected:
                return self._client

            settings = get_global_settings()
            factory = self._client_factory or HomeAssistantWebSocketClient
            self._client = factory(
                settings.homeassistant_url, settings.homeassistant_token
            )

            connected = await self._client.connect()
            if not connected:
                raise Exception("Failed to connect to Home Assistant WebSocket")

            return self._client

    async def disconnect(self) -> None:
        """Disconnect WebSocket client."""
        self._ensure_lock()

        if not self._lock:
            raise Exception("Lock not initialized")
        async with self._lock:
            if self._client:
                await self._client.disconnect()
                self._client = None
                self._current_loop = None


# Global WebSocket manager instance
websocket_manager = WebSocketManager()


async def get_websocket_client() -> HomeAssistantWebSocketClient:
    """Get the global WebSocket client instance."""
    return await websocket_manager.get_client()
