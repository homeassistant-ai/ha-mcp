"""Unit tests for CIMD validation + client_id translation (oauth_ha_auth)."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import oauth_ha_auth  # noqa: E402
from custom_components.ha_mcp_tools.oauth_dcr import mint_client_id  # noqa: E402
from custom_components.ha_mcp_tools.oauth_ha_auth import (  # noqa: E402
    origin_client_id,
    redirect_matches,
    resolve_forward_client_id,
    stable_translation_origin,
)

KEY = b"k" * 32
GOOGLE_REDIRECT_URIS = [
    "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
    "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
]


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
    """Multi-origin and loopback-only DCR identities are UNREPRODUCIBLE."""
    client_id = mint_client_id(KEY, redirect_uris)

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=None,
        dcr_key=KEY,
        client_id=client_id,
    )

    assert translated is oauth_ha_auth.RefreshDisposition.UNREPRODUCIBLE


@pytest.mark.asyncio
async def test_refresh_same_origin_cimd_client_passes_through(monkeypatch):
    """Keep a same-origin CIMD client ID unchanged during refresh."""

    async def fetch_redirects(_session, _client_id):
        return ["https://claude.ai/api/mcp/auth_callback"]

    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch_redirects)

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=object(),
        dcr_key=None,
        client_id="https://claude.ai/oauth/mcp-oauth-client-metadata",
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
async def test_refresh_marks_cimd_clients_without_stable_origin(monkeypatch, redirects):
    """#2217 review sweep: VERIFIED CIMD identities with no reproducible
    origin are UNREPRODUCIBLE, exactly like the equivalent DCR blobs —
    previously they fell through to None/passthrough and were 307'd into a
    guaranteed core failure on every token expiry."""

    async def fetch_redirects(_session, _client_id):
        return redirects

    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch_redirects)

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=object(),
        dcr_key=None,
        client_id="https://spark.example/client-metadata.json",
    )

    assert translated is oauth_ha_auth.RefreshDisposition.UNREPRODUCIBLE


@pytest.mark.asyncio
async def test_refresh_translates_cross_origin_cimd_client(monkeypatch):
    """A CIMD identity with one stable web origin re-derives it on refresh."""

    async def fetch_redirects(_session, _client_id):
        return ["https://cb.example/callback"]

    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch_redirects)

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=object(),
        dcr_key=None,
        client_id="https://client.example/metadata.json",
    )

    assert translated == "https://cb.example"


@pytest.mark.asyncio
async def test_refresh_unverified_identity_passes_through(monkeypatch):
    """No DCR blob and no fetchable document → core stays the authority."""

    async def fetch_redirects(_session, _client_id):
        return None

    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch_redirects)

    translated = await oauth_ha_auth.translated_client_id_for_refresh(
        session=object(),
        dcr_key=None,
        client_id="https://unknown.example/metadata.json",
    )

    assert translated is oauth_ha_auth.RefreshDisposition.PASSTHROUGH


@pytest.mark.asyncio
async def test_symmetric_explicit_port_passes_through_on_both_legs(monkeypatch):
    """#2219 review: when the client_id AND the registered redirect both
    carry the same explicit default port, the authorize fast path fires (raw
    netlocs equal) and the token is bound to the full client_id — so refresh
    must pass through, not translate to the canonicalized origin."""

    async def fetch_redirects(_session, _client_id):
        return ["https://claude.ai:443/api/mcp/auth_callback"]

    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch_redirects)
    client_id = "https://claude.ai:443/oauth/client.json"

    authorize_id = await resolve_forward_client_id(
        session=object(),
        dcr_key=None,
        client_id=client_id,
        redirect_uri="https://claude.ai:443/api/mcp/auth_callback",
    )
    refresh_id = await oauth_ha_auth.translated_client_id_for_refresh(
        session=object(),
        dcr_key=None,
        client_id=client_id,
    )

    assert authorize_id == client_id
    assert refresh_id is oauth_ha_auth.RefreshDisposition.PASSTHROUGH


@pytest.mark.asyncio
async def test_same_case_uppercase_pair_passes_through_on_both_legs(monkeypatch):
    """#2219 review: an all-uppercase pair is equal under core's raw netloc
    rule, so authorize passes it through untranslated — and refresh matches
    the client_id against the REGISTERED redirects (not the canonicalized
    stable origin, which is lowercased), so it passes through too. This is
    the case the old stable-origin comparison got wrong."""

    session = object()
    redirects = AsyncMock(return_value=["https://CLAUDE.AI/api/mcp/auth_callback"])
    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", redirects)
    client_id = "https://CLAUDE.AI/oauth/client.json"

    authorize_id = await resolve_forward_client_id(
        session=session,
        dcr_key=None,
        client_id=client_id,
        redirect_uri="https://CLAUDE.AI/api/mcp/auth_callback",
    )
    # The fast path decided authorize — no lookup.
    redirects.assert_not_awaited()
    refresh_id = await oauth_ha_auth.translated_client_id_for_refresh(
        session=session,
        dcr_key=None,
        client_id=client_id,
    )

    assert authorize_id == client_id
    assert refresh_id is oauth_ha_auth.RefreshDisposition.PASSTHROUGH
    # Refresh DID fetch and matched the registered redirect — the passthrough
    # is the comparison's verdict, not a skipped lookup.
    redirects.assert_awaited_once_with(session, client_id)


@pytest.mark.asyncio
async def test_case_mismatched_pair_translates_on_both_legs(monkeypatch):
    """#2219 review, second round: a pair differing only in host casing MUST
    miss the fast path — core's own raw comparison would reject it
    untranslated — and take the validated translation to the canonical
    origin, with refresh deriving the same identity."""

    async def fetch_redirects(_session, _client_id):
        return ["https://claude.ai/api/mcp/auth_callback"]

    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch_redirects)
    client_id = "https://CLAUDE.AI/oauth/client.json"

    authorize_id = await resolve_forward_client_id(
        session=object(),
        dcr_key=None,
        client_id=client_id,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )
    refresh_id = await oauth_ha_auth.translated_client_id_for_refresh(
        session=object(),
        dcr_key=None,
        client_id=client_id,
    )

    assert authorize_id == "https://claude.ai"
    assert refresh_id == "https://claude.ai"


@pytest.mark.asyncio
async def test_explicit_default_port_translates_on_both_legs(monkeypatch):
    """#2218 review (reverting #2217's canonical comparison): a client_id
    with an explicit scheme-default port misses the raw-netloc authorize fast
    path, so its token is minted under the TRANSLATED origin — and the
    redirect-less refresh must re-derive that same origin, not pass the raw
    client_id through into a guaranteed core rejection. The two legs agree."""

    async def fetch_redirects(_session, _client_id):
        return ["https://claude.ai/api/mcp/auth_callback"]

    monkeypatch.setattr(oauth_ha_auth, "fetch_cimd_redirects", fetch_redirects)
    client_id = "https://claude.ai:443/oauth/mcp-oauth-client-metadata"

    authorize_id = await resolve_forward_client_id(
        session=object(),
        dcr_key=None,
        client_id=client_id,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )
    refresh_id = await oauth_ha_auth.translated_client_id_for_refresh(
        session=object(),
        dcr_key=None,
        client_id=client_id,
    )

    assert authorize_id == "https://claude.ai"
    assert refresh_id == "https://claude.ai"


@pytest.mark.asyncio
async def test_mixed_registration_refresh_derivation_is_unreproducible():
    """#2217 review: hybrid (web + loopback) registrations never derive a
    refresh identity — the server cannot know which redirect the token used,
    and the registration is advertised authorization_code-only. Authorize via
    the web redirect still translates normally."""
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
    # aligned with _refresh_identity_is_reproducible
    assert refresh_id is oauth_ha_auth.RefreshDisposition.UNREPRODUCIBLE


def test_core_token_base_url_uses_plain_http_loopback_port():
    """Use core's loopback listener directly when its API is plain HTTP."""
    hass = SimpleNamespace(
        config=SimpleNamespace(api=SimpleNamespace(use_ssl=False, port=9123))
    )

    assert oauth_ha_auth.core_token_base_url(hass) == "http://127.0.0.1:9123"


