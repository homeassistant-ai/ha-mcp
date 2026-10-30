"""IPv6 reaches the wildcard listener from the local network only (issue #2915)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ha_mcp.ipv6_lan_guard import (
    IPv6LanGuard,
    on_link_ipv6_networks,
    parse_allowed_networks,
)

# dest plen src splen next_hop metric refcnt use flags dev
_ROUTES = """\
20010db8000100000000000000000000 40 00000000000000000000000000000000 00 00000000000000000000000000000000 00000100 00000001 00000000 00000001 enp0s2
fe800000000000000000000000000000 40 00000000000000000000000000000000 00 00000000000000000000000000000000 00000100 00000001 00000000 00000001 enp0s2
00000000000000000000000000000000 00 00000000000000000000000000000000 00 fe800000000000000000000000000001 00000400 00000001 00000000 00000003 enp0s2
20010db8ff0000000000000000000000 38 00000000000000000000000000000000 00 00000000000000000000000000000000 00000400 00000001 00000000 00000201 lo
20010db8009900000000000000000007 80 00000000000000000000000000000000 00 00000000000000000000000000000000 00000000 00000001 00000000 80200001 lo
ff000000000000000000000000000000 08 00000000000000000000000000000000 00 00000000000000000000000000000000 00000100 00000001 00000000 00000001 enp0s2
"""
_LAN = "2001:db8:1::25"
_INTERNET = "2001:db8:99::7"


@pytest.fixture
def routes(tmp_path: Path) -> str:
    path = tmp_path / "ipv6_route"
    path.write_text(_ROUTES)
    return str(path)


def test_local_networks_come_from_direct_routes_only(routes: str) -> None:
    assert {str(n) for n in on_link_ipv6_networks(routes)} == {
        "2001:db8:1::/64",
        "fe80::/64",
    }


def test_unreadable_route_table_trusts_no_global_prefix(tmp_path: Path) -> None:
    assert on_link_ipv6_networks(str(tmp_path / "missing")) == ()


def test_allowlist_skips_invalid_and_ipv4_entries() -> None:
    nets = parse_allowed_networks("2001:db8:5::/48,\n bogus, 10.0.0.0/8\n2001:db8:6::1")
    assert [str(n) for n in nets] == ["2001:db8:5::/48", "2001:db8:6::1/128"]


async def _call(
    guard_kwargs: dict[str, Any],
    client: tuple[str, int],
    headers: list[tuple[bytes, bytes]] | None = None,
    kind: str = "http",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen["client"] = scope["client"]

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    scope = {"type": kind, "client": client, "headers": headers or []}
    await IPv6LanGuard(app, **guard_kwargs)(scope, receive, send)
    return sent, seen or None


@pytest.mark.parametrize(
    "peer",
    [_LAN, "::1", "fe80::1%enp0s2", "fd00::9", "192.168.1.20", "::ffff:192.168.1.20"],
    ids=["lan-prefix", "loopback", "link-local", "unique-local", "ipv4", "ipv4-mapped"],
)
async def test_local_peers_are_served(routes: str, peer: str) -> None:
    sent, seen = await _call({"routes_file": routes}, (peer, 1234))
    assert seen is not None and not sent


async def test_internet_ipv6_peer_is_refused(routes: str) -> None:
    sent, seen = await _call({"routes_file": routes}, (_INTERNET, 1234))
    assert seen is None
    assert sent[0]["status"] == 403


async def test_internet_ipv6_websocket_is_closed(routes: str) -> None:
    sent, seen = await _call(
        {"routes_file": routes}, (_INTERNET, 1234), kind="websocket"
    )
    assert seen is None
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_allowlisted_network_is_served(routes: str) -> None:
    allowed = parse_allowed_networks("2001:db8:99::/48")
    _sent, seen = await _call(
        {"routes_file": routes, "allowed_networks": allowed}, (_INTERNET, 1234)
    )
    assert seen is not None


async def test_forged_forwarded_for_cannot_admit_internet_peer(routes: str) -> None:
    _sent, seen = await _call(
        {"routes_file": routes},
        (_INTERNET, 1234),
        [(b"x-forwarded-for", _LAN.encode())],
    )
    assert seen is None


async def test_local_proxy_still_forwards_its_client(routes: str) -> None:
    _sent, seen = await _call(
        {"routes_file": routes, "trusted_proxies": "127.0.0.1"},
        ("127.0.0.1", 1234),
        [(b"x-forwarded-for", _INTERNET.encode())],
    )
    assert seen == {"client": (_INTERNET, 0)}
