"""Home Assistant MCP Server."""

import asyncio
import logging
import os
import signal
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Shutdown configuration
SHUTDOWN_TIMEOUT_SECONDS = 2.0

# Global shutdown state
_shutdown_event: asyncio.Event | None = None
_shutdown_in_progress = False


def _create_server():
    """Create server instance (deferred to avoid import during smoke test)."""
    from ha_mcp.server import HomeAssistantSmartMCPServer  # type: ignore[import-not-found]
    return HomeAssistantSmartMCPServer()


# Lazy server creation - only create when needed
_server = None


def _get_mcp():
    """Get the MCP instance, creating server if needed."""
    global _server
    if _server is None:
        _server = _create_server()
    return _server.mcp


def _get_server():
    """Get the server instance, creating if needed."""
    global _server
    if _server is None:
        _server = _create_server()
    return _server


# For module-level access (e.g., fastmcp.json referencing ha_mcp.__main__:mcp)
# This is accessed when the module is imported, so we need deferred creation
class _DeferredMCP:
    """Wrapper that defers MCP creation until actually accessed."""
    def __getattr__(self, name: str) -> Any:
        return getattr(_get_mcp(), name)

    def run(self, *args: Any, **kwargs: Any) -> None:
        return _get_mcp().run(*args, **kwargs)


mcp = _DeferredMCP()


async def _cleanup_resources() -> None:
    """Clean up all server resources gracefully."""
    global _server

    logger.info("Cleaning up server resources...")

    # Close WebSocket listener service if running
    try:
        from ha_mcp.client.websocket_listener import stop_websocket_listener
        await stop_websocket_listener()
        logger.debug("WebSocket listener stopped")
    except Exception as e:
        logger.debug(f"WebSocket listener cleanup: {e}")

    # Close WebSocket manager connections
    try:
        from ha_mcp.client.websocket_client import websocket_manager
        await websocket_manager.disconnect()
        logger.debug("WebSocket manager disconnected")
    except Exception as e:
        logger.debug(f"WebSocket manager cleanup: {e}")

    # Close the server's HTTP client
    if _server is not None:
        try:
            await _server.close()
            logger.debug("Server closed")
        except Exception as e:
            logger.debug(f"Server cleanup: {e}")

    logger.info("Server resources cleaned up")


def _signal_handler(signum: int, frame: Any) -> None:
    """Handle shutdown signals (SIGTERM, SIGINT).

    This handler initiates graceful shutdown on first signal.
    On second signal, forces immediate exit.
    """
    global _shutdown_in_progress, _shutdown_event

    sig_name = signal.Signals(signum).name

    if _shutdown_in_progress:
        # Second signal - force exit
        logger.warning(f"Received {sig_name} again, forcing exit")
        sys.exit(1)

    _shutdown_in_progress = True
    logger.info(f"Received {sig_name}, initiating graceful shutdown...")

    # Signal the shutdown event if we have an event loop
    if _shutdown_event is not None:
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(_shutdown_event.set)
        except RuntimeError:
            # No running event loop, just exit
            sys.exit(0)


def _setup_signal_handlers() -> None:
    """Set up signal handlers for graceful shutdown."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


async def _run_with_graceful_shutdown() -> None:
    """Run the MCP server with graceful shutdown support."""
    global _shutdown_event

    _shutdown_event = asyncio.Event()

    # Create a task for the MCP server
    server_task = asyncio.create_task(_get_mcp().run_async())

    # Wait for either the server to complete or a shutdown signal
    shutdown_task = asyncio.create_task(_shutdown_event.wait())

    try:
        done, pending = await asyncio.wait(
            [server_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # If shutdown was signaled, cancel the server task
        if shutdown_task in done:
            logger.info("Shutdown signal received, stopping server...")
            server_task.cancel()
            try:
                await asyncio.wait_for(server_task, timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("Server did not stop within timeout")
            except asyncio.CancelledError:
                pass

    except asyncio.CancelledError:
        logger.info("Server task cancelled")
    finally:
        # Clean up resources with timeout
        try:
            await asyncio.wait_for(
                _cleanup_resources(),
                timeout=SHUTDOWN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.warning("Resource cleanup timed out")

        # Cancel any remaining tasks
        for task in [server_task, shutdown_task]:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


# CLI entry point (for pyproject.toml) - use FastMCP's built-in runner
def main() -> None:
    """Run server via CLI using FastMCP's stdio transport."""
    # Check for smoke test flag
    if "--smoke-test" in sys.argv:
        from ha_mcp.smoke_test import main as smoke_test_main
        sys.exit(smoke_test_main())

    # Set up signal handlers before running
    _setup_signal_handlers()

    # Run with graceful shutdown support
    try:
        asyncio.run(_run_with_graceful_shutdown())
    except KeyboardInterrupt:
        # Handle case where KeyboardInterrupt is raised before our handler
        logger.info("Interrupted, exiting")
    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)

    sys.exit(0)


# HTTP entry point for web clients
def _get_http_runtime() -> tuple[int, str]:
    """Return runtime configuration shared by HTTP transports."""

    port = int(os.getenv("MCP_PORT", "8086"))
    path = os.getenv("MCP_SECRET_PATH", "/mcp")
    return port, path


async def _run_http_with_graceful_shutdown(
    transport: str,
    host: str,
    port: int,
    path: str,
) -> None:
    """Run HTTP server with graceful shutdown support."""
    global _shutdown_event

    _shutdown_event = asyncio.Event()

    # Create a task for the MCP server
    server_task = asyncio.create_task(
        _get_mcp().run_async(
            transport=transport,
            host=host,
            port=port,
            path=path,
        )
    )

    # Wait for either the server to complete or a shutdown signal
    shutdown_task = asyncio.create_task(_shutdown_event.wait())

    try:
        done, pending = await asyncio.wait(
            [server_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # If shutdown was signaled, cancel the server task
        if shutdown_task in done:
            logger.info("Shutdown signal received, stopping HTTP server...")
            server_task.cancel()
            try:
                await asyncio.wait_for(server_task, timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("HTTP server did not stop within timeout")
            except asyncio.CancelledError:
                pass

    except asyncio.CancelledError:
        logger.info("HTTP server task cancelled")
    finally:
        # Clean up resources with timeout
        try:
            await asyncio.wait_for(
                _cleanup_resources(),
                timeout=SHUTDOWN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.warning("Resource cleanup timed out")

        # Cancel any remaining tasks
        for task in [server_task, shutdown_task]:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


def _run_http_server(transport: str) -> None:
    """Common runner for HTTP-based transports."""
    port, path = _get_http_runtime()

    # Set up signal handlers before running
    _setup_signal_handlers()

    # Run with graceful shutdown support
    try:
        asyncio.run(
            _run_http_with_graceful_shutdown(
                transport=transport,
                host="0.0.0.0",
                port=port,
                path=path,
            )
        )
    except KeyboardInterrupt:
        logger.info("Interrupted, exiting")
    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"HTTP server error: {e}")
        sys.exit(1)

    sys.exit(0)


def main_web() -> None:
    """Run server over HTTP for web-capable MCP clients.

    Environment:
    - HOMEASSISTANT_URL (required)
    - HOMEASSISTANT_TOKEN (required)
    - MCP_PORT (optional, default: 8086)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    """
    _run_http_server("streamable-http")


def main_sse() -> None:
    """Run server using Server-Sent Events transport for MCP clients.

    Environment:
    - HOMEASSISTANT_URL (required)
    - HOMEASSISTANT_TOKEN (required)
    - MCP_PORT (optional, default: 8086)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    """
    _run_http_server("sse")


if __name__ == "__main__":
    main()
