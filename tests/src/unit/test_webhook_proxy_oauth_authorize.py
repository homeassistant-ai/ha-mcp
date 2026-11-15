"""The scoped authorize route and the discovery documents of every proxy mode."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest

from tests.addon.webhook_proxy._auth_modes import as_document_urls
from tests.addon.webhook_proxy._legacy_oauth import (
    CLIENT_ID,
    REDIRECT_URI,
    STATE,
    VERIFIER,
    RenderedPage,
    s256,
)

from ._webhook_proxy_oauth_core import (
    DEV,
    GOOGLE_REDIRECT_URIS,
    OAUTH_BASE,
    CimdWeb,
    ProxyStarter,
)
from ._webhook_proxy_oauth_core import (
    cimd_web as cimd_web,
)
from ._webhook_proxy_oauth_core import (
    component as component,
)
from ._webhook_proxy_oauth_core import (
    fs_root as fs_root,
)
from ._webhook_proxy_oauth_core import (
    proxy_components_root as proxy_components_root,
)
from ._webhook_proxy_oauth_core import (
    start_proxy_in_mode as start_proxy_in_mode,
)

CHALLENGE = "a" * 43


@pytest.mark.parametrize(
    ("redirect_uri", "expected"),
    [
        (GOOGLE_REDIRECT_URIS[0], "https://oauth-redirect.googleusercontent.com"),
        (
            GOOGLE_REDIRECT_URIS[1],
            "https://oauth-redirect-sandbox.googleusercontent.com",
        ),
    ],
)
async def test_ha_auth_authorize_sends_a_registered_client_in_as_its_origin(
    start_proxy_in_mode: ProxyStarter, redirect_uri: str, expected: str
) -> None:
    """A client registered with several origins reaches Core's sign-in as the
    origin of the redirect it presented, the one identity Core will accept."""
    proxy = await start_proxy_in_mode("ha_auth")
    client_id = await proxy.register(GOOGLE_REDIRECT_URIS)

    response = await proxy.authorize(
        response_type="code",
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
    )

    location = urlparse(response.headers["Location"])
    assert response.status == 302
    assert location.path == "/auth/authorize"
    assert parse_qs(location.query)["client_id"] == [expected]


async def test_ha_auth_authorize_looks_metadata_up_on_its_own_session(
    start_proxy_in_mode: ProxyStarter,
    cimd_web: CimdWeb,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public metadata lookup never borrows the authenticated relay session:
    the document is fetched on the dedicated session and the translated
    origin reaches Core's sign-in."""
    proxy = await start_proxy_in_mode("ha_auth")
    await cimd_web.attach(proxy, monkeypatch)
    client_id = "https://metadata.example/client.json"
    cimd_web.serve(client_id, ["https://callback.example/cb"])

    response = await proxy.authorize(
        client_id=client_id, redirect_uri="https://callback.example/cb"
    )

    assert response.status == 302
    assert cimd_web.fetches == 1
    forwarded = parse_qs(urlparse(response.headers["Location"]).query)
    assert forwarded["client_id"] == ["https://callback.example"]


async def test_ha_auth_authorize_percent_encodes_the_forwarded_query(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """The proxy forwards a loopback callback percent-encoded (#2318).

    The forward was previously built with ``yarl.URL().with_query()``, which
    legally leaves ":" and "/" unencoded inside a query value, so a native-app
    callback rendered as ``redirect_uri=http://127.0.0.1:19877/mcp/oauth/callback``.
    Reverse proxies shipping a generic "block common exploits" ruleset (Nginx
    Proxy Manager is a common one) match ``[a-zA-Z0-9_]=http://`` and answer
    403 before Core ever sees the request.
    """
    proxy = await start_proxy_in_mode("ha_auth")

    response = await proxy.authorize(
        response_type="code",
        client_id="http://127.0.0.1:19877",
        redirect_uri="http://127.0.0.1:19877/mcp/oauth/callback",
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
        state="xyz",
    )

    location = response.headers["Location"]
    raw_query = urlparse(location).query
    assert response.status == 302
    # The exact pattern those proxy rulesets match must not survive the hop.
    assert re.search(r"[a-zA-Z0-9_]=http://", location) is None
    # Stronger than the ruleset's own pattern: nothing in the forwarded query
    # is left in a form a "block common exploits" filter could latch onto.
    assert ":" not in raw_query and "/" not in raw_query
    # ...while redirect_uri, client_id and state survive the encoding
    # round-trip unchanged.
    forwarded = parse_qs(raw_query)
    assert forwarded["redirect_uri"] == ["http://127.0.0.1:19877/mcp/oauth/callback"]
    assert forwarded["client_id"] == ["http://127.0.0.1:19877"]
    assert forwarded["state"] == ["xyz"]


async def test_none_mode_authorize_refuses_a_redirect_the_server_cannot_serialize(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """#2218 review: a redirect whose authority holds a backslash before an
    ``@`` parses, but cannot be written back into a Location header. The
    validator must 400 it up front, never 500 on an unauthenticated view."""
    proxy = await start_proxy_in_mode("none")
    query = {
        "response_type": "code",
        "client_id": "https://client.example/client.json",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
    }

    refused = await proxy.authorize(
        **query, redirect_uri="https://client.example\\@evil.example/cb"
    )
    accepted = await proxy.authorize(**query, redirect_uri="https://client.example/cb")

    assert refused.status == 400
    assert (await refused.json())["error_description"] == "invalid redirect_uri"
    assert accepted.status == 302


async def test_legacy_mode_serves_its_consent_page_on_the_scoped_route(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """The scoped authorize route reuses the legacy authorization server, so
    a client keeps calling proxy-owned endpoints across mode switches."""
    proxy = await start_proxy_in_mode("legacy")

    response = await proxy.authorize(
        response_type="code",
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        state=STATE,
        code_challenge=s256(VERIFIER),
        code_challenge_method="S256",
    )

    page = RenderedPage(await response.text())
    assert response.status == 200
    assert response.content_type == "text/html"
    assert page.inputs()["client_id"]["value"] == CLIENT_ID
    assert page.inputs()["redirect_uri"]["value"] == REDIRECT_URI


@pytest.mark.parametrize(
    ("mode", "base"),
    [
        pytest.param("ha_auth", "http://ha.example", id="ha_auth"),
        pytest.param("none", "http://ha.example", id="none"),
        pytest.param("legacy", "https://ha.example", id="legacy"),
    ],
)
async def test_every_mode_advertises_proxy_owned_endpoints_and_cimd(
    start_proxy_in_mode: ProxyStarter, mode: str, base: str
) -> None:
    """Every advertised URL is proxy-owned and both public modes keep CIMD,
    so claude.ai's client selection never falls back to Core's own document."""
    proxy = await start_proxy_in_mode(mode)

    response = await proxy.client.get(
        as_document_urls(DEV)[0], headers={"Host": "ha.example"}
    )

    doc = await response.json()
    assert response.status == 200
    assert doc["authorization_endpoint"] == f"{base}{OAUTH_BASE}/authorize"
    assert doc["token_endpoint"] == f"{base}{OAUTH_BASE}/token"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["issuer"] == f"{base}{OAUTH_BASE}"
    if mode == "legacy":
        assert "registration_endpoint" not in doc
        assert "client_secret_basic" in doc["token_endpoint_auth_methods_supported"]
    else:
        assert doc["client_id_metadata_document_supported"] is True
        assert "none" in doc["token_endpoint_auth_methods_supported"]
        assert doc["registration_endpoint"] == f"{base}{OAUTH_BASE}/register"
