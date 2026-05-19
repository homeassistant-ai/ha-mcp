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

# OVMF firmware path varies by distribution. Default matches the
# Debian/Ubuntu ``ovmf`` package layout, which is what the GitHub-hosted
# runner image used by build-haos-test-image.yml provides. Override via
# HAOS_BUILD_OVMF on other distros (Fedora ships it under /usr/share/edk2,
# Arch under /usr/share/edk2-ovmf).
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
# Explicit ``start=True`` for visual symmetry with the ADDONS tuple
# entries; matches Addon's field default but reads more obviously.
GET_HACS_ADDON = Addon(
    repo="https://github.com/hacs/addons",
    name="Get HACS",
    start=True,
)

# Advanced SSH & Web Terminal — used by the inaddon CI tier for network
# diagnostics (#1349 item 7 debugging). The official ``core_ssh`` addon
# wants port 22 which conflicts with HAOS's host SSHD; the community
# ``Advanced SSH & Web Terminal`` addon defaults to its OWN port (22222)
# and accepts password auth, so we can SSH from the CI runner into HAOS
# to dump nftables rules, curl localhost:9583 from inside, etc. when
# the addon's MCP port isn't reachable from outside HAOS.
ADVANCED_SSH_ADDON = Addon(
    repo="https://github.com/hassio-addons/repository",
    name="Advanced SSH & Web Terminal",
    start=False,  # configured + started by ``install_advanced_ssh`` below
)

HA_MCP_ADDON_REPO = "https://github.com/homeassistant-ai/ha-mcp"

# Dev-channel ha-mcp addon baked into the qcow2 from local source for the
# inaddon HAOS E2E tier (#1349 item 7). The dev addon's config.yaml lives at
# ``homeassistant-addon-dev/`` in the repo; we stage it under
# ``/supervisor/addons/local/ha_mcp_dev/`` inside the qcow2 so Supervisor picks it up as
# a local addon (slug: ``local_<config-slug>`` → ``local_ha_mcp_dev``).
#
# The secret_path option must be set deterministically so the test harness
# can construct the addon's MCP URL without round-tripping Supervisor. Must
# start with ``/`` and contain only URL-safe chars (see _is_valid_secret_path
# in homeassistant-addon/start.py).
HA_MCP_DEV_ADDON_SLUG = "local_ha_mcp_dev"
HA_MCP_TEST_SECRET_PATH = "/mcp_e2e_test_path"
# Advanced SSH addon user/password set at install time so the runtime
# helper (``haos_runtime.ssh_exec``) can authenticate non-interactively.
# CI-test-only credential — overridable via env so the value never has
# to live in source for a deployable image. Must stay in sync with
# ``haos_runtime.SSH_ADDON_USER`` / ``SSH_ADDON_PASSWORD``.
SSH_ADDON_USER = os.environ.get("HAOS_TEST_SSH_USER", "root")
SSH_ADDON_PASSWORD = os.environ.get("HAOS_TEST_SSH_PASSWORD", "haosdebug")


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
        except (OSError, UnicodeDecodeError):
            # Closed socket or non-utf8 body — best-effort only; never hide
            # the original HTTPError, which is re-raised below.
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
            # %r so the exception type is visible — bare %s loses it for
            # most exception subclasses and a future maintainer reading
            # this in CI logs needs to know whether it was a timeout, a
            # WS protocol error, or a Supervisor 5xx.
            LOG.warning("Supervisor shutdown call failed: %r — sending SIGTERM", e)
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
            except (OSError, RuntimeError) as e:
                LOG.debug("WS close error (already-closed or transport): %r", e)

    def reconnect(self) -> None:
        """Tear down the current WS and re-establish + re-auth.

        Used after /core/restart: HA Core kicks every WS connection on
        restart, so any subsequent supervisor_api call needs a fresh
        connection (the access_token survives the restart).
        """
        if self._ws is not None:
            try:
                self._ws.close()
            except (OSError, RuntimeError) as e:
                LOG.debug("WS close error during reconnect: %r", e)
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


