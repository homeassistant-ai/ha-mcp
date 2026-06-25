"""Unit tests for the browser landing page on GET requests."""

import logging

import httpx
import pytest
from fastmcp import FastMCP

from ha_mcp.__main__ import (
    ProbeAccessLogFilter,
    _registered_landing_paths,
    register_browser_landing,
)


@pytest.fixture(autouse=True)
def _clear_landing_registry():
    """Clear the registered-paths set and access-log filters between tests."""
    _registered_landing_paths.clear()
    yield
    _registered_landing_paths.clear()
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.filters = [
        f for f in access_logger.filters if not isinstance(f, ProbeAccessLogFilter)
    ]


@pytest.fixture
def mcp_app():
    """Create a FastMCP app with the browser landing route registered."""
    server = FastMCP("test")
    register_browser_landing(server, "/mcp")
    return server.http_app(path="/mcp", stateless_http=True)


@pytest.mark.asyncio
async def test_get_returns_405_with_helpful_message(mcp_app):
    """GET should return 405 with the landing text and Allow header."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app), base_url="http://test"
    ) as client:
        resp = await client.get("/mcp")

    assert resp.status_code == 405
    assert "HA-MCP server is up and running" in resp.text
    assert "Block AI training bots" in resp.text
    assert '"do not block (allow crawlers)"' in resp.text
    assert "dash.cloudflare.com" in resp.text
    # Reverse-proxy / geo-blocking guidance (issue #1669)
    assert "Your URL is set up correctly" in resp.text
    assert "160.79.104.0/21" in resp.text
    assert resp.headers["allow"] == "POST, DELETE"


@pytest.mark.asyncio
async def test_head_returns_405_with_allow_header(mcp_app):
    """HEAD on the MCP path should return 405 with the correct Allow header."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app), base_url="http://test"
    ) as client:
        resp = await client.head("/mcp")

    assert resp.status_code == 405
    assert resp.headers["allow"] == "POST, DELETE"


@pytest.mark.asyncio
async def test_post_not_intercepted_by_landing(mcp_app):
    """POST on the MCP path must reach the MCP handler, not the landing page."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
            headers={"Content-Type": "application/json"},
        )

    # The MCP handler errors (no lifespan in test), but the key assertion is
    # that POST was NOT intercepted by the landing page route.
    assert resp.status_code != 405  # POST must not be intercepted by the landing route
    assert "HA-MCP server is up and running" not in resp.text


@pytest.mark.asyncio
async def test_custom_path_mounts_at_correct_path():
    """Landing page should mount at the custom path, not the default."""
    server = FastMCP("test")
    register_browser_landing(server, "/secret-abc")
    app = server.http_app(path="/secret-abc", stateless_http=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Custom path should serve the landing page
        resp = await client.get("/secret-abc")
        assert resp.status_code == 405
        assert "HA-MCP server is up and running" in resp.text

        # Default /mcp path should NOT serve the landing page
        resp_default = await client.get("/mcp")
        assert resp_default.status_code != 405
        assert "HA-MCP server is up and running" not in resp_default.text


def _access_record(method: str, path: str, status: int) -> logging.LogRecord:
    """Build a uvicorn.access-style LogRecord (args is a 5-tuple)."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4:5678", method, path, "1.1", status),
        exc_info=None,
    )


def test_filter_drops_favicon_404():
    """Browser favicon auto-requests (404) are dropped from the access log."""
    f = ProbeAccessLogFilter("/mcp")
    assert f.filter(_access_record("GET", "/favicon.ico", 404)) is False
    assert f.filter(_access_record("HEAD", "/favicon.ico", 404)) is False


def test_filter_drops_get_405_on_mcp_path():
    """The by-design GET/HEAD-405 probe on the MCP path is dropped (handler logs it)."""
    f = ProbeAccessLogFilter("/mcp")
    assert f.filter(_access_record("GET", "/mcp", 405)) is False
    assert f.filter(_access_record("HEAD", "/mcp", 405)) is False


def test_filter_drops_405_with_query_and_trailing_slash():
    """Path normalization handles query strings and trailing slashes."""
    f = ProbeAccessLogFilter("/private_abc")
    assert f.filter(_access_record("GET", "/private_abc/?x=1", 405)) is False


def test_filter_keeps_405_when_drop_disabled_for_sse():
    """SSE mode (drop_mcp_405=False) keeps the 405 line — there it's a real fault."""
    f = ProbeAccessLogFilter("/mcp", drop_mcp_405=False)
    assert f.filter(_access_record("GET", "/mcp", 405)) is True


