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
import shutil
import socket
import subprocess
import sys
import tempfile
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
ONBOARDING_USER = os.environ.get("HAOS_BUILD_USERNAME", "mcp")
ONBOARDING_PASSWORD = os.environ.get("HAOS_BUILD_PASSWORD", "mcp")
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
    the repo URL and shouldn't be hardcoded.

    ``start``: whether to attempt starting the addon after install. Defaults
    to True for addons that boot cleanly with no config. Set False for ones
    whose schemas require non-trivial configuration (MQTT certs, Z2M serial
    coordinator, Frigate cameras) — they're still present in the image and
    can be configured + started by the test runner at use time.
    """

    repo: str | None
    name: str
    start: bool = True


# v1 addon set — see #1281 comment thread for rationale. Configuring each
# addon with real options (certs, serial coordinators, camera streams,
# etc.) is deferred to follow-up commits; v1 just bakes them into the image
# so tests can install + start with custom configs at runtime.
#
# Frigate stays in: it's the only one of the v1 set with the
# ingress + ingress_panel=false + webui-set + full_access shape that
# ha_manage_addon needs coverage for. Cost is real (~2 GB qcow2 footprint,
# ~80s install, extra Docker registry dependency), but the testing surface
# it covers isn't available from the other addons.
#
# start=False addons fail to start without config and would block the build:
#   - Mosquitto: schema requires require_certificate + cert paths
#   - Z2M: needs a real or mocked serial coordinator
#   - Frigate: needs at least one camera defined
ADDONS: tuple[Addon, ...] = (
    Addon(repo=None, name="Mosquitto broker", start=False),
    Addon(repo="https://github.com/hassio-addons/repository", name="Node-RED"),
    # Official ESPHome repo addon is named "ESPHome Device Builder"; match by
    # the unique part of the name so dev/beta variants don't shadow stable.
    Addon(repo="https://github.com/esphome/home-assistant-addon", name="ESPHome Device Builder"),
    Addon(repo="https://github.com/zigbee2mqtt/hassio-zigbee2mqtt",
          name="Zigbee2MQTT", start=False),
    # Frigate repo ships "Frigate", "Frigate (Full Access)", "Frigate Beta",
    # "Frigate (Full Access) Beta" — plain "Frigate" is enough for the canary;
    # full-access variant only needed if tests need to mount camera devices.
    Addon(repo="https://github.com/blakeblackshear/frigate-hass-addons",
          name="Frigate", start=False),
)

# Get HACS addon — bootstraps HACS into /config/custom_components/.
# Has to start so it can do its one-shot install before we restart HA Core.
GET_HACS_ADDON = Addon(
    repo="https://github.com/hacs/addons",
    name="Get HACS",
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


def stop_qemu(proc: subprocess.Popen[bytes], ws: HAWebSocket | None) -> None:
    """Graceful shutdown via Supervisor's WS API; fall back to SIGTERM."""
    if ws is not None:
        try:
            ws.supervisor_api("/host/shutdown", method="post", timeout=10.0)
        except Exception as e:
            LOG.warning("Supervisor shutdown call failed: %s — sending SIGTERM", e)
            proc.terminate()
    else:
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


