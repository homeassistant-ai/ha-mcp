"""Keep the IPv6 listener local-network-only, like IPv4 behind NAT (issue #2915).

IPv4 clients reach the wildcard listener only from the LAN unless the user
forwards a port. IPv6 has no NAT: a router that admits inbound IPv6 would put
the port on the internet. This guard refuses IPv6 peers outside loopback,
link-local, unique-local, the host's own on-link networks and an operator
allowlist, and leaves IPv4 exactly as it was.

It judges the socket peer, never ``X-Forwarded-For``: uvicorn's own
proxy-header middleware runs after it with the same trusted proxies, so a local
reverse proxy keeps working and a forged header cannot get a peer through.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from collections.abc import Iterable
from typing import Any

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

logger = logging.getLogger(__name__)

_ALWAYS_LOCAL = tuple(
    ipaddress.IPv6Network(net) for net in ("::1/128", "fe80::/10", "fc00::/7")
)
_ROUTES_FILE = "/proc/net/ipv6_route"
_RTF_UP, _RTF_GATEWAY, _RTF_REJECT = 0x0001, 0x0002, 0x0200
_ROUTES_TTL_SECONDS = 30.0


def parse_allowed_networks(text: str) -> tuple[ipaddress.IPv6Network, ...]:
    """Parse the operator allowlist: IPv6 networks separated by commas or lines.

    Invalid or IPv4 entries are logged and skipped rather than failing startup.
    """
    networks = []
    for raw in text.replace(",", "\n").splitlines():
        entry = raw.strip()
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            logger.warning("Ignoring invalid IPv6 allowed network %r", entry)
            continue
        if not isinstance(network, ipaddress.IPv6Network):
            logger.warning("Ignoring non-IPv6 allowed network %r", entry)
            continue
        networks.append(network)
    return tuple(networks)


def on_link_ipv6_networks(
    routes_file: str = _ROUTES_FILE,
) -> tuple[ipaddress.IPv6Network, ...]:
    """Return the IPv6 networks the host reaches directly, from the kernel routes.

    These are the local networks: the LAN prefix even when the host's own
    address is a DHCPv6 /128, any docker bridge; never the default route, a
    reject route, or the loopback device's own-address routes. An unreadable table yields none, so the guard fails closed.
    """
    try:
        with open(routes_file, encoding="ascii") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ()
    networks = set()
    for line in lines:
        fields = line.split()
        if len(fields) < 10:
            continue
        dest, plen, next_hop, flags, dev = (
            fields[0],
            fields[1],
            fields[4],
            fields[8],
            fields[9],
        )
        prefix_len, route_flags = int(plen, 16), int(flags, 16)
        if (
            dev == "lo"  # the host's own addresses, not a network it reaches
            or prefix_len == 0
            or int(next_hop, 16) != 0
            or not route_flags & _RTF_UP
            or route_flags & (_RTF_GATEWAY | _RTF_REJECT)
        ):
            continue
        network = ipaddress.IPv6Network((int(dest, 16), prefix_len), strict=False)
        if not network.is_multicast:
            networks.add(network)
    return tuple(networks)


class IPv6LanGuard:
    """ASGI middleware refusing IPv6 peers outside the local network."""

    def __init__(
        self,
        app: Any,
        allowed_networks: Iterable[ipaddress.IPv6Network] = (),
        trusted_proxies: str | None = None,
        routes_file: str = _ROUTES_FILE,
    ) -> None:
        # Same default as uvicorn's own Config, which this replaces in the stack.
        trusted = trusted_proxies or os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
        self._app = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
        self._allowed = tuple(allowed_networks)
        self._routes_file = routes_file
        self._on_link: tuple[ipaddress.IPv6Network, ...] = ()
        self._on_link_at = float("-inf")

    def _peer_allowed(self, client: tuple[str, int] | None) -> bool:
        if not client:
            return True  # not a socket peer (unix socket / test client)
        try:
            peer = ipaddress.ip_address(client[0].split("%", 1)[0])
        except ValueError:
            return False
        if isinstance(peer, ipaddress.IPv4Address) or peer.ipv4_mapped:
            return True  # IPv4 keeps today's behaviour
        if any(peer in net for net in _ALWAYS_LOCAL + self._allowed):
            return True
        now = time.monotonic()
        if now - self._on_link_at > _ROUTES_TTL_SECONDS:
            self._on_link = on_link_ipv6_networks(self._routes_file)
            self._on_link_at = now
        return any(peer in net for net in self._on_link)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket") and not self._peer_allowed(
            scope.get("client")
        ):
            logger.warning(
                "Refused IPv6 connection from %s: outside the local network "
                "(add its network to the IPv6 allowed networks setting to permit it)",
                scope["client"][0],
            )
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"IPv6 access is limited to the local network.\n",
                }
            )
            return
        await self._app(scope, receive, send)
