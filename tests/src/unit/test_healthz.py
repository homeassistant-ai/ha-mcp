"""Unit tests for the opt-in GET /healthz liveness route."""

import httpx
import pytest
from fastmcp import FastMCP

from ha_mcp import browser_landing
from ha_mcp.__main__ import ProbeAccessLogFilter, _healthz_enabled
from ha_mcp.browser_landing import register_healthz


@pytest.mark.asyncio
async def test_healthz_returns_200_json():
    """GET /healthz answers 200 with the liveness JSON body."""
    server = FastMCP("test")
    assert register_healthz(server) is True
    app = server.http_app(path="/mcp", stateless_http=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "server": "ha-mcp"}


@pytest.mark.asyncio
async def test_healthz_does_not_leak_mcp_path():
    """The response body must not echo the (possibly secret) MCP path."""
    server = FastMCP("test")
    register_healthz(server)
    app = server.http_app(path="/private_abc", stateless_http=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")

    assert "private_abc" not in resp.text


def test_register_healthz_idempotent_per_instance():
    """Second registration on the same instance no-ops; a new instance
    (in-process config-entry reload) registers again."""
    first = FastMCP("test")
    assert register_healthz(first) is True
    assert register_healthz(first) is False

    reloaded = FastMCP("test")
    assert register_healthz(reloaded) is True


@pytest.mark.asyncio
async def test_healthz_not_registered_by_default():
    """Without register_healthz, /healthz stays a 404 (opt-in only)."""
    server = FastMCP("test")
    browser_landing.register_browser_landing(server, "/mcp")
    app = server.http_app(path="/mcp", stateless_http=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 404


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
    ],
)
def test_healthz_enabled_env_values(monkeypatch, value, expected):
    """MCP_HEALTHZ accepts the usual truthy spellings and defaults off."""
    if value:
        monkeypatch.setenv("MCP_HEALTHZ", value)
    else:
        monkeypatch.delenv("MCP_HEALTHZ", raising=False)
    assert _healthz_enabled() is expected


def _access_record(method: str, path: str, status: int):
    import logging

    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4:5678", method, path, "1.1", status),
        exc_info=None,
    )


def test_filter_drops_healthz_200():
    """Liveness-probe 200s on /healthz are dropped from the access log."""
    f = ProbeAccessLogFilter("/mcp")
    assert f.filter(_access_record("GET", "/healthz", 200)) is False


def test_filter_keeps_healthz_404():
    """A 404 on /healthz (route not enabled) is a real signal and is kept."""
    f = ProbeAccessLogFilter("/mcp")
    assert f.filter(_access_record("GET", "/healthz", 404)) is True
