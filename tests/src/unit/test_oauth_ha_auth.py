"""Unit tests for CIMD validation + client_id translation (oauth_ha_auth).

A client's metadata document lives on the public internet, so ``cimd_web``
serves it through Core's ``aioclient_mock`` and answers name resolution with a
public address. The lookup cache outlives a test, so every client_id that
reaches it is unique to its test.
"""

from __future__ import annotations

import asyncio
import json
import socket
import uuid

import pytest

from custom_components.ha_mcp_tools import oauth_dcr, oauth_ha_auth
from custom_components.ha_mcp_tools.oauth_dcr import (
    client_redirect_uris,
    mint_client_id,
)
from custom_components.ha_mcp_tools.oauth_ha_auth import (
    MAX_REVOKE_ENVELOPE_LEN,
    EnvelopeState,
    core_token_for_revocation,
    fetch_cimd_redirects,
    origin_client_id,
    redirect_matches,
    resolve_forward_client_id,
    rewrite_token_response_body,
    stable_translation_origin,
    unwrap_refresh_token,
    wrap_refresh_token,
)

from ._webhook_proxy_oauth_core import (
    PUBLIC_ADDRESS,
    CimdWeb,
    b64url,
    hand_signed_envelope,
)
from ._webhook_proxy_oauth_core import cimd_web as cimd_web

KEY = b"k" * 32
GOOGLE_REDIRECT_URIS = [
    "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
    "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
]
CALLBACK = "https://client.example/cb"


def unique_client_id(origin: str = "https://client.example") -> str:
    """A metadata document URL on ``origin`` that no earlier lookup cached."""
    return f"{origin}/{uuid.uuid4().hex}.json"


def test_cimd_timing_constants():
    """Pin the negative-cache and resolver timing bounds."""
    assert oauth_ha_auth.CIMD_NEGATIVE_TTL == 60.0
    assert oauth_ha_auth.CIMD_NEGATIVE_TTL < oauth_ha_auth.CIMD_CACHE_TTL
    assert oauth_ha_auth.CIMD_RESOLVE_TIMEOUT == 5.0


def test_redirect_matches_exact():
    """Require exact matching for non-loopback redirect URIs."""
    assert redirect_matches(["https://spark.example/cb"], "https://spark.example/cb")
    assert not redirect_matches(
        ["https://spark.example/cb"], "https://spark.example/other"
    )


def test_redirect_matches_loopback_port_agnostic():
    """Allow runtime loopback ports while preserving every other URI part."""
    # Claude Code declares http://localhost/callback and http://127.0.0.1/callback
    # without ports; the runtime request carries an ephemeral port (RFC 8252
    # §7.3 requires accepting any port for loopback).
    registered = ["http://localhost/callback", "http://127.0.0.1/callback"]
    assert redirect_matches(registered, "http://localhost:61264/callback")
    assert redirect_matches(registered, "http://127.0.0.1:3118/callback")
    assert not redirect_matches(registered, "http://localhost:61264/other")
    assert not redirect_matches(registered, "http://localhost:61264/callback?next=1")
    assert not redirect_matches(registered, "http://evil.example:80/callback")


def test_origin_client_id():
    """Reduce a redirect URI to the URL-shaped origin core accepts."""
    assert (
        origin_client_id("https://accounts.google.example/cb?x=1")
        == "https://accounts.google.example"
    )


@pytest.mark.asyncio
async def test_resolve_same_origin_passthrough():
    """Preserve URL client IDs that already share the redirect origin."""
    # claude.ai's hosted client: client_id and redirect share an origin —
    # forwarded untouched (today's working behavior).
    out = await resolve_forward_client_id(
        session=None,
        dcr_key=None,
        client_id="https://claude.ai/oauth/mcp-oauth-client-metadata",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )
    assert out == "https://claude.ai/oauth/mcp-oauth-client-metadata"


@pytest.mark.asyncio
async def test_resolve_dcr_blob_translates_to_redirect_origin():
    """Translate a validated single-origin DCR client to its web origin."""
    cid = mint_client_id(KEY, ["https://claude.ai/api/mcp/auth_callback"])
    out = await resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=cid,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )
    assert out == "https://claude.ai"


@pytest.mark.asyncio
async def test_resolve_loopback_only_dcr_uses_runtime_origin():
    """Give core a URL-shaped identity for a loopback-only DCR client."""
    cid = mint_client_id(KEY, ["http://localhost/callback"])

    out = await resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=cid,
        redirect_uri="http://localhost:61264/callback",
    )

    assert out == "http://localhost:61264"