# Per-addon post-install option overrides. Two situations need the bake
# to set options (rather than letting Supervisor use config.yaml defaults):
#
# 1. ``start=True`` addons whose default options would refuse to start —
#    e.g. Node-RED's addon defaults to ``ssl: true`` (verified live:
#    hassio-addons/addon-node-red/node-red/config.yaml) but ships no
#    cert, so a default-options install crashes the addon in a death
#    loop. Lifecycle tests against such an addon would see only s6-rc
#    startup spam instead of real runtime logs. Set ``ssl: false`` so
#    the addon boots cleanly.
#
# 2. ``start=False`` addons that we want to STAY stopped. Without
#    ``boot: manual`` + ``watchdog: false`` Supervisor's watchdog
#    auto-restarts them after the initial crash, racing the test
#    runner's "addon should be stopped" assertions. Explicitly setting
#    these makes the bake's start-state stable across boots.
_ADDON_OPTION_OVERRIDES: dict[str, dict[str, Any]] = {
    "Node-RED": {
        "options": {"ssl": False},
    },
    "Mosquitto broker": {
        "boot": "manual",
        "watchdog": False,
    },
    "Zigbee2MQTT": {
        "boot": "manual",
        "watchdog": False,
    },
    "Frigate": {
        "boot": "manual",
        "watchdog": False,
    },
}


def _install_one(ws: HAWebSocket, addon: Addon) -> str:
    """Install (and optionally start) a single addon. Returns slug.

    Verified Supervisor endpoints (from home-assistant/supervisor api/__init__.py):
      - POST /store/repositories                      add a repo
      - POST /store/reload                            refresh
      - GET  /store                                   list store contents
      - POST /store/addons/{slug}/install             install an addon
      - POST /addons/{slug}/options                   set addon options
      - POST /addons/{slug}/start                     start it
    Note the asymmetry: install lives under /store/addons/, options +
    start are on the installed-addon path /addons/.

    Per-addon option overrides live in ``_ADDON_OPTION_OVERRIDES`` —
    they fix addons whose config.yaml defaults are incompatible with
    starting fresh (Node-RED's ssl=true), or whose default ``boot``/
    ``watchdog`` settings would have Supervisor auto-restart a
    ``start=False`` addon (Mosquitto, Z2M, Frigate). Other addons get
    Supervisor's schema-default options.
    """
    if addon.repo:
        _add_repository(ws, addon.repo)
        _reload_store(ws)
    slug = _discover_slug(ws, addon)
    LOG.info("Installing %s (slug=%s)", addon.name, slug)
    ws.supervisor_api(f"/store/addons/{slug}/install", method="post", timeout=900.0)

    overrides = _ADDON_OPTION_OVERRIDES.get(addon.name)
    if overrides:
        # Supervisor's POST /addons/{slug}/options behaviour:
        #
        # - ``options`` is a FULL-REPLACE field; the value must satisfy
        #   the addon's config schema in its entirety. Sending a partial
        #   options payload drops every other required field and
        #   Supervisor rejects with e.g. "Missing option 'http_static'
        #   in root" (verified on PR #1375 CI run 29357350 for Node-RED).
        # - Top-level fields like ``boot``, ``watchdog``,
        #   ``auto_update`` are PARTIAL updates — only the keys present
        #   in the POST are touched. So a ``boot:manual /
        #   watchdog:false`` override doesn't need to include
        #   ``options`` in the same POST.
        #
        # Strategy: when overrides include an ``options`` block, GET
        # the addon's current options, merge our override on top, and
        # send the merged whole. When overrides only touch top-level
        # fields, skip the GET and POST just those fields.
        merged: dict[str, Any] = {
            k: v for k, v in overrides.items() if k != "options"
        }
        if "options" in overrides:
            current = ws.supervisor_api(
                f"/addons/{slug}/info", method="get", timeout=30.0
            )
            current_options = current.get("options") or {}
            merged["options"] = {**current_options, **overrides["options"]}

        LOG.info(
            "Applying option overrides to %s (slug=%s): %s",
            addon.name,
            slug,
            overrides,
        )
        ws.supervisor_api(
            f"/addons/{slug}/options",
            method="post",
            data=merged,
            timeout=60.0,
        )

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


