"""Mock Supervisor REST sidecar for E2E tests.

Stands in for ``http://supervisor`` so the three direct-Supervisor httpx call
sites — ``rest_client._supervisor_logs_get``, ``tools_bug_report._fetch_addon_logs``,
``settings_ui._restart_addon`` — can be exercised end-to-end. Production runs
against a real Supervisor; this mock makes the contract testable in CI without
needing HAOS / Supervised infrastructure.

Implementation: stdlib ``http.server.ThreadingHTTPServer`` on a daemon thread,
bound to ``127.0.0.1:0``. The fixture sets two env vars the production code
already keys off of:

- ``SUPERVISOR_TOKEN`` — flips ``is_running_in_addon()`` on
- ``SUPERVISOR_BASE_URL`` — points the three call sites at the mock

Stdlib instead of aiohttp/starlette so no new dev dep is needed for what is
ultimately a tiny canned-response server. Runs in a thread so it doesn't share
the test event loop and can't deadlock against in-process MCP server work.

Endpoints implemented (only what the code actually calls):

- ``GET /{service}/logs`` for service ∈ {supervisor, host, core, dns, audio,
  multicast, observer} — the seven Supervisor-managed system services
- ``GET /addons/{slug}/logs`` and ``GET /addons/self/logs`` — addon container logs
- ``POST /addons/self/restart`` — addon self-restart (Supervisor envelope reply)

All endpoints require ``Authorization: Bearer <SUPERVISOR_TOKEN>`` and 401 on
mismatch, matching real Supervisor behavior.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

logger = logging.getLogger(__name__)

MOCK_SUPERVISOR_TOKEN = "test-supervisor-token"

# The seven Supervisor-managed system services exposed at /<service>/logs.
# Mirrors SYSTEM_SERVICE_SLUGS in src/ha_mcp/tools/tools_utility.py.
SYSTEM_SERVICES = frozenset(
    {"supervisor", "host", "core", "dns", "audio", "multicast", "observer"}
)

_SERVICE_LOGS_RE = re.compile(r"^/([a-z]+)/logs$")
_ADDON_LOGS_RE = re.compile(r"^/addons/([^/]+)/logs$")


class _SupervisorMockHandler(BaseHTTPRequestHandler):
    """Routes the small set of Supervisor REST endpoints the codebase calls."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence the default per-request stderr line; pytest captures it as noise.
        return

    def _check_auth(self) -> bool:
        if self.headers.get("Authorization", "") == f"Bearer {MOCK_SUPERVISOR_TOKEN}":
            return True
        self._send_json(401, {"result": "error", "message": "Invalid Supervisor token"})
        return False

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, body: str) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if m := _SERVICE_LOGS_RE.match(self.path):
            service = m.group(1)
            if service not in SYSTEM_SERVICES:
                self._send_json(
                    404, {"result": "error", "message": f"Unknown service: {service}"}
                )
                return
            if not self._check_auth():
                return
            self._send_text(
                200,
                f"[{service}] mock log line 1\n"
                f"[{service}] mock log line 2\n"
                f"[{service}] mock log line 3\n",
            )
            return

        if m := _ADDON_LOGS_RE.match(self.path):
            slug = m.group(1)
            if not self._check_auth():
                return
            self._send_text(
                200,
                f"[addon:{slug}] mock log line 1\n[addon:{slug}] mock log line 2\n",
            )
            return

        self._send_json(
            404, {"result": "error", "message": f"Unknown path: {self.path}"}
        )

    def do_POST(self) -> None:
        if self.path == "/addons/self/restart":
            if not self._check_auth():
                return
            # Real Supervisor returns this envelope on success; the call site
            # discards the body but checks the status code.
            self._send_json(200, {"result": "ok", "data": {}})
            return

        self._send_json(
            404, {"result": "error", "message": f"Unknown path: {self.path}"}
        )


@pytest.fixture(scope="session")
def supervisor_mock() -> Iterator[str]:
    """Run the mock Supervisor on localhost and patch env vars to point at it.

    Yields the base URL (``http://127.0.0.1:<port>``). Tests that depend on
    this fixture have ``SUPERVISOR_TOKEN`` and ``SUPERVISOR_BASE_URL`` set
    for their entire session; tests that don't depend on it are unaffected.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SupervisorMockHandler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"

    thread = threading.Thread(
        target=server.serve_forever, name="supervisor-mock", daemon=True
    )
    thread.start()

    prior_token = os.environ.get("SUPERVISOR_TOKEN")
    prior_url = os.environ.get("SUPERVISOR_BASE_URL")
    os.environ["SUPERVISOR_TOKEN"] = MOCK_SUPERVISOR_TOKEN
    os.environ["SUPERVISOR_BASE_URL"] = base_url

    logger.info("🪞 Supervisor mock listening on %s", base_url)
    try:
        yield base_url
    finally:
        if prior_token is None:
            os.environ.pop("SUPERVISOR_TOKEN", None)
        else:
            os.environ["SUPERVISOR_TOKEN"] = prior_token
        if prior_url is None:
            os.environ.pop("SUPERVISOR_BASE_URL", None)
        else:
            os.environ["SUPERVISOR_BASE_URL"] = prior_url
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        logger.info("🪞 Supervisor mock stopped")
