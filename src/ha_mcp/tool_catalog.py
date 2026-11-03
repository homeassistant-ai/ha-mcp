"""Per-connection tool catalog negotiation.

``ENABLE_TOOL_SEARCH`` is a server-wide default. A single ha-mcp instance
is often shared by several MCP clients with different needs: one with its
own deferred tool search wants the full catalog, a small-context client
wants ha-mcp's search tools. Each connection may therefore choose its
catalog mode, first match wins:

1. The client declares the ``io.github.homeassistant-ai/tool-catalog``
   extension with a ``mode`` (``"full"`` or ``"search"``), either in a
   request's ``_meta`` client capabilities (protocol 2026-07-28) or in its
   ``initialize`` capabilities (SEP-2133 extension negotiation). ha-mcp
   serves HTTP statelessly, so over HTTP only ``_meta`` reaches later
   requests; initialize-time capabilities apply to stdio sessions. Clients
   that do not know the extension ignore it, and a server that does not
   know it ignores the client's declaration.
2. The HTTP endpoint selects one: ``<path>/catalog/search`` or
   ``<path>/catalog/full``, optionally after ``/readonly``. Most clients
   cannot declare arbitrary extensions, but every client can be given a URL.
3. Otherwise the ``ENABLE_TOOL_SEARCH`` setting applies.

Stdio serves one client per process, so its environment already selects
the mode.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal, cast

from ha_mcp._vendor import mcp_types as mt
from ha_mcp._vendor.fastmcp.server.extensions import ServerExtension
from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)

from .config import get_global_settings

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp import FastMCP

logger = logging.getLogger(__name__)

TOOL_CATALOG_EXTENSION_ID = "io.github.homeassistant-ai/tool-catalog"

CatalogMode = Literal["full", "search"]
CATALOG_MODES: tuple[CatalogMode, ...] = ("full", "search")

_endpoint_mode: ContextVar[CatalogMode | None] = ContextVar(
    "ha_mcp_endpoint_catalog_mode", default=None
)
# Stateless HTTP forgets initialize-time capabilities after the handshake,
# so honoring them at initialize would describe a catalog the client's
# later requests never get.
_initialize_capabilities_kept: ContextVar[bool] = ContextVar(
    "ha_mcp_initialize_capabilities_kept", default=True
)


def default_catalog_mode() -> CatalogMode:
    """The mode a connection gets when neither client nor endpoint chooses."""
    return "search" if get_global_settings().enable_tool_search else "full"


def parse_catalog_mode(settings: Mapping[str, Any] | None) -> CatalogMode | None:
    """The valid ``mode`` from a client's extension settings, if any."""
    if not settings:
        return None
    mode = settings.get("mode")
    if mode in CATALOG_MODES:
        return cast("CatalogMode", mode)
    if mode is not None:
        logger.debug("Ignoring unknown %s mode %r", TOOL_CATALOG_EXTENSION_ID, mode)
    return None


def initialize_extension_settings(capabilities: Any) -> dict[str, Any] | None:
    """This extension's settings from initialize-time client capabilities."""
    if capabilities is None:
        return None
    extensions = getattr(capabilities, "extensions", None)
    if extensions is None:
        # Clients serialized with ``extensions`` as an extra key.
        extensions = (getattr(capabilities, "model_extra", None) or {}).get(
            "extensions"
        )
    if not isinstance(extensions, Mapping):
        return None
    settings = extensions.get(TOOL_CATALOG_EXTENSION_ID)
    return dict(settings) if isinstance(settings, Mapping) else None


def _client_requested_mode() -> CatalogMode | None:
    from ha_mcp._vendor.fastmcp.server.dependencies import get_context

    try:
        ctx = get_context()
    except RuntimeError:
        return None
    mode = parse_catalog_mode(ctx.client_extension_settings(TOOL_CATALOG_EXTENSION_ID))
    if mode is not None:
        return mode
    try:
        client_params = ctx.session.client_params
    except RuntimeError:
        return None
    if client_params is None:
        return None
    return parse_catalog_mode(initialize_extension_settings(client_params.capabilities))


def connection_default_mode() -> CatalogMode:
    """The endpoint's mode, else the server default: what the server offers."""
    return _endpoint_mode.get() or default_catalog_mode()


def current_catalog_mode() -> CatalogMode:
    """The catalog mode for the request being served."""
    return _client_requested_mode() or connection_default_mode()


def tool_search_active() -> bool:
    """Whether this request's connection uses search-based tool discovery."""
    return current_catalog_mode() == "search"


@contextmanager
def stateless_request() -> Iterator[None]:
    """Mark a request served without a session that remembers initialize."""
    token = _initialize_capabilities_kept.set(False)
    try:
        yield
    finally:
        _initialize_capabilities_kept.reset(token)


@contextmanager
def catalog_endpoint(mode: CatalogMode) -> Iterator[None]:
    """Select ``mode`` for this request and its nested tool calls."""
    token = _endpoint_mode.set(mode)
    try:
        yield
    finally:
        _endpoint_mode.reset(token)


class ToolCatalogExtension(ServerExtension):
    """Advertise the catalog modes a client may choose between."""

    identifier = TOOL_CATALOG_EXTENSION_ID

    def settings(self) -> dict[str, Any]:
        return {"modes": list(CATALOG_MODES), "default": connection_default_mode()}


class ToolCatalogInstructionsMiddleware(Middleware):
    """Send each connection the server instructions for its catalog mode.

    ``initialize`` (protocol <= 2025-11-25) carries the client's choice in
    its capabilities, honored only where the session keeps them;
    ``server/discover`` (2026-07-28) carries it in its ``_meta``.
    """

    def __init__(self, instructions_for: Callable[[CatalogMode], str]) -> None:
        self._instructions_for = instructions_for

    async def on_initialize(
        self,
        context: MiddlewareContext[mt.InitializeRequest],
        call_next: CallNext[mt.InitializeRequest, mt.InitializeResult | None],
    ) -> mt.InitializeResult | None:
        result = await call_next(context)
        if result is None:
            return None
        mode = None
        if _initialize_capabilities_kept.get():
            params = getattr(context.message, "params", None)
            mode = parse_catalog_mode(
                initialize_extension_settings(getattr(params, "capabilities", None))
            )
        return result.model_copy(
            update={
                "instructions": self._instructions_for(
                    mode or connection_default_mode()
                )
            }
        )

    async def on_discover(
        self,
        context: MiddlewareContext[mt.DiscoverRequest],
        call_next: CallNext[mt.DiscoverRequest, mt.DiscoverResult | dict[str, Any]],
    ) -> mt.DiscoverResult | dict[str, Any]:
        result = await call_next(context)
        if not isinstance(result, mt.DiscoverResult):
            return result
        return result.model_copy(
            update={"instructions": self._instructions_for(current_catalog_mode())}
        )


def install_catalog_negotiation(
    mcp: FastMCP, instructions: Mapping[CatalogMode, str]
) -> None:
    """Advertise the extension and send each connection its instructions."""
    mcp.add_extension(ToolCatalogExtension())
    mcp.add_middleware(ToolCatalogInstructionsMiddleware(instructions.__getitem__))
