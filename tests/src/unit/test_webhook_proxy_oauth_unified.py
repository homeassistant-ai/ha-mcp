"""The dev proxy's scoped authorize route, discovery documents, dynamic client
registration and signed refresh envelope (#2248), over Core's real HTTP server."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
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
    KEY,
    LOOPBACK_CLIENT,
    LOOPBACK_REDIRECT,
    OAUTH_BASE,
    CimdWeb,
    CoreAuth,
    ProxyStarter,
    b64url,
    hand_signed_envelope,
    submodule,
)
from ._webhook_proxy_oauth_core import (
    cimd_web as cimd_web,
)
from ._webhook_proxy_oauth_core import (
    component as component,
)
from ._webhook_proxy_oauth_core import (
    core_auth as core_auth,
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
ROTATED_KEY = b"r" * 32


def _tampered(envelope: str) -> str:
    body, _, signature = envelope.rpartition(".")
    return f"{body}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"


# -- scoped authorize route and discovery documents ---------------------------


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


# -- dynamic client registration ----------------------------------------------


async def test_registered_client_id_keeps_every_presented_redirect(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """A client registering two origins can use either one: its client_id is
    signed with the proxy's key and still names both after the round trip."""
    proxy = await start_proxy_in_mode("ha_auth")
    dcr = proxy.module("oauth_dcr")

    client_id = await proxy.register(GOOGLE_REDIRECT_URIS)

    assert client_id.startswith("hamcp-dcr-")
    assert dcr.client_redirect_uris(proxy.dcr_key, client_id) == GOOGLE_REDIRECT_URIS
    assert dcr.client_redirect_uris(b"x" * 32, client_id) is None


