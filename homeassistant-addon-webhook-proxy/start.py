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
import signal
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


def log_warning(message: str) -> None:
    _log("WARNING", message, sys.stderr)


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

# OAuth mode values written into the proxy config's `oauth.mode`. Mirrored by
# the integration (mcp_proxy.OAUTH_MODE_* / auth_native.HA_AUTH_MODE).
#   ha_auth — HA core is the authorization server; no add-on credentials, no
#             signing key, no HA restart. Sign in with your Home Assistant login.
#   legacy  — the previous embedded authorization server (client id/secret).
# ha_auth is the default only for a FIRST-TIME OAuth enable; existing legacy
# setups are auto-detected and kept on legacy so an upgrade never switches them.
HA_AUTH_MODE = "ha_auth"
LEGACY_MODE = "legacy"

# ---------------------------------------------------------------------------
# Mutual exclusion (dev vs stable webhook proxy)
# ---------------------------------------------------------------------------
# The webhook-proxy add-on installs a webhook + (optional) OAuth views into HA.
# The OAuth provider registers /authorize and /token at the HA ROOT, which two
# live integrations cannot both own. The dev and stable flavors are otherwise
# fully isolated (separate domain, files, /data), but they must never run at the
# same time. On startup each refuses if its sibling is already running.
#
# This is the STABLE flavor: its sibling is the dev add-on. Supervisor prefixes
# third-party slugs with a repo hash (e.g. "abc123_ha_mcp_webhook_proxy_dev"),
# so we match by exact slug or "_<base>" suffix. The leading underscore in the
# suffix keeps the stable base from matching a dev slug (which ends in "_dev").
#
# The refusal is deliberately UNCONDITIONAL: it fires whenever the sibling is
# running, even if OAuth is off on both flavors (it does not track per-flavor
# OAuth state). Simplicity and safety are chosen over precision — the only cost
# of the broad guard is refusing a pair that happens not to collide today.
SIBLING_SLUG_BASE = "ha_mcp_webhook_proxy_dev"
MUTEX_NOTIFICATION_ID = "mcp_proxy_mutex"
# The sibling flavor's own mutex notification id, dismissed on our clean start
# so a stale "refused to start" banner it raised doesn't linger once the user
# has resolved the conflict in this flavor's favor.
SIBLING_MUTEX_NOTIFICATION_ID = "mcp_proxy_dev_mutex"
SIBLING_LABEL = "Webhook Proxy (Dev)"


def _sibling_is_running(addons: list[dict], sibling_base: str) -> bool:
    """True if an installed add-on whose slug is `sibling_base` (exact) or ends
    in `_<sibling_base>` (hash-prefixed install) is in state 'started'."""
    for addon in addons:
        slug = addon.get("slug", "")
        if (slug == sibling_base or slug.endswith("_" + sibling_base)) and addon.get(
            "state"
        ) == "started":
            return True
    return False


def _refuse_if_sibling_running() -> bool:
    """Return True (caller must exit) if the sibling webhook-proxy add-on is
    running. Logs the reason and raises a self-clearing HA notification."""
    # This guard protects a real root-route collision, so retry a few times on
    # a transient Supervisor error (both add-ons booting at once, 10s API
    # timeout) before giving up.
    data = None
    for attempt in range(3):
        data = _supervisor_get("/addons")
        if data is not None:
            break
        if attempt < 2:
            time.sleep(2)
    if data is None:
        # Could not determine whether the sibling is running. Fail OPEN (start
        # anyway) but LOUDLY (log the bypass) — this add-on-level Supervisor
        # check is only an early convenience guard. The authoritative backstop
        # lives in the integration: it records which flavor owns the root OAuth
        # /authorize + /token views (a shared hass.data marker) and refuses to
        # set up with a ConfigEntryError if the sibling already owns them, so a
        # real collision fails loudly there instead of silently shadowing — even
        # when the sibling add-on is stopped but its views are still bound (HA
        # keeps HTTP views until a restart).
        log_error(
            "Could not query the Supervisor /addons list to check whether the "
            f"'{SIBLING_LABEL}' add-on is running. Starting anyway, but if both "
            "Webhook Proxy flavors run they collide on the OAuth /authorize and "
            "/token routes — verify the other flavor is stopped."
        )
        return False
    addons = data.get("addons", [])
    if not _sibling_is_running(addons, SIBLING_SLUG_BASE):
        return False
    log_error("=" * 70)
    log_error(f"  The '{SIBLING_LABEL}' add-on is currently running.")
    log_error("  The dev and stable Webhook Proxy add-ons cannot run at the")
    log_error("  same time (they would both try to own the OAuth /authorize")
    log_error("  and /token routes in Home Assistant).")
    log_error(f"  Stop the '{SIBLING_LABEL}' add-on, then start this one.")
    log_error("=" * 70)
    _ha_core_api(
        "POST",
        "/services/persistent_notification/create",
        {
            "title": "MCP Webhook Proxy: only one flavor can run",
            "message": (
                f"The **{SIBLING_LABEL}** add-on is running, so this add-on "
                "refused to start. The dev and stable Webhook Proxy add-ons "
                "cannot run at the same time. Stop the other one, then start "
                "this add-on again."
            ),
            "notification_id": MUTEX_NOTIFICATION_ID,
        },
    )
    return True


