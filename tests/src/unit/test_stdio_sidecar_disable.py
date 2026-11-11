"""The stdio settings URL reaches the agent only while a sidecar serves it (#2954)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ha_mcp import stdio_settings_sidecar as sidecar
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools

from ._search_tool import _setup_visibility_disabled

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
    core_client: HomeAssistantClient,
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
    tools = SearchTools(core_client, SmartSearchTools(client=core_client))

    resp = await tools.ha_get_overview(fields=["success"])

    assert ("settings_url" in resp) is surfaced


def _wait_until_serving(proc: subprocess.Popen[bytes], data_dir: Path) -> None:
    """Return once the sidecar answers on the URL it published.

    uvicorn installs its signal handlers before it serves, so a SIGTERM sent
    after the first answer reaches uvicorn and not Python's default handler.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 60
    while True:
        assert proc.poll() is None, "the sidecar exited before it served"
        assert time.monotonic() < deadline, "the sidecar never served its URL"
        url_file = data_dir / "ui.url"
        if url_file.exists():
            try:
                opener.open(url_file.read_text().strip(), timeout=5).close()
                return
            except urllib.error.HTTPError:
                return
            except OSError:
                pass
        time.sleep(0.05)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is a POSIX signal")
def test_sidecar_shutdown_removes_its_discovery_files(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """uvicorn re-raises SIGTERM once the app has shut down, so only the app's own
    shutdown can unlink ui.url before the process dies (#2954)."""
    monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
    proc = subprocess.Popen(
        [sys.executable, "-m", "ha_mcp.stdio_settings_sidecar"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_serving(proc, data_dir)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert not (data_dir / "ui.url").exists()
    assert not (data_dir / "ui.pid").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="zombies are a POSIX state")
def test_sigkilled_sidecar_url_is_hidden_while_its_parent_runs(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreaped child stays a zombie that ``os.kill(pid, 0)`` still finds, so a
    SIGKILLed sidecar's URL kept reaching the agent from the parent that spawned it."""
    monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
    real_popen = subprocess.Popen

    def spawn(_cmd: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
        )
        _publish(data_dir, proc.pid)
        return proc

    with patch("subprocess.Popen", side_effect=spawn):
        sidecar.maybe_spawn()
    os.kill(int((data_dir / "ui.pid").read_text()), signal.SIGKILL)

    deadline = time.monotonic() + 5
    while sidecar.live_sidecar_url() is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sidecar.live_sidecar_url() is None