async def test_none_mode_registration_promises_no_refresh_grant(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """None mode has no refresh cycle, so a client must not be told it has one."""
    proxy = await start_proxy_in_mode("none")

    response = await proxy.client.post(
        proxy.url("register"), json={"redirect_uris": ["https://a.example/cb"]}
    )

    assert response.status == 201
    assert (await response.json())["grant_types"] == ["authorization_code"]


@pytest.mark.parametrize(
    "redirect_uris",
    [
        pytest.param(["https://a.example/cb"], id="single-origin"),
        pytest.param(GOOGLE_REDIRECT_URIS, id="multi-origin"),
        pytest.param(
            ["http://127.0.0.1/callback", "http://localhost/callback"],
            id="loopback-only",
        ),
        pytest.param(
            ["https://a.example/cb", "http://localhost/callback"], id="hybrid"
        ),
    ],
)
async def test_ha_auth_registration_is_refreshable_whatever_its_shape(
    start_proxy_in_mode: ProxyStarter, redirect_uris: list[str]
) -> None:
    """#2248: every ha_auth registration gets a refresh grant, including
    loopback and multi-origin clients, because the signed refresh envelope
    records the identity Core bound the grant to."""
    proxy = await start_proxy_in_mode("ha_auth")

    response = await proxy.client.post(
        proxy.url("register"), json={"redirect_uris": redirect_uris}
    )

    body = await response.json()
    assert response.status == 201
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    registered = proxy.module("oauth_dcr").client_redirect_uris(
        proxy.dcr_key, body["client_id"]
    )
    assert registered == redirect_uris


async def test_registration_keeps_an_explicit_zero_port_distinct(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """Port zero is falsy: a registration naming ``:0`` next to the default
    port must still hand both redirect URIs back, not collapse them."""
    proxy = await start_proxy_in_mode("ha_auth")
    redirect_uris = ["https://a.example/cb", "https://a.example:0/cb"]

    client_id = await proxy.register(redirect_uris)

    assert (
        proxy.module("oauth_dcr").client_redirect_uris(proxy.dcr_key, client_id)
        == redirect_uris
    )


def test_origin_identity_keeps_an_explicit_zero_port_distinct(
    component: ModuleType,
) -> None:
    """``:0`` and the scheme default must never compare equal, or the authorize
    leg would translate a client's two origins to one."""
    dcr = submodule(component, "oauth_dcr")

    assert dcr.normalized_origin("https://a.example:0/cb") == ("https", "a.example", 0)
    assert dcr.normalized_origin("https://a.example/cb") == ("https", "a.example", 443)


async def test_registration_rejects_a_non_loopback_http_redirect(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """Only HTTPS and RFC 8252 loopback HTTP callbacks are accepted."""
    proxy = await start_proxy_in_mode("none")

    response = await proxy.client.post(
        proxy.url("register"), json={"redirect_uris": ["http://evil.example/cb"]}
    )

    assert response.status == 400
    assert (await response.json())["error"] == "invalid_redirect_uri"


async def test_registration_answers_a_deeply_nested_body_with_400(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """#2218: an anonymous endpoint must call a body nested thousands deep
    malformed metadata, never a 500. Whether the parser overflows or returns a
    list depends on the interpreter's stack, so only the 400 is pinned."""
    proxy = await start_proxy_in_mode("ha_auth")
    payload = b"[" * 32000 + b"]" * 32000
    assert len(payload) < proxy.module("oauth_dcr").MAX_DCR_BODY_BYTES

    response = await proxy.client.post(proxy.url("register"), data=payload)

    body = await response.json()
    assert response.status == 400
    assert body["error"] == "invalid_client_metadata"


async def test_registration_answers_a_parser_stack_overflow_with_400(
    start_proxy_in_mode: ProxyStarter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overflow is forced through the module's own ``json`` name: the
    endpoint calls it malformed metadata, never a 500."""
    proxy = await start_proxy_in_mode("ha_auth")
    overflowing = SimpleNamespace(
        loads=Mock(side_effect=RecursionError), dumps=json.dumps
    )
    monkeypatch.setattr(proxy.module("oauth_dcr"), "json", overflowing)

    response = await proxy.client.post(proxy.url("register"), data=b"[[]]")

    body = await response.json()
    assert response.status == 400
    assert body["error_description"] == "body must be JSON"


async def test_registration_rejects_text_that_is_not_json(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """A body that does not parse is malformed metadata, not a server error."""
    proxy = await start_proxy_in_mode("ha_auth")

    response = await proxy.client.post(proxy.url("register"), data=b"redirect_uris=")

    body = await response.json()
    assert response.status == 400
    assert body["error_description"] == "body must be JSON"


async def test_registration_reassembles_a_chunked_body(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """#2219: a body arriving in small fragments must be read to EOF; one
    ``read()`` may return early and truncate the document."""
    proxy = await start_proxy_in_mode("ha_auth")
    body = json.dumps({"redirect_uris": ["https://a.example/cb"]}).encode()

    async def fragments() -> AsyncIterator[bytes]:
        for start in range(0, len(body), 7):
            yield body[start : start + 7]

    response = await proxy.client.post(proxy.url("register"), data=fragments())

    assert response.status == 201


async def test_registration_rejects_an_oversized_body(
    start_proxy_in_mode: ProxyStarter,
) -> None:
    """#2219: a conforming registration is a few KB, so the read is capped
    rather than riding Home Assistant's 16 MiB request limit."""
    proxy = await start_proxy_in_mode("ha_auth")
    oversized = b"x" * (proxy.module("oauth_dcr").MAX_DCR_BODY_BYTES + 1)

    response = await proxy.client.post(proxy.url("register"), data=oversized)

    body = await response.json()
    assert response.status == 400
    assert body["error"] == "invalid_client_metadata"
    assert body["error_description"] == "body is too large"


# -- signed refresh envelope --------------------------------------------------


def test_envelope_round_trips_and_is_disjoint_from_the_registration_blob(
    component: ModuleType,
) -> None:
    """#2248: relabelling one blob family as the other does not make it verify.

    Both are HMAC-signed under the DCR key, and the only thing separating them
    is that the envelope's MAC covers its prefix while the blob's covers the
    bare body. Swapping the prefixes is exactly the attack that difference
    exists to stop, so both directions are driven here.
    """
    dcr = submodule(component, "oauth_dcr")
    indirect = submodule(component, "oauth_indirect")
    states = indirect.EnvelopeState
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )

    assert indirect.unwrap_refresh_token(KEY, envelope, client_id) == (
        "core-refresh",
        "http://127.0.0.1:54321",
    )
    assert (
        indirect.unwrap_refresh_token(KEY, envelope, "other-client") is states.INVALID
    )
    assert indirect.unwrap_refresh_token(KEY, client_id, client_id) is states.ABSENT
    assert dcr.client_redirect_uris(KEY, envelope) is None
    # Relabelled: same signed bodies, the other family's prefix.
    relabelled_blob = f"hamcp-rt-{client_id.removeprefix('hamcp-dcr-')}"
    relabelled_envelope = f"hamcp-dcr-{envelope.removeprefix('hamcp-rt-')}"
    assert (
        indirect.unwrap_refresh_token(KEY, relabelled_blob, client_id) is states.INVALID
    )
    assert dcr.client_redirect_uris(KEY, relabelled_envelope) is None


def test_tampering_a_rotated_key_and_bad_payloads_are_invalid(
    component: ModuleType,
) -> None:
    """A bad MAC, a rotated signing key, or a wrong payload shape is INVALID.

    The signature only proves WE minted the blob, so the version guard behind
    it needs a valid MAC to be exercised at all, hence the hand-signed payload.
    """
    indirect = submodule(component, "oauth_indirect")
    states = indirect.EnvelopeState
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "https://a.example", "cid"
    )

    assert indirect.unwrap_refresh_token(KEY, _tampered(envelope), "cid") is (
        states.INVALID
    )
    assert indirect.unwrap_refresh_token(b"x" * 32, envelope, "cid") is states.INVALID
    future_version = hand_signed_envelope(
        KEY, {"v": 2, "t": "core-refresh", "c": "https://a.example", "p": "x"}
    )
    assert indirect.unwrap_refresh_token(KEY, future_version, None) is states.INVALID


def test_a_missing_presenter_unwraps_any_intact_envelope(
    component: ModuleType,
) -> None:
    """A None presenter skips the client binding (RFC 7009 revocation).

    Revocation authorizes the BEARER of the token, not a client identity, so
    the revoke path recovers Core's token without knowing which client_id the
    envelope was minted alongside.
    """
    indirect = submodule(component, "oauth_indirect")
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "https://a.example", "cid-a"
    )

    assert indirect.unwrap_refresh_token(KEY, envelope, None) == (
        "core-refresh",
        "https://a.example",
    )


def test_revocation_reads_verified_and_unverifiable_envelopes(
    component: ModuleType,
) -> None:
    """#2249 review: revocation recovers Core's token even on a failed MAC.

    The rotated key is the case that matters: removing and re-adding the
    integration mints a new DCR key, invalidating every envelope already in a
    client's hands. Forwarding a body we did not verify is sound here and only
    here: RFC 7009 authorizes the bearer, and Core's revoke endpoint is
    anonymous and idempotent, so a forger gains nothing they could not get by
    POSTing to Core themselves.
    """
    dcr = submodule(component, "oauth_dcr")
    indirect = submodule(component, "oauth_indirect")
    envelope = indirect.wrap_refresh_token(KEY, "core-rt", "https://a.example", "cid")
    rotated = indirect.wrap_refresh_token(
        ROTATED_KEY, "core-rt", "https://a.example", "cid"
    )

    assert indirect.core_token_for_revocation(KEY, envelope) == "core-rt"
    assert indirect.core_token_for_revocation(KEY, _tampered(envelope)) == "core-rt"
    assert indirect.core_token_for_revocation(KEY, rotated) == "core-rt"
    # No key configured means no envelope of ours to recognise at all.
    assert indirect.core_token_for_revocation(None, envelope) is None
    # And the registration blob family stays disjoint from the envelope family.
    blob = dcr.mint_client_id(KEY, ["https://a.example/cb"])
    assert indirect.core_token_for_revocation(KEY, blob) is None


@pytest.mark.parametrize(
    "token",
    [
        # Not ours at all: no prefix, so nothing is parsed.
        pytest.param("core-opaque-refresh-token", id="pre-envelope-token"),
        pytest.param("", id="empty"),
        # Our prefix, nothing readable behind it.
        pytest.param("hamcp-rt-", id="no-body"),
        pytest.param("hamcp-rt-nodot", id="no-separator"),
        pytest.param("hamcp-rt-###.not-a-real-signature", id="undecodable-base64"),
    ],
)
def test_revocation_finds_nothing_to_substitute_in_unusable_values(
    component: ModuleType, token: str
) -> None:
    """Nothing to substitute, so the caller forwards the value as presented."""
    indirect = submodule(component, "oauth_indirect")

    assert indirect.core_token_for_revocation(KEY, token) is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b'["not","an","object"]', id="json-array"),
        pytest.param(b'{"v":1,"t":42}', id="int-t"),
        pytest.param(b'{"v":1,"c":"https://a.example"}', id="missing-t"),
        pytest.param(b"[" * 1500 + b"]" * 1500, id="deeply-nested"),
    ],
)
def test_revocation_refuses_unusable_bodies(
    component: ModuleType, payload: bytes
) -> None:
    """A body that yields no string token is refused, never guessed at.

    This parse runs BEFORE any MAC check, so the JSON parser sees caller-chosen
    nesting (#2218).
    """
    indirect = submodule(component, "oauth_indirect")
    token = f"hamcp-rt-{b64url(payload)}.not-a-real-signature"

    assert len(token) <= indirect.MAX_REVOKE_ENVELOPE_LEN  # not the cap at work
    assert indirect.core_token_for_revocation(KEY, token) is None


