"""Container/Docker-lane E2E for the dashboard screenshot sidecar path.

The real Chromium engine is exercised only on the inaddon lane
(``haos_only/test_dashboard_screenshot_addon.py``). This module covers the
**Docker / Container deployment** instead — the explicit
``HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL`` sidecar path (``resolve_engine_url``
mode 1), which is a different resolution branch from the inaddon lane's
Supervisor auto-discovery and is otherwise untested.

A faithful FAKE engine stands in for ha-puppet, mirroring the real engine's
HTTP contract so the capture client is exercised against the same wire shape it
will hit in production: ``GET /<dashboard-path>?viewport=WxH&zoom=N&wait=ms&
format=png`` returning an ``image/png`` body sized to the requested viewport.
What is covered end-to-end here:

* the explicit-URL resolution branch + the httpx capture client + the request
  contract (asserted: the engine actually received the viewport/format params);
* ``ha_get_dashboard_screenshot`` returning an image;
* ``include_screenshot`` / ``return_screenshot`` on the dashboard config tools
  (the HA-dependent create-and-see path).

Beta gating is the real production gate (``ENABLE_BETA_FEATURES`` master +
``HAMCP_ENABLE_DASHBOARD_SCREENSHOT`` sub-flag); the tool is only registered
when both are on, same as for end users. The engine's *failure* degradation
(feature off / engine unreachable → warning, never breaks the op) is pinned by
the unit tests in ``tests/src/unit/test_dashboard_screenshot.py``.

``container_only``: the test builds an in-process MCP server, which the inaddon
lane does not have (it already covers the real engine), and the external-HAOS
lane would only duplicate this.
"""

from __future__ import annotations

import base64
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastmcp import Client
from test_constants import TEST_TOKEN

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.server import HomeAssistantSmartMCPServer

pytestmark = [pytest.mark.container_only]

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Faithful fake engine — same HTTP contract as ha-puppet's ingress port.
# ---------------------------------------------------------------------------


def _make_png(width: int, height: int) -> bytes:
    """Build a real, minimal PNG of the requested size (8-bit RGB, black).

    Faithful to the engine, which returns a PNG sized to the viewport — so the
    capture client and PNG-header parsing exercise a genuine image, not a stub.
    """

    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        _PNG_MAGIC
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


class _FakeEngine:
    """A running fake screenshot engine; records the requests it served."""

    def __init__(self) -> None:
        recorded: list[dict[str, Any]] = []
        self.requests = recorded

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                recorded.append({"path": parsed.path, "params": params})
                viewport = params.get("viewport", "1280x800")
                try:
                    width, height = (int(x) for x in viewport.split("x"))
                except ValueError:
                    self.send_error(400, "bad viewport")
                    return
                png = _make_png(width, height)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return  # silence test-server request logging

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}"


@pytest.fixture
def fake_engine():
    engine = _FakeEngine()
    engine.start()
    try:
        yield engine
    finally:
        engine.stop()


@pytest.fixture
async def screenshot_mcp_client(
    ha_container_with_fresh_config: Any, fake_engine: _FakeEngine, monkeypatch
):
    """In-process MCP server with the screenshot beta feature enabled.

    Mirrors the production gate: both the master beta flag and the
    dashboard-screenshot sub-flag must be on for the tool to register. Points
    the engine at the fake sidecar via the explicit-URL env var (the Docker
    deployment path), then rebuilds the settings cache so a fresh server picks
    the flags up.
    """
    import ha_mcp.config

    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")
    monkeypatch.setenv("HAMCP_ENABLE_DASHBOARD_SCREENSHOT", "true")
    monkeypatch.setenv("HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", fake_engine.url)
    # Force a settings re-read so the freshly-built server sees the flags.
    ha_mcp.config._settings = None

    container_info = ha_container_with_fresh_config
    client = HomeAssistantClient(
        base_url=container_info["base_url"],
        token=container_info.get("token", TEST_TOKEN),
    )
    server = HomeAssistantSmartMCPServer(client=client)
    mcp_client = Client(server.mcp)
    try:
        async with mcp_client:
            yield mcp_client
    finally:
        await client.close()
        # monkeypatch reverts the env; drop the cached settings so later
        # tests don't observe the screenshot flags.
        ha_mcp.config._settings = None


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------


def _extract_png_bytes(result: Any) -> bytes | None:
    content = getattr(result, "content", None)
    if not content:
        return None
    for block in content:
        data = getattr(block, "data", None)
        if isinstance(data, str):
            try:
                return base64.b64decode(data)
            except (ValueError, TypeError):
                continue
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
    return None


def _png_dimensions(data: bytes) -> tuple[int, int]:
    assert data[:8] == _PNG_MAGIC, f"not a PNG (magic={data[:8]!r})"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _parse_payload(result: Any) -> dict[str, Any]:
    """Pull the structured JSON payload out of an MCP tool result."""
    from ..utilities.assertions import parse_mcp_result

    return parse_mcp_result(result)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_tool_registered_only_with_flags(
    screenshot_mcp_client: Client,
) -> None:
    """The opt-in tool registers when both beta flags are on (production gate)."""
    tools = {t.name for t in await screenshot_mcp_client.list_tools()}
    assert "ha_get_dashboard_screenshot" in tools, (
        "ha_get_dashboard_screenshot should be registered when "
        "ENABLE_BETA_FEATURES + HAMCP_ENABLE_DASHBOARD_SCREENSHOT are set"
    )