@pytest.mark.asyncio
async def test_resolve_dcr_blob_with_unregistered_redirect_passes_through():
    """Leave a DCR client ID untouched when its redirect is unregistered."""
    # Fail-safe: no translation without validation — core then applies its own
    # (stricter) same-origin rule to the untouched request.
    cid = mint_client_id(KEY, ["https://claude.ai/api/mcp/auth_callback"])
    out = await resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=cid,
        redirect_uri="https://evil.example/cb",
    )
    assert out == cid


@pytest.mark.asyncio
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
async def test_resolve_google_multi_origin_registration_uses_presented_origin(
    redirect_uri, expected
):
    """Translate each Spark request to the origin of its matched redirect."""
    cid = mint_client_id(KEY, GOOGLE_REDIRECT_URIS)

    out = await resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=cid,
        redirect_uri=redirect_uri,
    )

    assert out == expected


@pytest.mark.parametrize(
    ("registered", "expected"),
    [
        (
            ["https://a.example/cb", "https://a.example/other"],
            "https://a.example",
        ),
        (["https://a.example/cb", "https://b.example/cb"], None),
        (["http://localhost/callback", "http://127.0.0.1/callback"], None),
        (
            ["http://localhost/callback", "https://a.example/cb"],
            "https://a.example",
        ),
    ],
)
def test_stable_translation_origin(registered, expected):
    """Return only the single reproducible non-loopback redirect origin."""
    assert oauth_ha_auth.stable_translation_origin(registered) == expected


@pytest.mark.asyncio
async def test_refresh_translates_single_origin_dcr_client():
    """Re-derive a single-origin DCR translation on the refresh leg."""
    client_id = mint_client_id(KEY, ["https://a.example/cb"])

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=None,
        dcr_key=KEY,
        client_id=client_id,
    )

    assert translated == "https://a.example"


@pytest.mark.parametrize(
    "redirect_uris",
    [
        ["https://a.example/cb", "https://b.example/cb"],
        ["http://localhost/callback", "http://127.0.0.1/callback"],
    ],
)
@pytest.mark.asyncio
async def test_refresh_marks_dcr_clients_without_stable_origin(redirect_uris):
    """Multi-origin and loopback-only DCR identities are UNREPRODUCIBLE.

    Only reachable for refresh tokens minted before the #2248 envelope; the
    token view consults the envelope first.
    """
    client_id = mint_client_id(KEY, redirect_uris)

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=None,
        dcr_key=KEY,
        client_id=client_id,
    )

    assert translated is oauth_ha_auth.RefreshDisposition.UNREPRODUCIBLE


@pytest.mark.asyncio
async def test_refresh_same_origin_cimd_client_passes_through(cimd_web: CimdWeb):
    """Keep a same-origin CIMD client ID unchanged during refresh."""
    client_id = unique_client_id("https://claude.ai")
    cimd_web.serve(client_id, ["https://claude.ai/api/mcp/auth_callback"])

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
    )

    assert translated is oauth_ha_auth.RefreshDisposition.PASSTHROUGH


@pytest.mark.parametrize(
    "redirects",
    [
        # Gemini Spark-class: several distinct web origins.
        [
            "https://oauth-redirect.googleusercontent.com/r/prod",
            "https://oauth-redirect-sandbox.googleusercontent.com/r/sandbox",
        ],
        # Claude Code-class: loopback-only callbacks.
        ["http://localhost/callback", "http://127.0.0.1/callback"],
        # Hybrid: web + loopback.
        ["https://a.example/cb", "http://localhost/callback"],
    ],
)
@pytest.mark.asyncio
async def test_refresh_marks_cimd_clients_without_stable_origin(
    cimd_web: CimdWeb, redirects: list[str]
):
    """#2217 review sweep: VERIFIED CIMD identities with no reproducible
    origin are UNREPRODUCIBLE, exactly like the equivalent DCR blobs —
    previously they fell through to None/passthrough and were 307'd into a
    guaranteed core failure on every token expiry."""
    client_id = unique_client_id("https://spark.example")
    cimd_web.serve(client_id, redirects)

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
    )

    assert translated is oauth_ha_auth.RefreshDisposition.UNREPRODUCIBLE


