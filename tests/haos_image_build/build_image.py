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
import urllib.parse
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

# Onboarding credentials. The username is stable across builds (tests need to
# know it). The password defaults to a known dev value but can be overridden
# via env var for builds that publish to a more privileged registry — even
# though the image is public and the password is not a real secret, keeping
# it overridable avoids hardcoding a credential string in the repo per the
# project's style guide.
ONBOARDING_USER = os.environ.get("HAOS_BUILD_USERNAME", "hamcp_test")
ONBOARDING_PASSWORD = os.environ.get("HAOS_BUILD_PASSWORD", "hamcp_test_password")
ONBOARDING_NAME = "HA-MCP CI"

# Local TCP ports the host uses to talk to the booted HAOS. Fixed because
# QEMU's hostfwd needs the port up front (no equivalent of bind-to-0-then-
# discover), and CI jobs run single-threaded so collision risk is low.
# Configurable via env var for the rare parallel-build scenario.
HA_HOST_PORT = int(os.environ.get("HAOS_BUILD_HA_PORT", "18123"))
SSH_HOST_PORT = int(os.environ.get("HAOS_BUILD_SSH_PORT", "12222"))

# OVMF firmware path varies by distribution. Default matches Ubuntu's
# ovmf package (which is what the GitHub-hosted ubuntu-22.04 runner uses).
OVMF_CODE_PATH = os.environ.get("HAOS_BUILD_OVMF", "/usr/share/OVMF/OVMF_CODE.fd")


@dataclass(frozen=True)
class Addon:
    """An addon entry to install via the Supervisor API.

    ``repo`` is the addon repository URL — ``None`` for the built-in core
    repository (Mosquitto, etc.). ``name`` is the addon's display name as it
    appears in the store and is used to discover the actual Supervisor slug
    after the repo is registered, because slug prefixes are SHA-derived from
    the repo URL and shouldn't be hardcoded. ``options`` is merged via
    ``POST /addons/<slug>/options`` after install.
    """

    repo: str | None
    name: str
    options: dict[str, Any]


# v1 addon set — see #1281 comment thread for rationale. Options are minimal
# and chosen so each addon starts cleanly without external hardware:
#   - Mosquitto: one local user for in-image MQTT traffic
#   - Node-RED: stock flows from the Home Assistant Community Add-ons repo
#   - ESPHome: dashboard-only, official addon (from esphome/home-assistant-addon)
#   - Z2M: serial port stub (no coordinator stick attached)
#   - Frigate: standard variant, no cameras configured yet
#
# Mock RTSP/MQTT feeders and companion-integration setup come in a follow-up
# commit; v1 only proves the addon lifecycle path.
ADDONS: tuple[Addon, ...] = (
    Addon(repo=None, name="Mosquitto broker",
          options={"logins": [{"username": "hamcp", "password": "hamcp"}]}),
    Addon(repo="https://github.com/hassio-addons/repository", name="Node-RED",
          options={}),
    Addon(repo="https://github.com/esphome/home-assistant-addon", name="ESPHome",
          options={}),
    Addon(repo="https://github.com/zigbee2mqtt/hassio-zigbee2mqtt", name="Zigbee2MQTT",
          options={"serial": {"port": "/dev/null"}}),
    Addon(repo="https://github.com/blakeblackshear/frigate-hass-addons", name="Frigate",
          options={}),
)

# Get HACS addon — its purpose is to bootstrap HACS into /config/custom_components/.
# Installing + starting this addon is the supported HAOS path; the older
# `wget | bash` flow only worked with the SSH addon and is no longer recommended.
GET_HACS_ADDON = Addon(
    repo="https://github.com/hacs/get",
    name="Get HACS",
    options={},
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
    form: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """JSON or form-encoded HTTP helper.

    ``body`` sends JSON; ``form`` sends application/x-www-form-urlencoded. The
    distinction matters for HA's auth endpoints — ``/auth/token`` only accepts
    form data because it parses via ``await request.post()``.
    """
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        # Surface the response body — HA's error responses are JSON with
        # useful messages and the bare HTTPError doesn't include them.
        err_body: str = ""
        try:
            err_body = e.read().decode()
        except Exception:
            pass
        LOG.error("%s %s -> HTTP %d: %s", method, url, e.code, err_body[:500])
        raise
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
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE_PATH}",
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
    """Create the first user and return a short-lived access token.

    The token only needs to live for the duration of the build session
    (addon installs, HACS bootstrap, shutdown). The canary test re-derives
    its own token at runtime by logging in with the known username/password
    via /auth/login_flow — that way no token needs to be baked into the
    pre-built qcow2.
    """
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
        # /auth/token uses await request.post() — must be form-encoded.
        # client_id passes indieauth.verify_client_id (any http://localhost
        # or http://127.0.0.1 URL is valid).
        form={
            "client_id": base_url,
            "grant_type": "authorization_code",
            "code": auth_code,
        },
    )
    return token_resp["access_token"]


def _add_repository(base_url: str, token: str, repo_url: str) -> None:
    """Register an addon repository with the Supervisor store."""
    LOG.info("Adding addon repository %s", repo_url)
    _http(
        "POST",
        f"{base_url}/api/hassio/store/repositories",
        token=token,
        body={"repository": repo_url},
        timeout=120.0,
    )


def _reload_store(base_url: str, token: str) -> None:
    """Force the Supervisor store to refresh after adding a repository."""
    _http("POST", f"{base_url}/api/hassio/store/reload", token=token, timeout=120.0)


