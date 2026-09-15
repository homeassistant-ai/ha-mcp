"""Shared HTTP endpoint modes and transport options for HA-MCP launchers."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any
from uuid import uuid4

from starlette.middleware import Middleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ha_mcp._vendor import fastmcp
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.server.http import StarletteWithLifespan

from .config import get_global_settings

logger = logging.getLogger(__name__)


def _get_route_path(scope: Scope) -> str:
    """Local copy of ``starlette._utils.get_route_path`` (private API).

    Strips a mount's ``root_path`` prefix from ``scope["path"]``, the same
    prefix Starlette's own routers use to resolve a sub-app's route. Vendored
    because the source module is private and a minor Starlette bump could
    move or change it without notice; keep this in sync with upstream if
    scope's routing fields ever change.
    """
    path: str = scope["path"]
    root_path = scope.get("root_path", "")
    if not root_path:
        return path
    if not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


@dataclass
class _DiagnosticLogLevel:
    """Shared ownership while HTTP app lifespans overlap during a restart."""

    users: int = 0
    previous_level: int = logging.NOTSET
    changed: bool = False
    lock: Lock = field(default_factory=Lock)


@contextmanager
def _diagnostic_logging() -> Iterator[None]:
    """Temporarily enable diagnostic records without changing other loggers."""
    # Logger objects survive embedded-server module purges. Keep ownership on
    # this logger so a retiring worker cannot reset its replacement's level.
    state: _DiagnosticLogLevel = vars(logger).setdefault(
        "_ha_mcp_http_diagnostics_level", _DiagnosticLogLevel()
    )
    with state.lock:
        if state.users == 0:
            state.previous_level = logger.level
            state.changed = logger.getEffectiveLevel() > logging.INFO
            if state.changed:
                # Use the normal setter: HA's explicit per-logger overrides
                # remain authoritative, and existing DEBUG levels stay intact.
                logger.setLevel(logging.INFO)
        state.users += 1
    try:
        yield
    finally:
        with state.lock:
            state.users -= 1
            if state.users == 0 and state.changed and logger.level == logging.INFO:
                logger.setLevel(state.previous_level)


class HttpTransportFastMCP(FastMCP):
    """Apply instance HTTP experiments at the shared app-construction boundary.

    Both CLI HTTP launchers and existing embedded components call ``http_app``.
    Keep FastMCP's arguments untouched when the options are off, including any
    existing FASTMCP_JSON_RESPONSE setting. Stdio does not use this method.
    """

    def http_app(self, *args: Any, **kwargs: Any) -> StarletteWithLifespan:
        """Build the app with a readonly alias and optional transport experiments."""
        settings = get_global_settings()
        if settings.http_json_response:
            # FastMCP accepts json_response as its third positional argument.
            if len(args) > 2:
                args = (*args[:2], True, *args[3:])
            else:
                kwargs["json_response"] = True
        if settings.http_transport_diagnostics:
            path = args[0] if args else kwargs.get("path")
            existing = args[1] if len(args) > 1 else kwargs.get("middleware")
            middleware = [
                *(existing or []),
                Middleware(
                    TransportDiagnostics,
                    path=path or fastmcp.settings.streamable_http_path,
                ),
            ]
            if len(args) > 1:
                args = (args[0], middleware, *args[2:])
            else:
                kwargs["middleware"] = middleware
        app = super().http_app(*args, **kwargs)
        if app.state.transport_type == "streamable-http":
            app.add_middleware(ReadOnlyEndpoint, path=app.state.path)
        return app


class ReadOnlyEndpoint:
    """Route the exact /readonly alias through the same authenticated MCP app.

    This selects read-only behavior for a configured client connection; the
    credential still works at the normal endpoint. Settings and other HTTP
    routes are not aliased.
    """

    def __init__(self, app: ASGIApp, path: str) -> None:
        self.app = app
        self.path = path
        self.readonly_path = f"{path.rstrip('/')}/readonly"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or _get_route_path(scope).rstrip("/") != self.readonly_path
        ):
            await self.app(scope, receive, send)
            return

        from .read_only import read_only_request

        route_path = _get_route_path(scope)
        prefix = scope["path"][: -len(route_path)]
        path = prefix + self.path
        scope = {**scope, "path": path, "raw_path": path.encode("utf-8")}
        with read_only_request():
            await self.app(scope, receive, send)


@dataclass
class _Transfer:
    """Counters local to one HTTP request; never retain body data."""

    request_bytes: int = 0
    response_bytes: int = 0
    request_complete: bool = False
    response_complete: bool = False
    disconnect: bool = False
    status: int | None = None
    error: str | None = None

    def received(self, message: Message, trace: str) -> None:
        """Count only events delivered by the underlying receive callable."""
        if message["type"] == "http.request":
            self.request_bytes += len(message.get("body", b""))
            if not message.get("more_body", False):
                self.request_complete = True
                logger.info("MCP HTTP trace=%s request body complete", trace)
        elif message["type"] == "http.disconnect":
            self.disconnect = True

    def sent(self, message: Message, trace: str, started: float) -> None:
        """Record accepted events, including final-body handoff before app cleanup."""
        if message["type"] == "http.response.start":
            self.status = message["status"]
            logger.info(
                "MCP HTTP trace=%s response headers status=%s", trace, self.status
            )
        elif message["type"] == "http.response.body":
            self.response_bytes += len(message.get("body", b""))
            self.response_complete = not message.get("more_body", False)
            if self.response_complete:
                logger.info(
                    "MCP HTTP trace=%s response body complete response_bytes=%d elapsed_ms=%.1f",
                    trace,
                    self.response_bytes,
                    (monotonic() - started) * 1000,
                )


def _declared_length(scope: Scope) -> int | None:
    """Read only a bounded numeric length, never log arbitrary header text."""
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            if len(value) <= 20 and value.isdigit():
                return int(value)
            return None
    return None


class TransportDiagnostics:
    """Observe the MCP HTTP boundary without buffering or altering messages.

    Completion means ASGI accepted the final body event, not that the client
    received or processed it. No paths, headers, request IDs supplied by clients,
    bodies or exception messages are logged. Other HTTP routes are untouched.
    """

    def __init__(self, app: ASGIApp, path: str) -> None:
        self.app = app
        self.path = path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            with _diagnostic_logging():
                await self.app(scope, receive, send)
            return
        if scope["type"] != "http" or scope["path"].rstrip("/") != self.path:
            await self.app(scope, receive, send)
            return

        transfer = _Transfer()
        trace = uuid4().hex[:16]
        started = monotonic()
        declared = _declared_length(scope)
        logger.info("MCP HTTP trace=%s started declared_bytes=%s", trace, declared)

        async def receive_observed() -> Message:
            message = await receive()
            transfer.received(message, trace)
            return message

        async def send_observed(message: Message) -> None:
            await send(message)
            transfer.sent(message, trace, started)

        try:
            await self.app(scope, receive_observed, send_observed)
        except BaseException as exc:
            # Cancellation is diagnostic too; always preserve the original exit.
            transfer.error = type(exc).__name__
            raise
        finally:
            logger.info(
                "MCP HTTP trace=%s finished elapsed_ms=%.1f declared_bytes=%s "
                "request_bytes=%d request_complete=%s status=%s "
                "response_bytes=%d response_complete=%s disconnect=%s error=%s",
                trace,
                (monotonic() - started) * 1000,
                declared,
                transfer.request_bytes,
                transfer.request_complete,
                transfer.status,
                transfer.response_bytes,
                transfer.response_complete,
                transfer.disconnect,
                transfer.error,
            )
