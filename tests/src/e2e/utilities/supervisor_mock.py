"""Mock Supervisor REST sidecar for E2E tests.

Stands in for ``http://supervisor`` so the three direct-Supervisor httpx call
sites — ``rest_client._supervisor_logs_get``, ``tools_bug_report._fetch_addon_logs``,
``settings_ui._restart_addon`` — can be exercised end-to-end. Production runs
against a real Supervisor; this mock makes the contract testable in CI without
needing HAOS / Supervised infrastructure.

The mock binds aiohttp to ``127.0.0.1:0`` and the fixture sets two env vars
the production code already keys off of:

- ``SUPERVISOR_TOKEN`` — flips ``is_running_in_addon()`` on
- ``SUPERVISOR_BASE_URL`` — points the three call sites at the mock

Endpoints implemented (only what the code actually calls):

- ``GET /{service}/logs`` for service ∈ {supervisor, host, core, dns, audio,
  multicast, observer} — the seven Supervisor-managed system services
- ``GET /addons/{slug}/logs`` and ``GET /addons/self/logs`` — addon container logs
- ``POST /addons/self/restart`` — addon self-restart (Supervisor envelope reply)

All endpoints require ``Authorization: Bearer <SUPERVISOR_TOKEN>`` and 401 on
mismatch, matching real Supervisor behavior.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator

import pytest
from aiohttp import web

logger = logging.getLogger(__name__)

MOCK_SUPERVISOR_TOKEN = "test-supervisor-token"

# The seven Supervisor-managed system services exposed at /<service>/logs.
# Mirrors SYSTEM_SERVICE_SLUGS in src/ha_mcp/tools/tools_utility.py.
SYSTEM_SERVICES = frozenset(
    {"supervisor", "host", "core", "dns", "audio", "multicast", "observer"}
)


def _check_auth(request: web.Request) -> web.Response | None:
    """Return a 401 response if the bearer token is missing or wrong."""
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {MOCK_SUPERVISOR_TOKEN}":
        return web.json_response(
            {"result": "error", "message": "Invalid Supervisor token"},
            status=401,
        )
    return None


async def _service_logs(request: web.Request) -> web.Response:
    service = request.match_info["service"]
    if service not in SYSTEM_SERVICES:
        return web.json_response(
            {"result": "error", "message": f"Unknown service: {service}"},
            status=404,
        )
    if (denied := _check_auth(request)) is not None:
        return denied
    body = (
        f"[{service}] mock log line 1\n"
        f"[{service}] mock log line 2\n"
        f"[{service}] mock log line 3\n"
    )
    return web.Response(text=body, content_type="text/plain")


async def _addon_logs(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    if (denied := _check_auth(request)) is not None:
        return denied
    body = f"[addon:{slug}] mock log line 1\n[addon:{slug}] mock log line 2\n"
    return web.Response(text=body, content_type="text/plain")


async def _addon_self_restart(request: web.Request) -> web.Response:
    if (denied := _check_auth(request)) is not None:
        return denied
    # Real Supervisor returns this envelope on success; the call site discards
    # the body but checks the status code.
    return web.json_response({"result": "ok", "data": {}})


def _build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/{service}/logs", _service_logs)
    app.router.add_get("/addons/{slug}/logs", _addon_logs)
    app.router.add_post("/addons/self/restart", _addon_self_restart)
    return app


@pytest.fixture(scope="session")
async def supervisor_mock() -> AsyncGenerator[str]:
    """Run the mock Supervisor on localhost and patch env vars to point at it.

    Yields the base URL (``http://127.0.0.1:<port>``). Tests that depend on
    this fixture have ``SUPERVISOR_TOKEN`` and ``SUPERVISOR_BASE_URL`` set
    for their entire session; tests that don't depend on it are unaffected.
    """
    runner = web.AppRunner(_build_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    host, port = runner.addresses[0][:2]
    base_url = f"http://{host}:{port}"

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
        await runner.cleanup()
        logger.info("🪞 Supervisor mock stopped")
