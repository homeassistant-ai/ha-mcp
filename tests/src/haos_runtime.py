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


def refresh_recorder_in_qcow2(
    image_path: Path, *, target_age_seconds: float = 300.0
) -> None:
    """Shift recorder timestamps inside the baked qcow2 to look ``recent``.

    The image cache key is content-hashed, so a cache hit re-uses an image
    whose ``home-assistant_v2.db`` timestamps are frozen at bake time —
    once that exceeds the ~24h window history queries use, every history
    pagination test silently regresses. This helper extracts the DB from
    the qcow2, runs the same uniform timestamp shift the testcontainer
    path does (``conftest._refresh_recorder_timestamps``), and copies the
    file back in place. Done once per pytest session before QEMU boots.

    Uses guestfish (libguestfs) for both copy-out and copy-in; sqlite3
    stdlib for the shift itself. ~30s wall-clock overhead per session.
    """
    import sqlite3
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="haos-ts-refresh-"))
    db_local = workdir / "home-assistant_v2.db"
    try:
        # copy-out the recorder DB from the qcow2's hassos-data partition.
        subprocess.run(
            [
                "guestfish",
                "--ro",
                "-a", str(image_path),
                "run",
                ":",
                "mount", "/dev/sda8", "/",
                ":",
                "copy-out",
                "/supervisor/homeassistant/home-assistant_v2.db",
                str(workdir),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )

        # Same logic as conftest._refresh_recorder_timestamps. Kept inline
        # rather than importing because conftest pulls in heavy dev deps
        # (docker, testcontainers) that the HAOS-only paths don't need.
        TIMESTAMP_COLUMNS = {
            "states": ("last_updated_ts", "last_changed_ts", "last_reported_ts"),
            "events": ("time_fired_ts",),
            "statistics": ("start_ts", "created_ts"),
            "statistics_short_term": ("start_ts", "created_ts"),
        }
        conn = sqlite3.connect(str(db_local))
        try:
            newest = 0.0
            for table, cols in TIMESTAMP_COLUMNS.items():
                for col in cols:
                    try:
                        row = conn.execute(
                            f"SELECT MAX({col}) FROM {table}"
                        ).fetchone()
                    except sqlite3.OperationalError as exc:
                        msg = str(exc).lower()
                        if "no such table" in msg or "no such column" in msg:
                            continue
                        raise
                    if row and row[0] is not None and isinstance(row[0], (int, float)):
                        newest = max(newest, float(row[0]))

            if newest <= 0:
                LOG.warning("Recorder DB has no numeric timestamps; skipping shift")
                return

            target = time.time() - target_age_seconds
            offset = target - newest
            if offset <= 0:
                LOG.info(
                    "Recorder timestamps already recent (newest=%.0f, "
                    "target=%.0f); no shift needed", newest, target,
                )
                return

            for table, cols in TIMESTAMP_COLUMNS.items():
                for col in cols:
                    try:
                        conn.execute(
                            f"UPDATE {table} SET {col} = {col} + ? "
                            f"WHERE {col} IS NOT NULL",
                            (offset,),
                        )
                    except sqlite3.OperationalError as exc:
                        msg = str(exc).lower()
                        if "no such table" in msg or "no such column" in msg:
                            continue
                        raise
            conn.commit()
            LOG.info("Shifted recorder timestamps by %+.0fs", offset)
        finally:
            conn.close()

        # copy-in the shifted DB. --rw so guestfish opens the qcow2 for
        # write; the file's owner/perms inside the qcow2 are preserved by
        # libguestfs when overwriting an existing path.
        subprocess.run(
            [
                "guestfish",
                "--rw",
                "-a", str(image_path),
                "run",
                ":",
                "mount", "/dev/sda8", "/",
                ":",
                "copy-in",
                str(db_local),
                "/supervisor/homeassistant/",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        LOG.info("Refreshed recorder DB in %s", image_path)
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


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