@pytest.mark.asyncio
async def test_refresh_translates_cross_origin_cimd_client(cimd_web: CimdWeb):
    """A CIMD identity with one stable web origin re-derives it on refresh."""
    client_id = unique_client_id()
    cimd_web.serve(client_id, ["https://cb.example/callback"])

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
    )

    assert translated == "https://cb.example"


@pytest.mark.asyncio
async def test_refresh_unverified_identity_passes_through(cimd_web: CimdWeb):
    """No DCR blob and no fetchable document → core stays the authority."""
    client_id = unique_client_id("https://unknown.example")
    cimd_web.serve(client_id, None, status=404)

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
    )

    assert translated is oauth_ha_auth.RefreshDisposition.PASSTHROUGH


@pytest.mark.asyncio
async def test_symmetric_explicit_port_passes_through_on_both_legs(
    cimd_web: CimdWeb,
):
    """#2219 review: when the client_id AND the registered redirect both
    carry the same explicit default port, the authorize fast path fires (raw
    netlocs equal) and the token is bound to the full client_id — so refresh
    must pass through, not translate to the canonicalized origin."""
    client_id = unique_client_id("https://claude.ai:443")
    cimd_web.serve(client_id, ["https://claude.ai:443/api/mcp/auth_callback"])

    authorize_id = await resolve_forward_client_id(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
        redirect_uri="https://claude.ai:443/api/mcp/auth_callback",
    )
    refresh_id = await oauth_ha_auth.translated_client_id_for_refresh(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
    )

    assert authorize_id == client_id
    assert refresh_id is oauth_ha_auth.RefreshDisposition.PASSTHROUGH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "presented_case, registered_case",
    [("upper", "lower"), ("lower", "lower"), ("lower", "upper")],
)
async def test_case_only_differences_pass_through_on_both_legs(
    cimd_web: CimdWeb, presented_case: str, registered_case: str
):
    """#2219 review round 3: core's authorize leg lowercases BOTH sides
    (indieauth._parse_url) while its refresh leg compares byte-exact, so a
    case-only difference is same-origin to core and the raw client_id is what
    both of our legs must forward. The registered document's casing is
    author-written and the presented redirect is client-runtime — they need
    not agree, and none of these combinations may diverge."""
    upper = "https://CLAUDE.AI/api/mcp/auth_callback"
    lower = "https://claude.ai/api/mcp/auth_callback"
    presented = upper if presented_case == "upper" else lower
    client_id = unique_client_id("https://CLAUDE.AI")
    cimd_web.serve(client_id, [upper if registered_case == "upper" else lower])

    authorize_id = await resolve_forward_client_id(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
        redirect_uri=presented,
    )
    refresh_id = await oauth_ha_auth.translated_client_id_for_refresh(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
    )

    assert authorize_id == client_id
    assert refresh_id is oauth_ha_auth.RefreshDisposition.PASSTHROUGH


@pytest.mark.asyncio
async def test_mixed_port_shapes_are_unreproducible(cimd_web: CimdWeb):
    """#2219 review round 3: the refresh identity check normalizes
    ports, so these two redirects are ONE canonical origin — but authorize
    passes the raw client_id through for the port-less one and translates the
    explicit-port one. Which was presented is unrecorded, so the honest
    answer is a local invalid_grant, not a coin flip."""
    client_id = unique_client_id("https://h.example")
    cimd_web.serve(client_id, ["https://h.example/a", "https://h.example:443/b"])

    refresh_id = await oauth_ha_auth.translated_client_id_for_refresh(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
    )

    assert refresh_id is oauth_ha_auth.RefreshDisposition.UNREPRODUCIBLE


@pytest.mark.asyncio
async def test_explicit_default_port_translates_on_both_legs(cimd_web: CimdWeb):
    """#2218 review (reverting #2217's canonical comparison): a client_id
    with an explicit scheme-default port misses the raw-netloc authorize fast
    path, so its token is minted under the TRANSLATED origin — and the
    redirect-less refresh must re-derive that same origin, not pass the raw
    client_id through into a guaranteed core rejection. The two legs agree."""
    client_id = unique_client_id("https://claude.ai:443")
    cimd_web.serve(client_id, ["https://claude.ai/api/mcp/auth_callback"])

    authorize_id = await resolve_forward_client_id(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )
    refresh_id = await oauth_ha_auth.translated_client_id_for_refresh(
        session=cimd_web.session,
        dcr_key=None,
        client_id=client_id,
    )

    assert authorize_id == "https://claude.ai"
    assert refresh_id == "https://claude.ai"


