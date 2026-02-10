"""Home Assistant MCP Server."""

import truststore

truststore.inject_into_ssl()

import asyncio  # noqa: E402
import copy  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402
import stat  # noqa: E402
import sys  # noqa: E402
from typing import Any  # noqa: E402

from cachetools import TTLCache  # noqa: E402

logger = logging.getLogger(__name__)

# OAuth client cache configuration
_OAUTH_CACHE_MAXSIZE = 100
_OAUTH_CACHE_TTL = 3600  # 1 hour, matching typical token expiry


class OAuthProxyClient:
    """Proxy client that dynamically forwards to the correct OAuth-authenticated client.

    This class is necessary because tools capture a reference to the client at registration time.
    The proxy allows us to inject different credentials per-request based on OAuth token claims.

    Uses a bounded TTL cache to prevent unbounded memory growth from token rotation.
    Evicted clients have their HTTP connections closed asynchronously.
    """

    def __init__(self, auth_provider):
        self._auth_provider = auth_provider
        self._oauth_clients = TTLCache(
            maxsize=_OAUTH_CACHE_MAXSIZE, ttl=_OAUTH_CACHE_TTL
        )
        # Track all live clients so we can detect evictions and close HTTP connections.
        # Uses a TTLCache with 2x the TTL so entries are cleaned up even if no further
        # requests arrive for that key, while still giving _close_evicted_clients time
        # to detect the eviction from _oauth_clients.
        self._all_clients = TTLCache(
            maxsize=_OAUTH_CACHE_MAXSIZE * 2, ttl=_OAUTH_CACHE_TTL * 2
        )

    def _close_evicted_clients(self):
        """Close HTTP connections for clients evicted from the cache by TTL or maxsize."""
        # Iterating the TTLCache triggers lazy expiry of stale entries
        evicted_keys = set(self._all_clients) - set(self._oauth_clients)
        for key in evicted_keys:
            client = self._all_clients.pop(key, None)
            if client is None:
                continue
            try:
                asyncio.get_running_loop().create_task(client.close())
                logger.debug("Scheduled cleanup for evicted OAuth client")
            except RuntimeError:
                logger.debug(
                    "No running event loop, cannot close evicted OAuth client"
                )

    def _get_oauth_client(self):
        """Get the OAuth client for the current request context."""
        from fastmcp.server.dependencies import get_access_token
        from ha_mcp.client.rest_client import HomeAssistantClient

        # Get the access token from the current request context
        token = get_access_token()

        if not token:
            logger.warning("No access token in context")
            raise RuntimeError("No OAuth token in request context")

        # Extract HA credentials from token claims
        claims = token.claims

        if not claims or "ha_url" not in claims or "ha_token" not in claims:
            logger.error(f"No HA credentials in token claims: {claims}")
            raise RuntimeError("No Home Assistant credentials in OAuth token claims")

        ha_url = claims["ha_url"]
        ha_token = claims["ha_token"]

        # Close HTTP connections for any clients evicted since last access
        self._close_evicted_clients()

        # Create or reuse client for these credentials.
        # Use .get() to avoid a TOCTOU race between the `in` check and the return:
        # TTLCache lazily evicts on access, so a key present during `in` could be
        # expired by the time __getitem__ runs.
        client_key = f"{ha_url}:{ha_token}"
        client = self._oauth_clients.get(client_key)
        if client is None:
            client = HomeAssistantClient(
                base_url=ha_url,
                token=ha_token,
            )
            # __setitem__ may evict an old entry when maxsize is reached
            self._oauth_clients[client_key] = client
            self._all_clients[client_key] = client
            self._close_evicted_clients()
            logger.info(f"Created OAuth client for {ha_url}")

        return client

    async def close(self):
        """Close all cached OAuth clients and release HTTP connections."""
        for client in self._all_clients.values():
            try:
                await client.close()
            except Exception as e:
                logger.debug(f"Error closing OAuth client: {e}")
        self._all_clients.clear()
        self._oauth_clients.clear()

    def __getattr__(self, name):
        """Forward all attribute access to the OAuth client."""
        client = self._get_oauth_client()
        return getattr(client, name)


# Shutdown configuration
SHUTDOWN_TIMEOUT_SECONDS = 2.0

# Global shutdown state
_shutdown_event: asyncio.Event | None = None
_shutdown_in_progress = False

