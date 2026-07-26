"""Pin the upstream defaults the MCP relay's stream timeouts depend on.

``custom_components/ha_mcp_tools/mcp_webhook.py`` and the webhook-proxy addon
build their relay session with
``aiohttp.ClientTimeout(connect=30, sock_connect=10, sock_read=300)`` — no
wall-clock ``total`` — so a long-lived MCP response stream is bounded by read
*idleness* rather than elapsed time. Two facts that make that safe live in
packages this repo does not own, and neither is visible to the suites that
exercise the relay:

1. **``ClientTimeout``'s constructor defaults.** The relay tests assert
   ``total is None`` against *stubs* (``_embedded_stubs.ClientTimeout`` and
   ``tests/addon/test_webhook_proxy._FakeClientTimeout``), because aiohttp is a
   Home Assistant runtime package that the unit environment fakes. Let aiohttp
   give ``total`` a non-``None`` default and production silently regains a
   wall-clock bound while both stub-backed suites stay green.

2. **sse-starlette's keepalive ping.** ``sock_read=300`` only spares a *healthy
   but idle* stream because something writes to that stream more often than
   every 300 s. That something is ``EventSourceResponse``'s ping task: the MCP
   SDK constructs every SSE response without a ``ping`` argument, so the
   interval is sse-starlette's ``DEFAULT_PING_INTERVAL`` of 15 s.

Both packages are importable here: ``aiohttp`` is a dev dependency added for
this module, ``sse-starlette`` arrives transitively with the ``mcp`` SDK.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
from typing import Any

import pytest
from mcp.server import streamable_http
from sse_starlette.sse import EventSourceResponse

from tests.addon.test_webhook_proxy import _FakeClientTimeout as AddonClientTimeout

from ._embedded_stubs import ClientTimeout as EmbeddedClientTimeout

# Every bound aiohttp's ClientTimeout carries. Production leaves `total` unset
# on purpose and sets the other three explicitly; `ceil_threshold` is aiohttp's
# own rounding knob, modelled by the stubs so an added field shows up as drift.
_TIMEOUT_FIELDS = ("total", "connect", "sock_read", "sock_connect", "ceil_threshold")

# The two hand-written stand-ins that stub-backed relay tests assert against.
_STUB_TIMEOUTS = {
    "tests/src/unit/_embedded_stubs.py::ClientTimeout": EmbeddedClientTimeout,
    "tests/addon/test_webhook_proxy.py::_FakeClientTimeout": AddonClientTimeout,
}

_STUB_TIMEOUT_CASES = [
    pytest.param(origin, stub, id=origin)
    for origin, stub in sorted(_STUB_TIMEOUTS.items())
]

# Reads the genuine ClientTimeout in a *pristine* interpreter and reports it as
# JSON. Field names arrive on argv. Anything that is not JSON-serializable —
# aiohttp swapping a default for a sentinel object, say — fails the dump here
# and surfaces as a loud probe error rather than a silently weakened assertion.
_AIOHTTP_PROBE = """\
import inspect
import json
import sys

import aiohttp

fields = sys.argv[1:]
signature = inspect.signature(aiohttp.ClientTimeout)
relay = aiohttp.ClientTimeout(connect=30, sock_connect=10, sock_read=300)
json.dump(
    {
        "version": aiohttp.__version__,
        "constructor": [
            [name, parameter.default]
            for name, parameter in signature.parameters.items()
        ],
        "defaults": {
            field: getattr(aiohttp.ClientTimeout(), field) for field in fields
        },
        "relay": {field: getattr(relay, field) for field in fields},
    },
    sys.stdout,
)
"""


@pytest.fixture(scope="module")
def real_aiohttp() -> dict[str, Any]:
    """Introspect the genuine ``aiohttp.ClientTimeout`` in a separate process.

    Importing aiohttp here would not give the real thing: ``_embedded_stubs``
    assigns a hand-built module to ``sys.modules["aiohttp"]`` (and two OAuth
    test modules do the same for ``yarl``, which the real aiohttp imports), all
    at *collection* time from whichever peer module pytest reaches first. An
    in-process import would therefore hand back a fake and turn the comparisons
    below into a stub measured against itself — precisely the blind spot this
    module exists to close. A fresh interpreter has no test stubs in it at all,
    so the result cannot depend on collection order.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _AIOHTTP_PROBE, *_TIMEOUT_FIELDS],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, (
        "Could not introspect the real aiohttp. It is a dev dependency for "
        "exactly this check, so without it nothing verifies that the relay's "
        "ClientTimeout stubs still match the class production uses:\n"
        f"{completed.stderr.strip()}"
    )
    return json.loads(completed.stdout)


def _constructor_pairs(cls: Any) -> list[list[Any]]:
    """Constructor parameters as ordered ``[name, default]`` pairs.

    Lists, not tuples, so the shape matches the probe's JSON round-trip.
    """
    return [
        [name, parameter.default]
        for name, parameter in inspect.signature(cls).parameters.items()
    ]


class TestAiohttpClientTimeoutContract:
    """The genuine ``aiohttp.ClientTimeout`` still behaves as the relay assumes."""

    def test_relay_timeout_leaves_total_unbounded(self, real_aiohttp):
        version = real_aiohttp["version"]
        relay = real_aiohttp["relay"]

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

    @pytest.mark.parametrize("origin,stub", _STUB_TIMEOUT_CASES)
    def test_stub_constructor_matches_aiohttp(self, origin, stub, real_aiohttp):
        assert _constructor_pairs(stub) == real_aiohttp["constructor"], (
            f"{origin} no longer mirrors aiohttp {real_aiohttp['version']}'s "
            "ClientTimeout constructor. Relay timeout tests build that stub and "
            "assert on the bounds production deliberately leaves unset, so a "
            "drifted stub makes them assert a shape production never has — they "
            "would keep passing while the real relay timed out differently."
        )

    @pytest.mark.parametrize("origin,stub", _STUB_TIMEOUT_CASES)
    def test_stub_defaults_match_aiohttp(self, origin, stub, real_aiohttp):
        fake = stub()
        # Sentinel rather than a bare getattr: a stub that DROPPED a bound
        # should read as a value mismatch in the diff below, not as an
        # AttributeError that buries the explanation.
        stub_defaults = {
            field: getattr(fake, field, "<absent from stub>")
            for field in _TIMEOUT_FIELDS
        }

        assert stub_defaults == real_aiohttp["defaults"], (
            f"A default-constructed {origin} does not match a default-constructed "
            f"aiohttp {real_aiohttp['version']} ClientTimeout. The relay passes "
            "only three bounds and relies on the rest defaulting to unset, so the "
            "stub-backed tests would be describing a timeout production never "
            "builds."
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

    No peer test stubs ``mcp`` or ``sse_starlette``, so unlike aiohttp above
    these can be imported directly.
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
