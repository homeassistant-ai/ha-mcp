"""Tests for ``ha_mcp.transport_security`` (fastmcp Host/Origin guard default).

fastmcp >= 3.4.3 ships an on-by-default DNS-rebinding guard that rejects
non-loopback ``Host`` headers (421) and cross-origin ``Origin`` headers (403)
before routing. ha-mcp is reached through operator-chosen proxies/tunnels and
LAN IPs on arbitrary hosts, so it defaults that guard off. These tests pin the
helper's contract and -- on fastmcp versions that actually have the guard --
prove both that the guard is real (Host and Origin paths) and that the default
neutralises it, plus that ``_create_server`` wires the call in.

Global-state cleanup (env var + settings singleton) is handled by the autouse
``_restore_fastmcp_host_origin_guard`` fixture in ``conftest.py``.
"""

import os

import fastmcp
import httpx
import pytest
from fastmcp import FastMCP

from ha_mcp.transport_security import (
    HOST_ORIGIN_PROTECTION_ENV,
    ensure_host_origin_guard_default_off,
)

_ATTR = "http_host_origin_protection"
_HAS_GUARD = hasattr(fastmcp.settings, _ATTR)
_requires_guard = pytest.mark.skipif(
    not _HAS_GUARD,
    reason="fastmcp < 3.4.3 has no Host/Origin (DNS-rebinding) guard to disable",
)

_CROSS_ORIGIN_PREFLIGHT = {
    "Origin": "https://claude.ai",
    "Access-Control-Request-Method": "GET",
}
_NON_LOOPBACK_HOST = {"Host": "mcp.example.com"}


# --- helper contract ---------------------------------------------------------


def test_noop_never_raises():
    """On any fastmcp version, calling the helper is safe (no exception)."""
    ensure_host_origin_guard_default_off()


def test_respects_explicit_env_value(monkeypatch):
    """An explicit operator value is never overwritten (either direction)."""
    monkeypatch.setenv(HOST_ORIGIN_PROTECTION_ENV, "true")
    ensure_host_origin_guard_default_off()
    assert os.environ[HOST_ORIGIN_PROTECTION_ENV] == "true"


@_requires_guard
def test_sets_setting_off_and_env_when_unset(monkeypatch):
    """The load-bearing effect: the settings singleton flips off + env is set."""
    monkeypatch.delenv(HOST_ORIGIN_PROTECTION_ENV, raising=False)
    monkeypatch.setattr(fastmcp.settings, _ATTR, True)
    ensure_host_origin_guard_default_off()
    assert getattr(fastmcp.settings, _ATTR) is False
    assert os.environ[HOST_ORIGIN_PROTECTION_ENV] == "false"


@_requires_guard
def test_explicit_opt_in_is_preserved(monkeypatch):
    """An operator who re-enabled the guard (env=true) keeps it on."""
    monkeypatch.setenv(HOST_ORIGIN_PROTECTION_ENV, "true")
    monkeypatch.setattr(fastmcp.settings, _ATTR, True)
    ensure_host_origin_guard_default_off()
    assert getattr(fastmcp.settings, _ATTR) is True


@_requires_guard
def test_idempotent_when_already_off(monkeypatch):
    """Already-off is a no-op and does not write the env var (retry-safe path)."""
    monkeypatch.delenv(HOST_ORIGIN_PROTECTION_ENV, raising=False)
    monkeypatch.setattr(fastmcp.settings, _ATTR, False)
    ensure_host_origin_guard_default_off()
    assert getattr(fastmcp.settings, _ATTR) is False
    assert HOST_ORIGIN_PROTECTION_ENV not in os.environ


# --- end-to-end against the real middleware (guard-bearing fastmcp only) ------


def _build_app():
    return FastMCP("t").http_app(path="/mcp", stateless_http=True)


@_requires_guard
@pytest.mark.asyncio
async def test_guard_blocks_cross_origin_preflight_when_forced_on(monkeypatch):
    """Non-vacuous: forced ON, a cross-origin Origin preflight is 403ed."""
    monkeypatch.setattr(fastmcp.settings, _ATTR, True)
    app = _build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.options("/mcp", headers=_CROSS_ORIGIN_PREFLIGHT)
    assert resp.status_code == 403


@_requires_guard
@pytest.mark.asyncio
async def test_guard_blocks_non_loopback_host_when_forced_on(monkeypatch):
    """Non-vacuous: forced ON, a non-loopback Host is 421ed (landing-page case)."""
    monkeypatch.setattr(fastmcp.settings, _ATTR, True)
    app = _build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/mcp", headers=_NON_LOOPBACK_HOST)
    assert resp.status_code == 421


@_requires_guard
@pytest.mark.asyncio
async def test_helper_allows_cross_origin_and_non_loopback(monkeypatch):
    """After the helper runs, neither the Origin (403) nor Host (421) guard fires."""
    monkeypatch.delenv(HOST_ORIGIN_PROTECTION_ENV, raising=False)
    monkeypatch.setattr(fastmcp.settings, _ATTR, True)
    ensure_host_origin_guard_default_off()
    app = _build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        preflight = await client.options("/mcp", headers=_CROSS_ORIGIN_PREFLIGHT)
        host_req = await client.get("/mcp", headers=_NON_LOOPBACK_HOST)
    assert preflight.status_code not in (403, 421)
    assert host_req.status_code != 421


# --- wiring: the deferred-mcp chokepoint calls the helper --------------------


def test_create_server_disables_guard(monkeypatch):
    """_create_server -- the chokepoint for ha-mcp-web/sse, the add-on, and the
    ``fastmcp run fastmcp-http.json`` container path -- calls the guard-disable
    before building the server."""
    import ha_mcp.__main__ as main_module

    calls: list[int] = []
    monkeypatch.setattr(
        "ha_mcp.transport_security.ensure_host_origin_guard_default_off",
        lambda: calls.append(1),
    )
    monkeypatch.setattr(
        "ha_mcp.server.HomeAssistantSmartMCPServer", lambda *a, **kw: object()
    )
    main_module._create_server()
    assert calls == [1]
