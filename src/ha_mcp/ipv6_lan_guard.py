"""Keep the IPv6 listener local-network-only, like IPv4 behind NAT (issue #2915).

IPv4 clients reach the wildcard listener only from the LAN unless the user
forwards a port. IPv6 has no NAT: a router that admits inbound IPv6 would put
the port on the internet. This guard refuses IPv6 peers outside loopback,
link-local, unique-local, the host's own on-link networks and an operator
allowlist, and leaves IPv4 exactly as it was.

It judges the socket peer, never ``X-Forwarded-For``: the guard wraps uvicorn's
``ProxyHeadersMiddleware`` itself (uvicorn's server-level one is turned off),
so proxy headers are applied after the check with uvicorn's default trusted
proxies. A local reverse proxy keeps working and a forged header cannot get a
peer through.
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

SETTING_NAME = "Extra IPv6 networks allowed (ipv6_allowed_networks)"
_ALWAYS_LOCAL = tuple(
    ipaddress.IPv6Network(net) for net in ("::1/128", "fe80::/10", "fc00::/7")
)
_ROUTES_FILE = "/proc/net/ipv6_route"
_RTF_UP, _RTF_GATEWAY, _RTF_REJECT = 0x0001, 0x0002, 0x0200
_ROUTES_TTL_SECONDS = 30.0
# A directly connected network is a LAN prefix (/64, at most a /48 site).
# Shorter device routes are catch-alls, such as a VPN client's 2000::/4 or
# ::/1, and would make every internet address look local.
_MIN_ON_LINK_PREFIX = 48
# Distinct refused peers logged at WARNING before the rest drop to DEBUG, so
# scanners cannot flood the log.
_WARNED_PEERS_CAP = 256


def _split_entries(text: str) -> list[str]:
    return [e.strip() for e in text.replace(",", "\n").splitlines() if e.strip()]


def _parse_entry(entry: str) -> ipaddress.IPv6Network | None:
    try:
        network = ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return None
    return network if isinstance(network, ipaddress.IPv6Network) else None


def invalid_allowed_networks(text: str) -> list[str]:
    """Return the allowlist entries that are not IPv6 networks."""
    return [entry for entry in _split_entries(text) if _parse_entry(entry) is None]


def parse_allowed_networks(text: str) -> tuple[ipaddress.IPv6Network, ...]:
    """Parse the operator allowlist: IPv6 networks separated by commas or lines.

    The settings page rejects invalid entries on save; one set another way
    (app options, environment) is logged and skipped rather than failing
    startup.
    """
    networks = []
    for entry in _split_entries(text):
        network = _parse_entry(entry)
        if network is None:
            logger.error(
                "Ignoring %s entry %r: not an IPv6 network", SETTING_NAME, entry
            )
            continue
        networks.append(network)
    return tuple(networks)


def configured_allowed_networks() -> tuple[ipaddress.IPv6Network, ...]:
    """Return the allowlist from the server's settings."""
    from .config import get_global_settings

    return parse_allowed_networks(get_global_settings().ipv6_allowed_networks)


def on_link_ipv6_networks(
    routes_file: str = _ROUTES_FILE,
) -> tuple[ipaddress.IPv6Network, ...]:
    """Return the IPv6 networks the host reaches directly, from the kernel routes.

    These are the local networks: the LAN prefix even when the host's own
    address is a DHCPv6 /128, any docker bridge. Never the default route, a
    route through a gateway, a reject route, a catch-all shorter than /48, or
    the loopback device's own-address routes. An unreadable table yields none,
    so the guard fails closed.
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
        dest, plen, _src, _src_plen, next_hop, _metric, _ref, _use, flags, dev = fields[
            :10
        ]
        prefix_len, route_flags = int(plen, 16), int(flags, 16)
        if (
            dev == "lo"
            or prefix_len < _MIN_ON_LINK_PREFIX
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
        self._allowed = _ALWAYS_LOCAL + tuple(allowed_networks)
        self._routes_file = routes_file
        self._on_link: tuple[ipaddress.IPv6Network, ...] = ()
        self._on_link_at = float("-inf")
        self._warned: set[str] = set()

    def _peer_allowed(self, client: tuple[str, int] | None) -> bool:
        if not client:
            return True  # no peer address (e.g. a unix socket)
        try:
            peer = ipaddress.ip_address(client[0].split("%", 1)[0])
        except ValueError:
            return False  # not an IP (e.g. Starlette TestClient's "testclient")
        if isinstance(peer, ipaddress.IPv4Address) or peer.ipv4_mapped:
            return True  # IPv4 is not filtered
        if any(peer in net for net in self._allowed):
            return True
        now = time.monotonic()
        if now - self._on_link_at > _ROUTES_TTL_SECONDS:
            self._on_link = on_link_ipv6_networks(self._routes_file)
            self._on_link_at = now
        return any(peer in net for net in self._on_link)

    def _log_refusal(self, peer: str) -> None:
        level = logging.DEBUG
        if peer not in self._warned and len(self._warned) < _WARNED_PEERS_CAP:
            self._warned.add(peer)
            level = logging.WARNING
        logger.log(
            level,
            "Refused IPv6 connection from %s: outside the local network "
            "(add its network to %s to permit it)",
            peer,
            SETTING_NAME,
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket") or self._peer_allowed(
            scope.get("client")
        ):
            await self._app(scope, receive, send)
            return
        self._log_refusal(scope["client"][0])
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
                "body": (
                    "IPv6 access is limited to the local network. To allow this "
                    f"network, add it to {SETTING_NAME}.\n"
                ).encode(),
            }
        )