# Stdin error message for Docker without -i flag
_STDIN_ERROR_MESSAGE = """
==============================================================================
                    Home Assistant MCP Server - Stdin Not Available
==============================================================================

The MCP server requires an interactive stdin for stdio transport mode.

This typically happens when running Docker without the -i flag:
  docker run ghcr.io/homeassistant-ai/ha-mcp:latest  # stdin is closed

To fix this, use one of the following options:

  1. Add the -i flag to enable interactive stdin:
     docker run -i -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... \\
       ghcr.io/homeassistant-ai/ha-mcp:latest

  2. Use HTTP mode instead (recommended for servers/automation):
     docker run -d -p 8086:8086 -e HOMEASSISTANT_URL=... -e HOMEASSISTANT_TOKEN=... \\
       ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-web

For more information, see:
  https://github.com/homeassistant-ai/ha-mcp#-docker

==============================================================================
"""

# Configuration error message template
_CONFIG_ERROR_MESSAGE = """
==============================================================================
                    Home Assistant MCP Server - Configuration Error
==============================================================================

Missing required environment variables:
{missing_vars}

To fix this, you need to provide your Home Assistant connection details:

  1. HOMEASSISTANT_URL - Your Home Assistant instance URL
     Example: http://homeassistant.local:8123

  2. HOMEASSISTANT_TOKEN - A long-lived access token
     Get one from: Home Assistant -> Profile -> Long-Lived Access Tokens

Configuration options:
  - Set environment variables directly:
      export HOMEASSISTANT_URL=http://homeassistant.local:8123
      export HOMEASSISTANT_TOKEN=your_token_here

  - Or create a .env file in the project directory (copy from .env.example)

For detailed setup instructions, see:
  https://github.com/homeassistant-ai/ha-mcp#-installation

==============================================================================
"""


def _check_stdin_available() -> bool:
    """Check if stdin is available for reading.

    Returns True if stdin is usable (terminal, pipe, or file).
    Returns False if stdin is closed or not readable (e.g., Docker without -i).

    When Docker runs without the -i flag, stdin is connected to /dev/null,
    which immediately returns EOF. This causes the stdio transport to exit.
    """
    # Check if stdin is closed
    if sys.stdin is None or sys.stdin.closed:
        return False

    try:
        fd = sys.stdin.fileno()
        mode = os.fstat(fd).st_mode
    except (ValueError, OSError):
        # fileno() or fstat() can raise if stdin is not a real file
        return False

    # Allow TTYs, pipes (how MCP clients communicate), and regular files (testing)
    if os.isatty(fd) or stat.S_ISFIFO(mode) or stat.S_ISREG(mode):
        return True

    # Block character devices that aren't TTYs (like /dev/null in Docker without -i)
    if stat.S_ISCHR(mode):
        return False

    # Unknown type - allow it and let the server handle any issues
    return True


def _handle_config_error(error: Exception) -> None:
    """Handle configuration errors with a user-friendly message."""
    from pydantic import ValidationError

    if isinstance(error, ValidationError):
        # Extract missing field names from pydantic errors
        missing_vars = []
        for err in error.errors():
            if err.get("type") == "missing":
                # The field name is the alias (env var name)
                field_loc = err.get("loc", ())
                if field_loc:
                    missing_vars.append(f"  - {field_loc[0]}")

        if missing_vars:
            print(
                _CONFIG_ERROR_MESSAGE.format(missing_vars="\n".join(missing_vars)),
                file=sys.stderr,
            )
            sys.exit(1)

    # For other validation errors, show the original error with guidance
    print(
        f"""
==============================================================================
                    Home Assistant MCP Server - Configuration Error
==============================================================================

{error}

For setup instructions, see:
  https://github.com/homeassistant-ai/ha-mcp#-installation

==============================================================================
""",
        file=sys.stderr,
    )
    sys.exit(1)


def _create_server():
    """Create server instance (deferred to avoid import during smoke test)."""
    try:
        from ha_mcp.server import HomeAssistantSmartMCPServer  # type: ignore[import-not-found]

        return HomeAssistantSmartMCPServer()
    except Exception as e:
        # Check if this is a pydantic validation error (missing env vars)
        from pydantic import ValidationError

        if isinstance(e, ValidationError):
            _handle_config_error(e)
        raise


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


_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _setup_logging(log_level_str: str, force: bool = False) -> None:
    """Configure root logger with consistent timestamp format."""
    logging.basicConfig(
        level=getattr(logging, log_level_str),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt=_LOG_DATE_FORMAT,
        force=force,
    )