@pytest.mark.asyncio
async def test_mixed_registration_refresh_derivation_is_unreproducible():
    """#2217 review: hybrid (web + loopback) registrations never derive a
    refresh identity from a pre-envelope token — the server cannot know which
    redirect the token used. Authorize via the web redirect still translates
    normally, and #2248 records that translation in the refresh token itself so
    the derivation below is only reached by tokens minted before it."""
    client_id = mint_client_id(
        KEY,
        ["http://localhost/callback", "https://a.example/cb"],
    )

    authorize_id = await resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=client_id,
        redirect_uri="https://a.example/cb",
    )
    refresh_id = await oauth_ha_auth.translated_client_id_for_refresh(
        session=None,
        dcr_key=KEY,
        client_id=client_id,
    )

    assert authorize_id == "https://a.example"
    assert refresh_id is oauth_ha_auth.RefreshDisposition.UNREPRODUCIBLE


@pytest.mark.asyncio
async def test_fetch_cimd_valid_document(cimd_web: CimdWeb):
    """Accept a valid self-identifying CIMD document."""
    client_id = unique_client_id()
    cimd_web.serve(client_id, ["https://g.example/cb"])

    assert await fetch_cimd_redirects(cimd_web.session, client_id) == [
        "https://g.example/cb"
    ]


@pytest.mark.parametrize(
    "client_id",
    [
        "https://127.0.0.1/x",
        "https://10.0.0.5/doc.json",
        "https://h.example/",
        "https://user@example.com/client.json",
        "https://example.com/a/../client.json",
        "https://example.com/client.json#",
        "https://example.com:bad/client.json",
    ],
    ids=[
        "loopback",
        "ip-literal",
        "no-path",
        "username",
        "dot-segment",
        "fragment",
        "bad-port",
    ],
)
@pytest.mark.asyncio
async def test_fetch_cimd_refuses_forbidden_client_ids(
    cimd_web: CimdWeb, client_id: str
):
    """Enforce the SSRF floor and the -00 path, username, dot-segment,
    fragment, and port MUSTs before any request leaves, even when the
    address serves a valid document."""
    cimd_web.serve(client_id, [CALLBACK])

    assert await fetch_cimd_redirects(cimd_web.session, client_id) is None
    assert cimd_web.fetches == 0


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"client_secret": "secret"},
        {"client_secret_expires_at": 0},
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"token_endpoint_auth_method": "client_secret_jwt"},
        {"token_endpoint_auth_method": "client_secret_post"},
    ],
)
@pytest.mark.asyncio
async def test_fetch_cimd_requires_name_and_forbids_shared_secrets(
    cimd_web: CimdWeb, extra: dict[str, object]
):
    """Enforce MCP's client_name and the -00 shared-secret prohibitions."""
    client_id = unique_client_id()
    document = {"client_id": client_id, "redirect_uris": [CALLBACK], **extra}
    if extra:
        document["client_name"] = "Client"
    cimd_web.serve(client_id, None, text=json.dumps(document))

    assert await fetch_cimd_redirects(cimd_web.session, client_id) is None


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(
            '{"client_id":"https://other.example/x","client_name":"Client",'
            f'"redirect_uris":["{CALLBACK}"]}}',
            id="mismatched-client-id",
        ),
        pytest.param(
            '{"client_id":"%s","client_name":"Client",'
            f'"redirect_uris":["{CALLBACK}"],"logo_uri":NaN}}',
            id="nan-constant",
        ),
    ],
)
@pytest.mark.asyncio
async def test_fetch_cimd_rejects_a_document_that_is_not_strict(
    cimd_web: CimdWeb, document: str
):
    """Reject a client_id that does not round-trip, and NaN, which Python's
    JSON decoder accepts by default."""
    client_id = unique_client_id()
    cimd_web.serve(client_id, None, text=document.replace("%s", client_id))

    assert await fetch_cimd_redirects(cimd_web.session, client_id) is None


@pytest.mark.asyncio
async def test_fetch_cimd_reads_entire_stream_before_accepting(cimd_web: CimdWeb):
    """Reject trailing bytes beyond the cap when the first read returns only a
    complete valid document: a slow server sends the rest later."""
    client_id = unique_client_id()
    valid_prefix = json.dumps(
        {"client_id": client_id, "client_name": "Client", "redirect_uris": [CALLBACK]}
    ).encode()
    cimd_web.serve_in_pieces(
        client_id, [valid_prefix, b" " * oauth_ha_auth.CIMD_MAX_BYTES]
    )

    assert await fetch_cimd_redirects(cimd_web.session, client_id) is None


