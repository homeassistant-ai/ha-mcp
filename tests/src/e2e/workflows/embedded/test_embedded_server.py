"""End-to-end test for the in-process ha_mcp_server integration (issue #1527).

Proves the whole install method for real, in a throwaway Home Assistant Core
container: the ha_mcp_server integration is installed, its config entry is seeded
(with a locally-built ha-mcp wheel as the pip spec), HA schedules the background
bring-up which runtime-installs the server package, starts the FastMCP server on
its worker thread, and registers the ingress webhook. The test then drives the
real MCP protocol over ``POST /api/webhook/<id>`` — ``initialize`` → ``tools/list``
→ one read-only tool call — asserting a Streamable-HTTP response parses and the
full tool inventory is present.

This uses a DEDICATED, module-scoped container (not the shared session one): the
always-on server would otherwise runtime-install the whole fastmcp tree and run a
server thread in every e2e session. It only runs on the testcontainer backend and
is skipped when Docker is unavailable or the HAOS backend is selected.

Strategy notes:
- The pip spec is a ``file://`` URL to a wheel built from the local checkout by
  ``pip wheel --no-deps`` and copied into the bind-mounted ``/config``; its
  DEPENDENCIES (fastmcp etc.) still resolve from PyPI under HA's constraints file
  — which is exactly the cryptography/py-version compatibility this proves.
- First bring-up runtime-installs that dependency tree, so readiness is polled
  with a generous timeout (minutes).
- The entry-driven pip path and every unit of the webhook / manager / flow logic
  are covered hermetically in tests/src/unit/test_{embedded_server,mcp_webhook,
  embedded_setup,config_flow_ha_mcp_server,ha_mcp_server_entry}.py; this test is
  the real-container proof of the mechanism end to end.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
import requests
from test_constants import HA_TEST_IMAGE, TEST_TOKEN

pytestmark = [pytest.mark.slow, pytest.mark.container_only]

_DOMAIN = "ha_mcp_server"
_ENTRY_ID = "e2e_test_ha_mcp_server_entry"
# Stable, known secrets seeded into entry.data so the test knows the webhook URL
# up front (otherwise async_setup_entry would generate them and the test could
# not address the endpoint without reading them back out of .storage).
_WEBHOOK_ID = "mcp_e2e_ha_mcp_server_0123456789abcdef"
_SECRET_PATH = "/private_e2e_ha_mcp_server_secret"
_SERVER_PORT = 9584

# The whole fastmcp dependency tree is runtime-installed on first bring-up.
_READY_TIMEOUT_S = 600
_READY_POLL_S = 5

_REPO_ROOT = Path(__file__).resolve().parents[5]
_INITIAL_STATE = _REPO_ROOT / "tests" / "initial_test_state"
_INTEGRATION_SRC = _REPO_ROOT / "homeassistant-integration" / "ha_mcp_server"


def _docker_available() -> bool:
    try:
        import docker as docker_sdk

        docker_sdk.from_env().ping()
        return True
    except Exception:
        return False


def _build_wheel(dest_dir: Path) -> Path:
    """Build a --no-deps ha-mcp wheel from the local checkout into ``dest_dir``."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(dest_dir),
            str(_REPO_ROOT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(dest_dir.glob("ha_mcp-*.whl"))
    if not wheels:
        raise AssertionError(f"no ha_mcp wheel built in {dest_dir}")
    return wheels[0]


def _seed_config(config_path: Path, wheel_name: str) -> None:
    """Install the integration + seed a config entry into a fresh config dir."""
    shutil.copytree(_INITIAL_STATE, config_path, dirs_exist_ok=True)

    dest = config_path / "custom_components" / _DOMAIN
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_INTEGRATION_SRC, dest, dirs_exist_ok=True)

    storage_file = config_path / ".storage" / "core.config_entries"
    data = json.loads(storage_file.read_text())
    entries = data.setdefault("data", {}).setdefault("entries", [])
    entries.append(
        {
            "created_at": "2025-09-07T23:56:28.040744+00:00",
            "data": {
                "webhook_id": _WEBHOOK_ID,
                "secret_path": _SECRET_PATH,
            },
            "disabled_by": None,
            "discovery_keys": {},
            "domain": _DOMAIN,
            "entry_id": _ENTRY_ID,
            "minor_version": 1,
            "modified_at": "2025-09-07T23:56:28.040747+00:00",
            "options": {
                # file:// wheel + PyPI-resolved deps under HA's constraints file.
                "pip_spec": f"ha-mcp @ file:///config/{wheel_name}",
                "server_port": _SERVER_PORT,
                "bind_host": "127.0.0.1",
                "webhook_auth": "none",
            },
            "pref_disable_new_entities": False,
            "pref_disable_polling": False,
            "source": "import",
            "subentries": [],
            "title": "Home Assistant MCP Server",
            "unique_id": _DOMAIN,
            "version": 1,
        }
    )
    storage_file.write_text(json.dumps(data, indent=2))

    # HA runs as uid 0 in the test image but the bind mount must be traversable.
    for path in config_path.rglob("*"):
        try:
            path.chmod(0o777 if path.is_dir() else 0o666)
        except OSError:
            pass  # Best-effort chmod; some testcontainer mounts refuse it.