class HAWebSocket:
    """Minimal HA WebSocket client for Supervisor API calls.

    HA's REST /api/hassio/* proxy only allows a narrow set of paths
    (PATHS_ADMIN in homeassistant/components/hassio/http.py — backups, logs,
    addon changelog/docs). Everything else — store repositories, addon
    install/options/start, supervisor info, core restart, host shutdown —
    is reachable only via the WebSocket ``supervisor/api`` command (see
    homeassistant/components/hassio/websocket_api.py:websocket_supervisor_api).
    The frontend uses the same path; this class is the build script's
    equivalent.

    Synchronous wrapper around ``websockets.sync.client`` so the existing
    procedural build flow doesn't need an asyncio rewrite.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self._ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        self._token = token
        self._ws = None  # type: ignore[var-annotated]
        self._next_id = 0

    def __enter__(self) -> HAWebSocket:
        # Imported lazily so the module still imports on systems without the
        # websockets package (e.g. local lint without the build venv).
        from websockets.sync.client import connect

        self._ws = connect(self._ws_url, open_timeout=30, close_timeout=10)
        # HA WS handshake: server sends auth_required → client sends auth →
        # server replies auth_ok or auth_invalid.
        auth_req = json.loads(self._ws.recv())
        if auth_req.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected WS handshake message: {auth_req}")
        self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        auth_resp = json.loads(self._ws.recv())
        if auth_resp.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth rejected: {auth_resp}")
        LOG.info("WS connected to %s (ha_version=%s)", self._ws_url, auth_resp.get("ha_version"))
        return self

    def __exit__(self, *_: object) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def reconnect(self) -> None:
        """Tear down the current WS and re-establish + re-auth.

        Used after /core/restart: HA Core kicks every WS connection on
        restart, so any subsequent supervisor_api call needs a fresh
        connection (the access_token survives the restart).
        """
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._next_id = 0
        self.__enter__()

    def supervisor_api(
        self,
        endpoint: str,
        method: str = "get",
        data: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Issue a supervisor/api command and return the result payload.

        Raises RuntimeError on a non-success response (HA's WS contract uses
        ``{"id": N, "type": "result", "success": false, "error": {...}}``).
        """
        assert self._ws is not None
        self._next_id += 1
        msg_id = self._next_id
        msg: dict[str, Any] = {
            "id": msg_id,
            "type": "supervisor/api",
            "endpoint": endpoint,
            "method": method,
            "timeout": timeout,
        }
        if data is not None:
            msg["data"] = data
        self._ws.send(json.dumps(msg))
        # Skip any out-of-band messages (events on subscriptions etc.) and
        # match by id.
        while True:
            resp = json.loads(self._ws.recv())
            if resp.get("id") != msg_id:
                continue
            if not resp.get("success", True):
                raise RuntimeError(f"supervisor/api {method} {endpoint} failed: {resp.get('error')}")
            return resp.get("result", {}) or {}


def _add_repository(ws: HAWebSocket, repo_url: str) -> None:
    """Register an addon repository with the Supervisor store.

    Idempotent: HAOS ships the Home Assistant Community Add-ons repo
    pre-installed, and the Supervisor returns ``"Can't add ..., already in
    the store"`` for any duplicate add. Treat that as success.
    """
    LOG.info("Adding addon repository %s", repo_url)
    try:
        ws.supervisor_api("/store/repositories", method="post", data={"repository": repo_url}, timeout=120.0)
    except RuntimeError as e:
        if "already in the store" in str(e):
            LOG.info("Repository %s already registered, continuing", repo_url)
            return
        raise


def _reload_store(ws: HAWebSocket) -> None:
    """Force the Supervisor store to refresh after adding a repository."""
    ws.supervisor_api("/store/reload", method="post", timeout=120.0)


def _discover_slug(ws: HAWebSocket, addon: Addon) -> str:
    """Resolve an addon's Supervisor slug by name from the live store.

    The prefix portion of every slug is a SHA hash of the repository URL,
    so it can't be hardcoded portably. After the repo is registered we list
    the store and match by display name. If multiple addons across repos
    share a name (rare for the v1 set), prefer the one whose ``repository``
    matches the expected source (``core`` vs not).
    """
    resp = ws.supervisor_api("/store", method="get")
    store_addons = resp.get("addons", [])
    candidates = [e for e in store_addons if e.get("name") == addon.name]
    if not candidates:
        # Log a sample so we can see what names the store is actually returning.
        sample = [{"name": e.get("name"), "slug": e.get("slug"), "repo": e.get("repository")}
                  for e in store_addons[:25]]
        LOG.error(
            "No store entry matched %r. First 25 entries (of %d total): %s",
            addon.name, len(store_addons), sample,
        )
        raise RuntimeError(f"Addon {addon.name!r} not found in store after repo refresh")
    if len(candidates) == 1:
        return candidates[0]["slug"]
    # Disambiguate by repository: addon.repo=None → core, otherwise non-core.
    for c in candidates:
        if addon.repo is None and c.get("repository") == "core":
            return c["slug"]
        if addon.repo is not None and c.get("repository") != "core":
            return c["slug"]
    return candidates[0]["slug"]