@pytest.mark.asyncio
async def test_invalid_cimd_is_not_negative_cached(cimd_web: CimdWeb):
    """A corrected document is retried immediately after an invalid response."""
    client_id = unique_client_id()
    nameless = {"client_id": client_id, "redirect_uris": [CALLBACK]}
    cimd_web.serve(client_id, None, text=json.dumps(nameless))
    assert await fetch_cimd_redirects(cimd_web.session, client_id) is None

    cimd_web.serve(client_id, [CALLBACK])

    assert await fetch_cimd_redirects(cimd_web.session, client_id) == [CALLBACK]
    assert cimd_web.fetches == 1


@pytest.mark.asyncio
async def test_dns_rrset_with_private_answer_is_rejected(cimd_web: CimdWeb):
    """Fail closed when a hostname mixes public and private DNS answers."""
    client_id = unique_client_id()
    cimd_web.serve(client_id, [CALLBACK])
    cimd_web.dns.return_value = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_ADDRESS, 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443)),
    ]

    assert await fetch_cimd_redirects(cimd_web.session, client_id) is None
    assert cimd_web.fetches == 0


@pytest.mark.asyncio
async def test_dns_resolution_timeout_uses_live_constant(
    cimd_web: CimdWeb, monkeypatch: pytest.MonkeyPatch
):
    """Apply the configurable resolver bound to each DNS lookup: a resolver
    that never answers is given up on, not waited out to the lookup deadline."""

    async def unanswered(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    client_id = unique_client_id()
    cimd_web.serve(client_id, [CALLBACK])
    cimd_web.dns.side_effect = unanswered
    monkeypatch.setattr(oauth_ha_auth, "CIMD_RESOLVE_TIMEOUT", 0.01)
    monkeypatch.setattr(oauth_ha_auth, "CIMD_TOTAL_LOOKUP_TIMEOUT", 3600.0)

    # Without the bound the lookup would wait the hour, so this guard fails it.
    async with asyncio.timeout(30):
        assert await fetch_cimd_redirects(cimd_web.session, client_id) is None
    assert cimd_web.fetches == 0


def test_stable_translation_origin_normalizes_default_ports():
    """#2213 review (Patch76): https://h/a and https://h:443/b are ONE origin
    in registration validation AND translation — a registration mixing the two
    forms must still translate instead of forwarding the raw DCR blob (which
    core rejects pre-login for having no scheme)."""
    assert (
        stable_translation_origin(["https://h.example/a", "https://h.example:443/b"])
        == "https://h.example"
    )


def test_origin_client_id_omits_scheme_default_port():
    assert origin_client_id("https://h.example:443/cb") == "https://h.example"
    assert origin_client_id("https://h.example:8443/cb") == "https://h.example:8443"
    assert origin_client_id("http://localhost:61264/cb") == "http://localhost:61264"


@pytest.mark.asyncio
async def test_resolve_mixed_default_port_registration_translates():
    cid = mint_client_id(KEY, ["https://h.example/a", "https://h.example:443/b"])
    out = await resolve_forward_client_id(
        session=None, dcr_key=KEY, client_id=cid, redirect_uri="https://h.example/a"
    )
    assert out == "https://h.example"


def test_canonical_origins_rebracket_ipv6():
    """#2213 review: urlparse().hostname strips IPv6 brackets — the canonical
    origin must restore them or the translated client_id is not a valid URL."""
    assert origin_client_id("https://[2001:db8::1]/cb") == "https://[2001:db8::1]"
    assert (
        origin_client_id("https://[2001:db8::1]:8443/cb")
        == "https://[2001:db8::1]:8443"
    )
    assert (
        stable_translation_origin(
            ["https://[2001:db8::1]/a", "https://[2001:db8::1]:443/b"]
        )
        == "https://[2001:db8::1]"
    )


@pytest.mark.asyncio
async def test_mixed_registration_loopback_authorize_refresh_stays_local():
    """#2217 review: the former caveat is closed — a hybrid client that
    authorized via its loopback redirect gets NO derived refresh identity
    (previously the web origin was derived and a mismatched identity was
    forwarded into core's failed-login accounting)."""
    client_id = mint_client_id(
        KEY, ["http://localhost/callback", "https://a.example/cb"]
    )
    authorize_id = await resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=client_id,
        redirect_uri="http://localhost:5000/callback",
    )
    refresh_id = await oauth_ha_auth.translated_client_id_for_refresh(
        session=None, dcr_key=KEY, client_id=client_id
    )
    assert authorize_id == "http://localhost:5000"
    assert refresh_id is oauth_ha_auth.RefreshDisposition.UNREPRODUCIBLE