def stage_dev_addon_source(qcow2: Path) -> None:
    """Bake the ha-mcp dev addon's source into the qcow2 under /supervisor/addons/local/.

    Runs BEFORE first start_qemu so HAOS boots with the local addon visible
    to Supervisor in the store. The bake then installs + builds the addon
    while HAOS is running, which means the cached qcow2 ships with the addon
    Docker image already built — every subsequent CI run only pays the cost
    of an ``addons/{slug}/update`` (Docker layer cache hit, ~20-30s) instead
    of a full first-install (~5 min).

    The dev addon's Dockerfile expects ``start.py``, ``pyproject.toml``,
    ``uv.lock``, and ``src/`` at the build-context root — same shape as the
    addon-repo-branch flow used for manual fork testing (see
    ``~/ha-mcp-fork/FORK-DEV.md``). We mirror that prep here so the
    in-HAOS build succeeds without any additional setup at install time.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    dev_addon_src = repo_root / "homeassistant-addon-dev"
    if not dev_addon_src.exists():
        raise RuntimeError(
            f"homeassistant-addon-dev not found at {dev_addon_src} — "
            f"checkout is incomplete; the image cannot be built."
        )

    LOG.info("Staging ha-mcp dev addon source into qcow2 /supervisor/addons/local/ha_mcp_dev/")
    workdir = Path(tempfile.mkdtemp(prefix="haos-dev-addon-"))
    try:
        staging = workdir / "ha_mcp_dev"
        shutil.copytree(dev_addon_src, staging)

        # Files outside the addon dir that the Dockerfile COPYs from.
        # Mirrors the addon-repo-branch manual steps.
        shutil.copy(repo_root / "homeassistant-addon" / "start.py", staging / "start.py")
        shutil.copy(repo_root / "pyproject.toml", staging / "pyproject.toml")
        shutil.copy(repo_root / "uv.lock", staging / "uv.lock")
        # src/ha_mcp: nuke + copy fresh so a stale tree (e.g. left over from
        # a prior local run) doesn't shadow the current version.
        addon_src_dir = staging / "src"
        if addon_src_dir.exists():
            shutil.rmtree(addon_src_dir)
        addon_src_dir.mkdir()
        shutil.copytree(repo_root / "src" / "ha_mcp", addon_src_dir / "ha_mcp")

        # Dockerfile in homeassistant-addon-dev/ uses
        # ``COPY homeassistant-addon/start.py /`` because it's authored to
        # be built from the repo root context. Inside /supervisor/addons/local/ the
        # build context is the addon dir itself, so the path needs to be
        # ``COPY start.py /``. Same patch the FORK-DEV.md flow applies.
        dockerfile = staging / "Dockerfile"
        original = dockerfile.read_text()
        patched = original.replace(
            "COPY homeassistant-addon/start.py /",
            "COPY start.py /",
        )
        if patched == original:
            # Fail fast — silently writing the unpatched Dockerfile would
            # cause an opaque addon-build failure 5+ min later during
            # ``addons/{slug}/install``. Better to point at the patch line
            # directly.
            raise RuntimeError(
                f"Dockerfile patch failed: expected line "
                f"'COPY homeassistant-addon/start.py /' not found in "
                f"{dockerfile}. The dev addon's Dockerfile may have been "
                f"restructured; update the patch in stage_dev_addon_source "
                f"to match the new shape."
            )
        dockerfile.write_text(patched)

        # Strip the ``image:`` field from config.yaml. Production dev-addon
        # ships built images at ghcr.io/homeassistant-ai/ha-mcp-addon-dev-{arch};
        # when Supervisor sees ``image:``, it tries to PULL from GHCR rather
        # than build from the local Dockerfile. Per-PR version bumps produce
        # tags that don't exist in GHCR → 404 → addon update fails.
        # Removing the field forces Supervisor to build locally from the
        # Dockerfile it sees in /supervisor/addons/local/ha_mcp_dev/.
        config_yaml = staging / "config.yaml"
        config_lines = [
            ln for ln in config_yaml.read_text().splitlines(keepends=True)
            if not ln.startswith("image:")
        ]
        config_yaml.write_text("".join(config_lines))
        # Verify the strip: a future restructure that indents the field
        # under a parent key would make the line-prefix filter a no-op,
        # silently re-introducing GHCR-pull behavior.
        post_strip = config_yaml.read_text()
        if "\nimage:" in post_strip or post_strip.startswith("image:"):
            raise RuntimeError(
                f"config.yaml ``image:`` strip did not remove the field "
                f"from {config_yaml}; Supervisor will pull from GHCR and "
                f"the per-PR version bump will 404. The field may now be "
                f"indented under a parent — update the filter accordingly."
            )

        # tar root-owned, root-mode files into /supervisor/addons/local/ on the qcow2's
        # hassos-data partition. Same approach as bake_test_state's seed-tar.
        seed_tar = workdir / "ha_mcp_dev.tar"
        _run([
            "tar", "--numeric-owner", "--owner=0", "--group=0",
            "-C", str(workdir), "-cf", str(seed_tar), "ha_mcp_dev",
        ])
        _run([
            "guestfish",
            "--rw",
            "-a", str(qcow2),
            "run",
            ":",
            "mount", "/dev/sda8", "/",
            ":",
            "mkdir-p", "/supervisor/addons/local",
            ":",
            "tar-in", str(seed_tar), "/supervisor/addons/local",
        ])
        LOG.info("Dev addon source staged at /supervisor/addons/local/ha_mcp_dev/")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def install_advanced_ssh(ws: HAWebSocket) -> str:
    """Install + configure Advanced SSH & Web Terminal for CI diagnostics.

    Sets a known root password ("haosdebug") so the CI workflow can
    SSH in unattended. Configures listen port 22222 (avoids HAOS host
    SSHD on 22) — the QEMU hostfwd in haos_runtime.py exposes this on
    127.0.0.1:22222 on the runner.
    """
    slug = _discover_slug(ws, ADVANCED_SSH_ADDON)
    LOG.info("Installing Advanced SSH (slug=%s) for inaddon CI diagnostics", slug)
    ws.supervisor_api(f"/store/addons/{slug}/install", method="post", timeout=600.0)
    # Schema: ssh.username, ssh.password, ssh.authorized_keys (list),
    # ssh.sftp (bool), ssh.compatibility_mode (bool); top-level:
    # apks (list), packages (list), init_commands (list)
    ws.supervisor_api(
        f"/addons/{slug}/options",
        method="post",
        data={
            "options": {
                "ssh": {
                    "username": SSH_ADDON_USER,
                    "password": SSH_ADDON_PASSWORD,
                    "authorized_keys": [],
                    "sftp": False,
                    "compatibility_mode": False,
                    "allow_agent_forwarding": False,
                    "allow_remote_port_forwarding": False,
                    "allow_tcp_forwarding": False,
                },
                "zsh": True,
                "share_sessions": False,
                "packages": [],
                "init_commands": [],
            },
            "boot": "auto",
        },
        timeout=60.0,
    )
    ws.supervisor_api(f"/addons/{slug}/start", method="post", timeout=120.0)
    LOG.info("Advanced SSH installed + started on port 22222 (user=root)")
    return slug


def install_ha_mcp_dev_addon(ws: HAWebSocket) -> str:
    """Install the local ha-mcp dev addon during the bake's running phase.

    Assumes ``stage_dev_addon_source`` ran before start_qemu so the source
    is already at /supervisor/addons/local/ha_mcp_dev/. Supervisor's local store
    scanner picks up the addon automatically on boot; we reload to be
    explicit, install (which builds the Docker image — slow, ~5 min, but
    only paid once per cache lifetime), set options including a
    deterministic secret_path so test harness can construct the MCP URL,
    and start the addon container.

    Returns the installed slug (``local_ha_mcp_dev``).
    """
    _reload_store(ws)
    slug = HA_MCP_DEV_ADDON_SLUG
    LOG.info("Installing ha-mcp dev addon (slug=%s) — building Docker image...", slug)
    # 900s install timeout matches the existing install_addons flow and
    # covers the worst-case from-scratch uv sync + image build.
    ws.supervisor_api(f"/store/addons/{slug}/install", method="post", timeout=900.0)

    # Pre-set every dev-channel flag the test suite relies on so the addon
    # exposes the full tool surface (mirrors the env-var setup in conftest's
    # external-HAOS branch). The schema in homeassistant-addon-dev/config.yaml
    # lists every flag we toggle here.
    LOG.info("Setting ha-mcp dev addon options (preset secret_path + all dev flags on)")
    # Supervisor's options POST replaces the full options dict, so we must
    # include every field with a non-optional schema entry in the dev addon's
    # homeassistant-addon-dev/config.yaml. Verified live: omitting
    # ``backup_hint`` returns "Missing option 'backup_hint' in root".
    ws.supervisor_api(
        f"/addons/{slug}/options",
        method="post",
        data={
            "options": {
                "backup_hint": "normal",
                "secret_path": HA_MCP_TEST_SECRET_PATH,
                "enable_tool_search": False,
                "enable_yaml_config_editing": True,
                "enable_code_mode": True,
                "enable_lite_docstrings": False,
                "enable_filesystem_tools": True,
                "enable_custom_component_integration": True,
                "tool_search_max_results": 5,
                "disabled_tools": "",
                "pinned_tools": "",
                "verify_ssl": True,
                "advanced_debug_logging": True,
            },
            "boot": "auto",
        },
        timeout=60.0,
    )
    LOG.info("Starting ha-mcp dev addon")
    ws.supervisor_api(f"/addons/{slug}/start", method="post", timeout=120.0)
    LOG.info("ha-mcp dev addon installed + started; slug=%s", slug)
    return slug


def _wait_supervisor_ready(ws: HAWebSocket) -> None:
    """Confirm the Supervisor responds via the WebSocket supervisor/api path.

    A single ping is enough — by the time HA Core has accepted the WS
    handshake and our auth_ok arrived, the hassio integration has loaded
    and supervisor/api commands route correctly.
    """
    info = ws.supervisor_api("/supervisor/info", method="get", timeout=30.0)
    LOG.info("Supervisor ready: version=%s arch=%s", info.get("version"), info.get("arch"))


def bake_test_state(qcow2: Path) -> None:
    """Inject tests/initial_test_state into the qcow2 via libguestfs.

    Runs *after* HAOS has been shut down so the qcow2 isn't in use. Uses
    guestfish to mount the HAOS data partition (/dev/sda8) and tar-in
    initial_test_state into /supervisor/homeassistant/.

    Also stages the in-repo ha_mcp_tools + mcp_proxy custom components and
    their config entries — the testcontainer dispatch installs them
    dynamically via _install_custom_component, but on HAOS we bake them
    directly into the image at this step so HA Core finds them on first
    boot.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    tests_dir = repo_root / "tests"
    initial_state_path = tests_dir / "initial_test_state"
    if not initial_state_path.exists():
        raise RuntimeError(f"initial_test_state not found at {initial_state_path}")

    LOG.info("Baking seed state into qcow2 via libguestfs from %s", initial_state_path)
    workdir = Path(tempfile.mkdtemp(prefix="haos-bake-"))
    try:
        # Stage the seed under a temp dir so we can normalise the recorder
        # DB and inject custom components before tarring (see below).
        staging = workdir / "config"
        shutil.copytree(initial_state_path, staging)

        # Inject custom components matched to what the testcontainer fixture
        # installs via _install_custom_component in tests/src/e2e/conftest.py.
        # Both are config_flow-only integrations, so HA won't pick them up
        # from YAML — a synthetic entry in .storage/core.config_entries is
        # how HA Core learns to set them up on boot.
        cc_dir = staging / "custom_components"
        cc_dir.mkdir(exist_ok=True)
        for src_rel, domain, title in (
            ("custom_components/ha_mcp_tools", "ha_mcp_tools", "HA MCP Tools"),
            (
                "homeassistant-addon-webhook-proxy/mcp_proxy",
                "mcp_proxy",
                "MCP Webhook Proxy",
            ),
        ):
            src = repo_root / src_rel
            if not src.exists():
                # Fail closed: a missing source tree means the build is
                # fundamentally wrong, not a transient skip. Without this
                # the image ships without the component baked in and the
                # downstream "COMPONENT_NOT_INSTALLED" test failures point
                # back to this step opaquely.
                raise RuntimeError(
                    f"Custom component source missing: {src} — checkout is "
                    f"incomplete; the image cannot be built."
                )
            dest = cc_dir / domain
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            LOG.info("Staged custom component %s ← %s", domain, src_rel)

            # Inject a config entry so HA loads the integration on boot.
            # Shape matches the testcontainer path in conftest.py:
            # _install_custom_component (entry_id, source=import, version=1).
            ce_path = staging / ".storage" / "core.config_entries"
            ce_data = json.loads(ce_path.read_text())
            # Shape guard: silently creating ``data.entries`` on a malformed
            # file would near-empty-wipe core.config_entries on the next
            # write — losing the seed integrations (HACS, demo, etc.).
            # If the storage schema ever bumps and breaks this expectation,
            # fail loudly so we update the bake instead of shipping a
            # broken image.
            if not isinstance(ce_data, dict) or not isinstance(
                ce_data.get("data"), dict
            ) or not isinstance(ce_data["data"].get("entries"), list):
                raise RuntimeError(
                    f"core.config_entries at {ce_path} has unexpected shape "
                    f"— expected dict with data.entries list; HA storage "
                    f"schema may have bumped. Bake script must be updated "
                    f"before continuing."
                )
            entries = ce_data["data"]["entries"]
            if not any(e.get("domain") == domain for e in entries):
                entries.append({
                    "created_at": "2025-09-07T23:56:28.040744+00:00",
                    "data": {},
                    "disabled_by": None,
                    "discovery_keys": {},
                    "domain": domain,
                    "entry_id": f"e2e_test_{domain}_entry",
                    "minor_version": 1,
                    "modified_at": "2025-09-07T23:56:28.040747+00:00",
                    "options": {},
                    "pref_disable_new_entities": False,
                    "pref_disable_polling": False,
                    "source": "import",
                    "subentries": [],
                    "title": title,
                    "unique_id": domain,
                    "version": 1,
                })
                ce_path.write_text(json.dumps(ce_data, indent=2))
                LOG.info("Injected config entry for %s", domain)

        # mcp_proxy reads target_url + webhook_id from this file on setup —
        # the testcontainer dispatch writes the same JSON before container
        # start. Tests assert the webhook_id matches.
        (staging / ".mcp_proxy_config.json").write_text(json.dumps({
            "target_url": "http://localhost:8123/api/",
            "webhook_id": "mcp_e2e_test_webhook_proxy",
        }))

        # Recorder DB normalisation. initial_test_state ships
        # home-assistant_v2.db in WAL journal mode but WITHOUT the
        # companion .wal/.shm files — when HAOS opens it, SQLite finds
        # the main DB inconsistent (last shutdown didn't checkpoint) and
        # logs "database disk image is malformed", which crashes the
        # recorder executor. VACUUM INTO a new file produces a single-
        # file, journal-mode, fully consistent DB with the same data —
        # no WAL dependency.
        db_src = staging / "home-assistant_v2.db"
        if db_src.exists():
            import sqlite3

            vacuumed = workdir / "home-assistant_v2.db"
            con = sqlite3.connect(str(db_src))
            try:
                con.execute(f"VACUUM INTO '{vacuumed}'")
            finally:
                con.close()
            shutil.move(str(vacuumed), str(db_src))
            LOG.info("Vacuumed recorder DB → %s (size %d B)", db_src, db_src.stat().st_size)

        seed_tar = workdir / "seed.tar"
        # --owner=0 --group=0 + --numeric-owner forces the archived files
        # to root:root regardless of the source UID on the build runner
        # (would otherwise be `runner:docker` on GitHub-hosted boxes).
        # HAOS's HA Core container expects /config files to be root-owned
        # so its homeassistant user can read them via the volume mount.
        _run([
            "tar", "--numeric-owner", "--owner=0", "--group=0",
            "-C", str(staging), "-cf", str(seed_tar), ".",
        ])

        # HAOS qcow2 has multiple partitions. The hassos-data partition
        # (usually /dev/sda8) holds /supervisor/homeassistant which HA Core
        # sees as /config. The -i inspector mounts the WRONG partition (the
        # system overlay) for our purpose, so manually find the data
        # partition by its filesystem label.
        # First probe: list filesystems + labels so we can debug if needed.
        probe = subprocess.run(
            ["guestfish", "--ro", "-a", str(qcow2), "run", ":", "list-filesystems"],
            capture_output=True, text=True, timeout=120,
        )
        LOG.info("guestfish filesystems on qcow2:\n%s", probe.stdout)
        if probe.returncode != 0:
            # Fail closed: continuing to the write step on the same qcow2 when
            # libguestfs itself is broken would fail opaquely (mount errors
            # without context). Surface the probe stderr now.
            raise RuntimeError(
                f"guestfish list-filesystems failed (rc={probe.returncode}): "
                f"{probe.stderr}"
            )
        # Now do the actual write. Mount data partition by label "hassos-data"
        # which HAOS sets at OS install time (stable across HAOS versions).
        # tar-in preserves the source files' permissions (644/755 as
        # checked out from git), so no separate chmod step is needed —
        # which is good because guestfish has no recursive chmod builtin
        # (`chmod-r` is not a valid command; only single-target `chmod`).
        _run([
            "guestfish",
            "--rw",
            "-a", str(qcow2),
            "run",
            ":",
            "mount", "/dev/sda8", "/",
            ":",
            "tar-in", str(seed_tar), "/supervisor/homeassistant",
        ])
        LOG.info("Bake complete")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
    # Stage the ha-mcp dev addon source into /supervisor/addons/local/ BEFORE first
    # boot so Supervisor's local-store scanner picks it up during the
    # running phase below. install_ha_mcp_dev_addon then builds the addon's
    # Docker image while HAOS is up — that built image stays in the cached
    # qcow2 so subsequent CI runs only need a quick ``addons/{slug}/update``.
    stage_dev_addon_source(qcow2)
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
            install_ha_mcp_dev_addon(ws)
            install_advanced_ssh(ws)
            # TODO(#1281 follow-up): integrations (ESPHome companion, Node-RED
            # companion, Local Calendar, Sun verification) and mock RTSP/MQTT
            # feeders. The canary test only needs addon lifecycle for now.
            stop_qemu(qemu, ws)
    except Exception:
        LOG.exception("Image build failed — leaving qcow2 in %s for inspection", qcow2)
        # Defensive: if Popen returned before exec (binary missing, OOM)
        # qemu.poll() will already be non-None and terminate() raises
        # ProcessLookupError. Guard the teardown so it never masks the
        # original build exception we're about to re-raise.
        if qemu.poll() is None:
            try:
                qemu.terminate()
                qemu.wait(timeout=60)
            except (ProcessLookupError, subprocess.TimeoutExpired) as e:
                LOG.warning("QEMU teardown after build failure: %r", e)
        raise

    # HAOS is shut down — safe to open the qcow2 with libguestfs and bake
    # the testcontainer's seed state into /config/ for the e2e suite.
    bake_test_state(qcow2)
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
