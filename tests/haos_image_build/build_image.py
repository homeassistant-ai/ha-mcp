#!/usr/bin/env python3
"""Build the HAOS test image used by the HAOS E2E tier (#1281).

The script boots a vanilla HAOS qcow2 inside QEMU/KVM, runs first-user
onboarding to obtain a long-lived access token, registers the ha-mcp addon
repository, installs the addons listed in ``ADDONS``, performs the HACS
bootstrap, then powers HAOS off and emits a compressed qcow2 ready for upload
to GHCR via oras.

Invoke from a Linux host with /dev/kvm available — both the local developer
flow and the build-haos-test-image.yml workflow follow the same path. The
output file is ``<work-dir>/haos-test-image.qcow2.xz``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("haos_image_build")

# Pin both the HAOS release and the addon set. Renovate watches the comment
# annotation below for the HAOS bump; the addon list is hand-curated in #1281
# and intentionally short for v1. Once the canary is stable, follow-up PRs can
# expand the list and migrate more existing E2E tests over.
#
# renovate: datasource=github-releases depName=home-assistant/operating-system
HAOS_VERSION = "17.3"
HAOS_QCOW2_URL = (
    f"https://github.com/home-assistant/operating-system/releases/download/"
    f"{HAOS_VERSION}/haos_ova-{HAOS_VERSION}.qcow2.xz"
)

# Long-lived access token credentials baked into the image. Tests resolve the
# token from tests/test_constants.py so the same value lives in one place.
ONBOARDING_USER = "hamcp_test"
ONBOARDING_PASSWORD = "hamcp_test_password"
ONBOARDING_NAME = "HA-MCP CI"

# Local TCP ports the host uses to talk to the booted HAOS. Random ports avoid
# collisions when the build script runs alongside the existing testcontainer
# E2E suite on the same runner.
HA_HOST_PORT = 18123
SSH_HOST_PORT = 12222


@dataclass(frozen=True)
class Addon:
    """An addon entry to install via the Supervisor API.

    ``slug`` is the Supervisor-side identifier ``<repo>_<name>``; ``options``
    is the addon-options payload merged via ``POST /addons/<slug>/options``.
    """

    slug: str
    options: dict[str, Any]


# v1 addon set — see #1281 comment thread for rationale. Options are minimal
# and chosen so each addon starts cleanly without external hardware:
#   - Frigate: dummy detector, no cameras configured yet
#   - ESPHome: dashboard-only, no devices adopted
#   - Node-RED: stock flows, default credential secret rotated at build time
#   - Mosquitto: anonymous-disabled, one local user
#   - Z2M: serial adapter pointed at an ephemeral PTY (no real coordinator)
#
# Mock streams/devices that exercise the integrations come in a follow-up
# commit; v1 only proves the addon lifecycle path.
ADDONS: tuple[Addon, ...] = (
    Addon(slug="core_mosquitto", options={"logins": [{"username": "hamcp", "password": "hamcp"}]}),
    Addon(slug="a0d7b954_nodered", options={}),
    Addon(slug="a0d7b954_esphome", options={}),
    Addon(slug="ccab4aaf_zigbee2mqtt", options={"serial": {"port": "/dev/null"}}),
    Addon(slug="ccab4aaf_frigate", options={}),
)

HA_MCP_ADDON_REPO = "https://github.com/homeassistant-ai/ha-mcp"


# ---------------------------------------------------------------------------
# Subprocess + HTTP helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    LOG.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, text=True, **kwargs)


def _http(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
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


# ---------------------------------------------------------------------------
# Image fetch + QEMU lifecycle
# ---------------------------------------------------------------------------


def fetch_haos_qcow2(work_dir: Path) -> Path:
    """Download and decompress the pinned HAOS qcow2 into ``work_dir``."""
    archive = work_dir / f"haos_ova-{HAOS_VERSION}.qcow2.xz"
    qcow2 = work_dir / "haos-test-image.qcow2"
    if qcow2.exists():
        LOG.info("Reusing existing qcow2 at %s", qcow2)
        return qcow2
    LOG.info("Downloading HAOS %s", HAOS_VERSION)
    _run(["curl", "-sfL", "-o", str(archive), HAOS_QCOW2_URL])
    LOG.info("Decompressing %s", archive.name)
    _run(["xz", "-dk", "--force", str(archive)])
    (archive.with_suffix("")).rename(qcow2)
    archive.unlink(missing_ok=True)
    # HAOS ships with a small data partition; grow it so addon installs fit.
    _run(["qemu-img", "resize", str(qcow2), "32G"])
    return qcow2


def start_qemu(qcow2: Path, work_dir: Path) -> subprocess.Popen[bytes]:
    """Boot HAOS in QEMU with KVM, NAT'd networking, and serial console log."""
    serial_log = work_dir / "haos-serial.log"
    cmd = [
        "qemu-system-x86_64",
        "-machine", "q35,accel=kvm",
        "-cpu", "host",
        "-smp", "2",
        "-m", "4096",
        "-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE.fd",
        "-drive", f"if=virtio,file={qcow2},format=qcow2",
        "-netdev",
        f"user,id=net0,hostfwd=tcp:127.0.0.1:{HA_HOST_PORT}-:8123,"
        f"hostfwd=tcp:127.0.0.1:{SSH_HOST_PORT}-:22",
        "-device", "virtio-net-pci,netdev=net0",
        "-display", "none",
        "-serial", f"file:{serial_log}",
    ]
    LOG.info("Booting HAOS (serial log: %s)", serial_log)
    return subprocess.Popen(cmd)