@pytest.mark.asyncio
async def test_cimd_negative_results_are_cached(cimd_web: CimdWeb):
    """Failed lookups cache briefly so an anonymous caller cannot force a
    fresh resolution per request (#2213 review round 2)."""
    client_id = unique_client_id("https://dead.example")
    cimd_web.dns.return_value = []

    assert await fetch_cimd_redirects(cimd_web.session, client_id) is None
    assert await fetch_cimd_redirects(cimd_web.session, client_id) is None
    assert cimd_web.dns.await_count == 1  # second call served from negative cache


# ---------------------------------------------------------------------------
# Refresh-token envelope (issue #2248)
# ---------------------------------------------------------------------------


def test_refresh_envelope_round_trips():
    """Recover core's token and the identity core bound it to."""
    client_id = mint_client_id(KEY, ["http://127.0.0.1/callback"])
    envelope = wrap_refresh_token(
        KEY, "core-refresh-token", "http://127.0.0.1:54321", client_id
    )

    assert envelope.startswith("hamcp-rt-")
    assert unwrap_refresh_token(KEY, envelope, client_id) == (
        "core-refresh-token",
        "http://127.0.0.1:54321",
    )


def test_refresh_envelope_rejects_a_tampered_signature():
    """A flipped signature byte, or a rotated key, invalidates the envelope."""
    envelope = wrap_refresh_token(KEY, "core-rt", "https://a.example", "cid")
    body, _, signature = envelope.rpartition(".")
    tampered = f"{body}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"

    assert unwrap_refresh_token(KEY, tampered, "cid") is EnvelopeState.INVALID
    assert unwrap_refresh_token(b"x" * 32, envelope, "cid") is EnvelopeState.INVALID


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        # ABSENT: no hamcp-rt- prefix, so the legacy derivation still owns it.
        ("core-opaque-refresh-token", EnvelopeState.ABSENT),  # pre-#2248 token
        ("", EnvelopeState.ABSENT),
        # INVALID: our prefix, nothing redeemable behind it.
        ("hamcp-rt-", EnvelopeState.INVALID),  # prefix with no body
        ("hamcp-rt-nodot", EnvelopeState.INVALID),
        ("hamcp-rt-.sig", EnvelopeState.INVALID),
        ("hamcp-rt-###.###", EnvelopeState.INVALID),  # undecodable base64url
    ],
)
def test_refresh_envelope_rejects_non_envelope_tokens(token, expected):
    """Never raise, and separate "not ours" from "ours but unredeemable"."""
    assert unwrap_refresh_token(KEY, token, "cid") is expected


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"v": 2, "t": "core-rt", "c": "https://a.example", "p": "x"},
            id="future-version",
        ),
        pytest.param({"v": 1, "t": 42, "c": "https://a.example", "p": "x"}, id="int-t"),
        pytest.param({"v": 1, "t": "core-rt", "c": None, "p": "x"}, id="null-c"),
        pytest.param(
            {"v": 1, "t": "core-rt", "c": "https://a.example", "p": ["x"]}, id="list-p"
        ),
        pytest.param({"v": 1, "t": "core-rt"}, id="missing-c-and-p"),
        pytest.param(["not", "an", "object"], id="json-array"),
    ],
)
def test_refresh_envelope_rejects_wrong_payload_shapes(payload):
    """A valid MAC over the wrong payload is still INVALID, never a crash.

    The signature only proves WE minted the blob; a future version or a
    non-string field would otherwise be unpacked into the forwarded form. The
    envelope is signed by hand because ``wrap_refresh_token`` cannot produce
    these shapes, so the guards are reached behind a VALID MAC; no presenter is
    bound (the revocation leg's call), so its check cannot answer for them.
    """
    assert (
        unwrap_refresh_token(KEY, hand_signed_envelope(KEY, payload), None)
        is EnvelopeState.INVALID
    )


