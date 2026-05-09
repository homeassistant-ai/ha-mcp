#!/usr/bin/env python3
"""Webhook Proxy for HA MCP — thin proxy addon startup script.

This addon does NOT run an MCP server. It discovers a running ha-mcp addon
(stable or dev), installs a webhook custom integration into HA Core, and
proxies remote MCP requests to the addon's local MCP server.

Supports Nabu Casa, Cloudflare, DuckDNS, nginx, or any reverse proxy.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlparse


class IntegrationInstall(NamedTuple):
    """Result of `_install_integration`. NamedTuple so callers can't
    accidentally swap the two booleans."""

    first_install: bool
    version_changed: bool

if TYPE_CHECKING:
    from typing import TextIO


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(level: str, message: str, stream: TextIO | None = None) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} [{level}] {message}", file=stream, flush=True)


def log_info(message: str) -> None:
    _log("INFO", message)


def log_error(message: str) -> None:
    _log("ERROR", message, sys.stderr)


# ---------------------------------------------------------------------------
# Supervisor API helpers
# ---------------------------------------------------------------------------

# Addon slug suffixes to match, in priority order (stable before dev).
# Third-party repos get a hash prefix from Supervisor (e.g. "abc123_ha_mcp"),
# so we match by suffix rather than exact slug.
MCP_ADDON_SLUG_SUFFIXES = ["_ha_mcp", "_ha_mcp_dev"]
# Also try exact slugs for official repo installs
MCP_ADDON_EXACT_SLUGS = ["ha_mcp", "ha_mcp_dev"]


def _supervisor_get(path: str) -> dict | None:
    """GET request to the Supervisor API. Returns data dict or None."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        req = urllib.request.Request(
            f"http://supervisor{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            response_data = json.loads(resp.read())
            if not isinstance(response_data, dict):
                log_error(f"Supervisor API GET {path}: unexpected response type {type(response_data)}")
                return None
            data = response_data.get("data", {})
            return data if isinstance(data, dict) else {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log_error(f"Supervisor API GET {path}: {e}")
        return None


def _supervisor_post(path: str, data: dict) -> bool:
    """POST to the Supervisor API. Returns True on 2xx."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"http://supervisor{path}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status: int = resp.status
            return 200 <= status < 300
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        log_error(f"Supervisor API POST {path} ({type(e).__name__}): {e} — {body}")
        return False
    except (urllib.error.URLError, TimeoutError) as e:
        log_error(f"Supervisor API POST {path} ({type(e).__name__}): {e}")
        return False


def _supervisor_get_text(path: str) -> str | None:
    """GET request returning raw text (e.g. addon logs)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        req = urllib.request.Request(
            f"http://supervisor{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "text/plain",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            text: str = resp.read().decode("utf-8", errors="replace")
            return text
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        log_error(f"Supervisor API GET text {path}: {e} — {body}")
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        log_error(f"Supervisor API GET text {path}: {e}")
        return None


def _ha_core_api(method: str, path: str, data: dict | None = None) -> dict | list | None:
    """Request to HA Core API via Supervisor proxy. Returns parsed JSON."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    url = f"http://supervisor/core/api{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result: dict | list = json.loads(resp.read())
            return result
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        log_error(f"HA Core API {method} {path}: {e}")
        return None


# ---------------------------------------------------------------------------
# MCP addon auto-discovery
# ---------------------------------------------------------------------------


def _find_mcp_addon_slugs() -> list[str]:
    """List installed addons and return slugs matching ha-mcp patterns.

    The Supervisor prefixes third-party addon slugs with a hash of the
    repository URL (e.g. "abc12345_ha_mcp_dev"). We list all addons and
    match by slug suffix, prioritizing stable over dev.
    """
    data = _supervisor_get("/addons")
    if not data:
        log_info("Could not list addons from Supervisor API")
        return list(MCP_ADDON_EXACT_SLUGS)  # Fall back to exact slugs

    addons = data.get("addons", [])
    if not addons:
        return list(MCP_ADDON_EXACT_SLUGS)

    # Collect matching slugs, grouped by priority (stable first, then dev)
    matched: list[str] = []
    for suffix in MCP_ADDON_SLUG_SUFFIXES:
        for addon in addons:
            slug = addon.get("slug", "")
            if slug == suffix.lstrip("_") or slug.endswith(suffix):
                if slug not in matched:
                    matched.append(slug)

    if matched:
        log_info(f"Found MCP addon slugs: {matched}")
    else:
        log_info(f"No MCP addons found among {len(addons)} installed addons")
    return matched


def _discover_addon() -> tuple[str | None, str | None, dict | None]:
    """Find a running ha-mcp addon and return (slug, ip, info).

    Dynamically discovers addon slugs (handles repo hash prefixes),
    then tries stable before dev.
    """
    slugs = _find_mcp_addon_slugs()
    for slug in slugs:
        info = _supervisor_get(f"/addons/{slug}/info")
        if info is None:
            continue
        state = info.get("state")
        if state != "started":
            log_info(f"Addon {slug} found but not running (state={state})")
            continue
        # When the MCP addon uses host_network, the Supervisor's ip_address
        # field returns a Docker bridge IP (172.30.x.x) that's not reachable.
        # Since this proxy addon also uses host_network, use 127.0.0.1 instead.
        ip: str | None
        if info.get("host_network"):
            ip = "127.0.0.1"
            log_info(f"Addon {slug} uses host_network — using 127.0.0.1")
        else:
            ip = info.get("ip_address")
            if not ip:
                log_info(f"Addon {slug} running but no IP address")
                continue
        log_info(f"Discovered running MCP addon: {slug} at {ip}")
        return slug, ip, info
    return None, None, None


def _discover_secret_path(slug: str, info: dict) -> str | None:
    """Discover the MCP server's secret path.

    1. Check addon options for explicit secret_path
    2. Parse addon logs for 'Secret Path: /private_...' or URL containing /private_
    3. Try multiple Supervisor log endpoints (logs, logs/latest)
    """
    # Check options first
    options = info.get("options", {})
    secret: str = str(options.get("secret_path", ""))
    if secret and secret.strip():
        path = secret.strip()
        if not path.startswith("/"):
            path = "/" + path
        log_info(f"Secret path from {slug} options: {path}")
        return path

    # Try multiple log endpoints — some Supervisor versions return 500 on /logs
    log_endpoints = [
        f"/addons/{slug}/logs",
        f"/addons/{slug}/logs/latest",
        f"/addons/{slug}/logs/boots/0",
    ]
    for endpoint in log_endpoints:
        logs = _supervisor_get_text(endpoint)
        if not logs:
            continue

        # Match "Secret Path: /private_..." or URL like "http://...:/private_..."
        match = re.search(r"(/private_\S+)", logs)
        if match:
            path = match.group(1)
            # Clean trailing whitespace and ANSI escape sequences
            path = re.sub(r"(\x1b\[[0-9;]*m|\s)+$", "", path)
            log_info(f"Secret path from {slug} logs ({endpoint}): {path}")
            return path
        log_info(f"No secret path found in {endpoint} output ({len(logs)} chars)")

    log_error(f"Could not discover secret path for {slug}")
    return None


# ---------------------------------------------------------------------------
# Nabu Casa auto-detection
# ---------------------------------------------------------------------------


def get_nabu_casa_url() -> str | None:
    """Read Nabu Casa remote URL from HA cloud storage."""
    cloud_storage = Path("/config/.storage/cloud")
    try:
        if cloud_storage.exists():
            cloud_data = json.loads(cloud_storage.read_text())
            data = cloud_data.get("data", {})
            if data.get("remote_enabled"):
                domain = data.get("remote_domain")
                if domain:
                    return f"https://{domain}"
            else:
                log_info("Nabu Casa remote UI is not enabled")
    except (OSError, json.JSONDecodeError) as e:
        log_info(f"Nabu Casa cloud config not available: {e}")
    return None


# ---------------------------------------------------------------------------
# Webhook proxy setup
# ---------------------------------------------------------------------------


def _resolve_remote_url(remote_url: str) -> str | None:
    """Return the public base URL for the proxy, or None if unknown.

    User-supplied `remote_url` from the addon config wins; falls back to
    the Nabu Casa cloud URL when blank. The returned value is an absolute
    https URL with no trailing slash, or None when neither source has one
    (cloudflared/custom proxy users who haven't filled remote_url).
    """
    if remote_url and remote_url.strip():
        url = remote_url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url
    return get_nabu_casa_url()


def _regenerate_oauth_creds(data_dir: Path) -> None:
    """Wipe the persisted OAuth creds file so the next resolve generates fresh
    values. Idempotent: missing file is fine."""
    creds_file = data_dir / "oauth_creds.json"
    try:
        if creds_file.exists():
            creds_file.unlink()
            log_info("Wiped existing OAuth credentials per regenerate toggle")
    except OSError as e:
        log_error(
            f"Failed to wipe OAuth creds for regeneration "
            f"({type(e).__name__}): {e}"
        )


def _clear_regenerate_toggle(current_config: dict) -> bool:
    """Flip regenerate_oauth_creds back to false in the addon's own options
    so subsequent restarts don't keep regenerating.

    Posts the merged options dict to /addons/self/options. Returns True on
    success. Falsy return means the user has to flip it off manually.
    """
    new_options = dict(current_config)
    new_options["regenerate_oauth_creds"] = False
    return _supervisor_post("/addons/self/options", {"options": new_options})


def _resolve_oauth_creds(
    data_dir: Path, configured_id: str, configured_secret: str
) -> tuple[str, str]:
    """Return the (client_id, client_secret) pair to use for OAuth.

    Resolution order (per field, since users may rotate one without the
    other):
      1. Value supplied in the addon config (after trim).
      2. Value persisted in /data/oauth_creds.json from a prior run.
      3. Freshly generated and persisted now.

    The persisted file makes auto-generated creds stable across restarts —
    important so a Claude.ai connector keeps working after addon restart.
    Persisted values are written with 0600 permissions; the file is in the
    addon's /data volume which already has restricted access.

    Empty return tuple ("", "") signals an unrecoverable error (bad
    permissions etc); main() will refuse to start.
    """
    creds_file = data_dir / "oauth_creds.json"
    stored: dict = {}
    if creds_file.exists():
        try:
            loaded = json.loads(creds_file.read_text())
            if isinstance(loaded, dict):
                stored = loaded
        except (OSError, json.JSONDecodeError) as e:
            log_error(
                f"Could not read existing OAuth creds "
                f"({type(e).__name__}): {e}"
            )

    final_id = configured_id.strip() or stored.get("client_id", "")
    final_secret = configured_secret.strip() or stored.get("client_secret", "")

    if not final_id:
        # 16 hex bytes after the "hamcp-" prefix → 38 chars total, well past
        # the 16-char floor we enforce on client_ids
        final_id = "hamcp-" + secrets.token_hex(16)
        log_info("Generated new OAuth Client ID (no value configured or stored)")
    if not final_secret:
        # 32 random bytes → ~43 base64url chars → 256 bits of entropy
        final_secret = secrets.token_urlsafe(32)
        log_info("Generated new OAuth Client Secret")

    # Persist whatever we ended up with so the same values come back next
    # restart. Skip the write if nothing changed vs what's on disk.
    needs_write = (
        stored.get("client_id") != final_id
        or stored.get("client_secret") != final_secret
    )
    if needs_write:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            creds_file.write_text(
                json.dumps(
                    {"client_id": final_id, "client_secret": final_secret}
                )
            )
            try:
                creds_file.chmod(0o600)
            except OSError:
                pass  # tmpfs / non-POSIX filesystems may not support chmod
        except OSError as e:
            log_error(
                f"Failed to persist OAuth creds ({type(e).__name__}): {e}"
            )
            return "", ""

    return final_id, final_secret


def _get_or_create_webhook_id(data_dir: Path) -> str:
    """Get or create a persistent webhook ID."""
    wh_file = data_dir / "webhook_id.txt"
    if wh_file.exists():
        try:
            wid = wh_file.read_text().strip()
            if wid:
                return wid
        except OSError:
            pass
    wid = f"mcp_{secrets.token_hex(16)}"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        wh_file.write_text(wid)
    except OSError as e:
        log_error(f"Failed to save webhook ID: {e}")
    return wid


def _install_integration() -> IntegrationInstall:
    """Install/update the mcp_proxy custom component into HA config dir.

    `first_install` is True when the integration directory didn't exist
    (fresh setup, full HA restart required to load it).

    `version_changed` is True only when the destination manifest existed,
    was readable, AND its version differs from the source manifest. A
    missing or unreadable destination manifest does NOT trigger
    version_changed — that case still copies the new files (since we can
    no longer be sure they're current) but doesn't fire the
    "restart required" notification, which is reserved for genuine
    version bumps.
    """
    src = Path("/opt/mcp_proxy")
    dst = Path("/config/custom_components/mcp_proxy")

    if not src.exists():
        log_error("Integration source not found at /opt/mcp_proxy")
        return IntegrationInstall(False, False)

    Path("/config/custom_components").mkdir(parents=True, exist_ok=True)

    first_install = not dst.exists()
    src_manifest = src / "manifest.json"
    dst_manifest = dst / "manifest.json"

    # Determine whether to copy and whether this is a real version change.
    # Version change is a strict comparison: both manifests readable and
    # versions differ. A corrupt/missing dst manifest forces a copy (to
    # repair the install) but isn't reported as a version change.
    sv: str | None = None
    dv: str | None = None
    if src_manifest.exists():
        try:
            sv = json.loads(src_manifest.read_text()).get("version")
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Could not parse source manifest: {e}")
    if dst_manifest.exists():
        try:
            dv = json.loads(dst_manifest.read_text()).get("version")
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Could not parse destination manifest: {e}")

    versions_differ = sv is not None and dv is not None and sv != dv
    needs_update = first_install or versions_differ or dv is None
    version_changed = versions_differ and not first_install

    if needs_update:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        log_info("Installed mcp_proxy integration")
    else:
        log_info("mcp_proxy integration up to date")

    return IntegrationInstall(first_install, version_changed)


def _ensure_config_entry(retries: int = 5, delay: int = 10) -> bool:
    """Ensure a config entry exists for mcp_proxy. Creates one if missing."""
    for attempt in range(1, retries + 1):
        entries = _ha_core_api("GET", "/config/config_entries/entry")
        if entries is not None:
            for entry in entries:
                if isinstance(entry, dict) and entry.get("domain") == "mcp_proxy":
                    log_info("mcp_proxy config entry exists")
                    return True

            # Create via config flow
            log_info(f"Creating config entry (attempt {attempt}/{retries})...")
            flow = _ha_core_api(
                "POST", "/config/config_entries/flow", {"handler": "mcp_proxy"}
            )
            if flow is None:
                if attempt < retries:
                    time.sleep(delay)
                continue
            if not isinstance(flow, dict):
                continue

            rtype = flow.get("type")
            if rtype in ("abort", "create_entry"):
                log_info("Config entry ready")
                return True
            if rtype == "form" and flow.get("flow_id"):
                complete = _ha_core_api(
                    "POST", f"/config/config_entries/flow/{flow['flow_id']}", {}
                )
                if isinstance(complete, dict) and complete.get("type") == "create_entry":
                    log_info("Config entry created")
                    return True

        if attempt < retries:
            log_info(f"HA not ready, retrying in {delay}s...")
            time.sleep(delay)

    return False


def _remove_config_entry() -> None:
    """Remove the mcp_proxy config entry if it exists."""
    entries = _ha_core_api("GET", "/config/config_entries/entry")
    if entries is None:
        return
    for entry in entries:
        if isinstance(entry, dict) and entry.get("domain") == "mcp_proxy":
            eid = entry.get("entry_id")
            if eid:
                _ha_core_api("DELETE", f"/config/config_entries/entry/{eid}")
                log_info("Removed mcp_proxy config entry")


def _reload_config_entry() -> None:
    """Reload the mcp_proxy config entry so it picks up the latest config file.

    If the entry was loaded during HA boot (before this addon wrote the config),
    async_setup_entry would have found no config and skipped webhook registration.
    Reloading forces it to re-read the file.
    """
    entries = _ha_core_api("GET", "/config/config_entries/entry")
    if entries is None:
        return
    for entry in entries:
        if isinstance(entry, dict) and entry.get("domain") == "mcp_proxy":
            eid = entry.get("entry_id")
            if eid:
                result = _ha_core_api(
                    "POST", f"/config/config_entries/entry/{eid}/reload"
                )
                if result is not None:
                    log_info("Reloaded mcp_proxy config entry")
                else:
                    log_info("Config entry reload returned no response (may be OK)")
                return


# ---------------------------------------------------------------------------
# Wait for HA restart
# ---------------------------------------------------------------------------


def _ha_core_api_quiet(method: str, path: str) -> list | dict | None:
    """Like _ha_core_api but suppresses error logging (for polling loops)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    url = f"http://supervisor/core/api{path}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result: list | dict = json.loads(resp.read())
            return result
    except Exception:
        return None


def _wait_for_ha_restart(poll_interval: int = 10, timeout: int = 600) -> None:
    """Wait for the user to restart HA Core, then wait for it to come back.

    On first install, the addon keeps running while HA Core restarts.
    We poll the HA API: first wait for it to go down (or for the integration
    to appear), then wait for it to come back up with the integration loaded.
    """
    log_info("Waiting for Home Assistant to restart...")
    start = time.monotonic()

    # Phase 1: Wait for HA to go down OR for the integration to appear
    while time.monotonic() - start < timeout:
        result = _ha_core_api_quiet("GET", "/config/config_entries/entry")
        if result is None:
            log_info("HA Core is restarting...")
            break
        # Check if integration already loaded (user restarted fast)
        if isinstance(result, list):
            for entry in result:
                if isinstance(entry, dict) and entry.get("domain") == "mcp_proxy":
                    log_info("Integration already loaded — HA must have restarted")
                    return
        time.sleep(poll_interval)

    # Phase 2: Wait for HA to come back up (quietly — 502s are expected)
    while time.monotonic() - start < timeout:
        time.sleep(poll_interval)
        result = _ha_core_api_quiet("GET", "/config/config_entries/entry")
        if result is not None:
            log_info("HA Core is back up")
            return

    log_info("Timed out waiting for HA restart — continuing anyway")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def _health_check(target_url: str) -> bool:
    """Check if the MCP server is reachable via TCP connection test.

    We use a raw socket connect instead of HTTP because the MCP server's
    Streamable HTTP endpoint opens a long-lived SSE stream on GET, which
    would always time out with urllib.
    """
    try:
        parsed = urlparse(target_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9583
        with socket.create_connection((host, port), timeout=5):
            return True
    except (OSError, TimeoutError):
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    log_info("Starting Webhook Proxy for HA MCP...")

    # Read config
    config_file = Path("/data/options.json")
    data_dir = Path("/data")
    remote_url = ""
    mcp_server_url = ""
    mcp_port = 9583
    enable_oauth = False
    oauth_client_id = ""
    oauth_client_secret = ""
    regenerate_oauth_creds = False
    config: dict = {}

    if config_file.exists():
        try:
            config = json.load(config_file.open())
            remote_url = config.get("remote_url", "")
            mcp_server_url = config.get("mcp_server_url", "")
            mcp_port = config.get("mcp_port", 9583)
            enable_oauth = bool(config.get("enable_oauth", False))
            oauth_client_id = config.get("oauth_client_id", "")
            oauth_client_secret = config.get("oauth_client_secret", "")
            regenerate_oauth_creds = bool(
                config.get("regenerate_oauth_creds", False)
            )
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Failed to read config ({type(e).__name__}): {e}")

    # OAuth credential resolution: user-supplied values in the addon config
    # win. Otherwise fall back to a persisted creds file in /data, and if
    # that doesn't exist either, auto-generate. This makes the happy path
    # "toggle on, restart, copy creds from log" without the user ever having
    # to invent secret strings.
    if enable_oauth:
        if regenerate_oauth_creds:
            _regenerate_oauth_creds(data_dir)
            # Force fresh generation: ignore any user-supplied values in this
            # run since the user explicitly asked for new random ones.
            oauth_client_id = ""
            oauth_client_secret = ""
            # Flip the toggle back to off via Supervisor self-options API.
            # Failure is fatal: if we proceed with the toggle still on, the
            # next restart would regenerate AGAIN — the user's MCP client
            # would lose access on every restart with no explanation. We
            # surface a persistent_notification + return non-zero so the
            # user can't miss the manual fix.
            if not _clear_regenerate_toggle(config):
                log_error(
                    "Could not auto-clear the 'Regenerate OAuth Credentials' "
                    "toggle via the Supervisor API. Refusing to start to "
                    "avoid an infinite-regeneration loop. Flip the toggle "
                    "back to OFF manually in the addon configuration, then "
                    "start the addon again."
                )
                _ha_core_api(
                    "POST",
                    "/services/persistent_notification/create",
                    {
                        "title": (
                            "MCP Webhook Proxy: manual action required"
                        ),
                        "message": (
                            "The Webhook Proxy addon could not "
                            "automatically clear the 'Regenerate OAuth "
                            "Credentials on Next Start' toggle via the "
                            "Supervisor API. To avoid regenerating "
                            "credentials on every restart, please flip "
                            "the toggle back to OFF in the addon "
                            "configuration and start the addon again."
                        ),
                        "notification_id": "mcp_proxy_regen_stuck",
                    },
                )
                return 1

        oauth_client_id, oauth_client_secret = _resolve_oauth_creds(
            data_dir, oauth_client_id, oauth_client_secret
        )
        if not oauth_client_id or not oauth_client_secret:
            log_error(
                "Failed to resolve OAuth credentials. Check addon log "
                "for prior errors and disk permissions on /data."
            )
            return 1
        if len(oauth_client_id) < 16:
            log_error(
                f"OAuth Client ID is too short (got {len(oauth_client_id)} "
                "characters, need >= 16). Clear the field to let the addon "
                "generate one, or pick a longer string."
            )
            return 1

    # Resolve the MCP server target URL
    target_url = None

    if mcp_server_url and mcp_server_url.strip():
        target_url = mcp_server_url.strip()
        log_info(f"Using configured mcp_server_url: {target_url}")
    else:
        # Auto-discover running MCP addon
        slug, ip, info = _discover_addon()
        if slug is None:
            log_error(
                "No running MCP addon found. Install and start the "
                "'Home Assistant MCP Server' addon first, or set "
                "'mcp_server_url' manually."
            )
            return 1

        if info is None:
            log_error("Internal error: addon discovered without info dict")
            return 1
        secret_path = _discover_secret_path(slug, info)
        if secret_path is None:
            log_error(
                f"Could not discover secret path for {slug}. "
                "Set 'mcp_server_url' manually in addon config."
            )
            return 1

        target_url = f"http://{ip}:{mcp_port}{secret_path}"
        log_info(f"Auto-discovered MCP server: {target_url}")

    # Get or create webhook ID
    webhook_id = _get_or_create_webhook_id(data_dir)
    webhook_path = f"/api/webhook/{webhook_id}"

    # Write proxy config for the mcp_proxy integration. The OFF (no-OAuth)
    # path writes exactly the same two keys that v1.0.2 wrote — no extra
    # fields, no shape change. The new keys (`public_base_url`, `oauth`)
    # are only added when OAuth is enabled, so existing users who don't
    # turn on OAuth see no change to their config file.
    proxy_config: dict = {"target_url": target_url, "webhook_id": webhook_id}
    resolved_remote: str | None = None
    if enable_oauth:
        # Resolve the remote URL so the OAuth provider can pin metadata
        # URLs to the operator-configured public URL instead of trusting
        # attacker-supplied Host headers on a per-request basis.
        resolved_remote = _resolve_remote_url(remote_url)
        if resolved_remote:
            proxy_config["public_base_url"] = resolved_remote
        proxy_config["oauth"] = {
            "client_id": oauth_client_id,
            "client_secret": oauth_client_secret,
        }
    proxy_config_file = Path("/config/.mcp_proxy_config.json")
    try:
        proxy_config_file.write_text(json.dumps(proxy_config))
    except OSError as e:
        log_error(f"Failed to write proxy config: {e}")
        return 1

    # Install the mcp_proxy custom component
    first_install, version_changed = _install_integration()

    if version_changed:
        # Existing user updating to a new addon version. The integration
        # files on disk were just refreshed but Python won't pick up the
        # new module code without an HA restart — `reload_config_entry`
        # only re-runs `async_setup_entry`, it doesn't re-import the
        # module. Surface a notification so the user knows.
        log_info("")
        log_info("*" * 60)
        log_info("  INTEGRATION UPDATED — restart Home Assistant to load")
        log_info("  the new mcp_proxy code. The addon will keep running")
        log_info("  with the previous version's code until then.")
        log_info("*" * 60)
        log_info("")
        _ha_core_api(
            "POST",
            "/services/persistent_notification/create",
            {
                "title": "MCP Webhook Proxy: Restart Required",
                "message": (
                    "The MCP Webhook Proxy integration was updated to a "
                    "new version. Please restart Home Assistant "
                    "(**Settings → System → Restart**) so the new code "
                    "takes effect. The webhook keeps working in the "
                    "meantime with the previous version."
                ),
                "notification_id": "mcp_proxy_update",
            },
        )

    if first_install:
        log_info("First install detected — HA restart required to load integration")
        _ha_core_api(
            "POST",
            "/services/persistent_notification/create",
            {
                "title": "MCP Webhook Proxy: Restart Required",
                "message": (
                    "The MCP Webhook Proxy integration was installed. "
                    "Please restart Home Assistant to complete setup. "
                    "Go to **Settings → System → Restart**. "
                    "The proxy will finish setup automatically after restart."
                ),
                "notification_id": "mcp_proxy_restart",
            },
        )
        log_info("")
        log_info("*" * 60)
        log_info("  RESTART HOME ASSISTANT to complete setup.")
        log_info("  A notification has been created in the HA UI.")
        log_info("  (Settings > System > Restart)")
        log_info("  The proxy will finish setup automatically.")
        log_info("*" * 60)
        log_info("")
        # Wait for HA to restart and come back, then finish setup.
        # The addon keeps running during an HA Core restart.
        _wait_for_ha_restart()
        if not _ensure_config_entry():
            log_info(
                "Could not create config entry after HA restart — "
                "try restarting Home Assistant again."
            )
        else:
            _reload_config_entry()
            _ha_core_api(
                "POST",
                "/services/persistent_notification/dismiss",
                {"notification_id": "mcp_proxy_restart"},
            )
            log_info("Setup completed after HA restart")
    else:
        if not _ensure_config_entry():
            log_info(
                "Could not create config entry — "
                "try restarting Home Assistant if this persists."
            )
        else:
            # Reload the config entry so the integration reads the fresh
            # config file we just wrote (it may have loaded with stale data
            # during HA boot, before this addon started).
            _reload_config_entry()
            # Dismiss any leftover restart notification from first install
            _ha_core_api(
                "POST",
                "/services/persistent_notification/dismiss",
                {"notification_id": "mcp_proxy_restart"},
            )

    # Log URLs. resolved_remote may already have been computed above for
    # the OAuth path; fall back to the same lookup for the no-OAuth path
    # so the log line still shows the right URL when OAuth is off.
    if resolved_remote is None:
        resolved_remote = _resolve_remote_url(remote_url)
    log_info("")
    log_info("=" * 70)
    log_info(f"  MCP target (local): {target_url}")
    log_info("")
    if resolved_remote:
        log_info(f"  MCP Server URL (remote): {resolved_remote}{webhook_path}")
    else:
        log_info(f"  MCP Server URL (remote): https://<your-external-url>{webhook_path}")
        log_info("    Set 'remote_url' in addon config, or enable Nabu Casa")
    log_info("")
    log_info("  Copy the remote URL above into your MCP client.")
    if enable_oauth:
        log_info("")
        log_info("  OAuth (Beta) is ENABLED for this URL.")
        log_info(f"    OAuth Client ID:     {oauth_client_id}")
        log_info(f"    OAuth Client Secret: {oauth_client_secret}")
        log_info(
            "    Paste both into the OAuth fields of your MCP client's"
        )
        log_info(
            "    connector setup (Claude.ai: connector → Advanced settings)."
        )
        log_info(
            "    These values persist at /data/oauth_creds.json — same"
        )
        log_info(
            "    values across addon restarts."
        )
    log_info("=" * 70)
    log_info("")

    # Keep-alive loop with periodic health check
    log_info("Entering keep-alive loop (health check every 60s)...")
    consecutive_failures = 0
    while True:
        try:
            time.sleep(60)
        except KeyboardInterrupt:
            log_info("Shutting down...")
            break

        if _health_check(target_url):
            if consecutive_failures > 0:
                log_info("MCP server is reachable again")
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures == 1:
                log_error(f"MCP server unreachable: {target_url}")
            elif consecutive_failures % 5 == 0:
                log_error(
                    f"MCP server still unreachable after "
                    f"{consecutive_failures} checks"
                )

    # Cleanup on stop: remove config entry to unregister the webhook (stops
    # proxying), but keep the config file and custom component files so the
    # next start doesn't require an HA restart. The webhook_id persists in
    # /data/webhook_id.txt so the URL stays the same across stop/start.
    #
    # On full uninstall, the user may need to manually remove
    # /config/custom_components/mcp_proxy/ and
    # /config/.mcp_proxy_config.json, then restart HA.
    _remove_config_entry()
    log_info("Webhook proxy stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