def _get_timestamped_uvicorn_log_config() -> dict:
    """Return a Uvicorn log config with human-readable timestamps added."""
    from uvicorn.config import LOGGING_CONFIG

    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config["formatters"]["default"]["fmt"] = (
        "%(asctime)s %(levelprefix)s %(message)s"
    )
    log_config["formatters"]["default"]["datefmt"] = _LOG_DATE_FORMAT
    log_config["formatters"]["access"]["fmt"] = (
        '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
    )
    log_config["formatters"]["access"]["datefmt"] = _LOG_DATE_FORMAT
    return log_config


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

    # Respect FastMCP's show_cli_banner setting
    # Users can disable banner via FASTMCP_SHOW_CLI_BANNER=false
    import fastmcp

    show_banner = fastmcp.settings.show_cli_banner

    # Create a task for the MCP server
    server_task = asyncio.create_task(_get_mcp().run_async(show_banner=show_banner))

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
                _cleanup_resources(), timeout=SHUTDOWN_TIMEOUT_SECONDS
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
    # Handle --version flag early, before server creation requires config
    if "--version" in sys.argv or "-V" in sys.argv:
        from importlib.metadata import version

        print(f"ha-mcp {version('ha-mcp')}")
        sys.exit(0)

    # Check for smoke test flag
    if "--smoke-test" in sys.argv:
        from ha_mcp.smoke_test import main as smoke_test_main

        sys.exit(smoke_test_main())

    # Configure logging before server creation
    from ha_mcp.config import get_settings

    settings = get_settings()

    # In standard mode (not OAuth), validate that real credentials are provided
    # The config has defaults for OAuth mode, but standard mode requires real values
    # Check config FIRST so users see helpful config errors before stdin errors
    missing_vars = []
    if settings.homeassistant_url == "http://oauth-mode":
        missing_vars.append("  - HOMEASSISTANT_URL")
    if settings.homeassistant_token == "oauth-mode-token":
        missing_vars.append("  - HOMEASSISTANT_TOKEN")

    if missing_vars:
        print(
            _CONFIG_ERROR_MESSAGE.format(missing_vars="\n".join(missing_vars)),
            file=sys.stderr,
        )
        sys.exit(1)

    # Check if stdin is available (fails in Docker without -i flag)
    # This check comes after config validation so users see config errors first
    if not _check_stdin_available():
        print(_STDIN_ERROR_MESSAGE, file=sys.stderr)
        sys.exit(1)

    _setup_logging(settings.log_level)

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


def main_dev() -> None:
    """Run server with DEBUG logging enabled (for ha-mcp-dev package)."""
    import os

    os.environ["LOG_LEVEL"] = "DEBUG"
    main()