def test_refresh_envelope_and_dcr_blob_are_disjoint():
    """Relabelling one blob family as the other does not make it verify.

    Both are HMAC-signed under the same key, and the only thing separating
    them is that the envelope's MAC covers its prefix while the DCR blob's
    covers the bare body. Swapping the prefixes is exactly the attack that
    difference exists to stop, so both directions are driven here rather than
    merely handing each verifier the other's intact string.
    """
    blob = mint_client_id(KEY, ["https://a.example/cb"])
    envelope = wrap_refresh_token(KEY, "core-rt", "https://a.example", "cid")

    assert unwrap_refresh_token(KEY, blob, "cid") is EnvelopeState.ABSENT
    assert client_redirect_uris(KEY, envelope) is None
    # Relabelled: same signed bodies, the other family's prefix.
    relabelled_blob = f"hamcp-rt-{blob.removeprefix('hamcp-dcr-')}"
    relabelled_envelope = f"hamcp-dcr-{envelope.removeprefix('hamcp-rt-')}"
    assert unwrap_refresh_token(KEY, relabelled_blob, "cid") is EnvelopeState.INVALID
    assert client_redirect_uris(KEY, relabelled_envelope) is None


def test_refresh_envelope_rejects_a_different_presenter():
    """Bind the envelope to the client_id it was minted alongside."""
    envelope = wrap_refresh_token(KEY, "core-rt", "https://a.example", "cid-a")

    assert unwrap_refresh_token(KEY, envelope, "cid-b") is EnvelopeState.INVALID
    assert unwrap_refresh_token(KEY, envelope, "cid-a") == (
        "core-rt",
        "https://a.example",
    )


def test_refresh_envelope_skips_the_presenter_binding_when_none():
    """A None presenter unwraps any intact envelope (RFC 7009 revocation).

    Revocation authorizes the BEARER of the token, not a client identity, so
    the revoke path must be able to recover core's token without knowing which
    client_id the envelope was minted alongside.
    """
    envelope = wrap_refresh_token(KEY, "core-rt", "https://a.example", "cid-a")

    assert unwrap_refresh_token(KEY, envelope, None) == (
        "core-rt",
        "https://a.example",
    )


# ---------------------------------------------------------------------------
# Best-effort revocation unwrap (#2249 review)
# ---------------------------------------------------------------------------


ROTATED_KEY = b"r" * 32


def _prefixed(payload: bytes) -> str:
    """A ``hamcp-rt-`` value over ``payload`` with a signature we never minted."""
    return f"hamcp-rt-{b64url(payload)}.not-a-real-signature"


def test_core_token_for_revocation_reads_a_verified_envelope():
    """The verified path answers first, exactly as the refresh leg's does."""
    envelope = wrap_refresh_token(KEY, "core-rt", "https://a.example", "cid")

    assert core_token_for_revocation(KEY, envelope) == "core-rt"


def test_core_token_for_revocation_recovers_a_tampered_or_rotated_envelope():
    """A failed MAC still yields core's token on the REVOCATION path.

    The rotated key is the case that matters: removing and re-adding the
    integration mints a new DCR key, which invalidates every envelope already
    in a client's hands. Forwarding a body we did not verify is sound here and
    only here — RFC 7009 authorizes the bearer, and core's revoke endpoint is
    anonymous and idempotent, so a forger gains nothing they could not get by
    POSTing to core themselves (#2249 review).
    """
    envelope = wrap_refresh_token(KEY, "core-rt", "https://a.example", "cid")
    body, _, signature = envelope.rpartition(".")
    tampered = f"{body}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"
    rotated = wrap_refresh_token(ROTATED_KEY, "core-rt", "https://a.example", "cid")

    assert unwrap_refresh_token(KEY, tampered, None) is EnvelopeState.INVALID
    assert unwrap_refresh_token(KEY, rotated, None) is EnvelopeState.INVALID
    assert core_token_for_revocation(KEY, tampered) == "core-rt"
    assert core_token_for_revocation(KEY, rotated) == "core-rt"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("core-opaque-refresh-token", id="pre-envelope-token"),
        pytest.param("", id="empty"),
        pytest.param(mint_client_id(KEY, ["https://a.example/cb"]), id="dcr-blob"),
    ],
)
def test_core_token_for_revocation_ignores_values_without_the_prefix(token):
    """Not ours at all: the caller forwards the presented value unchanged."""
    assert core_token_for_revocation(KEY, token) is None


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("hamcp-rt-", id="no-body"),
        pytest.param("hamcp-rt-nodot", id="no-separator"),
        pytest.param("hamcp-rt-###.not-a-real-signature", id="undecodable-base64"),
        pytest.param(_prefixed(b"not json"), id="not-json"),
        pytest.param(_prefixed(b'["not","an","object"]'), id="json-array"),
        pytest.param(_prefixed(b'{"v":1,"t":42}'), id="int-t"),
        pytest.param(_prefixed(b'{"v":1,"c":"https://a.example"}'), id="missing-t"),
        pytest.param(_prefixed(b"[" * 1500 + b"]" * 1500), id="deeply-nested"),
    ],
)
def test_core_token_for_revocation_refuses_unusable_bodies(token):
    """Prefixed but carrying no token: forward it as presented, never guess.

    The nesting case is the #2218 guard: this parse runs BEFORE any MAC check,
    so ``json.loads`` sees caller-chosen nesting and can raise RecursionError
    where :func:`unwrap_refresh_token` never can.
    """
    assert len(token) <= MAX_REVOKE_ENVELOPE_LEN  # not the cap doing the work
    assert core_token_for_revocation(KEY, token) is None


