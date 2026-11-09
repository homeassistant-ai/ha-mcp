"""Pin the upstream defaults the MCP relay's stream timeouts depend on.

``custom_components/ha_mcp_tools/mcp_webhook.py`` and the webhook-proxy addon
build their relay session with
``aiohttp.ClientTimeout(connect=30, sock_connect=10, sock_read=300)`` — no
wall-clock ``total`` — so a long-lived MCP response stream is bounded by read
*idleness* rather than elapsed time. Two facts that make that safe live in
packages this repo does not own, and neither is visible to the suites that
exercise the relay:

1. **``ClientTimeout``'s constructor defaults.** Let aiohttp give ``total`` a
   non-``None`` default and production silently regains a wall-clock bound.

2. **sse-starlette's keepalive ping.** ``sock_read=300`` only spares a *healthy
   but idle* stream because something writes to that stream more often than
   every 300 s. That something is ``EventSourceResponse``'s ping task: the MCP
   SDK constructs every SSE response without a ``ping`` argument, so the
   interval is sse-starlette's ``DEFAULT_PING_INTERVAL`` of 15 s.

Both packages are importable here: ``aiohttp`` comes with Home Assistant Core
(a dev dependency), ``sse-starlette`` arrives transitively with the ``mcp`` SDK.
"""

from __future__ import annotations

import ast
import inspect

import aiohttp
from sse_starlette.sse import EventSourceResponse

from ha_mcp._vendor.mcp.server import streamable_http


class TestAiohttpClientTimeoutContract:
    """The genuine ``aiohttp.ClientTimeout`` still behaves as the relay assumes."""

    def test_relay_timeout_leaves_total_unbounded(self):
        version = aiohttp.__version__
        timeout = aiohttp.ClientTimeout(connect=30, sock_connect=10, sock_read=300)
        relay = {
            field: getattr(timeout, field)
            for field in ("total", "connect", "sock_connect", "sock_read")
        }

        assert relay["total"] is None, (
            f"aiohttp {version} defaults ClientTimeout.total to "
            f"{relay['total']!r} when it is not passed. The relay omits `total` "
            "precisely to leave the wall-clock unbounded, so a long-lived MCP "
            "response stream (the spec's `subscriptions/listen`) would now be "
            "cut mid-stream and force the client to re-subscribe."
        )
        assert (relay["connect"], relay["sock_connect"], relay["sock_read"]) == (
            30,
            10,
            300,
        ), (
            f"aiohttp {version} no longer round-trips the relay's explicit "
            "bounds. Pool acquisition (30 s), TCP connect (10 s) and read "
            "idleness (300 s) are what keep a dead stream — and a pool "
            "exhausted by live ones — from hanging forever."
        )


class TestSseKeepalivePingContract:
    """A healthy idle MCP stream still gets written to well inside ``sock_read``.

    The MCP SDK builds its ``EventSourceResponse`` objects deep inside request
    handlers that need a live ASGI scope, so there is no way to obtain the
    production instance and read ``ping_interval`` off it without driving a full
    streamable-HTTP session. The strongest pin the installed API supports is
    therefore the pair of facts that *determine* that interval: sse-starlette's
    class default, and the absence of a ``ping`` override at every SDK call site
    (found by parsing the SDK module, which is immune to reformatting).
    """

    def test_default_ping_interval_outpaces_sock_read(self):
        interval = EventSourceResponse.DEFAULT_PING_INTERVAL

        assert interval == 15, (
            f"sse-starlette's default SSE keepalive ping is now {interval}s, not "
            "15s. That ping is the only traffic on a healthy but idle MCP "
            "stream; once it exceeds the relay's sock_read=300 the relay starts "
            "tearing down streams that are perfectly alive."
        )

    def test_omitting_ping_yields_the_class_default(self):
        ping_parameter = inspect.signature(EventSourceResponse.__init__).parameters[
            "ping"
        ]
        assert ping_parameter.default is None, (
            "EventSourceResponse's `ping` argument no longer defaults to None, so "
            "callers that omit it (the MCP SDK does) may no longer inherit "
            "DEFAULT_PING_INTERVAL — the relay's sock_read=300 safety margin "
            "would rest on an interval nothing here checks."
        )

        async def _content():
            """Never consumed — the constructor only stores the iterator."""
            yield ""  # pragma: no cover

        response = EventSourceResponse(content=_content())
        assert response.ping_interval == EventSourceResponse.DEFAULT_PING_INTERVAL, (
            "Constructing EventSourceResponse without `ping` no longer yields "
            "DEFAULT_PING_INTERVAL. The relay's sock_read=300 assumes a 15s "
            "keepalive on idle MCP streams."
        )

    def test_mcp_sdk_leaves_the_ping_interval_at_the_default(self):
        assert streamable_http.EventSourceResponse is EventSourceResponse, (
            "mcp.server.streamable_http no longer builds sse-starlette's "
            "EventSourceResponse, so the keepalive interval asserted above is not "
            "the one guarding the relay's idle MCP streams."
        )

        module = ast.parse(inspect.getsource(streamable_http))
        call_sites = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "EventSourceResponse"
        ]

        assert call_sites, (
            "No EventSourceResponse construction found in "
            "mcp.server.streamable_http. The SDK builds SSE responses somewhere "
            "else now; re-locate them before trusting that the relay's idle "
            "streams still get a 15s keepalive."
        )

        overrides = sorted(
            node.lineno
            for node in call_sites
            if any(keyword.arg == "ping" for keyword in node.keywords)
        )
        assert not overrides, (
            "mcp.server.streamable_http now passes an explicit `ping` at "
            f"line(s) {overrides}, overriding sse-starlette's 15s default. If the "
            "chosen interval exceeds the relay's sock_read=300, healthy idle MCP "
            "streams get torn down as if they were dead."
        )
