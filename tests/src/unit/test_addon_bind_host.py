"""The app (add-on) listener accepts IPv4 and IPv6 clients (issue #2915)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from ._listener_families import reachable_loopbacks, require_ipv6_loopback

_START_PY = Path(__file__).parents[3] / "homeassistant-addon" / "start.py"


def _load_start() -> ModuleType:
    spec = importlib.util.spec_from_file_location("addon_start", _START_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_app_accepts_ipv6_clients_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_ipv6_loopback()
    monkeypatch.delenv("MCP_HOST", raising=False)
    host = _load_start().resolve_bind_host()
    assert await reachable_loopbacks(host) == {"127.0.0.1", "::1"}


async def test_mcp_host_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    host = _load_start().resolve_bind_host()
    assert await reachable_loopbacks(host) == {"127.0.0.1"}