def test_core_token_for_revocation_caps_the_unverified_parse():
    """An oversized prefixed value is refused BEFORE it is decoded.

    Core's refresh tokens are short, so nothing legitimate approaches the cap;
    it exists because this parse is the one place an anonymous view hands
    attacker-chosen bytes to base64 and json.loads. It guards only that path —
    an envelope this server can still VERIFY is honoured at any size.
    """
    huge = "T" * MAX_REVOKE_ENVELOPE_LEN
    rotated = wrap_refresh_token(ROTATED_KEY, huge, "https://a.example", "cid")
    ours = wrap_refresh_token(KEY, huge, "https://a.example", "cid")

    assert len(rotated) > MAX_REVOKE_ENVELOPE_LEN
    assert core_token_for_revocation(KEY, rotated) is None
    assert core_token_for_revocation(KEY, ours) == huge


def test_core_token_for_revocation_without_a_signing_key_is_none():
    """No key configured means no envelope of ours to recognise at all."""
    envelope = wrap_refresh_token(KEY, "core-rt", "https://a.example", "cid")

    assert core_token_for_revocation(None, envelope) is None


def test_rewrite_token_response_body_wraps_a_string_refresh_token():
    """Swap core's refresh token for an envelope, leaving the rest intact."""
    raw = json.dumps(
        {
            "access_token": "core-access",
            "token_type": "Bearer",
            "expires_in": 1800,
            "refresh_token": "core-refresh",
        }
    ).encode()

    rewritten = json.loads(
        rewrite_token_response_body(KEY, raw, "http://127.0.0.1:54321", "cid")
    )

    assert rewritten["access_token"] == "core-access"
    assert rewritten["expires_in"] == 1800
    assert unwrap_refresh_token(KEY, rewritten["refresh_token"], "cid") == (
        "core-refresh",
        "http://127.0.0.1:54321",
    )


@pytest.mark.parametrize(
    "body",
    [
        b'{"access_token":"x","token_type":"Bearer"}',  # core's refresh response
        b'{"error":"invalid_grant"}',
        b'{"refresh_token":null}',
        b'{"refresh_token":42}',
        b'["not","an","object"]',
        b"not json at all",
        b"",
        b'{"refresh_token":"\xff\xfe"}',  # not UTF-8
    ],
)
def test_rewrite_token_response_body_leaves_other_bodies_byte_identical(body):
    """Relay anything with no string refresh_token to wrap, byte for byte."""
    assert rewrite_token_response_body(KEY, body, "https://a.example", "cid") is body


def test_normalized_origin_keeps_an_explicit_zero_port_distinct():
    """Port 0 is falsy, so a normalizer using ``or`` would collapse these two.

    The authorize-leg translation keys off this identity, so ``:0`` and the
    scheme default must never compare equal (registration round-trips both
    URIs — see test_register_preserves_explicit_zero_port_in_web_origin).
    """
    assert oauth_dcr.normalized_origin("https://a.example:0/cb") == (
        "https",
        "a.example",
        0,
    )
    assert oauth_dcr.normalized_origin("https://a.example/cb") == (
        "https",
        "a.example",
        443,
    )