def _discover_slug(base_url: str, token: str, addon: Addon) -> str:
    """Resolve an addon's Supervisor slug by name from the live store.

    The prefix portion of every slug is a hash of the repository URL, so it
    can't be hardcoded portably. After the repo is registered we list the
    store and match by display name.
    """
    resp = _http("GET", f"{base_url}/api/hassio/store", token=token)
    addons = resp.get("data", resp).get("addons", [])
    for entry in addons:
        if entry.get("name") == addon.name:
            if addon.repo and entry.get("url", "").startswith(addon.repo):
                return entry["slug"]
            if addon.repo is None and entry.get("repository") == "core":
                return entry["slug"]
    # Fall back to repository-scoped match if no name hit (display names
    # occasionally drift; slug stability is the real anchor).
    raise RuntimeError(f"Addon {addon.name!r} not found in store after repo refresh")


def _install_one(base_url: str, token: str, addon: Addon) -> str:
    """Install + (optionally configure) + start a single addon. Returns slug."""
    if addon.repo:
        _add_repository(base_url, token, addon.repo)
        _reload_store(base_url, token)
    slug = _discover_slug(base_url, token, addon)
    LOG.info("Installing %s (slug=%s)", addon.name, slug)
    _http(
        "POST",
        f"{base_url}/api/hassio/addons/{slug}/install",
        token=token,
        timeout=900.0,
    )
    if addon.options:
        _http(
            "POST",
            f"{base_url}/api/hassio/addons/{slug}/options",
            token=token,
            body={"options": addon.options},
            timeout=60.0,
        )
    _http(
        "POST",
        f"{base_url}/api/hassio/addons/{slug}/start",
        token=token,
        timeout=120.0,
    )
    return slug


def _check_core_auth(base_url: str, token: str) -> None:
    """Verify the access token authenticates against HA Core.

    HA's REST API has no current-user endpoint — admin status can only be
    introspected via the WebSocket ``auth/current_user`` message, which is
    overkill here. We confirm the token is parsed at the middleware level
    (/api/config returns 200) and that a generic authenticated read works
    (/api/states). If both succeed but Supervisor still 401s afterwards,
    that's the admin-or-proxy-readiness problem to debug — but at least
    we know auth itself is sound and can fail fast on it.
    """
    try:
        cfg = _http("GET", f"{base_url}/api/config", token=token, timeout=10.0)
        LOG.info("AUTH OK: /api/config version=%s state=%s", cfg.get("version"), cfg.get("state"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"HA Core rejected the access token at /api/config ({e.code}). "
            "Auth middleware did not parse the bearer token — token exchange is broken."
        ) from e
    try:
        states = _http("GET", f"{base_url}/api/states", token=token, timeout=10.0)
        LOG.info("AUTH OK: /api/states returned %d entities", len(states) if isinstance(states, list) else 0)
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"HA Core rejected the access token at /api/states ({e.code})."
        ) from e


def _wait_supervisor_ready(base_url: str, token: str) -> None:
    """Confirm the Supervisor proxy accepts our token. Single brief retry.

    Run *after* _check_core_auth has confirmed the token works at HA Core.
    HassIOView returns 401 specifically when ``request[KEY_HASS_USER].is_admin``
    is False — and auth being already verified means the only way to get
    here is an admin-status problem. Retry once after 5s in case there's
    some genuine Supervisor handshake latency, then bail with a clear error.
    """
    LOG.info("Checking Supervisor proxy (1 retry, ~5s)")
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            _http(
                "GET",
                f"{base_url}/api/hassio/supervisor/info",
                token=token,
                timeout=10.0,
            )
            LOG.info("Supervisor ready")
            return
        except urllib.error.HTTPError as e:
            last_err = e
            if attempt == 0:
                time.sleep(5.0)
    raise RuntimeError(
        f"Supervisor proxy returned {last_err} after retry. HA Core auth is "
        "confirmed working (/api/config + /api/states both succeeded), so this "
        "is almost certainly the HassIOView admin check (is_admin=False on the "
        "onboarded user). Next iteration needs a WebSocket auth/current_user "
        "call to confirm — REST has no equivalent endpoint."
    )


def install_addons(base_url: str, token: str) -> dict[str, str]:
    """Register the ha-mcp addon repo and install + configure each addon.

    Returns a mapping of addon display name → installed slug for downstream
    steps (e.g. canary tests that need to address a specific addon).
    """
    _check_core_auth(base_url, token)
    _wait_supervisor_ready(base_url, token)
    _add_repository(base_url, token, HA_MCP_ADDON_REPO)
    _reload_store(base_url, token)
    installed: dict[str, str] = {}
    for addon in ADDONS:
        installed[addon.name] = _install_one(base_url, token, addon)
    return installed


def install_hacs(base_url: str, token: str) -> None:
    """Bootstrap HACS via the Get HACS addon.

    The supported HAOS install path: register the Get HACS repo, install +
    run the addon, which writes HACS files into /config/custom_components/.
    A core restart picks up the new component; the HACS config flow then
    completes on first boot of the canary test.

    HACS-driven custom-component churn is the largest source of E2E flake
    the testcontainer suite cannot reproduce (#1281), so it must be in the
    pre-baked image rather than installed per-test-run.
    """
    LOG.info("Installing HACS via Get HACS addon")
    _install_one(base_url, token, GET_HACS_ADDON)
    # The Get HACS addon completes its work on first run and self-stops.
    # Restart HA core so the freshly-written custom_components/hacs is loaded.
    _http(
        "POST",
        f"{base_url}/api/hassio/core/restart",
        token=token,
        timeout=300.0,
    )
    _wait_http_ok(f"{base_url}/manifest.json", timeout=300.0)


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
    with output.open("wb") as f:
        _run(["xz", "-9", "-T0", "-c", str(qcow2)], stdout=f)
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
