"""
Background WebSocket listener for Home Assistant state changes.

This module provides a background service that listens to Home Assistant
WebSocket events and updates operation status in real-time.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from ..config import get_global_settings
from ..utils.operation_manager import get_operation_manager, update_pending_operations
from .websocket_client import HomeAssistantWebSocketClient, get_websocket_client

logger = logging.getLogger(__name__)


class WebSocketListenerService:
    """Background service for listening to Home Assistant WebSocket events."""

    def __init__(self) -> None:
        """Initialize the WebSocket listener service."""
        self.settings = get_global_settings()
        self.operation_manager = get_operation_manager()
        self.websocket_client: HomeAssistantWebSocketClient | None = None
        self.listener_task: asyncio.Task | None = None
        self.cleanup_task: asyncio.Task | None = None
        self.running = False
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self.stats: dict[str, Any] = {
            "events_processed": 0,
            "operations_updated": 0,
            "connection_errors": 0,
            "last_event_time": None,
            "start_time": None,
        }

    async def start(self) -> bool:
        """Start the WebSocket listener service.

        Returns:
            True if service started successfully
        """
        if self.running:
            logger.warning("WebSocket listener already running")
            return True

        try:
            # Get WebSocket client
            self.websocket_client = await get_websocket_client()

            # Subscribe to state change events
            await self.websocket_client.subscribe_events("state_changed")

            # Add event handler
            self.websocket_client.add_event_handler(
                "state_changed", self._handle_state_change
            )

            # Start background tasks
            self.listener_task = asyncio.create_task(self._connection_monitor())
            self.cleanup_task = asyncio.create_task(self._periodic_cleanup())

            self.running = True
            self.stats["start_time"] = datetime.now()

            logger.info("WebSocket listener service started successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to start WebSocket listener service: {e}")
            await self.stop()
            return False

    async def stop(self) -> None:
        """Stop the WebSocket listener service."""
        if not self.running:
            return

        self.running = False

        # Cancel background tasks
        if self.listener_task and not self.listener_task.done():
            self.listener_task.cancel()
            try:
                await self.listener_task
            except asyncio.CancelledError:
                pass

        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass

        # Remove event handler if WebSocket client exists
        if self.websocket_client:
            self.websocket_client.remove_event_handler(
                "state_changed", self._handle_state_change
            )

        logger.info("WebSocket listener service stopped")

    async def _handle_state_change(self, event: dict[str, Any]) -> None:
        """Handle state change events from Home Assistant.

        Args:
            event: State change event data
        """
        try:
            events_processed = self.stats["events_processed"]
            if isinstance(events_processed, int):
                self.stats["events_processed"] = events_processed + 1
            self.stats["last_event_time"] = datetime.now()

            # Extract event data
            entity_id = event.get("entity_id")
            new_state = event.get("new_state")
            old_state = event.get("old_state")

            if not entity_id or not new_state:
                return

            # Log significant state changes for debugging
            if old_state and old_state.get("state") != new_state.get("state"):
                logger.debug(
                    f"State change: {entity_id} {old_state.get('state')} -> {new_state.get('state')}"
                )

            # Update pending operations
            updated_ops = update_pending_operations(entity_id, new_state)
            if updated_ops:
                operations_updated = self.stats["operations_updated"]
                if isinstance(operations_updated, int):
                    self.stats["operations_updated"] = operations_updated + len(updated_ops)
                logger.info(f"Updated {len(updated_ops)} operations for {entity_id}")

        except Exception as e:
            logger.error(f"Error handling state change event: {e}")

    async def _connection_monitor(self) -> None:
        """Monitor WebSocket connection health."""
        while self.running:
            try:
                if self.websocket_client and self.websocket_client.is_connected:
                    # Ping Home Assistant to check connection
                    ping_success = await self.websocket_client.ping()
                    if not ping_success:
                        logger.warning("WebSocket ping failed")
                        connection_errors = self.stats["connection_errors"]
                        if isinstance(connection_errors, int):
                            self.stats["connection_errors"] = connection_errors + 1
                else:
                    logger.warning("WebSocket connection lost")
                    connection_errors = self.stats["connection_errors"]
                    if isinstance(connection_errors, int):
                        self.stats["connection_errors"] = connection_errors + 1

                    # Try to reconnect
                    try:
                        self.websocket_client = await get_websocket_client()
                        await self.websocket_client.subscribe_events("state_changed")
                        self.websocket_client.add_event_handler(
                            "state_changed", self._handle_state_change
                        )
                        logger.info("WebSocket reconnected successfully")
                    except Exception as e:
                        logger.error(f"WebSocket reconnection failed: {e}")

                # Wait before next health check
                await asyncio.sleep(30)  # Check every 30 seconds

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Connection monitor error: {e}")
                await asyncio.sleep(30)

    async def _periodic_cleanup(self) -> None:
        """Periodic cleanup of expired operations."""
        while self.running:
            try:
                # Clean up expired operations every 5 minutes
                self.operation_manager.cleanup_expired_operations()
                await asyncio.sleep(300)  # 5 minutes

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup task error: {e}")
                await asyncio.sleep(300)

    def get_status(self) -> dict[str, Any]:
        """Get service status and statistics.

        Returns:
            Dictionary with service status and statistics
        """
        uptime: float | None = None
        start_time = self.stats["start_time"]
        if isinstance(start_time, datetime):
            uptime = (datetime.now() - start_time).total_seconds()

        return {
            "running": self.running,
            "websocket_connected": (
                self.websocket_client.is_connected if self.websocket_client else False
            ),
            "uptime_seconds": uptime,
            "statistics": {
                **self.stats,
                "last_event_time": (
                    self.stats["last_event_time"].isoformat()
                    if isinstance(self.stats["last_event_time"], datetime)
                    else None
                ),
                "start_time": (
                    self.stats["start_time"].isoformat()
                    if isinstance(self.stats["start_time"], datetime)
                    else None
                ),
            },
            "operation_summary": self.operation_manager.get_operations_summary(),
        }

    async def force_reconnect(self) -> bool:
        """Force a WebSocket reconnection.

        Returns:
            True if reconnection successful
        """
        try:
            if self.websocket_client:
                self.websocket_client.remove_event_handler(
                    "state_changed", self._handle_state_change
                )

            self.websocket_client = await get_websocket_client()
            await self.websocket_client.subscribe_events("state_changed")
            self.websocket_client.add_event_handler(
                "state_changed", self._handle_state_change
            )

            logger.info("Forced WebSocket reconnection successful")
            return True

        except Exception as e:
            logger.error(f"Forced reconnection failed: {e}")
            connection_errors = self.stats["connection_errors"]
            if isinstance(connection_errors, int):
                self.stats["connection_errors"] = connection_errors + 1
            return False


# Global listener service instance
_listener_service: WebSocketListenerService | None = None
_listener_lock: asyncio.Lock | None = None


async def get_listener_service() -> WebSocketListenerService:
    """Get the global WebSocket listener service instance."""
    global _listener_service, _listener_lock
    import asyncio

    current_loop = asyncio.get_event_loop()

    # Initialize lock if needed (lazy initialization for event loop compatibility)
    if _listener_lock is None:
        _listener_lock = asyncio.Lock()

    # Use async lock to prevent race conditions during concurrent access
    async with _listener_lock:
        # If event loop changed or service doesn't exist, create new instance
        if _listener_service is None or (
            hasattr(_listener_service, "_event_loop")
            and _listener_service._event_loop != current_loop
        ):
            # Stop existing service if it exists
            if _listener_service is not None:
                try:
                    await _listener_service.stop()
                except Exception as e:
                    logger.debug(f"Error stopping previous listener service: {e}")

            _listener_service = WebSocketListenerService()
            _listener_service._event_loop = current_loop

    return _listener_service


async def start_websocket_listener() -> bool:
    """Start the global WebSocket listener service."""
    service = await get_listener_service()
    if service.running:
        logger.debug("WebSocket listener service already running")
        return True
    return await service.start()


async def stop_websocket_listener() -> None:
    """Stop the global WebSocket listener service."""
    global _listener_service
    if _listener_service:
        await _listener_service.stop()
        _listener_service = None


async def get_listener_status() -> dict[str, Any]:
    """Get WebSocket listener service status."""
    service = await get_listener_service()
    return service.get_status()


class WebSocketContextManager:
    """Context manager for WebSocket listener lifecycle."""

    def __init__(self) -> None:
        self.service: WebSocketListenerService | None = None

    async def __aenter__(self) -> WebSocketListenerService:
        """Start WebSocket listener."""
        service = await get_listener_service()
        success = await service.start()
        if not success:
            raise Exception("Failed to start WebSocket listener")
        self.service = service
        return service

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object) -> None:
        """Stop WebSocket listener."""
        if self.service:
            await self.service.stop()


def websocket_listener_context() -> WebSocketContextManager:
    """Create a WebSocket listener context manager."""
    return WebSocketContextManager()
