"""The ``[/readonly]/catalog/<mode>`` webhook routes that fix a connection's
tool catalog."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._proxy import Upstream, start_proxy
from ._relay import BEARER_MODES, WEBHOOK_PATH, relay_config
from ._variants import Variant

pytestmark = pytest.mark.usefixtures("component")


@pytest.mark.parametrize("suffix", ["/catalog/search", "/readonly/catalog/full"])
async def test_catalog_route_forwards_to_the_matching_server_endpoint(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    suffix: str,
) -> None:
    """A client on a catalog URL gets that catalog, read-only kept when the
    URL asks for it."""
    await start_proxy(hass, variant, fs_root, relay_config(upstream))
    client = await hass_client_no_auth()

    response = await client.post(f"{WEBHOOK_PATH}{suffix}", data=b"request-body")

    assert response.status == 200
    assert [r.path for r in upstream.requests] == [
        urlsplit(upstream.target_url()).path + suffix
    ]


@pytest.mark.parametrize(
    "path",
    [f"{WEBHOOK_PATH}/catalog/bogus", "/api/webhook/unrelated/catalog/search"],
    ids=["unknown-mode", "unknown-webhook"],
)
async def test_catalog_route_outside_the_known_ones_forwards_nothing(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    path: str,
) -> None:
    await start_proxy(hass, variant, fs_root, relay_config(upstream))
    client = await hass_client_no_auth()

    response = await client.post(path, data=b"request-body")

    assert response.status == 404
    assert upstream.requests == []


@pytest.mark.parametrize("mode", sorted(BEARER_MODES))
async def test_catalog_route_still_requires_the_bearer(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    mode: str,
) -> None:
    """With OAuth on, a catalog URL is no way around the token."""
    config = relay_config(upstream, oauth=BEARER_MODES[mode])
    await start_proxy(hass, variant, fs_root, config)
    client = await hass_client_no_auth()

    response = await client.post(f"{WEBHOOK_PATH}/catalog/search", data=b"{}")

    assert response.status == 401
    assert upstream.requests == []
