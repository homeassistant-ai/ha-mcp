"""Backup settings, a dispatcher and the restore harness shared by the backup restore tests."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ha_mcp.config import Settings
from ha_mcp.settings_ui import build_settings_handlers

from ._astro_setup_wizard import _assert_clean_init
from ._js_harness import run_script
from ._settings_ui_dom import DEFAULT_FETCHES, MIN_DOM


def _backup_settings() -> Settings:
    """Keep tool access independent of the developer's env and dotenv files."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        ENABLE_SNAPSHOT_ACTIONS=True,
        BACKUP_READ_ONLY=False,
        READ_ONLY_MODE=False,
    )


def _dispatcher() -> Any:
    """Resolve the raw ``ha_manage_backup`` closure from the ``@tool`` stack.

    Mirrors the addon-tool tests: a fake ``mcp`` captures the decorated
    function, then ``__wrapped__`` is unwound past ``@log_tool_usage`` so the
    dispatcher is called directly, bypassing the FastMCP transport.
    """
    from ha_mcp.tools.backup import register_backup_tools

    captured: dict[str, Any] = {}

    class _MockMCP:
        def tool(self, *args: Any, **kwargs: Any) -> Any:
            def deco(fn: Any) -> Any:
                captured.setdefault(fn.__name__, fn)
                return fn

            return deco

    register_backup_tools(_MockMCP(), MagicMock())
    fn = captured["ha_manage_backup"]
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def backup_tool(monkeypatch, manager) -> Any:
    """``ha_manage_backup`` working on ``manager``, under ``_backup_settings``."""
    monkeypatch.setattr("ha_mcp.tools.backup.get_backup_manager", lambda *args: manager)
    monkeypatch.setattr("ha_mcp.tools.backup.get_global_settings", _backup_settings)
    monkeypatch.setattr("ha_mcp.read_only.get_global_settings", _backup_settings)
    return _dispatcher()


NAME = "helper_template.sensor.example.20260909_000000.yaml"


def use_backup_manager(monkeypatch, manager) -> None:
    """Hand the settings backup routes ``manager``: their server is not built."""
    monkeypatch.setattr(
        "ha_mcp.settings_ui._handlers_backups._backup_mgr", lambda server: manager
    )


def response_for(monkeypatch, manager, name=NAME):
    use_backup_manager(monkeypatch, manager)
    handler = build_settings_handlers(None)["restore_backup"]
    app = Starlette(
        routes=[Route("/backups/{name}/restore", handler, methods=["POST"])]
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post(f"/backups/{name}/restore")


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
