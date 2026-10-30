"""IPv6 reaches the wildcard listener from the local network only (issue #2915)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from ha_mcp import ipv6_lan_guard
from ha_mcp.ipv6_lan_guard import (
    IPv6LanGuard,
    invalid_allowed_networks,
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
20010db8005000000000000000000000 30 00000000000000000000000000000000 00 fe800000000000000000000000000001 00000400 00000001 00000000 00000003 enp0s2
20010db8006000000000000000000000 30 00000000000000000000000000000000 00 00000000000000000000000000000000 00000400 00000001 00000000 00000201 enp0s2
20010db8007000000000000000000000 40 00000000000000000000000000000000 00 00000000000000000000000000000000 00000400 00000001 00000000 00000000 enp0s2
20000000000000000000000000000000 04 00000000000000000000000000000000 00 00000000000000000000000000000000 00000400 00000001 00000000 00000001 tun0
00000000000000000000000000000000 01 00000000000000000000000000000000 00 00000000000000000000000000000000 00000400 00000001 00000000 00000001 wg0
ff000000000000000000000000000000 08 00000000000000000000000000000000 00 00000000000000000000000000000000 00000100 00000001 00000000 00000001 enp0s2
"""
_LAN = "2001:db8:1::25"
_INTERNET = "2001:db8:99::7"


@pytest.fixture
def routes(tmp_path: Path) -> str:
    path = tmp_path / "ipv6_route"
    path.write_text(_ROUTES)
    return str(path)


def test_local_networks_come_from_direct_lan_routes_only(routes: str) -> None:
    # Gateway, reject, down and catch-all (VPN 2000::/4, ::/1) routes are not local.
    assert {str(n) for n in on_link_ipv6_networks(routes)} == {
        "2001:db8:1::/64",
        "fe80::/64",
    }


def test_unreadable_route_table_trusts_no_global_prefix(tmp_path: Path) -> None:
    assert on_link_ipv6_networks(str(tmp_path / "missing")) == ()


def test_allowlist_skips_invalid_and_ipv4_entries() -> None:
    text = "2001:db8:5::/48,\n bogus, 10.0.0.0/8\n2001:db8:6::1"
    nets = parse_allowed_networks(text)
    assert [str(n) for n in nets] == ["2001:db8:5::/48", "2001:db8:6::1/128"]
    assert invalid_allowed_networks(text) == ["bogus", "10.0.0.0/8"]


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


async def test_unparseable_client_address_is_refused(routes: str) -> None:
    _sent, seen = await _call({"routes_file": routes}, ("testclient", 50000))
    assert seen is None


async def test_new_lan_prefix_is_served_once_the_routes_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An ISP-rotated prefix must not need a restart.
    clock = [1000.0]
    monkeypatch.setattr(ipv6_lan_guard.time, "monotonic", lambda: clock[0])
    path = tmp_path / "ipv6_route"
    path.write_text("")
    seen_clients: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen_clients.append(scope["client"])

    async def send(message: dict[str, Any]) -> None:
        return None

    guard = IPv6LanGuard(app, routes_file=str(path))
    scope = {"type": "http", "client": (_LAN, 1), "headers": []}
    await guard(dict(scope), None, send)
    path.write_text(_ROUTES)
    clock[0] += 1  # within the refresh interval: still the old table
    await guard(dict(scope), None, send)
    assert seen_clients == []
    clock[0] += 60
    await guard(dict(scope), None, send)
    assert seen_clients == [(_LAN, 1)]


@pytest.mark.parametrize("proxy", ["127.0.0.1", "::1"])
async def test_local_proxy_on_either_loopback_forwards_its_client(
    routes: str, monkeypatch: pytest.MonkeyPatch, proxy: str
) -> None:
    # uvicorn's own default since 0.53, which the guard replaces in the stack.
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    forwarded = [(b"x-forwarded-for", _INTERNET.encode())]
    _sent, seen = await _call({"routes_file": routes}, (proxy, 1), forwarded)
    assert seen == {"client": (_INTERNET, 0)}


async def test_default_trusted_proxy_comes_from_forwarded_allow_ips(
    routes: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "192.168.1.5")
    forwarded = [(b"x-forwarded-for", _INTERNET.encode())]
    _sent, seen = await _call({"routes_file": routes}, ("192.168.1.5", 1), forwarded)
    assert seen == {"client": (_INTERNET, 0)}
    _sent, seen = await _call({"routes_file": routes}, ("192.168.1.6", 1), forwarded)
    assert seen == {"client": ("192.168.1.6", 1)}


async def test_repeated_refusals_do_not_flood_the_log(
    routes: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=ipv6_lan_guard.__name__)
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise AssertionError("refused peer reached the app")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    guard = IPv6LanGuard(app, routes_file=routes)
    for _ in range(3):
        await guard(
            {"type": "http", "client": (_INTERNET, 1), "headers": []}, None, send
        )
    levels = [r.levelno for r in caplog.records if "Refused IPv6" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]
    assert b"ipv6_allowed_networks" in sent[1]["body"]


async def _save_allowlist(value: str) -> tuple[int, dict[str, Any]]:
    import json

    from starlette.requests import Request

    from ha_mcp.settings_ui._handlers_advanced import build_advanced_handlers

    body = json.dumps({"ipv6_allowed_networks": value}).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body}

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    response = await build_advanced_handlers(None)["save_advanced_settings"](request)
    return response.status_code, json.loads(response.body)


async def test_settings_page_rejects_an_allowlist_typo() -> None:
    status, payload = await _save_allowlist("2001:db8:5::/48x, 10.0.0.0/8")
    assert status == 400
    assert "2001:db8:5::/48x" in str(payload) and "10.0.0.0/8" in str(payload)
    status, _payload = await _save_allowlist("2001:db8:5::/48")
    assert status == 200
