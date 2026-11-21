"""The Settings page's backup routes and restore harness for the backup tests.

The routes run on the real server for the test's client, so they reach the
backup manager production reaches: ``get_backup_manager(client, settings)``
with the configured settings (``_backup_core.backup_settings``).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.settings_ui import build_settings_handlers

from ._js_harness import _assert_clean_init, run_script
from ._real_server import build_server
from ._settings_ui_dom import DEFAULT_FETCHES, MIN_DOM

NAME = "helper_template.sensor.example.20260909_000000.yaml"


def settings_handlers(
    monkeypatch: pytest.MonkeyPatch, client: HomeAssistantClient
) -> dict[str, Any]:
    """The Settings page's handlers, on the real server for ``client``."""
    server = build_server(monkeypatch, client, reread_settings=False)
    return build_settings_handlers(server)


async def response_for(
    monkeypatch: pytest.MonkeyPatch, client: HomeAssistantClient, name: str = NAME
) -> httpx.Response:
    """The Settings page's answer to restoring ``name``, over HTTP on the test's
    loop, where ``client`` talks to Core."""
    handler = settings_handlers(monkeypatch, client)["restore_backup"]
    app = Starlette(
        routes=[Route("/backups/{name}/restore", handler, methods=["POST"])]
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as http:
        return await http.post(f"/backups/{name}/restore")


def run_restore(settings_script, reply, name=NAME):
    result = run_script(
        settings_script,
        initial_html=MIN_DOM,
        settle_ms=300,
        fetch_map={
            **DEFAULT_FETCHES,
            "/restore": reply,
            "/backups?": {"status": 200, "json": {"success": True, "backups": []}},
        },
        invoke=(
            "await new Promise(resolve => setTimeout(resolve, 100));\n"
            f"await window.backupAction('restore', {json.dumps(name)});"
        ),
    )
    _assert_clean_init(result)
    return result
