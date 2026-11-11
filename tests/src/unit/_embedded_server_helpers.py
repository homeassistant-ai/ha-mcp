"""Helpers shared by the embedded-server tests: a stubbed ha_mcp surface, worker environment and loopback ports."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import custom_components.ha_mcp_tools.embedded_server as es


@pytest.fixture(autouse=True)
def _no_manifest_pin(monkeypatch):
    """``async_start`` reads the manifest pin through the HA loader (a stub
    here); default to a source checkout's "no pin". Tests that need a pin
    patch it via ``_paired``; ``TestPairedServerRequirement`` calls the real
    reader."""
    monkeypatch.setattr(
        es, "_async_paired_server_requirement", AsyncMock(return_value=None)
    )


def _stub_ha_mcp_surface(
    monkeypatch, *, mcp, landing_mod=None, log_filters_mod=None
) -> None:
    """Install a minimal in-memory ``ha_mcp`` package so ``_serve`` runs hermetically.

    Wires a non-sentinel connection (so ``_serve`` passes its refuse-to-serve
    guard), a server whose ``.mcp`` is ``mcp``, no-op settings routes, and a stub
    uvicorn. Pass ``landing_mod`` to stub ``ha_mcp.browser_landing`` and
    ``log_filters_mod`` to stub ``ha_mcp.log_filters``; omit either to simulate
    an OLDER installed server without that helper — modeled as a module missing
    the ``register_browser_landing`` / ``install_sdk_log_filters`` attribute, so
    the from-import in ``_serve`` raises the same ImportError class its guard
    catches. Injection (a sys.modules hit) is the only hermetic way to force
    that failure: deleting the entry is NOT enough, because the editable install
    (``uv sync``) adds a meta-path finder that resolves ``ha_mcp.*`` by name and
    would re-import the REAL module even though the parent ``ha_mcp`` is faked
    with an empty ``__path__`` (live-found in CI).
    """
    settings = SimpleNamespace(
        homeassistant_url="http://127.0.0.1:8123", homeassistant_token="jwt"
    )
    ha_mcp_mod: Any = ModuleType("ha_mcp")
    ha_mcp_mod.__path__ = []  # package semantics for submodule imports
    cfg: Any = ModuleType("ha_mcp.config")
    cfg.reset_global_settings = lambda: None
    cfg.set_embedded_connection = lambda u, t: None
    cfg.OAUTH_MODE_URL = "__sentinel_url__"
    cfg.OAUTH_MODE_TOKEN = "__sentinel_token__"
    cfg.get_global_settings = lambda: settings
    server_mod: Any = ModuleType("ha_mcp.server")
    server_mod.HomeAssistantSmartMCPServer = lambda: SimpleNamespace(mcp=mcp)
    ui_mod: Any = ModuleType("ha_mcp.settings_ui")
    ui_mod.register_settings_routes = lambda *a, **k: None
    uvicorn_mod: Any = ModuleType("uvicorn")
    uvicorn_mod.Config = lambda *a, **k: SimpleNamespace()
    uvicorn_mod.Server = lambda config: SimpleNamespace(should_exit=False)
    ha_mcp_mod.config = cfg
    ha_mcp_mod.server = server_mod
    ha_mcp_mod.settings_ui = ui_mod
    mods = {
        "ha_mcp": ha_mcp_mod,
        "ha_mcp.config": cfg,
        "ha_mcp.server": server_mod,
        "ha_mcp.settings_ui": ui_mod,
        "uvicorn": uvicorn_mod,
    }
    if landing_mod is None:
        # Older-server stand-in: module present, helper attribute absent — the
        # from-import raises ImportError, same class as a missing module.
        landing_mod = ModuleType("ha_mcp.browser_landing")
    ha_mcp_mod.browser_landing = landing_mod
    mods["ha_mcp.browser_landing"] = landing_mod
    if log_filters_mod is None:
        # Older-server stand-in, same reasoning as landing_mod above.
        log_filters_mod = ModuleType("ha_mcp.log_filters")
    ha_mcp_mod.log_filters = log_filters_mod
    mods["ha_mcp.log_filters"] = log_filters_mod
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)


def _clear_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The worker writes these into os.environ. setenv first so monkeypatch
    # records the prior state and undoes the worker's writes; a bare delenv of
    # an unset key records nothing, and HA_MCP_EMBEDDED=1 would leak into later
    # tests (every app-mode check reads it).
    for key in ("HA_MCP_CONFIG_DIR", "HA_MCP_EMBEDDED"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require_ipv6_loopback() -> None:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback unavailable on this machine")


def _a_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
