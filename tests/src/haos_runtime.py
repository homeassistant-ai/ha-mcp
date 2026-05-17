"""Shared HAOS-QEMU runtime helpers (see #1281).

Imported by both ``tests/src/e2e/conftest.py`` (for backend-switched
session fixtures) and ``tests/src/haos_e2e/conftest.py`` (for the
HAOS-only canary suite). Keeping the QEMU lifecycle + HA login-flow
code in one place avoids drift between the two backends.

Stdlib-only on purpose so this module imports cleanly without
requiring the build-script's websockets dep.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

# Ports the host uses to reach the booted HAOS. Stay in sync with the build
# script's onboarding-time client_id so /auth/token doesn't reject us.
HA_HOST_PORT = int(os.environ.get("HAOS_TEST_HA_PORT", "18123"))
SSH_HOST_PORT = int(os.environ.get("HAOS_TEST_SSH_PORT", "12222"))
OVMF_CODE_PATH = os.environ.get("HAOS_BUILD_OVMF", "/usr/share/OVMF/OVMF_CODE.fd")
HAOS_IMAGE_ENV = "HAOS_TEST_IMAGE_PATH"


def is_haos_backend_selected() -> bool:
    """True iff the workflow has staged a HAOS qcow2 for this run."""
    raw = os.environ.get(HAOS_IMAGE_ENV)
    return bool(raw and Path(raw).exists())


def _http(
    method: str,
    url: str,
    *,
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


def login_for_token(base_url: str, username: str, password: str) -> str:
    """Drive HA's login flow against a pre-onboarded image, return access token.

    Same shape as the HA frontend's auth: /auth/login_flow (start) →
    /auth/login_flow/<flow_id> (submit creds) → /auth/token (form-encoded
    exchange). The returned access token is short-lived (~30 min) but long
    enough for a single test session.
    """
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
    submit = _http(
        "POST",
        f"{base_url}/auth/login_flow/{flow_id}",
        body={"client_id": base_url, "username": username, "password": password},
    )
    if submit.get("type") != "create_entry":
        raise RuntimeError(f"login_flow rejected credentials: {submit}")
    auth_code = submit["result"]
    token_resp = _http(
        "POST",
        f"{base_url}/auth/token",
        # /auth/token uses await request.post() — must be form-encoded
        # (same gotcha as the build script).
        form={
            "client_id": base_url,
            "grant_type": "authorization_code",
            "code": auth_code,
        },
    )
    return token_resp["access_token"]


@contextmanager
def boot_haos_qemu(image_path: Path, serial_log: Path | None = None) -> Iterator[str]:
    """Boot a HAOS qcow2 under QEMU/KVM; yield the HA base URL.

    Caller is responsible for guarding with ``is_haos_backend_selected()``
    or similar before invoking. On context exit, terminates QEMU (SIGTERM
    then SIGKILL after 60s if still alive).
    """
    if not Path("/dev/kvm").exists():
        raise RuntimeError("/dev/kvm not available — HAOS tests require KVM acceleration")

    serial = serial_log or Path("/tmp/haos-e2e-serial.log")
    cmd = [
        "qemu-system-x86_64",
        "-machine", "q35,accel=kvm",
        "-cpu", "host",
        "-smp", "2",
        "-m", "4096",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE_PATH}",
        "-drive", f"if=virtio,file={image_path},format=qcow2",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:127.0.0.1:{HA_HOST_PORT}-:8123,"
        f"hostfwd=tcp:127.0.0.1:{SSH_HOST_PORT}-:22",
        "-device", "virtio-net-pci,netdev=net0",
        "-display", "none",
        "-serial", f"file:{serial}",
    ]
    LOG.info("Booting HAOS (serial log: %s)", serial)
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
