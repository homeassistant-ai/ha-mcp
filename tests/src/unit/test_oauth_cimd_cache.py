"""The client metadata cache behind the anonymous ``/authorize`` view.

An anonymous caller can send any number of unique client_ids, so the cache of
looked-up metadata documents is bounded. Each lookup here is an ha_auth
authorize request on Core's HTTP app; ``cimd_web`` serves the documents
through Core's ``aioclient_mock`` and answers name resolution with a public
address. The cache outlives a test, so its earlier entries are older than
this test's.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import oauth_ha_auth
from custom_components.ha_mcp_tools.const import DATA_WEBHOOK, DOMAIN, WEBHOOK_AUTH_HA
from custom_components.ha_mcp_tools.oauth_autoapprove import CFG_CIMD_SESSION
from custom_components.ha_mcp_tools.oauth_ha_auth import fetch_cimd_redirects

from ._oauth_autoapprove_core import AUTHORIZE_URL, OpenSurface
from ._oauth_autoapprove_core import open_oauth_surface as open_oauth_surface
from ._webhook_proxy_oauth_core import CimdWeb
from ._webhook_proxy_oauth_core import cimd_web as cimd_web

CALLBACK = "https://client.example/cb"
# A client whose metadata lives on another origin than its callback, so
# authorizing it needs its document.
CLIENT_ORIGIN = "https://app.example"
# More than the cache holds, so the loop below always reaches an eviction.
MAX_CLIENTS = 1000


async def test_cimd_cache_evicts_oldest_not_wholesale(
    open_oauth_surface: OpenSurface,
    cimd_web: CimdWeb,
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At capacity the oldest entry goes, never the whole cache (#2213 r2).

    Clients are added until the first one is evicted; looking it up again
    then evicts only the next oldest, and the newest stay cached.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    # The surface answers lookups without a cache; this test is about the cache.
    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch_cimd_redirects)
    cfg = hass.data[DOMAIN][DATA_WEBHOOK]
    await cfg[CFG_CIMD_SESSION].close()
    monkeypatch.setitem(cfg, CFG_CIMD_SESSION, cimd_web.session)

    async def fetches_to_authorize(client_id: str) -> int:
        """Authorize as ``client_id``; how many documents that fetched."""
        cimd_web.serve(client_id, [CALLBACK])
        resp = await surface.client.get(
            AUTHORIZE_URL,
            params={"client_id": client_id, "redirect_uri": CALLBACK},
            allow_redirects=False,
        )
        forwarded = parse_qs(urlparse(resp.headers["Location"]).query)
        assert forwarded["client_id"] == ["https://client.example"]
        return cimd_web.fetches

    client_ids = [f"{CLIENT_ORIGIN}/{uuid.uuid4().hex}.json"]
    assert await fetches_to_authorize(client_ids[0]) == 1
    for _ in range(MAX_CLIENTS):
        client_ids.append(f"{CLIENT_ORIGIN}/{uuid.uuid4().hex}.json")
        assert await fetches_to_authorize(client_ids[-1]) == 1
        if await fetches_to_authorize(client_ids[0]) == 1:
            break
    else:
        pytest.fail(f"{MAX_CLIENTS} clients never evicted the first")

    # Fetching the first again evicted only the next oldest.
    assert await fetches_to_authorize(client_ids[-1]) == 0
    assert await fetches_to_authorize(client_ids[2]) == 0
    assert await fetches_to_authorize(client_ids[1]) == 1