# Inbound-request mirror file. The mcp_proxy integration (which runs inside
# Home Assistant, where the webhook actually executes) appends each inbound
# debug line here when "Log inbound requests" is on; this addon tails the file
# and echoes new lines to its own log so they're visible in the addon log too,
# not only in Settings -> System -> Logs. Path kept in sync with
# mcp_proxy/__init__.py:INBOUND_LOG_FILE.
INBOUND_LOG_FILE = Path("/config/.mcp_proxy_inbound.log")


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
                log_error(
                    f"Supervisor API GET {path}: unexpected response type {type(response_data)}"
                )
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
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            # Best-effort: the error body is optional diagnostic detail; if it
            # can't be read/decoded we still log the HTTPError below.
            pass
        log_error(f"Supervisor API POST {path} ({type(e).__name__}): {e} — {err_body}")
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
            # Best-effort: the error body is optional diagnostic detail; if it
            # can't be read/decoded we still log the HTTPError below.
            pass
        log_error(f"Supervisor API GET text {path}: {e} — {body}")
        return None
    except (urllib.error.URLError, TimeoutError) as e:
        log_error(f"Supervisor API GET text {path}: {e}")
        return None


def _ha_core_api(
    method: str, path: str, data: dict | None = None
) -> dict | list | None:
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
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
    ) as e:
        log_error(f"HA Core API {method} {path}: {e}")
        return None


def _request_restart_repair(issue_id: str) -> None:
    """Ask the loaded integration to raise a click-to-restart Repair NOW.

    Only in-process code can file HA repair issues, so the addon calls the
    integration's `refresh_repairs` service. On sessions still running
    integration code from before that service existed the call fails
    (logged by `_ha_core_api`, returns None) — the persistent notification
    posted alongside remains the fallback, so this is best-effort by design.
    """
    _ha_core_api(
        "POST",
        "/services/mcp_proxy/refresh_repairs",
        {"issue_id": issue_id, "action": "create"},
    )


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
            f"Failed to wipe OAuth creds for regeneration ({type(e).__name__}): {e}"
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


def _atomic_write_0600(path: Path, data: bytes) -> bool:
    """Write ``data`` to ``path`` with 0600, atomically and race-free.

    Writes to a sibling temp file created 0600 (the mode is applied by the
    ``open()`` syscall, so there is no chmod-after-write window), then
    ``os.replace()``s it over ``path``. Because the replace is atomic and the
    temp was 0600 from creation, ``path`` is never observable with the new
    bytes at wider-than-0600 permissions — on first create OR on an
    overwrite/regenerate. Returns True on success; returns False if the
    restricted-mode create OR the subsequent write/replace failed — i.e. on
    ANY OSError, whether the filesystem can't honor restricted mode (tmpfs /
    non-POSIX) or a genuine I/O error occurred (full disk, EACCES, read-only
    fs). A False therefore doesn't prove it was only the mode: the caller's
    plain-write fallback also fails on a hard I/O error, so its "wider
    permissions" warning must not be treated as certain.
    """
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                # Best-effort temp cleanup — if unlink fails the stale ".tmp"
                # is harmless and the caller's fallback path still runs.
                pass
            raise
        return True
    except OSError:
        return False


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
            log_error(f"Could not read existing OAuth creds ({type(e).__name__}): {e}")

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
            creds_json = json.dumps(
                {"client_id": final_id, "client_secret": final_secret}
            )
            if not _atomic_write_0600(creds_file, creds_json.encode("utf-8")):
                # False = the restricted-mode create OR the write/replace
                # failed (any OSError: tmpfs / non-POSIX that can't honor 0600,
                # or a hard I/O error like a full disk / EACCES / read-only fs).
                # Fall back to a plain write: on a mode-only limitation it
                # succeeds (the creds may be wider than 0600 — hence the
                # warning); on a hard I/O error it raises too and the outer
                # OSError guard handles it. (The sibling signing-key path warns
                # on the same degradation.)
                creds_file.write_text(creds_json)
                log_error(
                    f"Could not create the OAuth creds file with restricted "
                    f"permissions at {creds_file}. The credentials may have "
                    f"wider permissions than intended."
                )
        except OSError as e:
            log_error(f"Failed to persist OAuth creds ({type(e).__name__}): {e}")
            return "", ""

    return final_id, final_secret