# HTTP entry point for web clients
def _get_http_runtime(default_port: int = 8086) -> tuple[int, str]:
    """Return runtime configuration shared by HTTP transports.

    Args:
        default_port: Default port to use if MCP_PORT env var is not set.
    """

    port = int(os.getenv("MCP_PORT", str(default_port)))
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

    # Respect FastMCP's show_cli_banner setting
    # Users can disable banner via FASTMCP_SHOW_CLI_BANNER=false
    import fastmcp

    show_banner = fastmcp.settings.show_cli_banner

    # Create a task for the MCP server
    server_task = asyncio.create_task(
        _get_mcp().run_async(
            transport=transport,
            host=host,
            port=port,
            path=path,
            show_banner=show_banner,
            stateless_http=True,  # Enable stateless mode for horizontal scaling and restart resilience
            uvicorn_config={"log_config": _get_timestamped_uvicorn_log_config()},
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
                _cleanup_resources(), timeout=SHUTDOWN_TIMEOUT_SECONDS
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


def _run_http_server(transport: str, default_port: int = 8086) -> None:
    """Common runner for HTTP-based transports.

    Args:
        transport: Transport type (streamable-http or sse).
        default_port: Default port to use if MCP_PORT env var is not set.
    """
    port, path = _get_http_runtime(default_port)

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
    # Configure logging before server creation
    from ha_mcp.config import get_settings

    settings = get_settings()

    # Validate credentials (required in non-OAuth HTTP mode)
    missing_vars = []
    if settings.homeassistant_url == "http://oauth-mode":
        missing_vars.append("  - HOMEASSISTANT_URL")
    if settings.homeassistant_token == "oauth-mode-token":
        missing_vars.append("  - HOMEASSISTANT_TOKEN")

    if missing_vars:
        print(
            _CONFIG_ERROR_MESSAGE.format(missing_vars="\n".join(missing_vars)),
            file=sys.stderr,
        )
        sys.exit(1)

    _setup_logging(settings.log_level)

    _run_http_server("streamable-http", default_port=8086)


def main_sse() -> None:
    """Run server using Server-Sent Events transport for MCP clients.

    Environment:
    - HOMEASSISTANT_URL (required)
    - HOMEASSISTANT_TOKEN (required)
    - MCP_PORT (optional, default: 8087)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    """
    # Configure logging before server creation
    from ha_mcp.config import get_settings

    settings = get_settings()

    # Validate credentials (required in non-OAuth SSE mode)
    missing_vars = []
    if settings.homeassistant_url == "http://oauth-mode":
        missing_vars.append("  - HOMEASSISTANT_URL")
    if settings.homeassistant_token == "oauth-mode-token":
        missing_vars.append("  - HOMEASSISTANT_TOKEN")

    if missing_vars:
        print(
            _CONFIG_ERROR_MESSAGE.format(missing_vars="\n".join(missing_vars)),
            file=sys.stderr,
        )
        sys.exit(1)

    _setup_logging(settings.log_level)

    _run_http_server("sse", default_port=8087)


def main_oauth() -> None:
    """Run server with OAuth 2.1 authentication over HTTP.

    This mode enables zero-config authentication for MCP clients like Claude.ai.
    Users authenticate via a consent form where they enter their Home Assistant
    URL and Long-Lived Access Token.

    Environment:
    - MCP_BASE_URL (required): Public URL where this server is accessible (e.g., https://your-tunnel.com)
    - MCP_PORT (optional, default: 8086)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    - LOG_LEVEL (optional, default: INFO)

    Note: HOMEASSISTANT_URL and HOMEASSISTANT_TOKEN are NOT required in this mode.
    They are collected via the OAuth consent form.
    """
    # Configure logging for OAuth mode
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    _setup_logging(log_level, force=True)
    # Also configure all ha_mcp loggers
    for logger_name in ["ha_mcp", "ha_mcp.auth", "ha_mcp.auth.provider"]:
        logging.getLogger(logger_name).setLevel(getattr(logging, log_level))
    logger.info(f"OAuth mode logging configured at {log_level} level")

    port = int(os.getenv("MCP_PORT", "8086"))
    path = os.getenv("MCP_SECRET_PATH", "/mcp")
    base_url = os.getenv("MCP_BASE_URL")

    if not base_url:
        logger.error("MCP_BASE_URL environment variable is required for OAuth mode")
        logger.error(
            "Example: export MCP_BASE_URL=https://your-tunnel.trycloudflare.com"
        )
        sys.exit(1)

    # Set up signal handlers
    _setup_signal_handlers()

    try:
        asyncio.run(_run_oauth_server(base_url, port, path))
    except KeyboardInterrupt:
        logger.info("Interrupted, exiting")
    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"OAuth server error: {e}")
        sys.exit(1)

    sys.exit(0)


async def _run_oauth_server(base_url: str, port: int, path: str) -> None:
    """Run the OAuth-authenticated MCP server.

    Args:
        base_url: Public URL where this server is accessible (required)
        port: Port to listen on
        path: MCP endpoint path
    """
    global _shutdown_event

    from ha_mcp.auth import HomeAssistantOAuthProvider
    from ha_mcp.server import HomeAssistantSmartMCPServer

    _shutdown_event = asyncio.Event()

    # Create OAuth provider
    auth_provider = HomeAssistantOAuthProvider(
        base_url=base_url,
        service_documentation_url="https://github.com/homeassistant-ai/ha-mcp",
    )

    # Create full HomeAssistantSmartMCPServer with OAuth authentication
    # In OAuth mode, we don't require pre-configured HA credentials in environment.
    # Instead, tools will get credentials from the OAuth provider per-request.
    # The Settings class now has defaults that work for OAuth mode.

    # Create proxy client that dynamically forwards to OAuth clients
    # This is necessary because tools capture a reference to the client at registration time.
    proxy_client = OAuthProxyClient(auth_provider)

    # Create server with the proxy client
    server = HomeAssistantSmartMCPServer(client=proxy_client)
    mcp = server.mcp

    logger.info("Server created with OAuthProxyClient")

    # Add OAuth authentication to the MCP server
    mcp.auth = auth_provider

    # Get tool count (get_tools is async, but we can count registered tools)
    tools = await mcp.get_tools()
    logger.info(
        f"Starting OAuth-enabled MCP server with {len(tools)} tools on {base_url}{path}"
    )

    # Respect FastMCP's show_cli_banner setting for consistency
    import fastmcp

    show_banner = fastmcp.settings.show_cli_banner

    # Run server
    server_task = asyncio.create_task(
        mcp.run_async(
            transport="streamable-http",
            host="0.0.0.0",
            port=port,
            path=path,
            show_banner=show_banner,
            stateless_http=True,  # Enable stateless mode for horizontal scaling and restart resilience
            uvicorn_config={"log_config": _get_timestamped_uvicorn_log_config()},
        )
    )

    shutdown_task = asyncio.create_task(_shutdown_event.wait())

    try:
        done, pending = await asyncio.wait(
            [server_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if shutdown_task in done:
            logger.info("Shutdown signal received, stopping OAuth server...")
            server_task.cancel()
            try:
                await asyncio.wait_for(server_task, timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("OAuth server did not stop within timeout")
            except asyncio.CancelledError:
                pass

    except asyncio.CancelledError:
        logger.info("OAuth server task cancelled")
    finally:
        for task in [server_task, shutdown_task]:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


if __name__ == "__main__":
    main()
