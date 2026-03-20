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
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

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
            return cast(dict, json.loads(resp.read()).get("data", {}))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log_error(f"Supervisor API GET {path}: {e}")
        return None


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
            return cast(str, resp.read().decode("utf-8", errors="replace"))
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


def _install_integration() -> bool:
    """Install/update the mcp_proxy custom component into HA config dir.

    Returns True if this is a first install (HA restart required).
    """
    src = Path("/opt/mcp_proxy")
    dst = Path("/config/custom_components/mcp_proxy")

    if not src.exists():
        log_error("Integration source not found at /opt/mcp_proxy")
        return False

    Path("/config/custom_components").mkdir(parents=True, exist_ok=True)

    # Check if update needed
    needs_update = True
    src_manifest = src / "manifest.json"
    dst_manifest = dst / "manifest.json"
    if dst_manifest.exists() and src_manifest.exists():
        try:
            sv = json.loads(src_manifest.read_text()).get("version")
            dv = json.loads(dst_manifest.read_text()).get("version")
            if sv == dv:
                needs_update = False
        except (OSError, json.JSONDecodeError):
            pass

    first_install = not dst.exists()

    if needs_update:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        log_info("Installed mcp_proxy integration")
    else:
        log_info("mcp_proxy integration up to date")

    return first_install


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

    if config_file.exists():
        try:
            config = json.load(config_file.open())
            remote_url = config.get("remote_url", "")
            mcp_server_url = config.get("mcp_server_url", "")
            mcp_port = config.get("mcp_port", 9583)
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Failed to read config: {e}")

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

    # Write proxy config for the mcp_proxy integration
    proxy_config = {"target_url": target_url, "webhook_id": webhook_id}
    proxy_config_file = Path("/config/.mcp_proxy_config.json")
    try:
        proxy_config_file.write_text(json.dumps(proxy_config))
    except OSError as e:
        log_error(f"Failed to write proxy config: {e}")
        return 1

    # Install the mcp_proxy custom component
    first_install = _install_integration()

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

    # Resolve remote URL
    resolved_remote = None
    if remote_url and remote_url.strip():
        resolved_remote = remote_url.strip().rstrip("/")
        if not resolved_remote.startswith("http"):
            resolved_remote = "https://" + resolved_remote
    else:
        resolved_remote = get_nabu_casa_url()

    # Log URLs
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
