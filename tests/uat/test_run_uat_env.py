"""Tests for the environment BAT gives the MCP server it starts."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

RUN_UAT = Path(__file__).resolve().parent / "run_uat.py"

# Load by path: ``uat`` is not importable as a package from pytest's rootdir.
spec = importlib.util.spec_from_file_location("run_uat", str(RUN_UAT))
run_uat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_uat)

_PROBE = """
import json
from ha_mcp.stdio_settings_sidecar import _is_disabled
from ha_mcp.utils.data_paths import get_data_dir
print(json.dumps({"data_dir": str(get_data_dir()), "sidecar_off": _is_disabled()}))
"""


def _server_view(mcp_env: dict[str, str] | None, home: Path) -> dict:
    """Resolve the data dir and sidecar switch the server would see."""
    config = run_uat.build_stdio_mcp_config(
        "http://127.0.0.1:9", "unused", None, mcp_env
    )
    server_env = config["mcpServers"]["home-assistant"]["env"]
    base = {
        k: v
        for k, v in os.environ.items()
        if k not in {"HA_MCP_CONFIG_DIR", "HA_MCP_DISABLE_SETTINGS_UI"}
    }
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env={**base, "HOME": str(home), **server_env},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_bat_server_ignores_developer_config_dir(tmp_path):
    """BAT results must not depend on the developer's ~/.ha-mcp tool pins
    and settings, and a run must not start a settings sidecar there."""
    home = tmp_path / "home"
    (home / ".ha-mcp").mkdir(parents=True)

    view = _server_view(None, home)

    assert Path(view["data_dir"]) != home / ".ha-mcp"
    assert not Path(view["data_dir"]).is_relative_to(home)
    assert view["sidecar_off"] is True


def test_mcp_env_config_dir_overrides_isolation(tmp_path):
    """``--mcp-env HA_MCP_CONFIG_DIR=...`` still runs BAT with chosen settings."""
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    mcp_env = run_uat.parse_mcp_env([f"HA_MCP_CONFIG_DIR={chosen}"])

    view = _server_view(mcp_env, tmp_path / "home")

    assert Path(view["data_dir"]) == chosen