def _resolve_oauth_mode(
    configured_mode: str, configured_id: str, configured_secret: str, data_dir: Path
) -> str:
    """Decide which OAuth mode to activate, logging which rule fired.

    Upgrade-safe precedence — an existing legacy OAuth user is NEVER switched
    silently by an add-on update:
      1. An explicit ``oauth_mode`` add-on option wins outright.
      2. Otherwise, if legacy OAuth is already in use — the user set
         ``oauth_client_id``/``oauth_client_secret``, OR a persisted legacy
         creds file (``/data/oauth_creds.json``) exists from a prior run — OR
         that creds-file check is INDETERMINATE (a stat error such as EACCES
         stops us from knowing) — stay on ``legacy``. Flipping an existing
         legacy user to ``ha_auth`` would break their connector, so an
         unknown result preserves the previous flow.
      3. Otherwise (a first-time enable with no legacy trace) default to
         ``ha_auth``, the recommended mode.
    Switching an existing setup is therefore an explicit user action
    (set ``oauth_mode``), which also requires re-adding the MCP connector.
    """
    mode = (configured_mode or "").strip().lower()
    if mode in (HA_AUTH_MODE, LEGACY_MODE):
        log_info(f"OAuth mode: '{mode}' (set explicitly via the oauth_mode option)")
        return mode
    if mode:
        # Supervisor validates the option against the schema enum, so an unknown
        # value should be unreachable — but fall through to detection rather
        # than crash if one ever gets here.
        log_error(
            f"Ignoring unrecognized oauth_mode '{configured_mode}' "
            f"(valid: '{HA_AUTH_MODE}', '{LEGACY_MODE}'); auto-detecting instead."
        )
    creds_file = data_dir / "oauth_creds.json"
    try:
        creds_file_exists = creds_file.exists()
    except OSError as e:
        # Path.exists() re-raises stat errors other than "not there" (e.g.
        # EACCES). A stat failure makes legacy detection INDETERMINATE — it is
        # no proof the file is absent. Treating it as absent could flip an
        # existing persisted-creds legacy user to ha_auth and break their
        # connector, so preserve the previous flow: return legacy directly.
        log_error(
            f"Could not check {creds_file} ({type(e).__name__}): {e}; the "
            "legacy-install check is indeterminate. Preserving the previous "
            f"(legacy) OAuth flow; set oauth_mode: {HA_AUTH_MODE} explicitly "
            "to switch (you must then re-add your MCP connector)."
        )
        return LEGACY_MODE
    if configured_id.strip() or configured_secret.strip() or creds_file_exists:
        log_info(
            "OAuth mode: 'legacy' — existing legacy OAuth credentials detected "
            "(a configured client id/secret or a persisted /data/oauth_creds.json). "
            f"Staying on legacy mode; set oauth_mode: {HA_AUTH_MODE} to switch "
            "(you must then re-add your MCP connector)."
        )
        return LEGACY_MODE
    log_info(
        f"OAuth mode: '{HA_AUTH_MODE}' (default for a first-time OAuth enable — "
        "sign in with your Home Assistant account; leave the connector's OAuth "
        "fields blank)."
    )
    # OAuth was already enabled but no legacy trace was found. Normally that
    # IS a first-time enable — but if a legacy user's /data trace was lost,
    # this default silently changes their flow, so say how to get back.
    log_warning(
        f"Defaulting to '{HA_AUTH_MODE}'. If you previously used the legacy "
        "OAuth flow, your existing connector must be re-added — or set "
        f"oauth_mode: {LEGACY_MODE} to keep the previous flow."
    )
    return HA_AUTH_MODE


def _get_or_create_webhook_id(data_dir: Path) -> str:
    """Get or create a persistent webhook ID."""
    wh_file = data_dir / "webhook_id.txt"
    if wh_file.exists():
        try:
            wid = wh_file.read_text().strip()
            if wid:
                return wid
        except OSError:
            # Existing file unreadable (permissions/corruption); fall through
            # to generate and persist a fresh webhook ID below.
            pass
    wid = f"mcp_{secrets.token_hex(16)}"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        wh_file.write_text(wid)
    except OSError as e:
        log_error(f"Failed to save webhook ID: {e}")
    return wid


