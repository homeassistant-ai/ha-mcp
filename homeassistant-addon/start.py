#!/usr/bin/env python3
"""Home Assistant MCP Server Add-on startup script."""

import asyncio
import json
import os
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


def _log_with_timestamp(level: str, message: str, stream=None) -> None:
    """Log a message with a timestamp."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} [{level}] {message}", file=stream, flush=True)


def log_info(message: str) -> None:
    """Log info message."""
    _log_with_timestamp("INFO", message)


def log_error(message: str) -> None:
    """Log error message."""
    _log_with_timestamp("ERROR", message, sys.stderr)


def generate_secret_path() -> str:
    """Generate a secure random path with 128-bit entropy.

    Format: /private_<22-char-urlsafe-token>
    Example: /private_zctpwlX7ZkIAr7oqdfLPxw
    """
    return "/private_" + secrets.token_urlsafe(16)


def get_or_create_secret_path(data_dir: Path, custom_path: str = "") -> str:
    """Get existing secret path or create a new one.

    Args:
        data_dir: Path to the /data directory
        custom_path: Optional custom path from config (overrides auto-generated)

    Returns:
        The secret path to use
    """
    secret_file = data_dir / "secret_path.txt"

    # If custom path is provided, use it and update the stored path
    if custom_path and custom_path.strip():
        path = custom_path.strip()
        if not path.startswith("/"):
            path = "/" + path
        log_info("Using custom secret path from configuration")
        # Update stored path for consistency
        secret_file.write_text(path)
        return path

    # Check if we have a stored secret path
    if secret_file.exists():
        try:
            stored_path = secret_file.read_text().strip()
            if stored_path:
                log_info("Using existing auto-generated secret path")
                return stored_path
        except Exception as e:
            log_error(f"Failed to read stored secret path: {e}")

    # Generate new secret path
    new_path = generate_secret_path()
    log_info("Generated new secret path with 128-bit entropy")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(new_path)
        return new_path
    except Exception as e:
        log_error(f"Failed to save secret path: {e}")
        # Return the path anyway - it will work for this session
        return new_path


def _supervisor_api_get(path: str) -> dict | None:
    """Make a GET request to the Supervisor API.

    Args:
        path: API path (e.g. /addons/self/info)

    Returns:
        Parsed JSON response data, or None on failure.
    """
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        return None

    try:
        req = urllib.request.Request(
            f"http://supervisor{path}",
            headers={
                "Authorization": f"Bearer {supervisor_token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("data", {})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log_error(f"Failed to query Supervisor API ({path}): {e}")
        return None


def get_supervisor_addon_info() -> dict | None:
    """Query the Supervisor API for add-on info including ingress details."""
    return _supervisor_api_get("/addons/self/info")


def get_nabu_casa_url() -> str | None:
    """Get the Nabu Casa remote base URL (e.g. https://xyz.ui.nabu.casa).

    Reads the remote_domain from HA's cloud storage file at
    /config/.storage/cloud (requires map: config:rw).
    Returns the remote URL string, or None if unavailable.
    """
    cloud_storage = Path("/config/.storage/cloud")
    try:
        if cloud_storage.exists():
            cloud_data = json.loads(cloud_storage.read_text())
            data = cloud_data.get("data", {})
            if data.get("remote_enabled"):
                remote_domain = data.get("remote_domain")
                if remote_domain:
                    return f"https://{remote_domain}"
            else:
                log_info("Nabu Casa remote UI is not enabled in cloud settings")
    except (OSError, json.JSONDecodeError) as e:
        log_info(f"Nabu Casa cloud config not available: {e}")

    return None


def _ha_core_api(method: str, path: str, data: dict | None = None) -> dict | list | None:
    """Make a request to HA Core API via Supervisor proxy.

    Unlike _supervisor_api_get, this calls HA Core endpoints directly
    (e.g. /config/config_entries) without unwrapping Supervisor envelope.
    """
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        return None

    url = f"http://supervisor/core/api{path}"
    body = json.dumps(data).encode() if data else None

    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        },
        data=body,
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log_error(f"HA Core API error ({method} {path}): {e.code} {e.reason}")
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log_error(f"HA Core API request failed ({method} {path}): {e}")
        return None


def _ensure_config_entry(retries: int = 5, delay: int = 10) -> bool:
    """Ensure a config entry exists for the mcp_proxy integration.

    Creates one via the HA config flow API if missing. Retries on failure
    (HA Core may still be starting up).
    """
    for attempt in range(1, retries + 1):
        # Check if entry already exists
        entries = _ha_core_api("GET", "/config/config_entries/entry")
        if entries is not None:
            for entry in entries:
                if isinstance(entry, dict) and entry.get("domain") == "mcp_proxy":
                    log_info("Webhook proxy: mcp_proxy config entry exists")
                    return True

            # No existing entry — create one via config flow
            log_info(f"Webhook proxy: Creating config entry (attempt {attempt}/{retries})...")
            flow_result = _ha_core_api(
                "POST", "/config/config_entries/flow", {"handler": "mcp_proxy"}
            )
            if flow_result is None:
                if attempt < retries:
                    time.sleep(delay)
                continue

            result_type = flow_result.get("type")

            if result_type in ("abort", "create_entry"):
                log_info("Webhook proxy: Config entry ready")
                return True

            # Flow returned a form step — complete it
            flow_id = flow_result.get("flow_id")
            if flow_id and result_type == "form":
                complete = _ha_core_api(
                    "POST", f"/config/config_entries/flow/{flow_id}", {}
                )
                if complete and complete.get("type") == "create_entry":
                    log_info("Webhook proxy: Config entry created")
                    return True

        if attempt < retries:
            log_info(f"Webhook proxy: HA not ready, retrying in {delay}s...")
            time.sleep(delay)

    return False


def _remove_config_entry() -> None:
    """Remove the mcp_proxy config entry if it exists."""
    entries = _ha_core_api("GET", "/config/config_entries/entry")
    if entries is None:
        return

    for entry in entries:
        if isinstance(entry, dict) and entry.get("domain") == "mcp_proxy":
            entry_id = entry.get("entry_id")
            if entry_id:
                result = _ha_core_api("DELETE", f"/config/config_entries/entry/{entry_id}")
                if result is not None:
                    log_info("Webhook proxy: Removed mcp_proxy config entry")


def _resolve_remote_url(remote_url: str) -> str | None:
    """Resolve the remote base URL for logging.

    Priority:
    1. User-provided remote_url from addon config (for Cloudflare, DuckDNS, etc.)
    2. Auto-detected Nabu Casa URL from cloud storage

    Returns the base URL (e.g. https://example.com), or None if unavailable.
    """
    # User-provided URL takes priority
    if remote_url and remote_url.strip():
        url = remote_url.strip().rstrip("/")
        if not url.startswith("http"):
            url = "https://" + url
        return url

    # Fall back to Nabu Casa auto-detection
    return get_nabu_casa_url()


def setup_webhook_proxy(
    secret_path: str, addon_info: dict | None, data_dir: Path
) -> str | None:
    """Set up the webhook proxy for remote access.

    Installs the mcp_proxy custom integration into HA Core's config directory,
    writes the proxy config, and creates a config entry via the HA API.
    Never modifies configuration.yaml.

    Works with any reverse proxy (Nabu Casa, Cloudflare, DuckDNS, nginx, etc.)

    Returns the webhook URL path (e.g. /api/webhook/<id>), or None on failure.
    """
    config_dir = Path("/config")
    integration_src = Path("/opt/mcp_proxy")
    integration_dst = config_dir / "custom_components" / "mcp_proxy"
    proxy_config_file = config_dir / ".mcp_proxy_config.json"

    # Verify we can access /config (requires map: config:rw in addon config)
    if not config_dir.exists():
        log_error(
            "Webhook proxy: /config not accessible. "
            "Ensure 'map: config:rw' is in addon config."
        )
        return None

    # Get addon IP for the proxy target
    addon_ip = None
    if addon_info:
        addon_ip = addon_info.get("ip_address")
    if not addon_ip:
        log_error("Webhook proxy: Could not determine addon IP address")
        return None

    # Get or create persistent webhook ID
    webhook_id_file = data_dir / "webhook_id.txt"
    webhook_id = None
    if webhook_id_file.exists():
        try:
            webhook_id = webhook_id_file.read_text().strip()
        except Exception as e:
            log_error(f"Webhook proxy: Failed to read webhook ID: {e}")
    if not webhook_id:
        webhook_id = f"mcp_{secrets.token_hex(16)}"
        try:
            webhook_id_file.write_text(webhook_id)
        except Exception as e:
            log_error(f"Webhook proxy: Failed to save webhook ID: {e}")

    # Write proxy config for the mcp_proxy integration to read
    target_url = f"http://{addon_ip}:9583{secret_path}"
    proxy_config = {"target_url": target_url, "webhook_id": webhook_id}
    try:
        proxy_config_file.write_text(json.dumps(proxy_config))
    except Exception as e:
        log_error(f"Webhook proxy: Failed to write proxy config: {e}")
        return None

    # Install/update the mcp_proxy integration
    first_install = False
    if integration_src.exists():
        try:
            (config_dir / "custom_components").mkdir(parents=True, exist_ok=True)
            # Check if update is needed by comparing manifest versions
            needs_update = True
            dst_manifest = integration_dst / "manifest.json"
            src_manifest = integration_src / "manifest.json"
            if dst_manifest.exists() and src_manifest.exists():
                try:
                    dst_ver = json.loads(dst_manifest.read_text()).get("version")
                    src_ver = json.loads(src_manifest.read_text()).get("version")
                    if dst_ver == src_ver:
                        needs_update = False
                except (OSError, json.JSONDecodeError) as e:
                    log_error(f"Webhook proxy: Failed to compare integration versions: {e}")

            if needs_update:
                first_install = not integration_dst.exists()
                if integration_dst.exists():
                    shutil.rmtree(integration_dst)
                shutil.copytree(integration_src, integration_dst)
                log_info("Webhook proxy: Installed mcp_proxy integration")
            else:
                log_info("Webhook proxy: mcp_proxy integration up to date")
        except Exception as e:
            log_error(f"Webhook proxy: Failed to install integration: {e}")
            return None
    else:
        log_error("Webhook proxy: Integration source not found at /opt/mcp_proxy")
        return None

    # Create config entry via HA API (never touches configuration.yaml)
    if first_install:
        log_info("")
        log_info("*" * 60)
        log_info("  RESTART HOME ASSISTANT to load the new integration,")
        log_info("  then restart this add-on to complete setup.")
        log_info("  (Settings > System > Restart)")
        log_info("*" * 60)
        log_info("")
    else:
        if not _ensure_config_entry():
            log_info(
                "Webhook proxy: Could not create config entry. "
                "If this is a first install, restart HA then restart this add-on."
            )

    return f"/api/webhook/{webhook_id}"


async def run_dual_servers(mcp_instance, main_port: int, ingress_port: int, secret_path: str, uvicorn_log_config: dict | None = None) -> None:
    """Run MCP server on both the main port and ingress port concurrently."""
    log_info(f"Starting dual listeners: port {main_port} (direct) + port {ingress_port} (ingress)")

    run_kwargs: dict = {
        "transport": "streamable-http",
        "host": "0.0.0.0",
        "stateless_http": True,
    }
    if uvicorn_log_config is not None:
        run_kwargs["uvicorn_config"] = {"log_config": uvicorn_log_config}

    main_task = asyncio.create_task(
        mcp_instance.run_async(
            port=main_port,
            path=secret_path,
            **run_kwargs,
        )
    )
    ingress_task = asyncio.create_task(
        mcp_instance.run_async(
            port=ingress_port,
            path=secret_path,
            **run_kwargs,
        )
    )

    done, pending = await asyncio.wait(
        [main_task, ingress_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in done:
        if task.exception():
            raise task.exception()


def main() -> int:
    """Start the Home Assistant MCP Server."""
    log_info("Starting Home Assistant MCP Server...")

    # Read configuration from Supervisor
    config_file = Path("/data/options.json")
    data_dir = Path("/data")
    backup_hint = "normal"  # default
    custom_secret_path = ""  # default
    enable_webhook_proxy = False  # default
    remote_url = ""  # default (auto-detect Nabu Casa)

    if config_file.exists():
        try:
            with open(config_file) as f:
                config = json.load(f)
            backup_hint = config.get("backup_hint", "normal")
            custom_secret_path = config.get("secret_path", "")
            enable_webhook_proxy = config.get("enable_webhook_proxy", False)
            remote_url = config.get("remote_url", "")
        except Exception as e:
            log_error(f"Failed to read config: {e}, using defaults")

    # Generate or retrieve secret path
    secret_path = get_or_create_secret_path(data_dir, custom_secret_path)

    log_info(f"Backup hint mode: {backup_hint}")

    # Set up environment for ha-mcp
    os.environ["HOMEASSISTANT_URL"] = "http://supervisor/core"
    os.environ["BACKUP_HINT"] = backup_hint

    # Validate Supervisor token
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        log_error("SUPERVISOR_TOKEN not found! Cannot authenticate.")
        return 1

    os.environ["HOMEASSISTANT_TOKEN"] = supervisor_token

    log_info(f"Home Assistant URL: {os.environ['HOMEASSISTANT_URL']}")
    log_info("Authentication configured via Supervisor token")

    # Fixed port (internal container port)
    port = 9583

    # Query Supervisor for ingress info
    addon_info = get_supervisor_addon_info()
    ingress_port = None
    ingress_url = None
    ingress_enabled = False

    if addon_info:
        ingress_enabled = addon_info.get("ingress", False)
        ingress_port = addon_info.get("ingress_port")
        ingress_url = addon_info.get("ingress_url")  # e.g. /api/hassio_ingress/<token>

    # Set up webhook proxy for remote access if enabled
    webhook_path = None
    if enable_webhook_proxy:
        log_info("Remote webhook proxy: enabled")
        webhook_path = setup_webhook_proxy(secret_path, addon_info, data_dir)
    else:
        # Clean up if proxy was previously enabled then disabled
        proxy_config_file = Path("/config/.mcp_proxy_config.json")
        if proxy_config_file.exists():
            try:
                proxy_config_file.unlink()
                log_info("Remote webhook proxy: disabled (cleaned up proxy config)")
            except Exception:
                pass
            # Also remove the config entry so the webhook is unregistered
            _remove_config_entry()

    # Log URLs
    log_info("")
    log_info("=" * 80)
    log_info(f"  MCP Server URL (local): http://<home-assistant-ip>:9583{secret_path}")
    log_info("")
    if enable_webhook_proxy and webhook_path:
        remote_base = _resolve_remote_url(remote_url)
        if remote_base:
            log_info(f"  MCP Server URL (remote): {remote_base}{webhook_path}")
        else:
            log_info(f"  MCP Server URL (remote): https://<your-external-url>{webhook_path}")
            log_info("    (Set 'remote_url' in addon config, or enable Nabu Casa for auto-detection)")
        log_info("")
    log_info(f"   Secret Path: {secret_path}")
    log_info("")
    log_info("   Copy the exact URL above - the secret path is required!")
    log_info("=" * 80)
    log_info("")

    # Determine if we need dual-server mode
    # If ingress is enabled and the Supervisor assigned a different port, we need
    # to listen on both the main port (9583) and the ingress port.
    use_dual = (
        ingress_enabled
        and ingress_port is not None
        and ingress_port != 0
        and ingress_port != port
    )

    # Import and run MCP server
    try:
        log_info("Importing ha_mcp module...")
        from ha_mcp.__main__ import mcp, _get_timestamped_uvicorn_log_config

        uvicorn_log_config = _get_timestamped_uvicorn_log_config()

        if use_dual:
            log_info("Starting MCP server (dual-port mode)...")
            asyncio.run(run_dual_servers(mcp, port, ingress_port, secret_path, uvicorn_log_config))
        else:
            if ingress_enabled and ingress_port == port:
                log_info("Ingress port matches main port - single listener serves both")
            log_info("Starting MCP server...")
            mcp.run(
                transport="streamable-http",
                host="0.0.0.0",
                port=port,
                path=secret_path,
                stateless_http=True,
                uvicorn_config={"log_config": uvicorn_log_config},
            )
    except Exception as e:
        log_error(f"Failed to start MCP server: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
