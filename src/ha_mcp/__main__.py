"""Home Assistant MCP Server."""

import sys

if sys.version_info < (3, 13):  # noqa: UP036 — uvx can bypass requires-python and run on 3.12
    # Write directly to stderr (not print) so this import-time version gate
    # fires before any 3.13-only syntax in the rest of the module is parsed.
    sys.stderr.write(
        f"ERROR: ha-mcp requires Python 3.13+, but you are running Python "
        f"{sys.version_info.major}.{sys.version_info.minor}.\n"
        "If using uvx, add '--python 3.13' to your config args:\n"
        '  "args": ["--python", "3.13", "--refresh", "ha-mcp@latest"]\n'
        "Or install Python 3.13: brew install python@3.13 (macOS) / "
        "sudo apt install python3.13 (Linux)\n"
    )
    sys.exit(1)

import truststore

truststore.inject_into_ssl()

import asyncio  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import ipaddress  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402
import stat  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from collections.abc import Coroutine  # noqa: E402
from typing import TYPE_CHECKING, Any  # noqa: E402

from fastmcp.exceptions import ToolError  # noqa: E402
from pydantic import ValidationError as PydanticValidationError  # noqa: E402

from ha_mcp.browser_landing import (  # noqa: E402
    register_browser_landing as _register_landing_route,
)
from ha_mcp.browser_landing import (  # noqa: E402
    register_healthz as _register_healthz_route,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ha_mcp.client.rest_client import HomeAssistantClient
    from ha_mcp.config import Settings
    from ha_mcp.server import HomeAssistantSmartMCPServer

logger = logging.getLogger(__name__)


class OAuthProxyClient:
    """Proxy client that dynamically forwards to the correct OAuth-authenticated client.

    This class is necessary because tools capture a reference to the client at registration time.
    The proxy allows us to inject different credentials per-request based on OAuth token claims.

    The Home Assistant URL is fixed server-side (HOMEASSISTANT_URL env var).
    Only the access token varies per-user (from OAuth consent form).
    """

    def __init__(self, ha_url: str) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._oauth_clients: dict[str, HomeAssistantClient] = {}
        self._lock = threading.Lock()

    def _get_oauth_client(self) -> "HomeAssistantClient":
        """Get the OAuth client for the current request context."""
        from fastmcp.server.dependencies import get_access_token

        from ha_mcp.client.rest_client import (
            HomeAssistantAuthError,
            HomeAssistantClient,
        )

        # Get the access token from the current request context
        token = get_access_token()

        if not token:
            logger.warning("No access token in context")
            raise HomeAssistantAuthError("No OAuth token in request context")

        # Extract HA token from claims (URL is server-side config)
        claims = token.claims

        if not claims or "ha_token" not in claims:
            logger.error(
                f"OAuth token missing HA credentials. Keys present: {list(claims.keys()) if claims else []}"
            )
            raise HomeAssistantAuthError(
                "No Home Assistant credentials in OAuth token claims"
            )

        ha_token = claims["ha_token"]

        # Hash token for cache key to avoid raw tokens appearing in dict keys
        client_key = hashlib.sha256(ha_token.encode()).hexdigest()

        with self._lock:
            if client_key not in self._oauth_clients:
                self._oauth_clients[client_key] = HomeAssistantClient(
                    base_url=self._ha_url,
                    token=ha_token,
                )
                logger.info(f"Created OAuth client for {self._ha_url}")

            return self._oauth_clients[client_key]

    async def close(self) -> None:
        """Close all cached OAuth clients to release httpx connection pools."""
        with self._lock:
            clients = list(self._oauth_clients.values())
            self._oauth_clients.clear()
        for client in clients:
            await client.close()

    def __getattr__(self, name: str) -> Any:
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
    # Unknown type - allow it and let the server handle any issues
    return not stat.S_ISCHR(mode)


def _handle_config_error(error: Exception) -> None:
    """Handle configuration errors with a user-friendly message and exit.

    Always calls sys.exit(1) — never returns normally.
    """
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


def _validate_standard_credentials(settings: "Settings") -> None:
    """Exit with error if HA credentials are OAuth sentinels in standard (non-OAuth) mode."""
    from ha_mcp.config import OAUTH_MODE_TOKEN, OAUTH_MODE_URL

    missing_vars = []
    if settings.homeassistant_url == OAUTH_MODE_URL:
        missing_vars.append("  - HOMEASSISTANT_URL")
    if settings.homeassistant_token == OAUTH_MODE_TOKEN:
        missing_vars.append("  - HOMEASSISTANT_TOKEN")

    if missing_vars:
        print(
            _CONFIG_ERROR_MESSAGE.format(missing_vars="\n".join(missing_vars)),
            file=sys.stderr,
        )
        sys.exit(1)


def _get_show_banner() -> bool:
    """Check if server banner should be shown (respects FASTMCP_SHOW_SERVER_BANNER env var)."""
    import fastmcp

    return fastmcp.settings.show_server_banner


def _setup_standard_mode() -> None:
    """Validate credentials and configure logging for standard (non-OAuth) modes."""
    from ha_mcp.config import get_settings

    settings = get_settings()
    _validate_standard_credentials(settings)
    _setup_logging(settings.log_level)
    _log_startup_version()


def _http_run_kwargs(transport: str, host: str, port: int, path: str) -> dict[str, Any]:
    """Build common run_async kwargs for HTTP-based transports.

    ``stateless_http`` is a Streamable-HTTP concept and is only valid for the
    ``http``/``streamable-http`` transports. Passing it alongside
    ``transport="sse"`` makes fastmcp's ``run_async`` raise
    ``ValueError("SSE transport does not support stateless mode")``. Gating it to
    non-SSE transports keeps SSE startup working. (Before this fix that raise was
    also swallowed into a silent exit 0; ``_run_with_shutdown`` now surfaces a
    self-terminating server task's exception instead.) See #1544.
    """
    kwargs: dict[str, Any] = {
        "transport": transport,
        "host": host,
        "port": port,
        "path": path,
        "show_banner": _get_show_banner(),
        "uvicorn_config": {"log_config": _get_timestamped_uvicorn_log_config()},
    }
    if transport != "sse":
        kwargs["stateless_http"] = True
    return kwargs


def _create_server() -> "HomeAssistantSmartMCPServer":
    """Create server instance (deferred to avoid import during smoke test)."""
    from pydantic import ValidationError

    # Every deferred-``mcp`` entry point funnels through here before its
    # Streamable-HTTP app is built -- ha-mcp-web, the add-on, and the
    # ``fastmcp run fastmcp-http.json`` container path -- so default fastmcp's
    # DNS-rebinding guard off once, here, for all of them. Direct-construction
    # paths (_run_oauth_server, the in-process component server) call it themselves.
    from ha_mcp.transport_security import ensure_host_origin_guard_default_off

    ensure_host_origin_guard_default_off()

    try:
        from ha_mcp.server import HomeAssistantSmartMCPServer

        return HomeAssistantSmartMCPServer()
    except ValidationError as e:
        _handle_config_error(e)
        raise  # _handle_config_error calls sys.exit, but satisfy type checker


# Lazy server creation - only create when needed
_server: "HomeAssistantSmartMCPServer | None" = None


def _get_mcp() -> "FastMCP":
    """Get the MCP instance, creating server if needed."""
    global _server
    if _server is None:
        _server = _create_server()
    return _server.mcp


def _get_server() -> "HomeAssistantSmartMCPServer":
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


mcp = _DeferredMCP()


_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class StatelessSessionLogFilter(logging.Filter):
    """Suppress the routine 'Terminating session: None' log from the MCP SDK.

    In stateless HTTP mode every request creates and tears down a temporary
    session whose id is ``None``, so the SDK emits an INFO
    ``Terminating session: None`` (mcp/server/streamable_http.py) on *every*
    request. The line is routine but looks alarming and has repeatedly
    confused users into thinking the connection is broken.

    Returning ``False`` drops the record at this logger before it reaches any
    handler. (Merely downgrading the level to DEBUG did not work: the level
    gate is applied before the filter runs, so the record was already admitted
    and still emitted -- just relabelled.) Real session terminations carry an
    actual id and are not matched, so they still log.

    # TODO: remove when modelcontextprotocol/python-sdk#2329 is resolved
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "mcp.server.streamable_http":
            return True
        try:
            message = record.getMessage()
        except (ValueError, TypeError):
            # A malformed %-format record on this logger is not our target, and
            # a filter must not raise: filters run in Logger.handle() with no
            # exception handling, so a raise would crash the logging call.
            return True
        # Drop the stateless teardown noise; keep everything else.
        return "Terminating session: None" not in message


class ToolValidationLogFilter(logging.Filter):
    """Demote fastmcp tool-failure tracebacks to single-line warnings.

    Pydantic ValidationError and tool-raised ToolError aren't server bugs,
    so the traceback through fastmcp/pydantic internals is just noise. The
    structured error detail is preserved in the WARNING message; stack is
    intentionally dropped because these are user-input errors, not bugs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "fastmcp.server.server" or not record.exc_info:
            return True

        msg = record.getMessage()
        err = record.exc_info[1]
        if "Error validating tool" in msg and isinstance(err, PydanticValidationError):
            record.msg = f"{msg}: {err.errors(include_url=False)}"
        elif "Error calling tool" in msg and isinstance(err, ToolError):
            record.msg = f"{msg}: {err}"
        else:
            return True

        record.args = ()
        record.levelno = logging.WARNING
        record.levelname = "WARNING"
        record.exc_info = None
        record.exc_text = None
        return True


class ProbeAccessLogFilter(logging.Filter):
    """Drop benign, non-MCP HTTP probe noise from the uvicorn access log.

    * ``GET``/``HEAD`` ``/favicon.ico`` -> ``404``: browsers auto-request a
      favicon that doesn't exist. Pure noise, always dropped.
    * ``GET``/``HEAD`` on the MCP path -> ``405``: a non-MCP caller (browser,
      health check, reverse proxy, or a connector's SSE-style pre-flight) hit a
      POST-only Streamable HTTP endpoint. The raw access line is dropped and the
      landing handler logs one annotated "(NORMAL for most non-SSE connections)"
      line in its place. Dropped only when ``drop_mcp_405`` is set — SSE callers
      pass False, since there a GET answers 200 and a GET-405 is a genuine fault.
    """

    def __init__(self, mcp_path: str, *, drop_mcp_405: bool = True) -> None:
        super().__init__()
        self._mcp_path = mcp_path.rstrip("/") or "/"
        self._drop_mcp_405 = drop_mcp_405

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access records carry structured args: (client, method, path,
        # http_version, status_int). Match on those, not the formatted string.
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        method, raw_path, status = args[1], args[2], args[4]
        if method not in ("GET", "HEAD"):
            return True
        path = str(raw_path).split("?", 1)[0].rstrip("/") or "/"
        if status == 404 and path == "/favicon.ico":
            return False  # browser favicon auto-request — pure noise
        if status == 200 and path == "/healthz":
            return False  # opt-in liveness probe (register_healthz) — pure noise
        # By-design probe 405 on the MCP path; the handler logs an annotated line
        # instead. This trusts that the landing route is the only GET/HEAD responder
        # on the MCP path (true today). Kept in SSE mode (drop_mcp_405=False), where
        # a GET answers 200 and a 405 is a real fault.
        is_dropped_probe = (
            status == 405 and path == self._mcp_path and self._drop_mcp_405
        )
        return not is_dropped_probe


def _setup_logging(log_level_str: str, force: bool = False) -> None:
    """Configure root logger with consistent timestamp format."""
    logging.basicConfig(
        level=getattr(logging, log_level_str),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt=_LOG_DATE_FORMAT,
        force=force,
    )
    logging.getLogger("mcp.server.streamable_http").addFilter(
        StatelessSessionLogFilter()
    )
    logging.getLogger("fastmcp.server.server").addFilter(ToolValidationLogFilter())


def _log_startup_version() -> None:
    """Log ha-mcp version at startup, plus dev-channel / update banners.

    The dev banner only fires for standalone dev installs (Docker ``:dev`` /
    ``:latest``, or ``pip install ha-mcp-dev``). It is suppressed under the HA
    Supervisor because add-on users already pick dev vs stable in the HAOS UI.

    The update banner fires on every startup when a newer release is available,
    on every deployment — pip/Docker/stdio compare against PyPI, the HA add-on
    (stable AND dev) against the Supervisor add-on store — mirroring FastMCP's
    ``log_server_banner``. ``get_update_info`` is a no-op for the ``unknown``
    version (PyPI path) and the ``HA_MCP_DISABLE_UPDATE_CHECK`` opt-out, and
    never raises.
    """
    from ha_mcp._version import get_version, is_dev_version, is_running_in_addon

    version = get_version()
    logger.info(f"ha-mcp {version}")

    if is_dev_version(version) and not is_running_in_addon():
        logger.warning(
            "This is the dev channel. For the stable release use the "
            "'ghcr.io/homeassistant-ai/ha-mcp:stable' Docker tag "
            "(or 'pip install ha-mcp' on PyPI)."
        )

    from ha_mcp.update_check import get_update_info, update_command_hint

    info = get_update_info()
    if info is not None and info.update_available:
        logger.warning(
            "A newer ha-mcp release is available: %s (you have %s). %s",
            info.latest,
            info.current,
            update_command_hint(info.current),
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
    except ImportError:
        logger.debug("WebSocket listener module not available")
    except Exception as e:
        logger.warning(f"WebSocket listener cleanup failed: {e}")

    # Close WebSocket manager connections
    try:
        from ha_mcp.client.websocket_client import websocket_manager

        await websocket_manager.disconnect()
        logger.debug("WebSocket manager disconnected")
    except ImportError:
        logger.debug("WebSocket manager module not available")
    except Exception as e:
        logger.warning(f"WebSocket manager cleanup failed: {e}")

    # Close the server's HTTP client
    if _server is not None:
        try:
            await _server.close()
            logger.debug("Server closed")
        except Exception as e:
            logger.warning(f"Server cleanup failed: {e}")

    logger.info("Server resources cleaned up")


async def _cancel_tasks(*tasks: asyncio.Task) -> None:
    """Cancel tasks and wait for completion, swallowing CancelledError."""
    for task in tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # Expected: we just cancelled this task, swallow its
                # CancelledError so remaining tasks still get awaited.
                pass


async def _run_with_shutdown(server_coro: Coroutine[Any, Any, Any]) -> None:
    """Run a server coroutine with graceful shutdown support.

    Handles signal-based shutdown, resource cleanup, and task cancellation.
    """
    global _shutdown_event

    _shutdown_event = asyncio.Event()

    server_task = asyncio.create_task(server_coro)
    shutdown_task = asyncio.create_task(_shutdown_event.wait())

    try:
        done, pending = await asyncio.wait(
            [server_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if shutdown_task in done:
            logger.info("Shutdown signal received, stopping server...")
            server_task.cancel()
            try:
                await asyncio.wait_for(server_task, timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("Server did not stop within timeout")
            except asyncio.CancelledError:
                # Expected: we just cancelled server_task above; swallow its
                # CancelledError so shutdown can proceed to cleanup.
                pass
        elif server_task in done:
            # Server task finished on its own (no shutdown signal). Re-raise any
            # exception it captured so a hard startup failure surfaces as a
            # logged sys.exit(1) instead of a silent exit 0 — without this the
            # exception on the already-done task is never retrieved. See #1544.
            server_task.result()

    except asyncio.CancelledError:
        # A shutdown-initiated cancel is a graceful stop. A cancel without a
        # shutdown signal — including one re-raised by server_task.result()
        # above — is a hard stop masquerading as success; re-raise it so it
        # becomes a logged sys.exit(1) rather than a silent exit 0. See #1544.
        if _shutdown_event is not None and _shutdown_event.is_set():
            logger.info("Server task cancelled")
        else:
            logger.error("Server task cancelled without a shutdown signal")
            raise
    finally:
        try:
            await asyncio.wait_for(
                _cleanup_resources(), timeout=SHUTDOWN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.warning("Resource cleanup timed out")

        try:
            await _cancel_tasks(server_task, shutdown_task)
        except Exception as e:
            # Teardown must never mask the exception being propagated from the
            # try block (Python drops the original if finally raises).
            logger.warning(f"Task cancellation during shutdown failed: {e}")


def _run_entrypoint(coro: Coroutine[Any, Any, Any], label: str) -> None:
    """Run an async entrypoint with standard exception handling."""
    _setup_signal_handlers()

    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        logger.info("Interrupted, exiting")
    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"{label} error: {e}", exc_info=True)
        sys.exit(1)

    sys.exit(0)


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
    await _run_with_shutdown(_get_mcp().run_async(show_banner=_get_show_banner()))


# CLI entry point (for pyproject.toml) - use FastMCP's built-in runner
def main() -> None:
    """Run server via CLI using FastMCP's stdio transport."""
    # Handle --version flag early, before server creation requires config
    if "--version" in sys.argv or "-V" in sys.argv:
        from ha_mcp._version import get_version

        print(f"ha-mcp {get_version()}")
        sys.exit(0)

    # Check for smoke test flag
    if "--smoke-test" in sys.argv:
        from ha_mcp.smoke_test import main as smoke_test_main

        sys.exit(smoke_test_main())

    # Configure logging before server creation
    from ha_mcp.config import get_settings

    settings = get_settings()

    # Check config FIRST so users see helpful config errors before stdin errors
    _validate_standard_credentials(settings)

    # Check if stdin is available (fails in Docker without -i flag)
    if not _check_stdin_available():
        print(_STDIN_ERROR_MESSAGE, file=sys.stderr)
        sys.exit(1)

    _setup_logging(settings.log_level)
    _log_startup_version()

    # Spawn the persistent settings UI sidecar (issue #863). The sidecar
    # is a detached subprocess so the settings page stays reachable even
    # when this stdio process is SIGTERM'd or idle-killed by the client.
    # Best-effort: failure logs a warning but doesn't block MCP startup.
    _maybe_spawn_settings_sidecar()

    _run_entrypoint(_run_with_graceful_shutdown(), "Server")


def _maybe_spawn_settings_sidecar() -> None:
    """Dump tool metadata cache + spawn the stdio settings UI sidecar.

    Split out of ``main()`` to keep the entrypoint readable. The cache
    dump uses a one-off ``asyncio.run`` because ``_get_tool_metadata``
    is async; this happens before the main stdio loop so there's no
    nested-loop conflict with ``_run_entrypoint``'s own ``asyncio.run``.

    Performance: the dump constructs the full FastMCP server, which is
    heavy. Skip it (and the server build) when there's nothing to spawn
    for — sidecar disabled or already alive. Warm restarts that already
    have a sidecar pay zero cold-start tax from this path.
    """
    from ha_mcp.settings_ui import (
        _get_tool_metadata,
        dump_tool_metadata_cache,
    )
    from ha_mcp.stdio_settings_sidecar import (
        _existing_sidecar_alive,
        _is_disabled,
        maybe_spawn,
    )

    # Cheap gates first; skip the heavy metadata dump when the sidecar
    # would be a no-op anyway. Any condition that makes maybe_spawn()
    # short-circuit also makes the dump pointless (the running sidecar
    # already has a cache from a prior parent startup; a disabled
    # sidecar never reads one).
    if _is_disabled() or _existing_sidecar_alive():
        try:
            maybe_spawn()
        except Exception as e:
            logger.warning(
                "Failed to invoke maybe_spawn no-op path (%s)",
                type(e).__name__,
                exc_info=True,
            )
        return

    try:
        metadata = asyncio.run(_get_tool_metadata(_get_server()))
        dumped = dump_tool_metadata_cache(metadata)
        # Log a deliberate one-liner so users debugging an empty
        # settings page can see whether the parent's dump succeeded
        # by grepping the stdio process output (which Claude Desktop
        # surfaces in its MCP server log panel).
        logger.info(
            "Tool metadata cache: %d tools dumped, write %s",
            len(metadata),
            "succeeded" if dumped else "FAILED",
        )
    except Exception as e:
        # Cache dump is best-effort — the sidecar falls back to an empty
        # tools list rather than blocking stdio startup. Include the
        # exception class in the warning so ops can distinguish
        # server-init failures (Pydantic ValidationError) from cache I/O
        # (OSError) from event-loop issues (RuntimeError).
        logger.warning(
            "Failed to dump tool metadata cache (%s)",
            type(e).__name__,
            exc_info=True,
        )

    try:
        maybe_spawn()
    except Exception as e:
        # Spawn failures already log inside maybe_spawn(); the bare
        # except here is a defense-in-depth guard for any unexpected
        # path (e.g. import error in the sidecar module). Settings UI
        # is advisory — never let it block MCP startup.
        logger.warning(
            "Failed to spawn settings UI sidecar (%s)",
            type(e).__name__,
            exc_info=True,
        )


def main_dev() -> None:
    """Run server with DEBUG logging enabled (for ha-mcp-dev package)."""
    os.environ["LOG_LEVEL"] = "DEBUG"
    main()


# HTTP entry point for web clients
def _get_http_runtime(default_port: int = 8086) -> tuple[str, int, str]:
    """Return runtime configuration shared by HTTP transports.

    Args:
        default_port: Default port to use if MCP_PORT env var is not set.

    The bind host comes from ``MCP_HOST`` and defaults to ``0.0.0.0``. The
    explicit literal default is load-bearing: FastMCP's own ``Settings.host``
    defaults to ``127.0.0.1``, so dropping the fallback would silently flip
    the default and break existing LAN deployments. Set ``MCP_HOST=127.0.0.1``
    to bind to loopback on workstation deployments.

    Note: FastMCP also honors a ``FASTMCP_HOST`` env var natively, but
    because ``_http_run_kwargs`` passes ``host=`` explicitly to
    ``run_async``, any ``FASTMCP_HOST`` value in the environment is
    ignored — ``MCP_HOST`` is the only env var that affects bind host
    for ha-mcp's CLI entry points.
    """

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port_str = os.getenv("MCP_PORT", str(default_port))
    try:
        port = int(port_str)
    except ValueError:
        logger.error(f"Invalid MCP_PORT value: {port_str!r}. Must be an integer.")
        sys.exit(1)
    path = os.getenv("MCP_SECRET_PATH", DEFAULT_MCP_PATH)
    return host, port, path


# Default ``MCP_SECRET_PATH`` value, shared by ``_get_http_runtime`` (the
# read-from-env fallback) and ``_warn_if_default_path_exposed`` (the
# hardening-nudge predicate). Single source of truth so the two sites
# can't drift.
DEFAULT_MCP_PATH = "/mcp"

# Hostname literals (not IP addresses) treated as loopback by
# ``_is_loopback_host``. IP literals — the whole ``127.0.0.0/8`` block,
# ``::1``, bracketed forms, zone-suffixed forms, and IPv4-mapped IPv6 — are
# handled by the ``ipaddress`` parse before this set is consulted.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def _is_loopback_host(host: str) -> bool:
    """Return True when ``host`` names the local machine only.

    Accepts IPv6 hosts in bracketed (``[::1]``) or zone-suffixed
    (``::1%eth0``) form, the full ``127.0.0.0/8`` range, IPv4-mapped IPv6
    loopback (``::ffff:127.0.0.1``, which ``is_loopback`` resolves on its
    own), and the names in ``_LOOPBACK_HOSTNAMES``. A value that is neither
    an IP literal nor a known loopback name (a real hostname, or a malformed
    string) is treated as non-loopback.
    """
    try:
        candidate = host.strip("[]").split("%", 1)[0]
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # Not an IP literal — fall back to the known loopback hostnames.
        return host.lower() in _LOOPBACK_HOSTNAMES


def _is_running_in_container() -> bool:
    """Best-effort detection of containerized execution.

    Inside a container the server binds ``0.0.0.0`` regardless of how the
    operator restricted host-side exposure (``docker run -p 127.0.0.1:...``),
    so the bind host alone can't tell a loopback-only deployment from a
    LAN-reachable one — the default-path warning would be a false positive
    for every container. Container deployments are hardened through the
    published guidance instead (AGENTS.md -> Docker; the add-on
    auto-generates a secret path).
    """
    # Docker writes /.dockerenv; Podman writes /run/.containerenv.
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    # The Home Assistant add-on runs under the Supervisor.
    return bool(os.getenv("SUPERVISOR_TOKEN"))


def _warn_if_default_path_exposed(host: str, port: int, path: str) -> None:
    """Warn on a direct run that leaves the default path on a LAN bind.

    Standard-mode HTTP/SSE authenticates by URL-path secrecy (see
    SECURITY.md → Threat Model). The default ``/mcp`` is not the
    high-entropy secret that model assumes once the bind leaves loopback.

    Fires only for a direct ``ha-mcp-web`` / ``ha-mcp-sse`` start (uvx, pip,
    source) that uses the default path on a non-loopback host. Operators
    silence it the same way they harden — bind ``MCP_HOST=127.0.0.1`` or set
    a high-entropy ``MCP_SECRET_PATH``. Containers are skipped: an
    in-container ``0.0.0.0`` bind says nothing about real exposure, which is
    set by the ``docker -p`` mapping the process can't observe (see
    ``_is_running_in_container``).
    """
    if path != DEFAULT_MCP_PATH:
        return
    if _is_running_in_container():
        return
    if _is_loopback_host(host):
        return
    logger.warning(
        "ha-mcp listening on %s:%s%s with default MCP_SECRET_PATH. "
        "Standard-mode HTTP/SSE authenticates by URL-path secrecy and assumes "
        "a high-entropy MCP_SECRET_PATH for non-loopback binds "
        "(see SECURITY.md → Threat Model). "
        "Either bind loopback (MCP_HOST=127.0.0.1) or set MCP_SECRET_PATH "
        "to a high-entropy value (e.g. /private_<token_urlsafe(16)>).",
        host,
        port,
        path,
    )


async def _run_http_with_graceful_shutdown(
    transport: str,
    host: str,
    port: int,
    path: str,
) -> None:
    """Run HTTP server with graceful shutdown support."""
    await _run_with_shutdown(
        _get_mcp().run_async(**_http_run_kwargs(transport, host, port, path))
    )


def _healthz_enabled() -> bool:
    """True when MCP_HEALTHZ opts in to the unauthenticated /healthz route.

    Off by default: standard mode authenticates by URL-path secrecy, and an
    always-on liveness route would confirm to any scanner that ha-mcp is
    listening. Operators running their own monitoring set MCP_HEALTHZ=true.
    """
    return os.getenv("MCP_HEALTHZ", "").strip().lower() in ("1", "true", "yes", "on")


def _oidc_verify_id_token_enabled() -> bool:
    """True when OIDC_VERIFY_ID_TOKEN opts in to ID-token verification.

    Off by default: FastMCP's OIDCProxy verifies the access token as a JWT,
    which works for providers like Authentik and Keycloak. Providers that
    issue opaque access tokens (Google always; Auth0 without an API
    audience) need ``verify_id_token=True`` so FastMCP verifies the ID token
    instead.
    """
    return os.getenv("OIDC_VERIFY_ID_TOKEN", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _oidc_allowed_client_redirect_uris() -> list[str] | None:
    """Parse OIDC_ALLOWED_CLIENT_REDIRECT_URIS into a list, or None if unset.

    Comma-separated list of redirect URI patterns FastMCP's OIDCProxy will
    accept from dynamically-registered clients. With open DCR and an
    allow-all redirect policy, a malicious dynamically-registered client can
    ride a victim's IdP session; internet-facing deployments should set this.
    """
    raw = os.getenv("OIDC_ALLOWED_CLIENT_REDIRECT_URIS", "")
    uris = [uri.strip() for uri in raw.split(",") if uri.strip()]
    return uris or None


def register_browser_landing(
    mcp_instance: "FastMCP | _DeferredMCP",
    path: str,
    *,
    quiet_probe_log: bool = True,
) -> None:
    """Register the friendly browser landing page and tidy the uvicorn access log.

    Delegates the GET landing route to the reusable core in
    :mod:`ha_mcp.browser_landing` (also used by the in-process ``ha_mcp_tools``
    server) and, when the route is newly registered, attaches
    :class:`ProbeAccessLogFilter` to the uvicorn access logger. Browsers and
    misconfigured clients that send GET instead of POST see a human-readable
    explanation instead of a bare "Method Not Allowed" error.

    Args:
        mcp_instance: The FastMCP server to register the route on.
        path: The MCP endpoint path (e.g. "/mcp" or a secret path).
        quiet_probe_log: When True (default, for Streamable HTTP), drop the
            by-design GET/HEAD-405 probe line on the MCP path from the uvicorn
            access log (the handler logs an annotated replacement). Pass False
            for SSE, where a GET answers 200 and a 405 is a genuine fault.
    """
    if not _register_landing_route(mcp_instance, path):
        # Already registered for this path — don't double-attach the log filter.
        return

    # Tidy uvicorn's access log: always drop browser favicon 404s, and drop the
    # raw by-design GET/HEAD-405 probe line on the MCP path (the landing handler
    # logs an annotated replacement). The 405 drop is skipped for SSE
    # (quiet_probe_log=False), where a GET answers 200 and a 405 is a real fault.
    # Attach to uvicorn.access directly — it has propagate=False, so a root-logger
    # filter would miss it.
    logging.getLogger("uvicorn.access").addFilter(
        ProbeAccessLogFilter(path, drop_mcp_405=quiet_probe_log)
    )


def _log_settings_url(host: str, port: int, path: str) -> None:
    """Log the web settings-UI URL at HTTP startup.

    Non-add-on operators (Docker / standalone) otherwise have no easy way to
    discover the settings page or its secret-path URL (issue #1458). When the
    bind host is the wildcard (``0.0.0.0`` / ``::``) the process can't know its
    externally reachable address, so we log a ``<host>`` placeholder.
    """
    is_wildcard = host in ("0.0.0.0", "::")
    if is_wildcard:
        display_host = "<host>"
    elif ":" in host:
        # IPv6 literal (e.g. ::1, 2001:db8::1) needs brackets in a URL.
        display_host = f"[{host}]"
    else:
        display_host = host
    url = f"http://{display_host}:{port}{path.rstrip('/')}/settings"
    note = "  (substitute this server's address for <host>)" if is_wildcard else ""
    logger.info(f"Settings UI available at: {url}{note}")


def _run_http_server(transport: str, default_port: int = 8086) -> None:
    """Common runner for HTTP-based transports.

    Args:
        transport: Transport type (http or sse).
        default_port: Default port to use if MCP_PORT env var is not set.
    """
    from ha_mcp.settings_ui import register_settings_routes

    # The DNS-rebinding guard is defaulted off in _create_server (reached here via
    # _get_mcp below, before the app is built) -- see transport_security.
    host, port, path = _get_http_runtime(default_port)
    _warn_if_default_path_exposed(host, port, path)
    # SSE transport answers GET with 200 (the event stream), so a GET->405 there
    # would be a real fault, not a benign probe — keep its access log intact.
    register_browser_landing(_get_mcp(), path, quiet_probe_log=transport != "sse")
    if _healthz_enabled():
        _register_healthz_route(_get_mcp())
    register_settings_routes(_get_mcp(), _get_server(), secret_path=path)
    _log_settings_url(host, port, path)

    _run_entrypoint(
        _run_http_with_graceful_shutdown(transport, host, port, path),
        "HTTP server",
    )


def main_web() -> None:
    """Run server over HTTP for web-capable MCP clients.

    Environment:
    - HOMEASSISTANT_URL (required)
    - HOMEASSISTANT_TOKEN (required)
    - MCP_HOST (optional, default: "0.0.0.0"; set 127.0.0.1 to restrict to loopback)
    - MCP_PORT (optional, default: 8086)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    """
    _setup_standard_mode()
    _run_http_server("http", default_port=8086)


def main_sse() -> None:
    """Run server using Server-Sent Events transport for MCP clients.

    Environment:
    - HOMEASSISTANT_URL (required)
    - HOMEASSISTANT_TOKEN (required)
    - MCP_HOST (optional, default: "0.0.0.0"; set 127.0.0.1 to restrict to loopback)
    - MCP_PORT (optional, default: 8087)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    """
    _setup_standard_mode()
    _run_http_server("sse", default_port=8087)


def main_oauth() -> None:
    """Run server with OAuth 2.1 authentication over HTTP.

    This mode enables per-user authentication for MCP clients like Claude.ai.
    Users authenticate via a consent form where they provide their
    Long-Lived Access Token.

    Environment:
    - HOMEASSISTANT_URL (required): URL of the Home Assistant instance
    - MCP_BASE_URL (required): Public URL where this server is accessible (e.g., https://your-tunnel.com)
    - MCP_HOST (optional, default: "0.0.0.0"; set 127.0.0.1 to restrict to loopback)
    - MCP_PORT (optional, default: 8086)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    - LOG_LEVEL (optional, default: INFO)

    Note: HOMEASSISTANT_TOKEN is NOT required in this mode.
    Per-user tokens are collected via the OAuth consent form.
    """
    # In OAuth mode, per-user tokens come from the consent form — no
    # server-level HOMEASSISTANT_TOKEN is needed.  Set the sentinel so
    # Settings validation passes even when the env var is empty (e.g.
    # Dockerfile sets HOMEASSISTANT_TOKEN="").  Fixes #886.
    if not os.getenv("HOMEASSISTANT_TOKEN"):
        from ha_mcp.config import OAUTH_MODE_TOKEN

        os.environ["HOMEASSISTANT_TOKEN"] = OAUTH_MODE_TOKEN

    # Configure logging for OAuth mode
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    _setup_logging(log_level, force=True)
    # Also configure all ha_mcp loggers
    for logger_name in ["ha_mcp", "ha_mcp.auth", "ha_mcp.auth.provider"]:
        logging.getLogger(logger_name).setLevel(getattr(logging, log_level))
    logger.info(f"OAuth mode logging configured at {log_level} level")
    _log_startup_version()

    host, port, path = _get_http_runtime(default_port=8086)
    base_url = os.getenv("MCP_BASE_URL")
    ha_url = os.getenv("HOMEASSISTANT_URL")

    missing = []
    if not base_url:
        missing.append("  - MCP_BASE_URL (e.g., https://your-tunnel.trycloudflare.com)")
    if not ha_url:
        missing.append("  - HOMEASSISTANT_URL (e.g., http://homeassistant.local:8123)")

    if missing:
        missing_vars = "\n".join(missing)
        print(
            f"""
==============================================================================
                    Home Assistant MCP Server - Configuration Error
==============================================================================

Missing required environment variables for OAuth mode:
{missing_vars}

For setup instructions, see:
  https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/OAUTH.md

==============================================================================
""",
            file=sys.stderr,
        )
        sys.exit(1)

    # Type narrowing: ha_url and base_url are guaranteed non-None after the check above
    assert ha_url is not None
    assert base_url is not None
    _run_entrypoint(
        _run_oauth_server(ha_url, base_url, host, port, path), "OAuth server"
    )


async def _run_oauth_server(
    ha_url: str, base_url: str, host: str, port: int, path: str
) -> None:
    """Run the OAuth-authenticated MCP server.

    Args:
        ha_url: Home Assistant instance URL (server-side config)
        base_url: Public URL where this server is accessible (required)
        host: Bind host (typically 0.0.0.0; override via MCP_HOST)
        port: Port to listen on
        path: MCP endpoint path
    """
    from ha_mcp.auth import HomeAssistantOAuthProvider
    from ha_mcp.server import HomeAssistantSmartMCPServer
    from ha_mcp.transport_security import ensure_host_origin_guard_default_off

    # Browser OAuth clients (Claude.ai / ChatGPT) reach the discovery endpoints
    # cross-origin, and users front this server with proxies/tunnels on arbitrary
    # hosts; default fastmcp's DNS-rebinding guard off so it does not 403/421 them.
    ensure_host_origin_guard_default_off()

    # Create OAuth provider
    auth_provider = HomeAssistantOAuthProvider(
        base_url=base_url,
        service_documentation_url="https://github.com/homeassistant-ai/ha-mcp",
    )

    # In OAuth mode, the HA URL is fixed server-side. Per-user tokens come
    # from the OAuth consent form and are extracted from token claims.
    proxy_client = OAuthProxyClient(ha_url)

    global _server
    _server = HomeAssistantSmartMCPServer(
        client=proxy_client,  # type: ignore[arg-type]  # OAuthProxyClient forwards all HomeAssistantClient attrs via __getattr__
    )
    mcp = _server.mcp
    mcp.auth = auth_provider

    logger.info("Server created with OAuthProxyClient")
    register_browser_landing(mcp, path)
    if _healthz_enabled():
        _register_healthz_route(mcp)

    from ha_mcp.settings_ui import register_settings_routes

    register_settings_routes(mcp, _server, secret_path=path)
    _log_settings_url(host, port, path)

    tools = await mcp.list_tools()
    logger.info(
        f"Starting OAuth-enabled MCP server with {len(tools)} tools on {base_url}{path}"
    )

    await _run_with_shutdown(
        mcp.run_async(**_http_run_kwargs("http", host, port, path))
    )


def main_oidc() -> None:
    """Run server with OIDC authentication over HTTP.

    This mode enables authentication via an external OIDC provider
    (Authentik, Keycloak, Auth0, Google, etc.). All authenticated users
    share the same Home Assistant instance via the configured credentials.

    Unlike OAuth mode which collects per-user HA credentials via a consent form,
    OIDC mode is purely an access gate — users authenticate through the OIDC provider,
    then all requests use the shared HA credentials from environment variables.

    Environment:
    - OIDC_CONFIG_URL (required): OIDC discovery URL (.well-known/openid-configuration)
    - OIDC_CLIENT_ID (required): OAuth client ID registered with your OIDC provider
    - OIDC_CLIENT_SECRET (required): OAuth client secret from your OIDC provider
    - MCP_BASE_URL (required): Public HTTPS URL where this server is accessible
    - HOMEASSISTANT_URL (required): Home Assistant instance URL
    - HOMEASSISTANT_TOKEN (required): Home Assistant long-lived access token or supervisor token
    - OIDC_JWT_SIGNING_KEY (optional): Secret key for signing FastMCP JWTs. Set this to
      persist sessions across server restarts. Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    - OIDC_ALLOWED_CLIENT_REDIRECT_URIS (optional): Comma-separated list of redirect URI
      patterns accepted from dynamically-registered clients. Strongly recommended for
      internet-facing deployments.
    - OIDC_VERIFY_ID_TOKEN (optional, default: false): Set true for providers that issue
      opaque access tokens (e.g. Google, or Auth0 without an API audience).
    - MCP_PORT (optional, default: 8086)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    - LOG_LEVEL (optional, default: INFO)
    """
    # Configure logging for OIDC mode (force=True needed since logging may already be configured)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    _setup_logging(log_level, force=True)
    for logger_name in ["ha_mcp", "ha_mcp.auth", "ha_mcp.auth.provider"]:
        logging.getLogger(logger_name).setLevel(getattr(logging, log_level))
    logger.info(f"OIDC mode logging configured at {log_level} level")
    _log_startup_version()

    # Validate HA credentials (OIDC mode needs them — unlike OAuth mode)
    from ha_mcp.config import get_settings

    settings = get_settings()
    _validate_standard_credentials(settings)

    # Validate OIDC-specific env vars
    oidc_config_url = os.getenv("OIDC_CONFIG_URL")
    oidc_client_id = os.getenv("OIDC_CLIENT_ID")
    oidc_client_secret = os.getenv("OIDC_CLIENT_SECRET")
    base_url = os.getenv("MCP_BASE_URL")

    missing = []
    if not oidc_config_url:
        missing.append("OIDC_CONFIG_URL")
    if not oidc_client_id:
        missing.append("OIDC_CLIENT_ID")
    if not oidc_client_secret:
        missing.append("OIDC_CLIENT_SECRET")
    if not base_url:
        missing.append("MCP_BASE_URL")

    if missing:
        logger.error(
            f"Missing required environment variables for OIDC mode: {', '.join(missing)}"
        )
        logger.error(
            "OIDC mode requires an external OIDC provider (Authentik, Keycloak, Auth0, etc.)"
        )
        sys.exit(1)

    assert oidc_config_url is not None
    assert oidc_client_id is not None
    assert oidc_client_secret is not None
    assert base_url is not None

    host, port, path = _get_http_runtime(default_port=8086)

    _run_entrypoint(
        _run_oidc_server(
            oidc_config_url,
            oidc_client_id,
            oidc_client_secret,
            base_url,
            host,
            port,
            path,
        ),
        "OIDC server",
    )


async def _run_oidc_server(
    config_url: str,
    client_id: str,
    client_secret: str,
    base_url: str,
    host: str,
    port: int,
    path: str,
) -> None:
    """Run the OIDC-authenticated MCP server.

    Unlike OAuth mode which uses OAuthProxyClient for per-user credential routing,
    OIDC mode uses the standard HomeAssistantClient with shared credentials.
    OIDC is purely an access gate — all authenticated users share the same HA instance.

    Args:
        config_url: OIDC discovery URL (.well-known/openid-configuration)
        client_id: OAuth client ID from the OIDC provider
        client_secret: OAuth client secret from the OIDC provider
        base_url: Public HTTPS URL where this server is accessible
        host: Bind host (typically 0.0.0.0; override via MCP_HOST)
        port: Port to listen on
        path: MCP endpoint path
    """
    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    from ha_mcp.server import HomeAssistantSmartMCPServer
    from ha_mcp.transport_security import ensure_host_origin_guard_default_off

    # OIDC mode is deployed behind a public reverse proxy or LAN hostname;
    # disable FastMCP's Host/Origin rebinding guard so proxied requests are
    # not rejected with 421/403 before OIDC auth runs.
    ensure_host_origin_guard_default_off()

    proxy_kwargs: dict[str, Any] = {
        "config_url": config_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "base_url": base_url,
        # "external" tells FastMCP that consent is handled by the upstream
        # IdP (Authentik, Keycloak, etc.) -- unlike `False`, this does not
        # log a security warning at startup that consent is disabled.
        "require_authorization_consent": "external",
        # Preserve `or None`: an empty-but-set env var must not bypass
        # FastMCP's derive-from-client-secret default for jwt_signing_key.
        "jwt_signing_key": os.getenv("OIDC_JWT_SIGNING_KEY") or None,
    }

    allowed_redirect_uris = _oidc_allowed_client_redirect_uris()
    if allowed_redirect_uris:
        proxy_kwargs["allowed_client_redirect_uris"] = allowed_redirect_uris

    if _oidc_verify_id_token_enabled():
        proxy_kwargs["verify_id_token"] = True

    # Create OIDC auth provider — auto-discovers endpoints from config_url
    auth = OIDCProxy(**proxy_kwargs)

    # Standard server with shared credentials (no proxy client needed)
    global _server
    _server = HomeAssistantSmartMCPServer()
    mcp_instance = _server.mcp
    mcp_instance.auth = auth

    logger.info("Server created with OIDC authentication")
    if _healthz_enabled():
        _register_healthz_route(mcp_instance)
    logger.info(f"Starting OIDC-enabled MCP server at {base_url}{path}")

    await _run_with_shutdown(
        mcp_instance.run_async(**_http_run_kwargs("http", host, port, path))
    )


if __name__ == "__main__":
    main()