def _read_integration_domain() -> str | None:
    """Read the integration's domain from the source manifest.

    Used by the OAuth probe to construct the metadata URL without
    hard-coding `mcp_proxy` here.
    """
    src_manifest = Path("/opt/mcp_proxy/manifest.json")
    try:
        domain = json.loads(src_manifest.read_text()).get("domain")
    except (OSError, json.JSONDecodeError) as e:
        log_error(f"Could not read integration manifest ({type(e).__name__}): {e}")
        return None
    return domain if isinstance(domain, str) else None


def _probe_oauth_active(attempts: int = 3, delay: int = 2) -> bool:
    """Probe the OAuth protected-resource metadata endpoint.

    Returns True only if HA serves the metadata URL — which means the
    OAuth-enforcing integration code is the one actually loaded in HA's
    Python module cache. HA loads custom_component modules once per boot
    and `reload_config_entry` doesn't reimport them, so an addon update
    that places new code on disk doesn't take effect until HA fully
    restarts. Without this probe, the addon would happily reload the
    config entry against the OLD module's `async_setup_entry`, which
    has no OAuth gate, and the webhook would serve unauthenticated
    requests despite the user's "OAuth ENABLED" line in the log.

    This gates a destructive action (tearing down a working webhook and
    demanding a restart), so a single transient HA-API hiccup or a
    momentarily unreadable manifest must not trigger it. Retry up to
    ``attempts`` times with a short sleep between tries, returning True the
    moment the endpoint reports active. Only an active response
    short-circuits; the fail-closed default (absent → False) still holds
    once every attempt is exhausted. Mirrors the bounded retries in
    `_refuse_if_sibling_running` and `_ensure_config_entry`.
    """
    for attempt in range(attempts):
        domain = _read_integration_domain()
        if domain:
            result = _ha_core_api("GET", f"/{domain}/oauth/protected-resource")
            if isinstance(result, dict) and "authorization_servers" in result:
                return True
        if attempt < attempts - 1:
            time.sleep(delay)
    return False


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
                if (
                    isinstance(complete, dict)
                    and complete.get("type") == "create_entry"
                ):
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


def _emit_new_inbound_lines(state: dict[str, int]) -> None:
    """Echo new lines from the integration's inbound-request mirror file to
    this addon's log.

    The webhook runs inside Home Assistant, so the integration logs inbound
    requests there, not in this addon. It also appends each line to
    ``INBOUND_LOG_FILE``; this tails that file so the same lines show up in the
    addon log. ``state["offset"]`` tracks the byte position consumed so far.
    Only whole lines are emitted; a trailing partial line is left for the next
    poll. Best-effort — any filesystem (``OSError``) error is swallowed so
    mirroring never disrupts the keep-alive loop.
    """
    try:
        if not INBOUND_LOG_FILE.exists():
            return
        offset = state.get("offset", 0)
        with INBOUND_LOG_FILE.open("rb") as fh:
            size = fh.seek(0, 2)
            if offset > size:
                # File was truncated/rotated by the integration — restart. Reset
                # the stored offset too, otherwise a truncated file with no
                # newline yet keeps re-reading from 0 on every poll.
                offset = 0
                state["offset"] = 0
            if offset == size:
                return
            fh.seek(offset)
            data = fh.read()
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return  # no complete line buffered yet
        state["offset"] = offset + last_nl + 1
        for line in data[: last_nl + 1].decode("utf-8", errors="replace").splitlines():
            if line.strip():
                log_info(line)
    except OSError:
        return


def _initial_tail_offset() -> int:
    """Byte offset to start tailing the inbound mirror from — the end of the
    file, so startup doesn't replay history. Guarded: a stat() failure
    (permissions, or the file vanishing between exists() and stat()) must not
    crash startup."""
    try:
        return INBOUND_LOG_FILE.stat().st_size if INBOUND_LOG_FILE.exists() else 0
    except OSError as e:
        log_error(f"Could not stat inbound log file ({type(e).__name__}): {e}")
        return 0


def _install_shutdown_handlers() -> dict[str, str | None]:
    """Install SIGTERM/SIGINT handlers and return the shared shutdown-reason
    dict. A Supervisor "stop" sends SIGTERM; without a handler the process was
    killed mid-loop, so the cleanup never ran (the webhook was left registered)
    and nothing logged why it exited. The handler raises KeyboardInterrupt so an
    in-progress time.sleep() returns immediately (PEP 475 would otherwise resume
    it)."""
    shutdown_reason: dict[str, str | None] = {"reason": None}

    def _on_signal(signum: int, _frame: object) -> None:
        shutdown_reason["reason"] = signal.Signals(signum).name
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)
    return shutdown_reason


