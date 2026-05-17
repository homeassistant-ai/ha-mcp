"""HAOS-tier E2E session fixtures (see #1281).

Parallel to ``tests/src/e2e/conftest.py`` but boots a pre-baked HAOS
qcow2 in QEMU/KVM instead of pulling a HA Core Docker container. The
``haos_mcp_client`` fixture is API-compatible with the existing
``mcp_client`` fixture so the same FastMCP-driven test code can run
against either backend; the long-term goal (#1281) is one shared test
suite that targets both harnesses.

Required env: ``HAOS_TEST_IMAGE_PATH`` pointing at an uncompressed
qcow2 already built by ``tests/haos_image_build/build_image.py`` (the
``haos-e2e-tests.yml`` workflow stages this on the runner's local disk).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest

# Bring src/ on path so `ha_mcp.*` imports resolve when pytest runs from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import Client
from test_constants import TEST_PASSWORD, TEST_USER

from ha_mcp.client import HomeAssistantClient
from ha_mcp.server import HomeAssistantSmartMCPServer

LOG = logging.getLogger(__name__)

# Match the ports the build script onboards against — keeps client_id stable
# for /auth/token (HA validates client_id matches what was used during onboard).
HA_HOST_PORT = int(os.environ.get("HAOS_TEST_HA_PORT", "18123"))
SSH_HOST_PORT = int(os.environ.get("HAOS_TEST_SSH_PORT", "12222"))
OVMF_CODE_PATH = os.environ.get("HAOS_BUILD_OVMF", "/usr/share/OVMF/OVMF_CODE.fd")


# ---------------------------------------------------------------------------
# Low-level helpers (intentionally stdlib-only; mirror build_image.py shape)
# ---------------------------------------------------------------------------


def _http(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    data: bytes | None
    headers: dict[str, str] = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    else:
        data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def _wait_port(port: int, host: str = "127.0.0.1", timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            try:
                s.connect((host, port))
                return
            except OSError:
                time.sleep(2.0)
    raise TimeoutError(f"{host}:{port} did not open within {timeout}s")


def _wait_http_ok(url: str, timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = e
        time.sleep(3.0)
    raise TimeoutError(f"{url} did not become ready within {timeout}s (last: {last_err})")


def _login_for_token(base_url: str) -> str:
    """Drive HA's login flow to get a short-lived access token.

    The pre-baked image is already onboarded, so /api/onboarding/users
    returns 403. Use /auth/login_flow + /auth/token instead — the same
    flow the HA frontend uses.
    """
    # 1) start a login flow with the homeassistant provider
    flow = _http(
        "POST",
        f"{base_url}/auth/login_flow",
        body={
            "client_id": base_url,
            "handler": ["homeassistant", None],
            "redirect_uri": base_url,
        },
    )
    flow_id = flow["flow_id"]
    # 2) submit credentials
    submit = _http(
        "POST",
        f"{base_url}/auth/login_flow/{flow_id}",
        body={
            "client_id": base_url,
            "username": TEST_USER,
            "password": TEST_PASSWORD,
        },
    )
    if submit.get("type") != "create_entry":
        raise RuntimeError(f"login_flow rejected credentials: {submit}")
    auth_code = submit["result"]
    # 3) exchange auth_code for an access token (form-encoded body — same gotcha
    #    as build_image.py: /auth/token uses await request.post())
    token_resp = _http(
        "POST",
        f"{base_url}/auth/token",
        form={
            "client_id": base_url,
            "grant_type": "authorization_code",
            "code": auth_code,
        },
    )
    return token_resp["access_token"]


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def haos_image_path() -> Path:
    """Path to the pre-baked HAOS qcow2 staged by the workflow."""
    raw = os.environ.get("HAOS_TEST_IMAGE_PATH")
    if not raw:
        pytest.skip("HAOS_TEST_IMAGE_PATH not set — workflow has not staged the image")
    path = Path(raw)
    if not path.exists():
        pytest.skip(f"qcow2 missing at {path}")
    return path


@pytest.fixture(scope="session")
def haos_qemu(haos_image_path: Path) -> Generator[str]:
    """Boot the qcow2 in QEMU/KVM and yield the HA base URL.

    Re-uses the QEMU invocation shape from build_image.py: q35+KVM, OVMF,
    NAT'd networking with hostfwd for HA (8123→HAOS_TEST_HA_PORT) and SSH
    (22→HAOS_TEST_SSH_PORT). Serial output goes to a tempfile for
    diagnostics on boot failures.
    """
    if not Path("/dev/kvm").exists():
        pytest.skip("/dev/kvm not available — HAOS tests require KVM acceleration")

    serial_log = Path("/tmp/haos-e2e-serial.log")
    cmd = [
        "qemu-system-x86_64",
        "-machine", "q35,accel=kvm",
        "-cpu", "host",
        "-smp", "2",
        "-m", "4096",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE_PATH}",
        "-drive", f"if=virtio,file={haos_image_path},format=qcow2",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:127.0.0.1:{HA_HOST_PORT}-:8123,"
        f"hostfwd=tcp:127.0.0.1:{SSH_HOST_PORT}-:22",
        "-device", "virtio-net-pci,netdev=net0",
        "-display", "none",
        "-serial", f"file:{serial_log}",
    ]
    LOG.info("Booting HAOS for E2E (serial log: %s)", serial_log)
    proc = subprocess.Popen(cmd)
    base_url = f"http://127.0.0.1:{HA_HOST_PORT}"
    try:
        _wait_port(HA_HOST_PORT, timeout=180)
        _wait_http_ok(f"{base_url}/manifest.json", timeout=600)
        LOG.info("HAOS frontend ready at %s", base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture(scope="session")
def haos_token(haos_qemu: str) -> str:
    """Short-lived access token obtained via the login flow."""
    return _login_for_token(haos_qemu)


@pytest.fixture(scope="session")
async def haos_ha_client(
    haos_qemu: str, haos_token: str
) -> AsyncGenerator[HomeAssistantClient]:
    """HomeAssistantClient pointed at the booted HAOS."""
    client = HomeAssistantClient(base_url=haos_qemu, token=haos_token)
    cfg = await client.get_config()
    if not cfg:
        pytest.fail(f"Failed to connect to HAOS at {haos_qemu}")
    LOG.info("Connected to HAOS: v%s components=%d", cfg.get("version"), len(cfg.get("components", [])))
    yield client
    await client.close()


@pytest.fixture(scope="session")
async def haos_mcp_server(
    haos_qemu: str, haos_token: str
) -> AsyncGenerator[HomeAssistantSmartMCPServer]:
    """In-process MCP server bound to the booted HAOS."""
    client = HomeAssistantClient(base_url=haos_qemu, token=haos_token)
    server = HomeAssistantSmartMCPServer(client=client)
    tools = await server.mcp.list_tools()
    LOG.info("MCP server initialized: %d tools, connected to %s", len(tools), haos_qemu)
    yield server


@pytest.fixture(scope="session")
async def haos_mcp_client(
    haos_mcp_server: HomeAssistantSmartMCPServer,
) -> AsyncGenerator[Client]:
    """FastMCP Client (in-memory transport) for the haos MCP server.

    API-compatible with the testcontainer ``mcp_client`` fixture; tests
    that depend on either fixture can be moved between backends with a
    fixture-name swap (or, once the unified ``ha_environment`` selector
    lands in a follow-up, no change at all).
    """
    async with Client(haos_mcp_server.mcp) as c:
        yield c


# ---------------------------------------------------------------------------
# Async loop config — match the parent e2e suite so session-scoped async
# fixtures share one loop with their tests.
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(_config, items):
    # Auto-apply the haos marker to everything in this package so the
    # workflow's `-m haos` filter works without per-file pytestmark.
    haos = pytest.mark.haos
    for item in items:
        if "haos_e2e" in str(item.fspath):
            item.add_marker(haos)
