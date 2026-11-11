"""The ``/api/webhook/<id>/readonly`` alias that restricts a connection to reads."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

import pytest
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import ClientSessionGenerator

from ._proxy import Upstream, start_proxy
from ._relay import (
    BEARER_MODES,
    WEBHOOK_ID,
    WEBHOOK_PATH,
    existing_webhook_handler,
    relay_config,
)
from ._variants import Variant

pytestmark = pytest.mark.usefixtures("component")


@pytest.mark.parametrize("method", ["POST", "GET"])
async def test_readonly_alias_forwards_to_the_servers_readonly_endpoint(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    method: str,
) -> None:
    """A client connected through ``/readonly`` reaches ha-mcp's read-only
    endpoint with its request intact, never the full one."""
    await start_proxy(hass, variant, fs_root, relay_config(upstream))
    client = await hass_client_no_auth()

    response = await client.request(
        method, f"{WEBHOOK_PATH}/readonly", data=b"request-body"
    )

    assert response.status == 200
    (relayed,) = upstream.requests
    assert relayed.method == method
    assert relayed.path == urlsplit(upstream.target_url()).path + "/readonly"
    assert relayed.body == b"request-body"


async def test_readonly_alias_of_an_unknown_webhook_forwards_nothing(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    await start_proxy(hass, variant, fs_root, relay_config(upstream))
    client = await hass_client_no_auth()

    response = await client.post("/api/webhook/unrelated_webhook/readonly", data=b"x")

    assert response.status == 404
    assert upstream.requests == []


@pytest.mark.parametrize("mode", sorted(BEARER_MODES))
async def test_readonly_alias_still_requires_the_bearer(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    mode: str,
) -> None:
    """With OAuth on, the read-only alias is no way around the token."""
    config = relay_config(upstream, oauth=BEARER_MODES[mode])
    await start_proxy(hass, variant, fs_root, config)
    client = await hass_client_no_auth()

    response = await client.post(f"{WEBHOOK_PATH}/readonly", data=b"{}")

    assert response.status == 401
    assert upstream.requests == []


async def test_failed_setup_leaves_another_integrations_webhook_serving(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    component: ModuleType,
) -> None:
    """When the in-process component already serves this webhook id, the
    proxy's failed setup must not tear that integration's routes down."""
    assert await async_setup_component(hass, webhook.DOMAIN, {})
    webhook.async_register(
        hass, "ha_mcp_tools", "MCP", WEBHOOK_ID, existing_webhook_handler
    )
    component.readonly_webhook.register_readonly_webhook(
        hass, WEBHOOK_ID, existing_webhook_handler
    )

    entry = await start_proxy(hass, variant, fs_root, relay_config(upstream))
    client = await hass_client_no_auth()
    full = await client.post(WEBHOOK_PATH, data=b"{}")
    readonly = await client.post(f"{WEBHOOK_PATH}/readonly", data=b"{}")

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert await full.json() == {"handled_by": "existing"}
    assert await readonly.json() == {"handled_by": "existing", "read_only": True}
    assert upstream.requests == []
