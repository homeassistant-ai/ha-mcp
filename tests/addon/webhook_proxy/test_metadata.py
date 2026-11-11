"""The OAuth discovery entry point: the webhook's 401 challenge and the
protected-resource document it points at, for every endpoint suffix."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._proxy import Upstream, start_proxy
from ._relay import LEGACY_OAUTH, WEBHOOK_PATH, relay_config
from ._variants import Variant

pytestmark = pytest.mark.usefixtures("component")

PUBLIC_BASE = "https://legit.example"


@pytest.mark.parametrize(
    "suffix", ["", "/readonly", "/catalog/full", "/readonly/catalog/search"]
)
async def test_challenge_leads_each_endpoint_to_its_own_resource_document(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    suffix: str,
) -> None:
    """A client's first request gets a 401 naming the protected-resource
    document for exactly the URL it used, under the operator's public URL
    (a forged Host header cannot redirect discovery), and that document
    names the resource and the proxy's authorization server."""
    config = relay_config(upstream, oauth=LEGACY_OAUTH, public_base_url=PUBLIC_BASE)
    await start_proxy(hass, variant, fs_root, config)
    client = await hass_client_no_auth()
    resource_path = f"{WEBHOOK_PATH}{suffix}"
    metadata_path = f"/.well-known/oauth-protected-resource{resource_path}"
    forged = {"Host": "evil.example"}

    challenge = await client.post(resource_path, data=b"{}", headers=forged)
    document = await client.get(metadata_path, headers=forged)

    assert challenge.status == 401
    assert challenge.headers["WWW-Authenticate"] == (
        f'Bearer realm="MCP Proxy", resource_metadata="{PUBLIC_BASE}{metadata_path}"'
    )
    assert document.status == 200
    assert await document.json() == {
        "resource": f"{PUBLIC_BASE}{resource_path}",
        "authorization_servers": [f"{PUBLIC_BASE}{variant.oauth_base}"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/homeassistant-ai/ha-mcp",
    }