def test_core_token_base_url_uses_configured_https_url(monkeypatch):
    """Use Home Assistant's configured URL when core serves HTTPS."""

    class NoURLAvailableError(Exception):
        pass

    kwargs = {}

    def configured_url(_hass, **call_kwargs):
        kwargs.update(call_kwargs)
        return "https://ha.example"

    network = ModuleType("homeassistant.helpers.network")
    network.NoURLAvailableError = NoURLAvailableError
    network.get_url = configured_url
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.network", network)
    hass = SimpleNamespace(
        config=SimpleNamespace(api=SimpleNamespace(use_ssl=True, port=8123))
    )

    assert oauth_ha_auth.core_token_base_url(hass) == "https://ha.example"
    assert kwargs == {
        "prefer_external": False,
        "allow_cloud": False,
        "require_ssl": True,
    }


def test_core_token_base_url_falls_back_when_no_url_available(monkeypatch):
    """Fall back to loopback when no configured HTTPS URL is available."""

    class NoURLAvailableError(Exception):
        pass

    def unavailable(_hass, **_kwargs):
        raise NoURLAvailableError

    network = ModuleType("homeassistant.helpers.network")
    network.NoURLAvailableError = NoURLAvailableError
    network.get_url = unavailable
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.network", network)
    hass = SimpleNamespace(
        config=SimpleNamespace(api=SimpleNamespace(use_ssl=True, port=9443))
    )

    assert oauth_ha_auth.core_token_base_url(hass) == "https://127.0.0.1:9443"