def _shutdown_cleanup(reason: str | None) -> None:
    """Clean shutdown: restore default signal handling first so a second signal
    can't abort cleanup, log why we exited, remove the config entry to
    unregister the webhook (keeping the config + component files so the next
    start needs no HA restart), and drop the inbound mirror file.

    On full uninstall the user may still need to manually remove
    /config/custom_components/mcp_proxy/, /config/.mcp_proxy_config.json, and
    /config/.mcp_proxy_inbound.log, then restart HA."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, signal.SIG_DFL)
    log_info(f"Shutting down (reason: {reason or 'unknown'})...")
    _remove_config_entry()
    try:
        INBOUND_LOG_FILE.unlink(missing_ok=True)
    except OSError as e:
        log_error(f"Could not remove inbound log file ({type(e).__name__}): {e}")
    log_info("Webhook proxy stopped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    log_info("Starting Webhook Proxy for HA MCP...")

    # Refuse to start if the sibling (dev/stable) webhook proxy is running.
    if _refuse_if_sibling_running():
        return 1

    # Read config. Supervisor always writes /data/options.json, so the outer
    # existence check stays best-effort for the (unreachable) absent-file case.
    # A present-but-unreadable/corrupt file, however, is FATAL (fail closed
    # below): we can't tell whether the user enabled OAuth, and silently
    # falling back to the unauthenticated defaults would betray that intent.
    config_file = Path("/data/options.json")
    data_dir = Path("/data")
    remote_url = ""
    mcp_server_url = ""
    mcp_port = 9583
    enable_oauth = False
    oauth_client_id = ""
    oauth_client_secret = ""
    oauth_mode_option = ""
    regenerate_oauth_creds = False
    debug_logging = False
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
            # Unset by default in config.yaml options (no default value), so an
            # upgrader inherits nothing — the mode is resolved below.
            oauth_mode_option = config.get("oauth_mode", "")
            regenerate_oauth_creds = bool(config.get("regenerate_oauth_creds", False))
            debug_logging = bool(config.get("debug_logging", False))
        except (OSError, json.JSONDecodeError) as e:
            # Fail closed: the add-on config is present but unreadable/corrupt,
            # so the user's intent is unknown — including whether OAuth should
            # be enforced. Continuing would leave enable_oauth at its False
            # default, silently serving an unauthenticated proxy (URL-secrecy
            # only) to a user who turned OAuth ON. Refuse to start instead.
            log_error("")
            log_error("=" * 70)
            log_error(
                f"  Could not read the add-on configuration ({type(e).__name__}): {e}"
            )
            log_error("")
            log_error("  The add-on options file could not be parsed, so your")
            log_error("  configured intent is unknown — including whether OAuth")
            log_error("  should be enforced. Starting now could serve the webhook")
            log_error("  WITHOUT the authentication you asked for, so the add-on is")
            log_error("  refusing to start.")
            log_error("")
            log_error("  Restart the add-on to recover: the Supervisor rewrites")
            log_error("  this file from your saved configuration on every start,")
            log_error("  so a restart normally heals a corrupted copy. If the")
            log_error("  error persists across restarts, re-save the add-on's")
            log_error("  Configuration tab and check the system's storage.")
            log_error("=" * 70)
            log_error("")
            return 1

    # OAuth mode + credential resolution. ha_auth (HA-native) needs no add-on
    # credentials at all; legacy resolves/persists a client id + secret (user
    # value wins, else a persisted /data file, else auto-generated) so the happy
    # path is "toggle on, restart, copy creds from log".
    oauth_mode = ""
    if enable_oauth:
        oauth_mode = _resolve_oauth_mode(
            oauth_mode_option, oauth_client_id, oauth_client_secret, data_dir
        )
    if enable_oauth and oauth_mode == HA_AUTH_MODE:
        # HA core is the authorization server: no credential resolution,
        # generation, or persistence; no regenerate handling. The regenerate
        # toggle only applies to legacy creds. (The fail-closed stale-code
        # restart probe below still runs in this mode — it closes the
        # code-upgrade fail-open window without breaking the no-restart-to-
        # toggle promise.)
        if regenerate_oauth_creds:
            log_info(
                "Ignoring 'Regenerate OAuth Credentials on Next Start': it only "
                "applies to legacy OAuth mode. In ha_auth mode you sign in with "
                "your Home Assistant account; there are no add-on credentials to "
                "regenerate."
            )
    elif enable_oauth:
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
                        "title": ("MCP Webhook Proxy: manual action required"),
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
    if enable_oauth and oauth_mode == HA_AUTH_MODE:
        # ha_auth is deliberately host-agnostic: the ResourceServer resolves the
        # public base URL per-request so the discovery documents work on ANY
        # hostname regardless of HA's external_url. Exactly ONE oauth-section
        # shape is ever written — {"mode": "ha_auth"} with NO credential keys —
        # so ha_auth and legacy can never both be active.
        proxy_config["oauth"] = {"mode": HA_AUTH_MODE}
        if remote_url and remote_url.strip():
            log_info(
                "Note: the configured External URL is ignored in ha_auth mode — "
                "OAuth discovery is host-derived and works on any hostname."
            )
    elif enable_oauth:
        # legacy: pin the public base URL so metadata/redirect URLs can't be
        # poisoned via a forged Host header, and carry the resolved creds. The
        # explicit "mode": "legacy" marker makes the section unambiguous (the
        # integration also treats a creds-only section as legacy for back-compat).
        resolved_remote = _resolve_remote_url(remote_url)
        if resolved_remote:
            proxy_config["public_base_url"] = resolved_remote
        proxy_config["oauth"] = {
            "mode": LEGACY_MODE,
            "client_id": oauth_client_id,
            "client_secret": oauth_client_secret,
        }
    # Inbound-request debug logging. Like the OAuth keys, only added when the
    # toggle is on, so the default config file shape is unchanged for users
    # who leave it off.
    if debug_logging:
        proxy_config["debug_logging"] = True
    proxy_config_file = Path("/config/.mcp_proxy_config.json")
    proxy_config_json = json.dumps(proxy_config)
    try:
        if not _atomic_write_0600(proxy_config_file, proxy_config_json.encode("utf-8")):
            # False = the restricted-mode create OR the write/replace failed —
            # same degradation semantics as the OAuth creds path above. The
            # file carries the OAuth keys when auth is enabled, so it gets the
            # same 0600-first treatment; a mode-only limitation falls back to
            # a plain write rather than breaking startup.
            proxy_config_file.write_text(proxy_config_json)
            log_error(
                f"Could not create the proxy config file with restricted "
                f"permissions at {proxy_config_file}. It may have wider "
                f"permissions than intended."
            )
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
        # Also raise a HACS-style click-to-restart Repair via the loaded
        # integration (the notification alone is easy to overlook). The
        # issue is non-persistent, so a successful restart clears it and
        # the new code's async_setup dismisses the notification above.
        _request_restart_repair("update_restart_required")

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
            log_error(
                "Could not create config entry after HA restart. "
                "Webhook is NOT active. Restart Home Assistant again, "
                "or remove and re-add the integration manually from "
                "Settings → Devices & Services."
            )
            _ha_core_api(
                "POST",
                "/services/persistent_notification/create",
                {
                    "title": ("MCP Webhook Proxy: setup did not complete"),
                    "message": (
                        "After restarting Home Assistant, the addon "
                        "could not create the integration's config "
                        "entry. **The webhook URL is not active.** "
                        "Restart Home Assistant once more, or remove "
                        "and re-add the MCP Webhook Proxy integration "
                        "from Settings → Devices & Services."
                    ),
                    "notification_id": "mcp_proxy_setup_failed",
                },
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
            log_error(
                "Could not create config entry. Webhook is NOT active. "
                "Restart Home Assistant; if the problem persists, "
                "remove and re-add the integration manually from "
                "Settings → Devices & Services."
            )
            _ha_core_api(
                "POST",
                "/services/persistent_notification/create",
                {
                    "title": ("MCP Webhook Proxy: webhook URL is not active"),
                    "message": (
                        "The addon could not create the integration's "
                        "config entry, so the webhook URL is currently "
                        "**not active**. Restart Home Assistant; if "
                        "the problem persists, remove and re-add the "
                        "MCP Webhook Proxy integration from "
                        "Settings → Devices & Services."
                    ),
                    "notification_id": "mcp_proxy_setup_failed",
                },
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

    # OAuth fail-closed gate. If the user enabled OAuth but the integration
    # code currently loaded in HA's Python module cache is the old (no-auth)
    # version, the webhook would happily serve unauthenticated requests
    # despite the addon log saying "OAuth ENABLED". Detect this directly by
    # probing the OAuth metadata endpoint — only the new code registers it.
    # If absent, unregister the webhook so the URL stops working entirely
    # until HA is restarted, and tell the user loudly.
    # Path to the marker file that the integration's repairs flow watches.
    # Kept in sync with `mcp_proxy/repairs.py:RESTART_MARKER_FILE`.
    oauth_restart_marker = Path("/config/.mcp_proxy_oauth_restart_required")

    # The fail-closed stale-code probe runs in BOTH modes. It only fails when
    # the OAuth metadata endpoint isn't served yet — i.e. HA is still running
    # the OLD cached integration module (which has no ha_auth branch) after a
    # code UPGRADE, which genuinely requires an HA restart for HA to import the
    # new module. ha_auth needs no restart to TOGGLE once its code is loaded, so
    # running the probe here does NOT break that promise; it closes the
    # fail-open window where the old module could serve the webhook while the
    # banner says "OAuth ENABLED". The restart marker / notification / Repair is
    # a generic click-to-restart signal, appropriate in either mode.
    if enable_oauth and not _probe_oauth_active():
        log_error("")
        log_error("=" * 70)
        log_error("  OAuth is enabled but the integration code currently loaded in")
        log_error("  Home Assistant does not enforce it (HA was not restarted after")
        log_error("  the addon update).")
        log_error("")
        log_error("  Disabling the webhook to prevent unauthenticated access.")
        log_error("  RESTART HOME ASSISTANT (Settings → System → Restart) — the")
        log_error("  webhook will reactivate automatically afterwards.")
        log_error("=" * 70)
        log_error("")
        _remove_config_entry()
        # Drop a marker file that the integration's repairs flow checks at
        # next HA boot, so the user also sees a "Restart Home Assistant"
        # Repair card with a click-to-restart submit button (in addition to
        # the persistent_notification below).
        try:
            oauth_restart_marker.write_text(
                json.dumps({"reason": "stale_integration_code"})
            )
        except OSError as e:
            log_error(f"Could not write OAuth restart marker ({type(e).__name__}): {e}")
        _ha_core_api(
            "POST",
            "/services/persistent_notification/create",
            {
                "title": ("MCP Webhook Proxy: HA restart required for OAuth"),
                "message": (
                    "OAuth is enabled in the addon configuration, but "
                    "the new OAuth-enforcing integration code has not "
                    "been loaded into Home Assistant yet (HA was not "
                    "restarted after the addon update).\n\n"
                    "**The webhook URL has been disabled** to prevent "
                    "unauthenticated access while in this state. Please "
                    "restart Home Assistant (**Settings → System → "
                    "Restart**); the webhook will reactivate "
                    "automatically once HA comes back up."
                ),
                "notification_id": "mcp_proxy_oauth_stale",
            },
        )
        # Raise the click-to-restart Repair NOW via the loaded integration
        # (marker-gated on the integration side). Without this the marker
        # is only read at the next HA boot — i.e. the Repair card would
        # only appear after the very restart it is meant to prompt.
        _request_restart_repair("oauth_restart_required")
        # Wait for HA to restart, then re-create the config entry. With
        # the new code now in memory, the next async_setup_entry call
        # registers the OAuth-enforcing handler.
        _wait_for_ha_restart()
        if not _ensure_config_entry():
            log_error(
                "Could not re-create config entry after HA restart. "
                "Webhook remains disabled. Restart Home Assistant again "
                "or remove and re-add the integration manually."
            )
        elif _probe_oauth_active():
            _ha_core_api(
                "POST",
                "/services/persistent_notification/dismiss",
                {"notification_id": "mcp_proxy_oauth_stale"},
            )
            # Marker is the integration's signal — the integration's
            # `async_setup_entry` will already delete it on its own next
            # boot, but cleaning up here too prevents the Repair card from
            # flickering on a subsequent restart.
            try:
                oauth_restart_marker.unlink(missing_ok=True)
            except OSError:
                # Best-effort cleanup; the integration's async_setup_entry
                # removes this marker on its next boot regardless.
                pass
            log_info(
                "OAuth-enforcing integration code is now active; webhook re-enabled."
            )
        else:
            # Re-probe failed AFTER HA was restarted and the config entry
            # was re-created. The integration loaded SOMETHING — possibly
            # OAuth-enforcing, possibly not. Don't leave the webhook live
            # with indeterminate auth: tear the entry back down so the
            # URL returns 404 again, and escalate the notification so the
            # user sees that the auto-recovery didn't work.
            log_error(
                "OAuth metadata endpoint still not reachable after HA "
                "restart. Disabling the webhook again to prevent "
                "unauthenticated access. The user must remove and "
                "re-add the integration manually."
            )
            _remove_config_entry()
            _ha_core_api(
                "POST",
                "/services/persistent_notification/create",
                {
                    "title": ("MCP Webhook Proxy: OAuth could not be enabled"),
                    "message": (
                        "After Home Assistant was restarted, the "
                        "OAuth-enforcing integration code still did "
                        "not load (the OAuth metadata endpoint is not "
                        "reachable). **The webhook URL has been "
                        "disabled** to prevent unauthenticated "
                        "access. Please remove the MCP Webhook Proxy "
                        "integration from Settings → Devices & "
                        "Services, then restart the addon to re-add "
                        "it. If the problem persists, file a bug "
                        "report with the addon log."
                    ),
                    "notification_id": "mcp_proxy_oauth_stuck",
                },
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
        log_info(
            f"  MCP Server URL (remote): https://<your-external-url>{webhook_path}"
        )
        log_info("    Set 'remote_url' in addon config, or enable Nabu Casa")
    log_info("")
    log_info("  Copy the remote URL above into your MCP client.")
    if enable_oauth and oauth_mode == HA_AUTH_MODE:
        log_info("")
        log_info("  OAuth (Beta) is ENABLED — sign in with your Home Assistant login.")
        log_info("    LEAVE the connector's OAuth fields BLANK (no Client ID or")
        log_info("    Client Secret needed). You sign in with your Home Assistant")
        log_info("    account when your MCP client connects.")
        log_info("    Works with any hostname/URL, and no Home Assistant restart")
        log_info("    is needed to enable or disable it.")
        log_info("    Revoke access anytime from your Home Assistant profile's")
        log_info("    sessions/tokens.")
        log_info("    Note: if your MCP client's UI insists on a Client Secret,")
        log_info("    any value works — Home Assistant ignores it.")
    elif enable_oauth:
        log_info("")
        log_info("  OAuth (Beta) is ENABLED for this URL (legacy mode).")
        log_info(f"    OAuth Client ID:     {oauth_client_id}")
        log_info(f"    OAuth Client Secret: {oauth_client_secret}")
        log_info("    Paste both into the OAuth fields of your MCP client's")
        log_info("    connector setup (Claude.ai: connector → Advanced settings).")
        log_info("    These values persist at /data/oauth_creds.json — same")
        log_info("    values across addon restarts.")
    if debug_logging:
        log_info("")
        log_info("  Inbound request debug logging is ON.")
        log_info("    Every request hitting this webhook is mirrored here in the")
        log_info("    addon log (it also appears in Home Assistant's log). Use it")
        log_info("    to confirm your MCP client is actually reaching the server.")
    log_info("=" * 70)
    log_info("")

    # Install SIGTERM/SIGINT handlers so a Supervisor "stop" shuts down through
    # the cleanup below instead of being killed mid-loop.
    shutdown_reason = _install_shutdown_handlers()

    # Clear any stale "refused to start" mutex banner from a prior blocked
    # start — both our own id and the sibling's (the user may have resolved the
    # conflict by keeping THIS flavor, leaving the sibling's banner up).
    # persistent_notification/dismiss is a no-op for an absent id and returns a
    # (non-None) result; a None result means a real HA API error, so surface it
    # since a failed dismiss leaves a stale banner on a healthy add-on.
    for _note_id in (MUTEX_NOTIFICATION_ID, SIBLING_MUTEX_NOTIFICATION_ID):
        if (
            _ha_core_api(
                "POST",
                "/services/persistent_notification/dismiss",
                {"notification_id": _note_id},
            )
            is None
        ):
            log_error(
                f"Could not dismiss the '{_note_id}' mutex notification; if a "
                "'refused to start' banner is showing it may need manual dismissal."
            )

    # Keep-alive loop. A short poll interval keeps inbound-log mirroring
    # responsive when "Log inbound requests" is on; the MCP-server health
    # check runs on its own ~60s cadence regardless of the poll interval.
    log_info("Entering keep-alive loop (health check every 60s)...")
    inbound_tail: dict[str, int] = {"offset": _initial_tail_offset()}
    consecutive_failures = 0
    last_health = 0.0
    try:
        while True:
            if debug_logging:
                _emit_new_inbound_lines(inbound_tail)

            now = time.monotonic()
            if now - last_health >= 60:
                last_health = now
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
                            "MCP server still unreachable after "
                            f"{consecutive_failures} checks"
                        )

            time.sleep(3 if debug_logging else 30)
    except KeyboardInterrupt:
        # SIGINT in environments where it still raises directly, rather than
        # going through the installed handler (which already set a reason).
        if shutdown_reason["reason"] is None:
            shutdown_reason["reason"] = "KeyboardInterrupt"

    _shutdown_cleanup(shutdown_reason["reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
