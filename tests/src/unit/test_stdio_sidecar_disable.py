"""The stdio settings URL reaches the agent only while a sidecar serves it (#2954)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ha_mcp import stdio_settings_sidecar as sidecar
from ha_mcp.tools.search import overview as search_overview

from ._component_routing_helpers import make_ws, patch_ws
from .test_ha_overview_component_routing import (
    _CAPS_OVERVIEW,
    OverviewRoutingClient,
    _build_overview_tool,
    _overview_slices,
    _setup_visibility_disabled,
)

URL = "http://127.0.0.1:54321/private_secret/settings"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path]:
    from ha_mcp.utils.data_paths import get_data_dir

    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()
    yield tmp_path
    get_data_dir.cache_clear()


def _publish(data_dir: Path, pid: int) -> None:
    (data_dir / "ui.pid").write_text(f"{pid}\n")
    (data_dir / "ui.url").write_text(URL + "\n")


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disabled", "sidecar_alive", "surfaced"),
    [
        pytest.param("0", True, True, id="serving-sidecar-is-surfaced"),
        pytest.param("1", True, False, id="disabled-ui-hides-running-sidecar"),
        pytest.param("0", False, False, id="dead-sidecars-url-is-hidden"),
    ],
)
async def test_overview_settings_url_only_while_a_sidecar_serves_it(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    disabled: str,
    sidecar_alive: bool,
    surfaced: bool,
) -> None:
    """A disabled UI or a stale ui.url must not leak the secret URL to the agent."""
    _setup_visibility_disabled(data_dir, monkeypatch)
    monkeypatch.setattr("ha_mcp.settings_ui.get_http_settings_prefix", lambda: None)
    monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", disabled)
    _publish(data_dir, os.getpid() if sidecar_alive else _dead_pid())
    ws = make_ws(
        "ha_mcp_tools/overview",
        info_result=_CAPS_OVERVIEW,
        cmd_result=_overview_slices(),
    )
    overview = _build_overview_tool(OverviewRoutingClient())

    with patch_ws(ws, search_overview):
        resp = await overview(fields=["success"])

    assert ("settings_url" in resp) is surfaced


def test_sidecar_shutdown_removes_its_discovery_files(data_dir: Path) -> None:
    """uvicorn re-raises SIGTERM once the app has shut down, so only the app's own
    shutdown can unlink ui.url before the process dies (#2954)."""
    _publish(data_dir, os.getpid())
    app = sidecar._build_app(
        host="127.0.0.1", port=54321, secret_path="/private_secret"
    )

    with TestClient(app):
        pass

    assert not (data_dir / "ui.url").exists()
    assert not (data_dir / "ui.pid").exists()