def _module_is(name: str, root: str) -> bool:
    """Return whether a module name is ``root`` or one of its children."""
    return name == root or name.startswith(f"{root}.")


@pytest.fixture
async def aiohttp_client_factory():
    """Serve real CIMD documents while the component keeps its aiohttp stub."""
    package_roots = ("aiohttp", "yarl")
    saved_modules = {
        name: module
        for name, module in tuple(sys.modules.items())
        if any(_module_is(name, root) for root in package_roots)
    }
    for name in saved_modules:
        sys.modules.pop(name, None)

    clients = []
    stub_timeout = oauth_ha_auth.CIMD_FETCH_TIMEOUT
    try:
        aiohttp = importlib.import_module("aiohttp")
        aiohttp_web = importlib.import_module("aiohttp.web")
        test_utils = importlib.import_module("aiohttp.test_utils")
        oauth_ha_auth.CIMD_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=5)

        async def factory(*, body, status, self_ref_client_id):
            doc_url = ""

            async def serve_document(_request):
                response_body = dict(body)
                if self_ref_client_id:
                    response_body["client_id"] = doc_url
                return aiohttp_web.json_response(response_body, status=status)

            app = aiohttp_web.Application()
            app.router.add_get("/client.json", serve_document)
            client = test_utils.TestClient(test_utils.TestServer(app, host="localhost"))
            await client.start_server()
            clients.append(client)
            doc_url = str(client.make_url("/client.json"))
            return doc_url, client.session

        yield factory
    finally:
        for client in clients:
            await client.close()
        oauth_ha_auth.CIMD_FETCH_TIMEOUT = stub_timeout
        for name in tuple(sys.modules):
            if any(_module_is(name, root) for root in package_roots):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