def test_filter_keeps_post_200_tool_call():
    """Real POST tool-call access lines are never dropped."""
    f = ProbeAccessLogFilter("/mcp")
    assert f.filter(_access_record("POST", "/mcp", 200)) is True


def test_filter_keeps_405_on_other_path():
    """A 405 on a different path is a real signal and is kept."""
    f = ProbeAccessLogFilter("/mcp")
    assert f.filter(_access_record("GET", "/other", 405)) is True


def test_filter_keeps_404_on_other_path():
    """A 404 on a non-favicon path is kept (only favicon noise is dropped)."""
    f = ProbeAccessLogFilter("/mcp")
    assert f.filter(_access_record("GET", "/something", 404)) is True


def test_filter_keeps_non_access_records():
    """Records without the uvicorn.access 5-tuple args shape pass through."""
    f = ProbeAccessLogFilter("/mcp")
    rec = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1, "boom", None, None
    )
    assert f.filter(rec) is True


def test_register_attaches_filter():
    """register_browser_landing wires the probe filter onto uvicorn.access."""
    server = FastMCP("test")
    register_browser_landing(server, "/mcp")
    access_logger = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, ProbeAccessLogFilter) for f in access_logger.filters)


@pytest.mark.asyncio
async def test_get_405_logs_annotated_note(mcp_app, caplog):
    """A GET probe returns 405 and logs the annotated 'NORMAL' note line."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app), base_url="http://test"
    ) as client:
        with caplog.at_level(logging.INFO):
            resp = await client.get("/mcp")

    assert resp.status_code == 405
    assert any(
        "NORMAL for most non-SSE connections" in r.getMessage() for r in caplog.records
    )


def test_filter_drops_favicon_404_even_in_sse_mode():
    """Favicon 404s are dropped regardless of drop_mcp_405 (SSE mode included)."""
    f = ProbeAccessLogFilter("/mcp", drop_mcp_405=False)
    assert f.filter(_access_record("GET", "/favicon.ico", 404)) is False


def test_filter_drops_favicon_with_query_and_slash():
    """Favicon drop survives query strings and trailing slashes."""
    f = ProbeAccessLogFilter("/mcp")
    assert f.filter(_access_record("GET", "/favicon.ico?v=2", 404)) is False
    assert f.filter(_access_record("HEAD", "/favicon.ico/", 404)) is False


def test_filter_drops_405_on_root_mcp_path():
    """A root MCP path '/' normalizes correctly and its GET-405 probe is dropped."""
    f = ProbeAccessLogFilter("/")
    assert f.filter(_access_record("GET", "/", 405)) is False


def test_filter_keeps_record_with_non_int_status():
    """A record whose status isn't an int (unexpected format) fails open (kept)."""
    f = ProbeAccessLogFilter("/mcp")
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %s',
        ("1.2.3.4:5678", "GET", "/mcp", "1.1", "405"),
        None,
    )
    assert f.filter(rec) is True


def test_register_does_not_double_attach_same_path():
    """Registering the same path twice attaches only one filter (dedup guard)."""
    server = FastMCP("test")
    register_browser_landing(server, "/mcp")
    register_browser_landing(server, "/mcp")
    access_logger = logging.getLogger("uvicorn.access")
    attached = [f for f in access_logger.filters if isinstance(f, ProbeAccessLogFilter)]
    assert len(attached) == 1


def test_register_quiet_probe_log_false_keeps_mcp_405():
    """quiet_probe_log=False (SSE) wires drop_mcp_405=False, so the attached filter
    keeps a /mcp GET-405 instead of dropping it."""
    server = FastMCP("test")
    register_browser_landing(server, "/mcp", quiet_probe_log=False)
    access_logger = logging.getLogger("uvicorn.access")
    attached = [f for f in access_logger.filters if isinstance(f, ProbeAccessLogFilter)]
    assert len(attached) == 1
    assert attached[0].filter(_access_record("GET", "/mcp", 405)) is True


@pytest.mark.asyncio
async def test_head_405_logs_annotated_note(mcp_app, caplog):
    """HEAD also reaches the landing handler (Starlette auto-routes it) and logs
    the annotated note."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app), base_url="http://test"
    ) as client:
        with caplog.at_level(logging.INFO):
            resp = await client.head("/mcp")

    assert resp.status_code == 405
    assert any(
        "NORMAL for most non-SSE connections" in r.getMessage() for r in caplog.records
    )
