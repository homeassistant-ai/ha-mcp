"""Helpers for the proxy's setup, relay, Repairs and endpoint-suffix tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from ._proxy import Upstream, state_path
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

UPDATE_ISSUE = "update_restart_required"


def relay_config(upstream: Upstream, **extra: Any) -> dict[str, Any]:
    """The proxy config ``start.py`` writes, pointing at ``upstream``."""
    return {"target_url": upstream.target_url(), "webhook_id": WEBHOOK_ID, **extra}


def restart_marker(fs_root: Path, variant: Variant) -> Path:
    """The marker file that keeps the OAuth restart Repair across boots."""
    return state_path(fs_root, variant.oauth_marker)


async def existing_webhook_handler(
    hass: HomeAssistant, webhook_id: str, request: web.Request, **kwargs: Any
) -> web.Response:
    """Another integration's handler already serving the proxy's webhook id."""
    return web.json_response({"handled_by": "existing", **kwargs})
