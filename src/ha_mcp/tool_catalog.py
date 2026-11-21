"""Per-endpoint tool catalog selection.

``ENABLE_TOOL_SEARCH`` (off by default) sets the catalog of the main
endpoint. A single ha-mcp instance is often shared by several MCP clients
with different needs: one with its own deferred tool search wants the full
catalog, a small-context client wants ha-mcp's search tools. Each URL
therefore serves one fixed catalog:

- ``<path>`` follows ``ENABLE_TOOL_SEARCH``;
- ``<path>/catalog/full`` always serves the full catalog;
- ``<path>/catalog/search`` always serves the search tools.

Either suffix may follow ``/readonly``. The catalog is fixed per URL, not
negotiated per request, because clients do not refresh their tool list
when it changes: a client picks its catalog by the URL it is given.

Stdio serves one client per process, so its environment selects the mode.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final, Literal, TypedDict

from ha_mcp._vendor import mcp_types as mt
from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)

from .config import get_global_settings

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp import FastMCP

CatalogMode = Literal["full", "search"]
CATALOG_MODES: tuple[CatalogMode, ...] = ("full", "search")


class CacheHintArgs(TypedDict):
    """FastMCP's server-level SEP-2549 caching arguments."""

    cache_ttl: int
    cache_scope: Literal["public", "private"]


# MCP 2026-07-28 (SEP-2549) puts ``ttlMs``/``cacheScope`` on every cacheable
# result: tools/list, prompts/list, resources/list, resources/templates/list,
# resources/read and server/discover. With one Home Assistant credential per
# server, each is the same for every caller of an endpoint (the catalog follows
# the deployment's settings and the URL; the resources are the bundled skill
# files), so the scope is ``public`` and shared caches such as MCP gateways may
# serve one copy to all users. The SDK default, ``private``, makes them treat it
# as per-user. The TTL bounds staleness after a live settings change, such as
# Read Only Mode toggled without a restart; the write blocker still applies at
# call time.
CATALOG_CACHE_TTL_S: Final = 300


def catalog_cache_hint(client: object | None) -> CacheHintArgs:
    """The cache hint for a server whose tools run on ``client``.

    A per-request client (the OAuth proxy, which has ``current_client``) runs
    each caller on their own credential, and the component-capability
    transforms shape ``ha_search`` and the helper schema from that caller's own
    probe (``ha_mcp_tools/info`` is admin-only), so its catalog stays private.
    """
    per_request = getattr(type(client), "current_client", None) is not None
    scope: Literal["public", "private"] = "private" if per_request else "public"
    return {"cache_ttl": CATALOG_CACHE_TTL_S, "cache_scope": scope}


_endpoint_mode: ContextVar[CatalogMode | None] = ContextVar(
    "ha_mcp_endpoint_catalog_mode", default=None
)
# False while the search transform cannot be installed: every endpoint then
# serves, and describes, the full catalog. Set by each server it builds, so
# a later server in the process (the component restarts in place) resets it.
_search_available = True


def set_search_available(available: bool) -> None:
    """Record whether this process's server installed the search transform."""
    global _search_available
    _search_available = available


def default_catalog_mode() -> CatalogMode:
    """The main endpoint's mode, set by ``ENABLE_TOOL_SEARCH``."""
    if _search_available and get_global_settings().enable_tool_search:
        return "search"
    return "full"


def current_catalog_mode() -> CatalogMode:
    """The catalog mode of the endpoint serving this request."""
    if not _search_available:
        return "full"
    return _endpoint_mode.get() or default_catalog_mode()


def tool_search_active() -> bool:
    """Whether this request's endpoint serves search-based tool discovery."""
    return current_catalog_mode() == "search"


@contextmanager
def catalog_endpoint(mode: CatalogMode) -> Iterator[None]:
    """Select ``mode`` for this request and its nested tool calls."""
    token = _endpoint_mode.set(mode)
    try:
        yield
    finally:
        _endpoint_mode.reset(token)


class ToolCatalogInstructionsMiddleware(Middleware):
    """Send each connection the server instructions for its endpoint's catalog.

    ``initialize`` (protocol <= 2025-11-25) and ``server/discover``
    (2026-07-28) both carry the instructions.
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
        return result.model_copy(
            update={"instructions": self._instructions_for(current_catalog_mode())}
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


def install_catalog_instructions(
    mcp: FastMCP, instructions: Mapping[CatalogMode, str]
) -> None:
    """Send each connection the instructions for its endpoint's catalog."""
    mcp.add_middleware(ToolCatalogInstructionsMiddleware(instructions.__getitem__))