def test_revocation_caps_the_unverified_parse(component: ModuleType) -> None:
    """An oversized prefixed value is refused BEFORE it is decoded.

    Core's refresh tokens are short, so nothing legitimate approaches the cap;
    it exists because this parse is the one place an anonymous view hands
    attacker-chosen bytes to base64 and the JSON parser. It guards only that
    path: an envelope the proxy can still VERIFY is honoured at any size.
    """
    indirect = submodule(component, "oauth_indirect")
    huge = "T" * indirect.MAX_REVOKE_ENVELOPE_LEN
    rotated = indirect.wrap_refresh_token(ROTATED_KEY, huge, "https://a.example", "cid")
    ours = indirect.wrap_refresh_token(KEY, huge, "https://a.example", "cid")

    assert len(rotated) > indirect.MAX_REVOKE_ENVELOPE_LEN
    assert indirect.core_token_for_revocation(KEY, rotated) is None
    assert indirect.core_token_for_revocation(KEY, ours) == huge


@pytest.mark.parametrize("refresh_token_is_valid", [True, False])
async def test_core_refresh_answers_are_relayed_byte_identical(
    component: ModuleType, core_auth: CoreAuth, refresh_token_is_valid: bool
) -> None:
    """Core's real answer to a refresh, success or invalid_grant, carries no
    refresh_token to wrap and is relayed untouched."""
    indirect = submodule(component, "oauth_indirect")
    tokens = await core_auth.tokens_for(LOOPBACK_CLIENT, LOOPBACK_REDIRECT)
    refresh_token = tokens["refresh_token"] if refresh_token_is_valid else "not-a-token"
    response = await core_auth.client.post(
        "/auth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": LOOPBACK_CLIENT,
        },
    )
    body = await response.read()
    assert response.status == (200 if refresh_token_is_valid else 400)

    rewritten = indirect.rewrite_token_response_body(
        KEY, body, "https://a.example", "cid"
    )

    assert rewritten is body


@pytest.mark.parametrize(
    "body",
    [
        b'{"refresh_token":null}',
        b'{"refresh_token":42}',
        b'["not","an","object"]',
        b"not json at all",
        b"",
        b'{"refresh_token":"\xff\xfe"}',  # not UTF-8
    ],
)
def test_bodies_without_a_string_refresh_token_are_relayed_byte_identical(
    component: ModuleType, body: bytes
) -> None:
    """Relay anything with no string refresh_token to wrap, byte for byte."""
    indirect = submodule(component, "oauth_indirect")

    rewritten = indirect.rewrite_token_response_body(
        KEY, body, "https://a.example", "cid"
    )

    assert rewritten is body