def _wait_http_ok(url: str, headers: dict[str, str], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(url, headers=headers, timeout=5).status_code == 200:
                return
        except requests.exceptions.RequestException:
            pass  # HA still booting; retry until the deadline.
        time.sleep(2)
    raise AssertionError(f"{url} not ready within {timeout}s")


def _mcp_post(
    base_url: str,
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
) -> requests.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return requests.post(
        f"{base_url}/api/webhook/{_WEBHOOK_ID}",
        headers=headers,
        data=json.dumps(payload),
        timeout=60,
    )


def _parse_mcp(resp: requests.Response) -> dict[str, Any] | None:
    """Parse a Streamable-HTTP MCP response (JSON body or SSE) to a JSON-RPC dict."""
    ctype = resp.headers.get("Content-Type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                    return obj
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _initialize(base_url: str) -> tuple[bool, str | None]:
    """Run the MCP initialize handshake.

    Returns ``(ok, session_id)`` — ``ok`` is True when the server returned a valid
    JSON-RPC result; ``session_id`` is the ``Mcp-Session-Id`` header if the server
    issued one (stateless mode may not).
    """
    resp = _mcp_post(
        base_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ha_mcp_server-e2e", "version": "1.0"},
            },
        },
    )
    parsed = _parse_mcp(resp)
    if not parsed or "result" not in parsed:
        return False, None
    session_id = resp.headers.get("Mcp-Session-Id")
    if session_id:
        # Best-effort: some servers require the initialized notification before
        # accepting further requests.
        _mcp_post(
            base_url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )
    return True, session_id


@pytest.fixture(scope="module")
def embedded_ha():
    """Boot a dedicated HA container running the ha_mcp_server integration.

    Yields ``(base_url, session_id)`` once the in-process MCP server has installed
    itself, started, and registered its ingress webhook.
    """
    if not _docker_available():
        pytest.skip("Docker is not available for the embedded-server e2e")

    from testcontainers.core.container import DockerContainer

    wheel_dir = Path(tempfile.mkdtemp(prefix="ha_mcp_wheel_"))
    try:
        wheel = _build_wheel(wheel_dir)
    except (subprocess.CalledProcessError, AssertionError) as err:
        pytest.skip(f"could not build the ha-mcp wheel: {err}")

    config_path = Path(tempfile.mkdtemp(prefix="ha_mcp_server_e2e_"))
    shutil.copy2(wheel, config_path / wheel.name)
    _seed_config(config_path, wheel.name)

    container = (
        DockerContainer(HA_TEST_IMAGE)
        .with_exposed_ports(8123)
        .with_volume_mapping(str(config_path), "/config", "rw")
        .with_env("TZ", "UTC")
    )
    container.start()
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8123)
        base_url = f"http://{host}:{port}"
        headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        _wait_http_ok(f"{base_url}/api/", headers, timeout=120)

        # Poll until the bring-up (runtime pip install of the fastmcp tree, then
        # server start + webhook registration) completes and MCP initialize
        # returns a valid JSON-RPC result.
        deadline = time.monotonic() + _READY_TIMEOUT_S
        session_id: str | None = None
        ready = False
        while time.monotonic() < deadline:
            try:
                ready, session_id = _initialize(base_url)
            except requests.exceptions.RequestException:
                ready = False
            if ready:
                break
            time.sleep(_READY_POLL_S)
        if not ready:
            logs = container.get_logs()
            raise AssertionError(
                "ha_mcp_server did not become reachable via its webhook within "
                f"{_READY_TIMEOUT_S}s. Container logs:\n{logs}"
            )
        yield base_url, session_id
    finally:
        with contextlib.suppress(Exception):
            container.stop()


class TestEmbeddedServerEndToEnd:
    def test_initialize_and_list_tools(self, embedded_ha):
        base_url, session_id = embedded_ha
        resp = _mcp_post(
            base_url,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_id=session_id,
        )
        parsed = _parse_mcp(resp)
        assert parsed is not None, f"unparseable tools/list response: {resp.text[:500]}"
        assert "result" in parsed, parsed
        tools = parsed["result"].get("tools", [])
        names = {t.get("name") for t in tools}
        # The full ha-mcp inventory is present (well above the selection-accuracy
        # threshold); a handful would mean a truncated / wrong server.
        assert len(tools) > 60, f"expected the full tool inventory, got {len(tools)}"
        assert "ha_get_state" in names

    def test_read_only_tool_call(self, embedded_ha):
        base_url, session_id = embedded_ha
        resp = _mcp_post(
            base_url,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "ha_get_state",
                    "arguments": {"entity_id": "sun.sun"},
                },
            },
            session_id=session_id,
        )
        parsed = _parse_mcp(resp)
        assert parsed is not None, f"unparseable tools/call response: {resp.text[:500]}"
        assert "result" in parsed, parsed
        # The tool ran against the real HA instance and returned content.
        assert parsed["result"].get("content"), parsed