def stop_qemu(proc: subprocess.Popen[bytes], token: str) -> None:
    """Graceful shutdown via Supervisor; fall back to SIGTERM if it hangs."""
    try:
        _http(
            "POST",
            f"http://127.0.0.1:{HA_HOST_PORT}/api/hassio/host/shutdown",
            token=token,
            timeout=10.0,
        )
    except Exception as e:
        LOG.warning("Supervisor shutdown call failed: %s — sending SIGTERM", e)
        proc.terminate()
    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        LOG.warning("QEMU did not exit cleanly — killing")
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# HAOS configuration steps (run against the live booted instance)
# ---------------------------------------------------------------------------


def onboard(base_url: str) -> str:
    """Create the first user and return its long-lived access token."""
    LOG.info("Onboarding first user")
    resp = _http(
        "POST",
        f"{base_url}/api/onboarding/users",
        body={
            "client_id": base_url,
            "name": ONBOARDING_NAME,
            "username": ONBOARDING_USER,
            "password": ONBOARDING_PASSWORD,
            "language": "en",
        },
    )
    auth_code = resp["auth_code"]
    token_resp = _http(
        "POST",
        f"{base_url}/auth/token",
        body={
            "client_id": base_url,
            "grant_type": "authorization_code",
            "code": auth_code,
        },
    )
    return token_resp["access_token"]


def install_addons(base_url: str, token: str) -> None:
    """Register the ha-mcp addon repo and install + configure each addon."""
    LOG.info("Adding ha-mcp addon repository")
    _http(
        "POST",
        f"{base_url}/api/hassio/store/repositories",
        token=token,
        body={"repository": HA_MCP_ADDON_REPO},
    )
    for addon in ADDONS:
        LOG.info("Installing %s", addon.slug)
        _http(
            "POST",
            f"{base_url}/api/hassio/addons/{addon.slug}/install",
            token=token,
            timeout=900.0,
        )
        if addon.options:
            _http(
                "POST",
                f"{base_url}/api/hassio/addons/{addon.slug}/options",
                token=token,
                body={"options": addon.options},
            )
        _http(
            "POST",
            f"{base_url}/api/hassio/addons/{addon.slug}/start",
            token=token,
            timeout=120.0,
        )


def install_hacs(base_url: str, token: str) -> None:
    """Bootstrap HACS via the official one-liner.

    Runs inside the HAOS container by way of the Supervisor host-exec endpoint.
    Required in v1 because HACS-driven custom-component churn is the largest
    source of E2E flake the testcontainer suite cannot reproduce (#1281).
    """
    LOG.info("Installing HACS")
    _http(
        "POST",
        f"{base_url}/api/hassio/host/services",
        token=token,
        body={
            "service": "shell_command.install_hacs",
            "command": "wget -O - https://get.hacs.xyz | bash -",
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build(work_dir: Path, output: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    qcow2 = fetch_haos_qcow2(work_dir)
    qemu = start_qemu(qcow2, work_dir)
    base_url = f"http://127.0.0.1:{HA_HOST_PORT}"
    try:
        _wait_port(HA_HOST_PORT, timeout=180)
        _wait_http_ok(f"{base_url}/manifest.json", timeout=600)
        token = onboard(base_url)
        install_addons(base_url, token)
        install_hacs(base_url, token)
        # TODO(#1281 follow-up): integrations (ESPHome companion, Node-RED
        # companion, Local Calendar, Sun verification) and mock RTSP/MQTT
        # feeders. The canary test only needs addon lifecycle for now.
        stop_qemu(qemu, token)
    except Exception:
        LOG.exception("Image build failed — leaving qcow2 in %s for inspection", qcow2)
        qemu.terminate()
        qemu.wait(timeout=60)
        raise
    LOG.info("Compressing image")
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(["xz", "-9", "-T0", "-c", str(qcow2)], stdout=output.open("wb"))
    LOG.info("Wrote %s (%.1f MB)", output, output.stat().st_size / 1024 / 1024)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(os.environ.get("HAOS_BUILD_WORK_DIR", "/tmp/haos-build")),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("haos-test-image.qcow2.xz"),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not Path("/dev/kvm").exists():
        LOG.error("/dev/kvm not available — HAOS build requires KVM acceleration")
        return 2
    build(args.work_dir, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