def _install_one(ws: HAWebSocket, addon: Addon) -> str:
    """Install (and optionally start) a single addon. Returns slug.

    Verified Supervisor endpoints (from home-assistant/supervisor api/__init__.py):
      - POST /store/repositories                      add a repo
      - POST /store/reload                            refresh
      - GET  /store                                   list store contents
      - POST /store/addons/{slug}/install             install an addon
      - POST /addons/{slug}/start                     start it
    Note the asymmetry: install lives under /store/addons/, start is on
    the installed-addon path /addons/.

    Options are deliberately not set here in v1 (#1281 comment thread). Many
    addon schemas have required fields (Mosquitto's require_certificate,
    Z2M's serial config, Frigate's cameras) that need realistic values; the
    test runner configures them per-test with mock streams/devices.
    """
    if addon.repo:
        _add_repository(ws, addon.repo)
        _reload_store(ws)
    slug = _discover_slug(ws, addon)
    LOG.info("Installing %s (slug=%s)", addon.name, slug)
    ws.supervisor_api(f"/store/addons/{slug}/install", method="post", timeout=900.0)
    if addon.start:
        ws.supervisor_api(f"/addons/{slug}/start", method="post", timeout=120.0)
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


def _wait_supervisor_ready(ws: HAWebSocket) -> None:
    """Confirm the Supervisor responds via the WebSocket supervisor/api path.

    A single ping is enough — by the time HA Core has accepted the WS
    handshake and our auth_ok arrived, the hassio integration has loaded
    and supervisor/api commands route correctly.
    """
    info = ws.supervisor_api("/supervisor/info", method="get", timeout=30.0)
    LOG.info("Supervisor ready: version=%s arch=%s", info.get("version"), info.get("arch"))


def bake_test_state(ws: HAWebSocket, base_url: str) -> None:
    """Install the SSH addon and scp tests/initial_test_state into /config/.

    Bakes the testcontainer's seed state into the qcow2 so the existing
    e2e suite runs against HAOS with the same data: custom components
    (ha_mcp_tools, mcp_proxy), seeded automations/scripts, .storage
    registries (devices, areas, integrations), and the pre-baked
    recorder DB. Equivalent of the testcontainer fixture's
    `shutil.copytree(initial_test_state, /config)` + `_install_custom_component()`
    steps — but routed through SSH into a live HAOS instead of a Docker
    volume mount.

    Restarts HA Core after extraction so the new config + custom
    components are picked up.
    """
    initial_state_path = Path(__file__).resolve().parent.parent / "initial_test_state"
    if not initial_state_path.exists():
        raise RuntimeError(f"initial_test_state not found at {initial_state_path}")

    LOG.info("Baking seed state into HAOS image from %s", initial_state_path)
    keydir = Path(tempfile.mkdtemp(prefix="haos-bake-ssh-"))
    privkey = keydir / "id_ed25519"
    try:
        _run([
            "ssh-keygen", "-t", "ed25519", "-f", str(privkey),
            "-N", "", "-q", "-C", "haos-build-bake",
        ])
        pubkey_str = privkey.with_suffix(".pub").read_text().strip()

        # Install the official Terminal & SSH addon (core repo, slug core_ssh).
        # Configured with our build-time key only; no password auth.
        ssh_addon = Addon(repo=None, name="Terminal & SSH", start=False)
        _install_one(ws, ssh_addon)
        ws.supervisor_api(
            "/addons/core_ssh/options",
            method="post",
            data={
                # Full set of core_ssh required options (verified against the
                # addon's schema): authorized_keys, password, apks, server.
                # Empty password = key-only auth; empty apks = no extra Alpine
                # packages.
                "options": {
                    "authorized_keys": [pubkey_str],
                    "password": "",
                    "apks": [],
                    "server": {"tcp_forwarding": False},
                },
            },
            timeout=60.0,
        )
        ws.supervisor_api("/addons/core_ssh/start", method="post", timeout=120.0)
        _wait_port(SSH_HOST_PORT, timeout=60)
        # Give sshd a moment to bind after the port is open
        time.sleep(3.0)

        ssh_base = [
            "ssh",
            "-i", str(privkey),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-p", str(SSH_HOST_PORT),
            "root@127.0.0.1",
        ]

        LOG.info("Streaming initial_test_state via tar | ssh into /config/")
        tar_proc = subprocess.Popen(
            ["tar", "-C", str(initial_state_path), "-cf", "-", "."],
            stdout=subprocess.PIPE,
        )
        ssh_proc = subprocess.Popen(
            [*ssh_base, "tar -C /config -xf - && chmod -R go+rX /config"],
            stdin=tar_proc.stdout,
        )
        if tar_proc.stdout is not None:
            tar_proc.stdout.close()  # let tar see SIGPIPE if ssh dies
        rc = ssh_proc.wait(timeout=300)
        tar_proc.wait(timeout=30)
        if rc != 0:
            raise RuntimeError(f"ssh-tar extract failed with exit {rc}")

        # Restart HA core so the new /config (custom_components especially)
        # is loaded. Same WS-close-during-restart handling as install_hacs.
        from websockets.exceptions import ConnectionClosed
        LOG.info("Restarting HA Core to apply baked seed state")
        try:
            ws.supervisor_api("/core/restart", method="post", timeout=300.0)
        except ConnectionClosed:
            LOG.info("WS closed during core restart (expected)")
        _wait_http_ok(f"{base_url}/manifest.json", timeout=300.0)
        ws.reconnect()
    finally:
        shutil.rmtree(keydir, ignore_errors=True)


