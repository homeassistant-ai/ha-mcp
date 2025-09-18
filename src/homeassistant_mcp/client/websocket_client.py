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
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import websockets

from ..config import get_global_settings

logger = logging.getLogger(__name__)


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
        self.connected = False
        self.authenticated = False
        self.message_id = 0
        self.pending_requests: dict[int, asyncio.Future] = {}
        self.event_handlers: dict[str, set[Callable[[dict[str, Any]], Awaitable[None]]]] = {}
        self.background_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()  # Prevent concurrent WebSocket operations
        self._auth_messages: dict[str, dict[str, Any]] = {}  # Store auth messages
        self._lock_loop: asyncio.AbstractEventLoop | None = None  # Track which event loop created the lock

        # Parse URL to get WebSocket endpoint
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        self.ws_url = f"{scheme}://{parsed.netloc}/api/websocket"

    async def connect(self) -> bool:
        """Connect to Home Assistant WebSocket API.

        Returns:
            True if connection and authentication successful
        """
        try:
            logger.info(f"Connecting to Home Assistant WebSocket: {self.ws_url}")

            # Connect to WebSocket
            self.websocket = await websockets.connect(
                self.ws_url, ping_interval=30, ping_timeout=10
            )
            self.connected = True

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

            self.authenticated = True
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

        if self.websocket:
            await self.websocket.close()

        self.connected = False
        self.authenticated = False
        self.websocket = None
        self._auth_messages.clear()
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
            if message_type in self._auth_messages:
                return self._auth_messages.pop(message_type)
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
            self.connected = False
            self.authenticated = False

    async def _process_message(self, data: dict[str, Any]) -> None:
        """Process incoming WebSocket message."""
        message_type = data.get("type")
        message_id = data.get("id")

        # Handle authentication messages (store for auth sequence)
        if message_type in ["auth_required", "auth_ok", "auth_invalid"]:
            self._auth_messages[message_type] = data
            return

        # Handle command responses
        if message_id and message_id in self.pending_requests:
            future = self.pending_requests.pop(message_id)
            if not future.cancelled():
                future.set_result(data)
            return

        # Handle events
        if message_type == "event":
            # Check if this is a render_template event we're waiting for
            if (
                message_id
                and hasattr(self, "_render_template_events")
                and message_id in self._render_template_events
            ):
                future = self._render_template_events.pop(message_id)
                if not future.cancelled():
                    future.set_result(data)
                return

            # Handle other events with registered handlers
            event_type = data.get("event", {}).get("event_type")
            if event_type and event_type in self.event_handlers:
                for handler in self.event_handlers[event_type]:
                    try:
                        await handler(data["event"])
                    except Exception as e:
                        logger.error(f"Error in event handler: {e}")

    def _get_next_id(self) -> int:
        """Get next message ID."""
        self.message_id += 1
        return self.message_id

    async def send_command(self, command_type: str, **kwargs: Any) -> dict[str, Any]:
        """Send command and wait for response.

        Args:
            command_type: Type of command to send
            **kwargs: Command parameters

        Returns:
            Response from Home Assistant
        """
        if not self.authenticated:
            raise Exception("WebSocket not authenticated")

        message_id = self._get_next_id()
        message = {"id": message_id, "type": command_type, **kwargs}

        # Create future for response
        future: asyncio.Future[dict[str, Any]] = asyncio.Future()
        self.pending_requests[message_id] = future

        # Use lock to prevent concurrent WebSocket access
        async with self._send_lock:
            try:
                # Send message
                if not self.websocket:
                    raise Exception("WebSocket not connected")
                logger.debug(f"WebSocket sending: {message}")
                await self.websocket.send(json.dumps(message))

            except Exception as e:
                self.pending_requests.pop(message_id, None)
                raise e

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

        except TimeoutError:
            self.pending_requests.pop(message_id, None)
            raise Exception("Command timeout")
        except Exception as e:
            self.pending_requests.pop(message_id, None)
            raise e

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
        subscription_id = response.get("id")
        if not isinstance(subscription_id, int):
            raise Exception("Failed to get subscription ID")
        return subscription_id

    def add_event_handler(self, event_type: str, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Add event handler for specific event type.

        Args:
            event_type: Event type to handle (e.g., 'state_changed')
            handler: Async function to handle events
        """
        if event_type not in self.event_handlers:
            self.event_handlers[event_type] = set()
        self.event_handlers[event_type].add(handler)

    def remove_event_handler(self, event_type: str, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Remove event handler."""
        if event_type in self.event_handlers:
            self.event_handlers[event_type].discard(handler)

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
        return self.connected and self.authenticated

    def _ensure_lock(self) -> None:
        """Ensure lock is created in the current event loop."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        # If we have a lock from a different loop, reset it
        if (
            self._send_lock is not None
            and self._lock_loop is not None
            and self._lock_loop != current_loop
        ):
            logger.debug("Event loop changed, resetting WebSocket client lock")
            self._send_lock = asyncio.Lock()

        # Set the current loop reference
        self._lock_loop = current_loop

        # Create lock if it doesn't exist
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
            self._lock_loop = current_loop


class WebSocketManager:
    """Singleton manager for Home Assistant WebSocket connections."""

    _instance = None
    _client = None
    _current_loop: asyncio.AbstractEventLoop | None = None
    _lock: asyncio.Lock | None = None
    _lock_loop: asyncio.AbstractEventLoop | None = None

    def __new__(cls) -> "WebSocketManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._lock = None  # Don't create lock here - create in event loop
            cls._instance._lock_loop = None
        return cls._instance

    def _ensure_lock(self) -> None:
        """Ensure lock is created in the current event loop."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        # If we have a lock from a different loop, reset it
        if (
            self._lock is not None
            and self._lock_loop is not None
            and self._lock_loop != current_loop
        ):
            logger.debug("Event loop changed, resetting WebSocketManager lock")
            self._lock = None

        # Create lock if needed
        if self._lock is None:
            self._lock = asyncio.Lock()
            self._lock_loop = current_loop
            logger.debug("Created new WebSocketManager lock for current event loop")

    async def get_client(self) -> HomeAssistantWebSocketClient:
        """Get WebSocket client, creating connection if needed."""
        import asyncio

        current_loop = asyncio.get_event_loop()

        # Ensure lock is created in current event loop
        self._ensure_lock()

        # Use async lock to prevent race conditions during concurrent access
        if not self._lock:
            raise Exception("Lock not initialized")
        async with self._lock:
            # If event loop changed, disconnect old client
            if self._current_loop is not None and self._current_loop != current_loop:
                if self._client:
                    try:
                        await self._client.disconnect()
                    except:
                        pass  # Ignore errors during cleanup
                    self._client = None

            self._current_loop = current_loop

            if self._client and self._client.is_connected:
                return self._client

            # Create new client
            settings = get_global_settings()
            self._client = HomeAssistantWebSocketClient(
                settings.homeassistant_url, settings.homeassistant_token
            )

            # Connect
            connected = await self._client.connect()
            if not connected:
                raise Exception("Failed to connect to Home Assistant WebSocket")

            return self._client

    async def disconnect(self) -> None:
        """Disconnect WebSocket client."""
        # Ensure lock is created in current event loop
        self._ensure_lock()

        if not self._lock:
            raise Exception("Lock not initialized")
        async with self._lock:
            if self._client:
                await self._client.disconnect()
                self._client = None


# Global WebSocket manager instance
websocket_manager = WebSocketManager()


async def get_websocket_client() -> HomeAssistantWebSocketClient:
    """Get the global WebSocket client instance."""
    return await websocket_manager.get_client()