async def test_get_dashboard_screenshot_returns_png(
    screenshot_mcp_client: Client, fake_engine: _FakeEngine
) -> None:
    """ha_get_dashboard_screenshot resolves the explicit engine URL, calls the
    sidecar with the documented request contract, and returns a sized PNG."""
    result = await screenshot_mcp_client.call_tool(
        "ha_get_dashboard_screenshot",
        {"dashboard_path": "lovelace/0", "width": 1024, "height": 768},
    )
    png = _extract_png_bytes(result)
    assert png is not None, (
        f"no image content returned: {getattr(result, 'content', result)!r}"
    )
    assert _png_dimensions(png) == (1024, 768)

    # Faithfulness: the engine actually received the request the real engine
    # expects (path + viewport + format crossed the wire).
    assert fake_engine.requests, "fake engine received no request"
    last = fake_engine.requests[-1]
    assert last["path"].endswith("lovelace/0"), last["path"]
    assert last["params"].get("viewport") == "1024x768", last["params"]
    assert last["params"].get("format") == "png", last["params"]


async def test_full_page_requests_tall_viewport(
    screenshot_mcp_client: Client, fake_engine: _FakeEngine
) -> None:
    """full_page=True asks the engine for a tall viewport so below-the-fold
    content is captured. The fake engine sizes the PNG to the viewport, so the
    returned image height equals FULL_PAGE_HEIGHT regardless of the height arg."""
    from ha_mcp.dashboard_screenshot.capture import FULL_PAGE_HEIGHT

    result = await screenshot_mcp_client.call_tool(
        "ha_get_dashboard_screenshot",
        {
            "dashboard_path": "lovelace/0",
            "width": 1024,
            "height": 200,
            "full_page": True,
        },
    )
    png = _extract_png_bytes(result)
    assert png is not None, "full_page render returned no image"
    assert _png_dimensions(png) == (1024, FULL_PAGE_HEIGHT)
    last = fake_engine.requests[-1]
    assert last["params"].get("viewport") == f"1024x{FULL_PAGE_HEIGHT}", last["params"]


async def test_get_dashboard_include_screenshot(
    screenshot_mcp_client: Client,
) -> None:
    """ha_config_get_dashboard(include_screenshot=True) returns config + PNG.

    Creates a storage-mode dashboard first: the auto-generated ``default``
    dashboard has no stored Lovelace config, so retrieving it raises
    "No config found" before screenshots are even reached.
    """
    url_path = "screenshot-sidecar-get-e2e"
    config = {
        "views": [
            {
                "title": "Sidecar Get E2E",
                "cards": [{"type": "markdown", "content": "# Sidecar Get E2E"}],
            }
        ]
    }
    try:
        setup = _parse_payload(
            await screenshot_mcp_client.call_tool(
                "ha_config_set_dashboard",
                {"url_path": url_path, "config": config, "title": "Sidecar Get E2E"},
            )
        )
        assert setup.get("success"), f"dashboard create failed: {setup}"

        result = await screenshot_mcp_client.call_tool(
            "ha_config_get_dashboard",
            {"url_path": url_path, "include_screenshot": True},
        )
        payload = _parse_payload(result)
        assert payload.get("success"), f"get_dashboard failed: {payload}"
        png = _extract_png_bytes(result)
        assert png is not None, (
            f"include_screenshot=True returned no image (warnings: {payload.get('warnings')})"
        )
        _png_dimensions(png)
    finally:
        await screenshot_mcp_client.call_tool(
            "ha_config_delete_dashboard", {"url_path": url_path}
        )


async def test_set_dashboard_return_screenshot(
    screenshot_mcp_client: Client,
) -> None:
    """ha_config_set_dashboard(return_screenshot=True) returns result + PNG."""
    url_path = "screenshot-sidecar-e2e"
    config = {
        "views": [
            {
                "title": "Sidecar E2E",
                "cards": [{"type": "markdown", "content": "# Sidecar E2E"}],
            }
        ]
    }
    try:
        result = await screenshot_mcp_client.call_tool(
            "ha_config_set_dashboard",
            {
                "url_path": url_path,
                "config": config,
                "title": "Sidecar E2E",
                "return_screenshot": True,
            },
        )
        payload = _parse_payload(result)
        assert payload.get("success"), f"set_dashboard failed: {payload}"
        png = _extract_png_bytes(result)
        assert png is not None, (
            f"return_screenshot=True returned no image (warnings: {payload.get('warnings')})"
        )
        _png_dimensions(png)
    finally:
        await screenshot_mcp_client.call_tool(
            "ha_config_delete_dashboard", {"url_path": url_path}
        )
