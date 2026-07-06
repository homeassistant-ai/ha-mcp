"""Tests for ``ha_mcp.transport_security`` (fastmcp Host/Origin guard default).

fastmcp >= 3.4.3 ships an on-by-default DNS-rebinding guard that rejects
non-loopback ``Host`` headers (421) and cross-origin ``Origin`` headers (403)
before routing. ha-mcp is reached through operator-chosen proxies/tunnels and
LAN IPs on arbitrary hosts, so it defaults that guard off. These tests pin the
helper's contract and -- on fastmcp versions that actually have the guard --
prove it is the disable that lets a cross-origin discovery preflight through.
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

# The guard (and its backing setting) exist only on fastmcp >= 3.4.3. Tests that
# assert on the setting / real middleware behaviour are skipped on older fastmcp,
# where the helper is a documented no-op.
_HAS_GUARD = hasattr(fastmcp.settings, "http_host_origin_protection")
_requires_guard = pytest.mark.skipif(
    not _HAS_GUARD,
    reason="fastmcp < 3.4.3 has no Host/Origin (DNS-rebinding) guard to disable",
)

_CROSS_ORIGIN_PREFLIGHT = {
    "Origin": "https://claude.ai",
    "Access-Control-Request-Method": "GET",
}


@pytest.fixture(autouse=True)
def _restore_global_guard_state():
    """Undo the helper's direct process-global mutations after each test.

    ``ensure_host_origin_guard_default_off`` writes ``os.environ`` and the
    fastmcp settings singleton directly (by design), so restore both here to
    keep the module hermetic and avoid leaking into other test modules.
    """
    original_env = os.environ.get(HOST_ORIGIN_PROTECTION_ENV)
    original_setting = (
        fastmcp.settings.http_host_origin_protection if _HAS_GUARD else None
    )
    yield
    if original_env is None:
        os.environ.pop(HOST_ORIGIN_PROTECTION_ENV, None)
    else:
        os.environ[HOST_ORIGIN_PROTECTION_ENV] = original_env
    if _HAS_GUARD:
        fastmcp.settings.http_host_origin_protection = original_setting


def test_defaults_env_off_when_unset(monkeypatch):
    """With the env var unset, the helper defaults it to 'false'."""
    monkeypatch.delenv(HOST_ORIGIN_PROTECTION_ENV, raising=False)
    ensure_host_origin_guard_default_off()
    assert os.environ[HOST_ORIGIN_PROTECTION_ENV] == "false"


def test_respects_explicit_env_value(monkeypatch):
    """An explicit operator value is never overwritten (either direction)."""
    monkeypatch.setenv(HOST_ORIGIN_PROTECTION_ENV, "true")
    ensure_host_origin_guard_default_off()
    assert os.environ[HOST_ORIGIN_PROTECTION_ENV] == "true"


@_requires_guard
def test_sets_setting_off_when_env_unset(monkeypatch):
    """The load-bearing effect: the fastmcp settings singleton flips to False."""
    monkeypatch.delenv(HOST_ORIGIN_PROTECTION_ENV, raising=False)
    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", True)
    ensure_host_origin_guard_default_off()
    assert fastmcp.settings.http_host_origin_protection is False


@_requires_guard
def test_explicit_opt_in_is_preserved(monkeypatch):
    """An operator who re-enabled the guard keeps it on."""
    monkeypatch.setenv(HOST_ORIGIN_PROTECTION_ENV, "true")
    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", True)
    ensure_host_origin_guard_default_off()
    assert fastmcp.settings.http_host_origin_protection is True


@_requires_guard
@pytest.mark.asyncio
async def test_guard_blocks_cross_origin_preflight_when_forced_on(monkeypatch):
    """Non-vacuous guard: forced ON, a cross-origin preflight is 403ed.

    Proves the guard is real and active, so the ``allows`` test below (and the
    metadata preflight tests) are not passing vacuously.
    """
    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", True)
    app = FastMCP("t").http_app(path="/mcp", stateless_http=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.options("/mcp", headers=_CROSS_ORIGIN_PREFLIGHT)
    assert resp.status_code == 403


@_requires_guard
@pytest.mark.asyncio
async def test_helper_allows_cross_origin_preflight(monkeypatch):
    """After the helper runs, the same preflight is no longer guard-blocked."""
    monkeypatch.delenv(HOST_ORIGIN_PROTECTION_ENV, raising=False)
    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", True)
    ensure_host_origin_guard_default_off()
    app = FastMCP("t").http_app(path="/mcp", stateless_http=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.options("/mcp", headers=_CROSS_ORIGIN_PREFLIGHT)
    # 403 == Origin blocked, 421 == Host blocked; neither may come from the guard.
    assert resp.status_code not in (403, 421)