def install_addons(ws: HAWebSocket) -> dict[str, str]:
    """Register the ha-mcp addon repo and install + configure each addon.

    Returns a mapping of addon display name → installed slug for downstream
    steps (e.g. canary tests that need to address a specific addon).
    """
    _wait_supervisor_ready(ws)
    _add_repository(ws, HA_MCP_ADDON_REPO)
    _reload_store(ws)
    installed: dict[str, str] = {}
    for addon in ADDONS:
        installed[addon.name] = _install_one(ws, addon)
    return installed


def install_hacs(ws: HAWebSocket, base_url: str) -> None:
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
    _install_one(ws, GET_HACS_ADDON)
    # /core/restart kicks every WebSocket connection as part of the restart,
    # so our recv() raises ConnectionClosedOK before any response arrives.
    # That's the success signal — the restart got initiated.
    LOG.info("Restarting HA Core so HACS custom component loads")
    from websockets.exceptions import ConnectionClosed

    try:
        ws.supervisor_api("/core/restart", method="post", timeout=300.0)
    except ConnectionClosed:
        LOG.info("WS closed during core restart (expected)")
    _wait_http_ok(f"{base_url}/manifest.json", timeout=300.0)
    # Reconnect for any subsequent supervisor_api calls (e.g. stop_qemu).
    ws.reconnect()


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
        _check_core_auth(base_url, token)
        with HAWebSocket(base_url, token) as ws:
            install_addons(ws)
            install_hacs(ws, base_url)
            bake_test_state(ws, base_url)
            # TODO(#1281 follow-up): integrations (ESPHome companion, Node-RED
            # companion, Local Calendar, Sun verification) and mock RTSP/MQTT
            # feeders. The canary test only needs addon lifecycle for now.
            stop_qemu(qemu, ws)
    except Exception:
        LOG.exception("Image build failed — leaving qcow2 in %s for inspection", qcow2)
        qemu.terminate()
        qemu.wait(timeout=60)
        raise
    # Skip post-build compression for now: empirically qemu-img convert -c
    # only shrinks ~7 GB → ~7 GB (Docker layer contents don't compress well
    # with zlib) but adds 9 min, and xz -9 -T0 adds >25 min. Just sparse-copy
    # the raw qcow2. Image-size optimization (zstd? strip unused docker
    # layers? slim addons?) is tracked separately as a follow-up.
    LOG.info("Copying qcow2 to %s (uncompressed)", output)
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(["cp", "--reflink=auto", str(qcow2), str(output)])
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