@pytest.mark.asyncio
async def test_fetch_cimd_valid_document(aiohttp_client_factory, monkeypatch):
    """Accept a valid self-identifying CIMD document."""
    doc_url, session = await aiohttp_client_factory(
        body={
            "client_id": "PLACEHOLDER_SELF",
            "client_name": "Test client",
            "redirect_uris": ["https://g.example/cb"],
        },
        status=200,
        self_ref_client_id=True,
    )
    monkeypatch.setattr(oauth_ha_auth, "_ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(oauth_ha_auth, "_is_loopback_host", lambda _host: False)
    monkeypatch.setattr(
        oauth_ha_auth,
        "_resolve_public_addresses",
        lambda _host, _port: _async_result(["127.0.0.1"]),
    )

    oauth_ha_auth._cimd_cache.clear()
    uris = await oauth_ha_auth.fetch_cimd_redirects(session, doc_url)
    assert uris == ["https://g.example/cb"]


@pytest.mark.asyncio
async def test_fetch_cimd_rejects_mismatched_client_id(
    aiohttp_client_factory, monkeypatch
):
    """Reject a CIMD document whose client ID does not round-trip."""
    doc_url, session = await aiohttp_client_factory(
        body={
            "client_id": "https://other.example/x",
            "client_name": "Test client",
            "redirect_uris": ["https://g.example/cb"],
        },
        status=200,
        self_ref_client_id=False,
    )
    monkeypatch.setattr(oauth_ha_auth, "_ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(oauth_ha_auth, "_is_loopback_host", lambda _host: False)
    monkeypatch.setattr(
        oauth_ha_auth,
        "_resolve_public_addresses",
        lambda _host, _port: _async_result(["127.0.0.1"]),
    )

    oauth_ha_auth._cimd_cache.clear()
    assert await oauth_ha_auth.fetch_cimd_redirects(session, doc_url) is None
    assert doc_url not in oauth_ha_auth._cimd_cache


@pytest.mark.asyncio
async def test_fetch_cimd_refuses_ip_literal_and_loopback():
    """Refuse CIMD fetch targets that violate the SSRF and path floor."""
    assert await oauth_ha_auth.fetch_cimd_redirects(None, "https://127.0.0.1/x") is None
    assert (
        await oauth_ha_auth.fetch_cimd_redirects(None, "https://10.0.0.5/doc.json")
        is None
    )
    assert await oauth_ha_auth.fetch_cimd_redirects(None, "https://h.example/") is None


async def _async_result(value):
    """Return a value from an async monkeypatch."""
    return value


@pytest.mark.parametrize(
    "client_id",
    [
        "https://user@example.com/client.json",
        "https://example.com/a/../client.json",
        "https://example.com/client.json#",
        "https://example.com:bad/client.json",
    ],
)
def test_cimd_client_id_rejects_forbidden_url_shapes(client_id):
    """Enforce the -00 username, dot-segment, fragment, and port MUSTs."""
    assert oauth_ha_auth._valid_cimd_client_id(client_id) is False


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
def test_parse_cimd_requires_name_and_forbids_shared_secrets(extra):
    """Enforce MCP's client_name and the -00 shared-secret prohibitions."""
    client_id = "https://client.example/client.json"
    document = {
        "client_id": client_id,
        "redirect_uris": ["https://client.example/callback"],
        **extra,
    }
    if extra:
        document["client_name"] = "Client"

    assert oauth_ha_auth._parse_cimd(json.dumps(document).encode(), client_id) is None


def test_parse_cimd_rejects_nonstandard_json_constants():
    """Reject NaN even though Python's JSON decoder accepts it by default."""
    client_id = "https://client.example/client.json"
    raw = (
        b'{"client_id":"https://client.example/client.json",'
        b'"client_name":"Client","redirect_uris":["https://client.example/cb"],'
        b'"logo_uri":NaN}'
    )

    assert oauth_ha_auth._parse_cimd(raw, client_id) is None


class _ChunkedContent:
    """A stream whose one-shot read omits later chunks (the reviewed failure)."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def read(self, _limit):
        return self._chunks[0]

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FetchResponse:
    status = 200

    def __init__(self, chunks):
        self.content = _ChunkedContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _FetchSession:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_fetch_cimd_reads_entire_stream_before_accepting(monkeypatch):
    """Reject trailing bytes beyond the cap even when read(n) returns early."""
    client_id = "https://client.example/client.json"
    valid_prefix = json.dumps(
        {
            "client_id": client_id,
            "client_name": "Client",
            "redirect_uris": ["https://client.example/cb"],
        }
    ).encode()
    session = _FetchSession(
        [_FetchResponse([valid_prefix, b" " * oauth_ha_auth.CIMD_MAX_BYTES])]
    )
    monkeypatch.setattr(
        oauth_ha_auth,
        "_resolve_public_addresses",
        lambda _host, _port: _async_result(["93.184.216.34"]),
    )
    oauth_ha_auth._cimd_cache.clear()

    assert await oauth_ha_auth.fetch_cimd_redirects(session, client_id) is None
    assert client_id not in oauth_ha_auth._cimd_cache


@pytest.mark.asyncio
async def test_invalid_cimd_is_not_negative_cached(monkeypatch):
    """A corrected document is retried immediately after an invalid response."""
    client_id = "https://client.example/client.json"
    invalid = json.dumps(
        {
            "client_id": client_id,
            "redirect_uris": ["https://client.example/cb"],
        }
    ).encode()
    valid = json.dumps(
        {
            "client_id": client_id,
            "client_name": "Client",
            "redirect_uris": ["https://client.example/cb"],
        }
    ).encode()
    session = _FetchSession([_FetchResponse([invalid]), _FetchResponse([valid])])
    monkeypatch.setattr(
        oauth_ha_auth,
        "_resolve_public_addresses",
        lambda _host, _port: _async_result(["93.184.216.34"]),
    )
    oauth_ha_auth._cimd_cache.clear()

    assert await oauth_ha_auth.fetch_cimd_redirects(session, client_id) is None
    assert await oauth_ha_auth.fetch_cimd_redirects(session, client_id) == [
        "https://client.example/cb"
    ]
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_dns_rrset_with_private_answer_is_rejected(monkeypatch):
    """Fail closed when a hostname mixes public and private DNS answers."""

    class Loop:
        async def getaddrinfo(self, *_args, **_kwargs):
            return [
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("192.168.1.10", 443)),
            ]

    monkeypatch.setattr(oauth_ha_auth.asyncio, "get_running_loop", Loop)

    assert await oauth_ha_auth._resolve_public_addresses("client.example", 443) == []


@pytest.mark.asyncio
async def test_dns_resolution_timeout_uses_live_constant(monkeypatch):
    """Apply the configurable resolver bound to each DNS lookup."""

    class Loop:
        async def getaddrinfo(self, *_args, **_kwargs):
            await asyncio.sleep(0.1)
            return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(oauth_ha_auth, "CIMD_RESOLVE_TIMEOUT", 0.01)
    monkeypatch.setattr(oauth_ha_auth.asyncio, "get_running_loop", Loop)

    assert await oauth_ha_auth._resolve_public_addresses("client.example", 443) == []


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
async def test_cimd_negative_results_are_cached(monkeypatch):
    """Failed lookups cache briefly so an anonymous caller cannot force a
    fresh resolution per request (#2213 review round 2)."""
    calls = []

    async def no_addresses(hostname, port):
        calls.append(hostname)
        return []

    monkeypatch.setattr(oauth_ha_auth, "_resolve_public_addresses", no_addresses)
    oauth_ha_auth._cimd_cache.clear()
    url = "https://dead.example/client.json"
    assert await oauth_ha_auth.fetch_cimd_redirects(object(), url) is None
    assert await oauth_ha_auth.fetch_cimd_redirects(object(), url) is None
    assert calls == ["dead.example"]  # second call served from negative cache
    oauth_ha_auth._cimd_cache.clear()


def test_cimd_cache_evicts_oldest_not_wholesale():
    """At capacity the oldest entry goes, never the whole cache (#2213 r2)."""
    oauth_ha_auth._cimd_cache.clear()
    now = 1000.0
    for i in range(oauth_ha_auth._CIMD_CACHE_MAX):
        oauth_ha_auth._cache_cimd(f"https://h{i}.example/c.json", now, ["https://x/cb"])
    oauth_ha_auth._cache_cimd("https://new.example/c.json", now, ["https://x/cb"])
    assert len(oauth_ha_auth._cimd_cache) == oauth_ha_auth._CIMD_CACHE_MAX
    assert "https://h0.example/c.json" not in oauth_ha_auth._cimd_cache  # oldest out
    assert "https://h1.example/c.json" in oauth_ha_auth._cimd_cache  # rest retained
    assert "https://new.example/c.json" in oauth_ha_auth._cimd_cache
    oauth_ha_auth._cimd_cache.clear()
