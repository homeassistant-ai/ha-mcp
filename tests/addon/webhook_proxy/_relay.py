"""Helpers for the proxy's setup, relay, Repairs and endpoint-suffix tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry

from ._proxy import Upstream, set_up_proxy, state_path, write_proxy_config
from ._variants import Variant

WEBHOOK_ID = "mcp_relay_webhook_id_0123456789"
WEBHOOK_PATH = f"/api/webhook/{WEBHOOK_ID}"

# What start.py writes for each OAuth mode the add-on offers.
LEGACY_OAUTH = {
    "mode": "legacy",
    "client_id": "client-id-1234567890ABCDEF",
    "client_secret": "client-secret-very-secret",
}
HA_AUTH_OAUTH = {"mode": "ha_auth"}
BEARER_MODES = {"ha_auth": HA_AUTH_OAUTH, "legacy": LEGACY_OAUTH}

OAUTH_ISSUE = "oauth_restart_required"
UPDATE_ISSUE = "update_restart_required"


def relay_config(upstream: Upstream, **extra: Any) -> dict[str, Any]:
    """The proxy config ``start.py`` writes, pointing at ``upstream``."""
    return {"target_url": upstream.target_url(), "webhook_id": WEBHOOK_ID, **extra}


async def start_proxy(
    hass: HomeAssistant, variant: Variant, fs_root: Path, config: dict[str, Any]
) -> MockConfigEntry:
    """Write ``config`` as the app would and let Core set the entry up."""
    write_proxy_config(fs_root, variant, config)
    return await set_up_proxy(hass, variant)


def restart_issue(
    hass: HomeAssistant, variant: Variant, issue_id: str = OAUTH_ISSUE
) -> ir.IssueEntry | None:
    """The proxy's click-to-restart Repairs issue, as Core's registry holds it."""
    return ir.async_get(hass).async_get_issue(variant.domain, issue_id)


def restart_marker(fs_root: Path, variant: Variant) -> Path:
    """The marker file that keeps the OAuth restart Repair across boots."""
    return state_path(fs_root, variant.oauth_marker)


async def existing_webhook_handler(
    hass: HomeAssistant, webhook_id: str, request: web.Request, **kwargs: Any
) -> web.Response:
    """Another integration's handler already serving the proxy's webhook id."""
    return web.json_response({"handled_by": "existing", **kwargs})
