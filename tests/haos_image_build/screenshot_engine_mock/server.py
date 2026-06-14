"""Test-only mock of the dashboard screenshot engine for the HAOS e2e bake.

Serves the same HTTP contract ha-mcp's ``ha_get_dashboard_screenshot`` calls
against balloob's Puppet add-on, but WITHOUT Chromium:

* ``GET /<dashboard-path>?viewport=WxH&...`` -> a synthetic, viewport-sized
  ``image/png`` (the only params ha-mcp sends are honoured: ``viewport`` for
  the dimensions, ``format=png``; the rest of balloob's surface -- device /
  eink / colors / dithering / bmp / rotate / ... -- is deliberately NOT
  reimplemented, since the tool never sends it and a copy would only add a
  maintenance fork that recovers no coverage: real rendering is Chromium,
  which a mock can't reproduce regardless).
* a bad ``viewport`` -> ``400`` (the engine's contract).
* rendering is gated on the configured ``access_token``: the mock validates it
  against Home Assistant (``GET /api/`` with the bearer). A valid token renders;
  an invalid/unreachable one returns ``502`` so the caller raises -- exactly the
  valid-vs-invalid contrast ``test_token_is_what_authenticates`` asserts (the
  real engine, given a bad token, reaches only the HA login screen and cannot
  reproduce the working render).

Stdlib only; runs on python:3.13-slim. Never shipped to users.
"""

from __future__ import annotations

import json
import struct
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 10000
# Supervisor writes the resolved add-on options here; read per-request so a
# token change (test_token_is_what_authenticates rewrites it) is picked up
# without depending on restart timing.
_OPTIONS_PATH = "/data/options.json"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_DEFAULT_HA_URL = "http://homeassistant:8123"


def _options() -> dict:
    try:
        with open(_OPTIONS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _make_png(width: int, height: int) -> bytes:
    """A real, viewport-sized 8-bit RGB PNG with per-row varying colour.

    Non-uniform on purpose: a solid-colour image zlib-compresses to well under
    the auth test's >3000-byte "not a blank/error frame" floor, so each row
    gets a distinct colour, which keeps the encoded image comfortably above it
    while staying cheap to build.
    """

    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(
        b"\x00" + bytes((y & 0xFF, (y * 2) & 0xFF, (y * 3) & 0xFF)) * width
        for y in range(height)
    )
    return (
        _PNG_MAGIC
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _token_authenticates(base_url: str, token: str) -> bool:
    """True iff ``token`` is a credential HA Core accepts (``GET /api/`` -> 200).

    Mirrors the real engine's behaviour: the configured token is what gets the
    headless browser past the HA frontend login. An empty/invalid token, or an
    unreachable HA, means "cannot authenticate" -> the mock refuses to render.
    """
    if not token:
        return False
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False  # 401 on a bad token, etc.
    except (urllib.error.URLError, OSError):
        return False  # HA not reachable -> never render under a broken auth state


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # BaseHTTPRequestHandler API (uppercase method)
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.send_response(404)
            self.end_headers()
            return
        if parsed.path == "/":
            body = b"<html><body>HA-MCP screenshot engine (test mock)</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            width, height = (int(x) for x in params.get("viewport", "").split("x"))
        except ValueError:
            self.send_error(400, "bad or missing viewport")
            return

        opts = _options()
        ha_url = opts.get("home_assistant_url") or _DEFAULT_HA_URL
        if not _token_authenticates(ha_url, opts.get("access_token", "")):
            self.send_error(
                502, "screenshot engine could not authenticate to Home Assistant"
            )
            return

        png = _make_png(width, height)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.end_headers()
        self.wfile.write(png)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # silence per-request stderr logging


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    print(f"HA-MCP screenshot engine (test mock) listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
