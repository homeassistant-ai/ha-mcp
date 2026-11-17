"""The ha_auth token forward reaches a core that serves TLS itself (#2950)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from custom_components.ha_mcp_tools.const import WEBHOOK_AUTH_HA
from custom_components.ha_mcp_tools.oauth_ha_auth import wrap_refresh_token

from ._oauth_autoapprove_core import DCR_KEY, TOKEN_URL, OpenSurface
from ._oauth_autoapprove_core import open_oauth_surface as open_oauth_surface
from ._tls_core import tls_core

CLIENT_ID = "https://client.example/metadata.json"


@pytest.mark.parametrize(
    "internal_url",
    [None, "https://homeassistant.local:9443"],
    ids=["no-https-url", "private-ca-internal-url"],
)
async def test_sign_in_reaches_a_tls_core(
    open_oauth_surface: OpenSurface, tmp_path: Path, internal_url: str | None
) -> None:
    """With http.ssl_certificate, every ha_auth sign-in answered 503: the
    forward verified a certificate issued for a name it could not trust. The
    pre-#2950 forward consulted the configured URL, so one is set as well."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    hass = surface.hass
    if internal_url is not None:
        await hass.config.async_update(internal_url=internal_url)
    # A signed refresh envelope is always proxied to core rather than 307'd.
    envelope = wrap_refresh_token(
        DCR_KEY, await surface.core_refresh_token(CLIENT_ID), CLIENT_ID, CLIENT_ID
    )

    async with tls_core(hass, tmp_path) as core_url:
        port = urlparse(core_url).port
        assert hass.config.api is not None and port is not None
        hass.config.api.port = port
        hass.config.api.use_ssl = True
        resp = await surface.client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": envelope,
            },
        )
        assert resp.status == 200, await resp.text()  # 503 is the sign-in failing
        body = await resp.json()

    assert surface.client_id_of(body["access_token"]) == CLIENT_ID
