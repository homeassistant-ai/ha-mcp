"""Proxy ports of the unified OAuth, DCR, and CIMD regression tests."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from ._embedded_stubs import install

install()

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPONENT_DIR = REPO_ROOT / "homeassistant-addon-webhook-proxy-dev" / "mcp_proxy_dev"
KEY = b"k" * 32
GOOGLE_REDIRECT_URIS = [
    "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
    "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
]


def _load_submodule(monkeypatch, package_name: str, name: str):
    module_name = f"{package_name}.{name}"
    spec = importlib.util.spec_from_file_location(
        module_name, COMPONENT_DIR / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def oauth_stack(monkeypatch):
    """Load the dev proxy OAuth modules as one package with a live backend."""
    package_name = "webhook_proxy_dev_oauth_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)

    oauth = _load_submodule(monkeypatch, package_name, "oauth")

    async def _addon_alive(_hass):
        return True

    monkeypatch.setattr(oauth, "_addon_alive", _addon_alive)
    dcr = _load_submodule(monkeypatch, package_name, "oauth_dcr")
    indirect = _load_submodule(monkeypatch, package_name, "oauth_indirect")
    autoapprove = _load_submodule(monkeypatch, package_name, "oauth_autoapprove")
    auth_native = _load_submodule(monkeypatch, package_name, "auth_native")
    return SimpleNamespace(
        oauth=oauth,
        dcr=dcr,
        indirect=indirect,
        autoapprove=autoapprove,
        auth_native=auth_native,
        package_name=package_name,
    )


def _hass(oauth, mode: str, *, dcr_key: bytes | None = KEY):
    data = {
        "oauth_mode": mode,
        "target_url": "http://127.0.0.1:9583/private_x",
    }
    if dcr_key is not None:
        data["dcr_signing_key"] = dcr_key
    return SimpleNamespace(data={oauth.DOMAIN: data}, http=SimpleNamespace())


def _reader(raw: bytes, *, chunk: int | None = None):
    """An async .read(limit) over ``raw``; ``chunk`` fragments it like a real
    StreamReader, which may return early before EOF."""
    pos = 0

    async def read(limit):
        nonlocal pos
        take = min(limit, chunk) if chunk else limit
        out = raw[pos : pos + take]
        pos += len(out)
        return out

    return read


def _raw_request(raw: bytes):
    """A request whose bounded .content.read() yields ``raw``."""

    return SimpleNamespace(content=SimpleNamespace(read=_reader(raw)))


def _json_request(body):
    return _raw_request(json.dumps(body).encode())


def _oauth_request(*, query=None, form=None, host="ha.example"):
    """Build the request surface shared by the unified view tests."""
    return SimpleNamespace(
        query=dict(query or {}),
        headers={"Host": host},
        scheme="https",
        path="/api/mcp_proxy_dev/oauth/authorize",
        post=AsyncMock(return_value=dict(form or {})),
    )


def test_dcr_client_id_round_trips_multiple_redirects(oauth_stack):
    """A signed registration preserves every presented redirect URI."""
    dcr = oauth_stack.dcr
    client_id = dcr.mint_client_id(KEY, GOOGLE_REDIRECT_URIS)

    assert client_id.startswith("hamcp-dcr-")
    assert dcr.client_redirect_uris(KEY, client_id) == GOOGLE_REDIRECT_URIS
    assert dcr.client_redirect_uris(b"x" * 32, client_id) is None


async def test_dcr_none_mode_advertises_authorization_code_only(oauth_stack):
    """None-mode registrations do not promise an unsupported refresh grant."""
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    response = await dcr.DcrRegisterView(
        _hass(oauth, oauth.MODE_NONE_AUTOAPPROVE)
    ).post(_json_request({"redirect_uris": ["https://a.example/cb"]}))

    assert response.status == 201
    assert response.json_body["grant_types"] == ["authorization_code"]


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
async def test_dcr_ha_auth_advertises_refresh_for_every_registration(
    oauth_stack, redirect_uris
):
    """#2248: every ha_auth registration is refreshable, whatever its shape.

    The signed refresh envelope records the identity core bound the grant to,
    so registration no longer has to re-derive an origin — and no longer
    withholds ``refresh_token`` from loopback or multi-origin clients.
    """
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    response = await dcr.DcrRegisterView(_hass(oauth, oauth.MODE_HA_AUTH)).post(
        _json_request({"redirect_uris": redirect_uris})
    )

    assert response.status == 201
    assert response.json_body["grant_types"] == [
        "authorization_code",
        "refresh_token",
    ]
    assert dcr.client_redirect_uris(KEY, response.json_body["client_id"]) == (
        redirect_uris
    )


async def test_dcr_preserves_explicit_zero_port_origin(oauth_stack):
    """Round-trip an explicit port zero through the registration view.

    The origin identity itself is pinned next to the other translation unit
    tests, by test_normalized_origin_keeps_an_explicit_zero_port_distinct.
    """
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    redirect_uris = ["https://a.example/cb", "https://a.example:0/cb"]
    response = await dcr.DcrRegisterView(_hass(oauth, oauth.MODE_HA_AUTH)).post(
        _json_request({"redirect_uris": redirect_uris})
    )

    assert response.status == 201
    assert dcr.client_redirect_uris(KEY, response.json_body["client_id"]) == (
        redirect_uris
    )


async def test_dcr_rejects_non_loopback_http_redirect(oauth_stack):
    """Only HTTPS and RFC 8252 loopback HTTP callbacks are accepted."""
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    response = await dcr.DcrRegisterView(
        _hass(oauth, oauth.MODE_NONE_AUTOAPPROVE)
    ).post(_json_request({"redirect_uris": ["http://evil.example/cb"]}))

    assert response.status == 400
    assert response.json_body["error"] == "invalid_redirect_uri"


def test_cimd_timing_and_negative_cache_contract(oauth_stack):
    """Pin the anonymous lookup's resolver, total deadline, and cache bounds."""
    indirect = oauth_stack.indirect

    assert indirect.CIMD_RESOLVE_TIMEOUT == 5.0
    assert indirect.CIMD_TOTAL_LOOKUP_TIMEOUT == 12.0
    assert indirect.CIMD_NEGATIVE_TTL == 60.0
    assert indirect.CIMD_NEGATIVE_TTL < indirect.CIMD_CACHE_TTL


def test_cimd_redirect_matching_includes_loopback_port_variance(oauth_stack):
    """Non-loopback matches are exact; loopback runtime ports may vary."""
    indirect = oauth_stack.indirect
    assert indirect.redirect_matches(
        ["https://spark.example/cb"], "https://spark.example/cb"
    )
    assert not indirect.redirect_matches(
        ["https://spark.example/cb"], "https://spark.example/other"
    )
    assert indirect.redirect_matches(
        ["http://localhost/callback"], "http://localhost:61264/callback"
    )
    assert not indirect.redirect_matches(
        ["http://localhost/callback"], "http://localhost:61264/other"
    )


def test_normalized_origin_keeps_an_explicit_zero_port_distinct(oauth_stack):
    """Port 0 is falsy, so a normalizer using ``or`` would collapse these two.

    The authorize-leg translation keys off this identity, so ``:0`` and the
    scheme default must never compare equal (registration round-trips both
    URIs — see test_dcr_preserves_explicit_zero_port_origin).
    """
    dcr = oauth_stack.dcr

    assert dcr.normalized_origin("https://a.example:0/cb") == ("https", "a.example", 0)
    assert dcr.normalized_origin("https://a.example/cb") == ("https", "a.example", 443)


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
async def test_multi_origin_dcr_uses_presented_redirect_translation(
    oauth_stack, redirect_uri, expected
):
    """Each authorization leg translates to its matched presented origin."""
    dcr, indirect = oauth_stack.dcr, oauth_stack.indirect
    client_id = dcr.mint_client_id(KEY, GOOGLE_REDIRECT_URIS)

    translated = await indirect.resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=client_id,
        redirect_uri=redirect_uri,
    )

    assert translated == expected


async def test_explicit_default_port_translates_on_both_legs(oauth_stack, monkeypatch):
    """#2218 review: a client_id with an explicit scheme-default port misses
    the raw-netloc fast path, so authorize mints the token under the
    TRANSLATED origin — and the redirect-less refresh must re-derive that
    same origin, not pass the raw client_id through. The two legs agree."""
    indirect = oauth_stack.indirect
    redirects = AsyncMock(return_value=["https://claude.ai/api/mcp/auth_callback"])
    monkeypatch.setattr(indirect, "fetch_cimd_redirects", redirects)
    client_id = "https://claude.ai:443/oauth/client.json"

    authorize_id = await indirect.resolve_forward_client_id(
        session=object(),
        dcr_key=None,
        client_id=client_id,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )
    refresh_id = await indirect.translated_client_id_for_refresh(
        object(), None, client_id
    )

    assert authorize_id == "https://claude.ai"
    assert refresh_id == "https://claude.ai"


async def test_same_case_uppercase_pair_passes_through_on_both_legs(
    oauth_stack, monkeypatch
):
    """#2219 review: an all-uppercase pair is equal under core's raw netloc
    rule, so authorize passes it through — and refresh matches the client_id
    against the REGISTERED redirects raw, not the lowercased canonical
    origin, so it passes through too."""
    indirect = oauth_stack.indirect
    session = object()
    redirects = AsyncMock(return_value=["https://CLAUDE.AI/api/mcp/auth_callback"])
    monkeypatch.setattr(indirect, "fetch_cimd_redirects", redirects)
    client_id = "https://CLAUDE.AI/oauth/client.json"

    authorize_id = await indirect.resolve_forward_client_id(
        session=session,
        dcr_key=None,
        client_id=client_id,
        redirect_uri="https://CLAUDE.AI/api/mcp/auth_callback",
    )
    # The fast path decided authorize — no lookup.
    redirects.assert_not_awaited()
    refresh_id = await indirect.translated_client_id_for_refresh(
        session, None, client_id
    )

    assert authorize_id == client_id
    assert refresh_id is indirect.RefreshDisposition.PASSTHROUGH
    # Refresh DID fetch and matched the registered redirect — the passthrough
    # is the comparison's verdict, not a skipped lookup.
    redirects.assert_awaited_once_with(session, client_id)


@pytest.mark.parametrize(
    "presented_case, registered_case",
    [("upper", "lower"), ("lower", "lower"), ("lower", "upper")],
)
async def test_case_only_differences_pass_through_on_both_legs(
    oauth_stack, monkeypatch, presented_case, registered_case
):
    """#2219 review round 3: core's authorize leg lowercases BOTH sides
    (indieauth._parse_url) while its refresh leg compares byte-exact, so a
    case-only difference is same-origin to core and the raw client_id is what
    both legs must forward — the document's casing is author-written and the
    presented redirect is client-runtime, so they need not agree."""
    indirect = oauth_stack.indirect
    upper = "https://CLAUDE.AI/api/mcp/auth_callback"
    lower = "https://claude.ai/api/mcp/auth_callback"
    presented = upper if presented_case == "upper" else lower
    registered = [upper if registered_case == "upper" else lower]
    monkeypatch.setattr(
        indirect, "fetch_cimd_redirects", AsyncMock(return_value=registered)
    )
    client_id = "https://CLAUDE.AI/oauth/client.json"

    authorize_id = await indirect.resolve_forward_client_id(
        session=object(),
        dcr_key=None,
        client_id=client_id,
        redirect_uri=presented,
    )
    refresh_id = await indirect.translated_client_id_for_refresh(
        object(), None, client_id
    )

    assert authorize_id == client_id
    assert refresh_id is indirect.RefreshDisposition.PASSTHROUGH


async def test_mixed_port_shapes_are_unreproducible(oauth_stack, monkeypatch):
    """#2219 review round 3: port normalization makes these ONE canonical
    origin, but authorize passes through for the port-less redirect and
    translates the explicit-port one — which was presented is unrecorded, so
    the honest answer is a local invalid_grant."""
    indirect = oauth_stack.indirect
    monkeypatch.setattr(
        indirect,
        "fetch_cimd_redirects",
        AsyncMock(return_value=["https://h.example/a", "https://h.example:443/b"]),
    )

    refresh_id = await indirect.translated_client_id_for_refresh(
        object(), None, "https://h.example/client.json"
    )

    assert refresh_id is indirect.RefreshDisposition.UNREPRODUCIBLE


async def test_symmetric_explicit_port_passes_through_on_both_legs(
    oauth_stack, monkeypatch
):
    """#2219 review: when the client_id AND the registered redirect both
    carry the same explicit default port, the authorize fast path fires (raw
    netlocs equal) and the token is bound to the full client_id — so refresh
    must pass through, not translate to the canonicalized origin."""
    indirect = oauth_stack.indirect
    redirects = AsyncMock(return_value=["https://claude.ai:443/api/mcp/auth_callback"])
    monkeypatch.setattr(indirect, "fetch_cimd_redirects", redirects)
    client_id = "https://claude.ai:443/oauth/client.json"

    authorize_id = await indirect.resolve_forward_client_id(
        session=object(),
        dcr_key=None,
        client_id=client_id,
        redirect_uri="https://claude.ai:443/api/mcp/auth_callback",
    )
    refresh_id = await indirect.translated_client_id_for_refresh(
        object(), None, client_id
    )

    assert authorize_id == client_id
    assert refresh_id is indirect.RefreshDisposition.PASSTHROUGH


def test_redirect_validator_rejects_yarl_unserializable_authority(oauth_stack):
    """#2218 review: urlparse accepts a backslash-before-@ authority that
    yarl later rejects in _redirect_with — the validator must 400 it up
    front, never let it 500 on an unauthenticated view."""
    assert not oauth_stack.oauth._is_valid_redirect_uri(
        "https://client.example\\@evil.example/cb"
    )


async def test_portless_same_origin_client_passes_through_on_both_legs(
    oauth_stack, monkeypatch
):
    """claude.ai's real shape: raw netlocs match, so authorize forwards the
    full client_id untranslated and refresh passes through to match."""
    indirect = oauth_stack.indirect
    redirects = AsyncMock(return_value=["https://claude.ai/api/mcp/auth_callback"])
    monkeypatch.setattr(indirect, "fetch_cimd_redirects", redirects)
    client_id = "https://claude.ai/oauth/client.json"

    authorize_id = await indirect.resolve_forward_client_id(
        session=object(),
        dcr_key=None,
        client_id=client_id,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )
    refresh_id = await indirect.translated_client_id_for_refresh(
        object(), None, client_id
    )

    assert authorize_id == client_id
    assert refresh_id is indirect.RefreshDisposition.PASSTHROUGH


async def test_refresh_identity_has_three_variants(oauth_stack, monkeypatch):
    """Refresh derives an origin, passes through, or rejects unreproducible IDs."""
    dcr, indirect = oauth_stack.dcr, oauth_stack.indirect
    reproducible = dcr.mint_client_id(KEY, ["https://a.example/cb"])
    multi_origin = dcr.mint_client_id(KEY, GOOGLE_REDIRECT_URIS)

    assert (
        await indirect.translated_client_id_for_refresh(None, KEY, reproducible)
        == "https://a.example"
    )
    assert (
        await indirect.translated_client_id_for_refresh(None, KEY, multi_origin)
        is indirect.RefreshDisposition.UNREPRODUCIBLE
    )

    monkeypatch.setattr(indirect, "fetch_cimd_redirects", AsyncMock(return_value=None))
    assert (
        await indirect.translated_client_id_for_refresh(
            object(), None, "https://unknown.example/client.json"
        )
        is indirect.RefreshDisposition.PASSTHROUGH
    )


async def test_unreproducible_cimd_refresh_is_explicit(oauth_stack, monkeypatch):
    """Verified CIMD multi-origin identities use UNREPRODUCIBLE too."""
    indirect = oauth_stack.indirect
    monkeypatch.setattr(
        indirect,
        "fetch_cimd_redirects",
        AsyncMock(return_value=GOOGLE_REDIRECT_URIS),
    )

    translated = await indirect.translated_client_id_for_refresh(
        object(), None, "https://spark.example/client.json"
    )

    assert translated is indirect.RefreshDisposition.UNREPRODUCIBLE


async def test_cimd_unreachable_result_is_negative_cached(oauth_stack, monkeypatch):
    """Repeated requests for a dead identity do not repeat DNS resolution."""
    indirect = oauth_stack.indirect
    resolve = AsyncMock(return_value=[])
    monkeypatch.setattr(indirect, "_resolve_public_addresses", resolve)
    indirect._cimd_cache.clear()
    client_id = "https://dead.example/client.json"

    assert await indirect.fetch_cimd_redirects(object(), client_id) is None
    assert await indirect.fetch_cimd_redirects(object(), client_id) is None

    resolve.assert_awaited_once_with("dead.example", 443)
    indirect._cimd_cache.clear()


class _CimdContent:
    """Response body stream over fixed chunks."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _CimdResponse:
    status = 200

    def __init__(self, body: bytes):
        self.content = _CimdContent([body])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _CimdSession:
    """Serves queued bodies over the real fetch/parse path."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.calls: list[str] = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        return _CimdResponse(self._bodies.pop(0))


async def test_invalid_cimd_document_is_not_negative_cached(oauth_stack, monkeypatch):
    """Draft -00 §4.4.3: an INVALID document is deliberately not cached, so a
    client that fixes its metadata recovers on the very next request.

    Mirrors the component's test_invalid_cimd_is_not_negative_cached, and like
    it drives REAL bodies through _fetch_pinned_cimd and _parse_cimd rather
    than stubbing the fetch — otherwise a _cache_cimd() introduced inside the
    fetch path would stay green here while failing on the component side
    (#2218 review by Patch76). Without this pin the policy is only a comment
    on this side, and both review bots have already re-raised the
    non-caching as a defect."""
    indirect = oauth_stack.indirect
    client_id = "https://client.example/client.json"
    invalid = json.dumps(  # no client_name -> rejected by _parse_cimd
        {"client_id": client_id, "redirect_uris": ["https://client.example/cb"]}
    ).encode()
    valid = json.dumps(
        {
            "client_id": client_id,
            "client_name": "Client",
            "redirect_uris": ["https://client.example/cb"],
        }
    ).encode()
    monkeypatch.setattr(
        indirect,
        "_resolve_public_addresses",
        AsyncMock(return_value=["93.184.216.34"]),
    )
    session = _CimdSession([invalid, valid])
    indirect._cimd_cache.clear()

    assert await indirect.fetch_cimd_redirects(session, client_id) is None
    assert await indirect.fetch_cimd_redirects(session, client_id) == [
        "https://client.example/cb"
    ]
    assert len(session.calls) == 2  # re-fetched rather than served from a cache
    indirect._cimd_cache.clear()


async def test_as_documents_pin_the_claude_cimd_selection_contract(oauth_stack):
    """Every advertised URL is proxy-owned and both public modes keep CIMD."""
    oauth = oauth_stack.oauth
    base = "https://ha.example"
    hass = _hass(oauth, oauth.MODE_LEGACY)
    provider = SimpleNamespace(
        _hass=hass,
        base_url_for=lambda _request: base,
        authorization_server_url=lambda value: f"{value}{oauth.OAUTH_BASE}",
    )
    hass.data[oauth.DOMAIN]["oauth"] = provider
    legacy_doc = (
        await oauth.AuthorizationServerMetadataView(provider).get(_oauth_request())
    ).json_body
    ha_auth_doc = oauth_stack.auth_native.authorization_server_document(base)
    none_doc = oauth_stack.autoapprove.authorization_server_document(base)

    for doc in (ha_auth_doc, none_doc):
        assert doc["client_id_metadata_document_supported"] is True
        assert "none" in doc["token_endpoint_auth_methods_supported"]
        assert doc["registration_endpoint"] == (f"{base}{oauth.OAUTH_BASE}/register")

    for doc in (ha_auth_doc, none_doc, legacy_doc):
        assert doc["authorization_endpoint"] == (f"{base}{oauth.OAUTH_BASE}/authorize")
        assert doc["token_endpoint"] == f"{base}{oauth.OAUTH_BASE}/token"
        assert doc["code_challenge_methods_supported"] == ["S256"]
        assert doc["issuer"] == f"{base}{oauth.OAUTH_BASE}"

    assert "registration_endpoint" not in legacy_doc
    assert "client_secret_basic" in legacy_doc["token_endpoint_auth_methods_supported"]


async def test_unified_authorize_dispatches_legacy_handler(oauth_stack, monkeypatch):
    """The scoped authorize route reuses the extracted legacy implementation."""
    oauth, autoapprove = oauth_stack.oauth, oauth_stack.autoapprove
    provider = object()
    hass = _hass(oauth, oauth.MODE_LEGACY)
    hass.data[oauth.DOMAIN]["oauth"] = provider
    sentinel = object()
    handler = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(autoapprove, "handle_legacy_authorize_get", handler)

    response = await autoapprove.AutoApproveAuthorizeView(hass).get(_oauth_request())

    assert response is sentinel
    handler.assert_awaited_once()
    assert handler.await_args.args[0] is provider


async def test_none_mode_autoapproves_any_valid_redirect(oauth_stack):
    """None mode has no client allowlist after the maintainer decision."""
    oauth, autoapprove = oauth_stack.oauth, oauth_stack.autoapprove
    hass = _hass(oauth, oauth.MODE_NONE_AUTOAPPROVE)
    provider = autoapprove.AutoApproveProvider(hass, "mcp_test", None)
    hass.data[oauth.DOMAIN][oauth.AUTOAPPROVE_PROVIDER_KEY] = provider
    redirect_uri = "https://connector.example/oauth/callback"

    response = await autoapprove.AutoApproveAuthorizeView(hass).get(
        _oauth_request(
            query={
                "response_type": "code",
                "client_id": "https://metadata.example/client.json",
                "redirect_uri": redirect_uri,
                "code_challenge": "A" * 43,
                "code_challenge_method": "S256",
                "state": "state-1",
            }
        )
    )

    assert response.status == 302
    location = response.headers["Location"]
    assert f"{urlparse(location).scheme}://{urlparse(location).netloc}" == (
        "https://connector.example"
    )
    assert parse_qs(urlparse(location).query)["state"] == ["state-1"]


async def test_ha_auth_authorize_uses_dedicated_cimd_session(oauth_stack, monkeypatch):
    """Public metadata lookup never borrows the authenticated relay session."""
    oauth = oauth_stack.oauth
    autoapprove = oauth_stack.autoapprove
    indirect = oauth_stack.indirect
    hass = _hass(oauth, oauth.MODE_HA_AUTH)
    cimd_session = object()
    relay_session = object()
    data = hass.data[oauth.DOMAIN]
    data[autoapprove.CFG_CIMD_SESSION] = cimd_session
    data["session"] = relay_session
    resolver = AsyncMock(return_value="https://callback.example")
    monkeypatch.setattr(indirect, "resolve_forward_client_id", resolver)

    response = await autoapprove.AutoApproveAuthorizeView(hass).get(
        _oauth_request(
            query={
                "client_id": "https://metadata.example/client.json",
                "redirect_uri": "https://callback.example/cb",
            }
        )
    )

    assert response.status == 302
    resolver.assert_awaited_once_with(
        cimd_session,
        KEY,
        "https://metadata.example/client.json",
        "https://callback.example/cb",
    )
    forwarded = parse_qs(urlparse(response.headers["Location"]).query)
    assert forwarded["client_id"] == ["https://callback.example"]


async def test_ha_auth_token_307s_passthrough_identity(oauth_stack):
    """An unchanged token body stays client-side so core sees the real IP."""
    oauth, autoapprove = oauth_stack.oauth, oauth_stack.autoapprove
    hass = _hass(oauth, oauth.MODE_HA_AUTH, dcr_key=None)

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "authorization_code",
                "client_id": "https://client.example/metadata.json",
                "redirect_uri": "https://client.example/callback",
            }
        )
    )

    assert response.status == 307
    assert response.headers == {
        "Location": "/auth/token",
        "Cache-Control": "no-store",
    }


async def test_ha_auth_refresh_rejects_unreproducible_identity_locally(oauth_stack):
    """A pre-#2248 multi-origin refresh never enters core's failed-login
    accounting. Only tokens minted before the signed envelope reach this
    guard — an envelope names its identity outright."""
    oauth, dcr, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.autoapprove,
    )
    hass = _hass(oauth, oauth.MODE_HA_AUTH)
    client_id = dcr.mint_client_id(KEY, GOOGLE_REDIRECT_URIS)

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": "opaque",
            }
        )
    )

    assert response.status == 400
    assert response.json_body["error"] == "invalid_grant"


async def test_ha_auth_refresh_with_redirect_translates_presented_origin(
    oauth_stack, monkeypatch
):
    """A redirect-carrying refresh remains usable for multi-origin clients."""
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    redirect_uri = GOOGLE_REDIRECT_URIS[0]
    client_id = dcr.mint_client_id(KEY, GOOGLE_REDIRECT_URIS)
    core_response = SimpleNamespace(
        status=200,
        content_type="application/json",
        read=AsyncMock(return_value=b'{"access_token":"core"}'),
    )

    class _CoreRequest:
        async def __aenter__(self):
            return core_response

        async def __aexit__(self, *_args):
            return False

    relay_session = SimpleNamespace(post=MagicMock(return_value=_CoreRequest()))
    hass = _hass(oauth, oauth.MODE_HA_AUTH)
    hass.data[oauth.DOMAIN]["session"] = relay_session
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "refresh_token",
                "refresh_token": "opaque",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
            }
        )
    )

    assert response.status == 200
    forwarded = relay_session.post.call_args.kwargs["data"]
    assert forwarded["client_id"] == ("https://oauth-redirect.googleusercontent.com")


def _hand_signed_envelope(indirect, payload: object) -> str:
    """Sign an arbitrary envelope payload under KEY, exactly as the code does."""
    body = indirect._b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(
        KEY, f"hamcp-rt-{body}".encode("ascii"), hashlib.sha256
    ).digest()
    return f"hamcp-rt-{body}.{indirect._b64url_encode(signature)}"


def _core_relay_session(body: bytes, status: int = 200):
    """A relay session whose core /auth/token answer is ``body``/``status``."""
    core_response = SimpleNamespace(
        status=status,
        content_type="application/json",
        read=AsyncMock(return_value=body),
    )

    class _CoreRequest:
        async def __aenter__(self):
            return core_response

        async def __aexit__(self, *_args):
            return False

    return SimpleNamespace(post=MagicMock(return_value=_CoreRequest()))


def test_refresh_envelope_round_trips_and_is_disjoint_from_the_dcr_blob(oauth_stack):
    """#2248: relabelling one blob family as the other does not make it verify.

    Both are HMAC-signed under the DCR key, and the only thing separating them
    is that the envelope's MAC covers its prefix while the blob's covers the
    bare body. Swapping the prefixes is exactly the attack that difference
    exists to stop, so both directions are driven here rather than merely
    handing each verifier the other's intact string.
    """
    dcr, indirect = oauth_stack.dcr, oauth_stack.indirect
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


def test_refresh_envelope_rejects_tampering_a_rotated_key_and_bad_payloads(oauth_stack):
    """A bad MAC, a rotated signing key, or a wrong payload shape is INVALID.

    The signature only proves WE minted the blob, so the version and type
    guards behind it need a valid MAC to be exercised at all — hence the
    hand-signed payload rather than one ``wrap_refresh_token`` could produce.
    """
    indirect = oauth_stack.indirect
    states = indirect.EnvelopeState
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "https://a.example", "cid"
    )
    body, _, signature = envelope.rpartition(".")
    tampered = f"{body}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"

    assert indirect.unwrap_refresh_token(KEY, tampered, "cid") is states.INVALID
    assert indirect.unwrap_refresh_token(b"x" * 32, envelope, "cid") is states.INVALID

    future_version = _hand_signed_envelope(
        indirect, {"v": 2, "t": "core-refresh", "c": "https://a.example", "p": "x"}
    )
    assert indirect.unwrap_refresh_token(KEY, future_version, "cid") is states.INVALID


def test_refresh_envelope_skips_the_presenter_binding_when_none(oauth_stack):
    """A None presenter unwraps any intact envelope (RFC 7009 revocation).

    Revocation authorizes the BEARER of the token, not a client identity, so
    the revoke path recovers core's token without knowing which client_id the
    envelope was minted alongside.
    """
    indirect = oauth_stack.indirect
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "https://a.example", "cid-a"
    )

    assert indirect.unwrap_refresh_token(KEY, envelope, None) == (
        "core-refresh",
        "https://a.example",
    )


ROTATED_KEY = b"r" * 32


def test_core_token_for_revocation_reads_verified_and_unverifiable_envelopes(
    oauth_stack,
):
    """#2249 review: revocation recovers core's token even on a failed MAC.

    The rotated key is the case that matters — removing and re-adding the
    integration mints a new DCR key, invalidating every envelope already in a
    client's hands. Forwarding a body we did not verify is sound here and only
    here: RFC 7009 authorizes the bearer, and core's revoke endpoint is
    anonymous and idempotent, so a forger gains nothing they could not get by
    POSTing to core themselves.
    """
    indirect = oauth_stack.indirect
    envelope = indirect.wrap_refresh_token(KEY, "core-rt", "https://a.example", "cid")
    body, _, signature = envelope.rpartition(".")
    tampered = f"{body}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"
    rotated = indirect.wrap_refresh_token(
        ROTATED_KEY, "core-rt", "https://a.example", "cid"
    )

    assert indirect.core_token_for_revocation(KEY, envelope) == "core-rt"
    assert indirect.core_token_for_revocation(KEY, tampered) == "core-rt"
    assert indirect.core_token_for_revocation(KEY, rotated) == "core-rt"
    # No key configured means no envelope of ours to recognise at all.
    assert indirect.core_token_for_revocation(None, envelope) is None
    # And the DCR blob family stays disjoint from the envelope family.
    blob = oauth_stack.dcr.mint_client_id(KEY, ["https://a.example/cb"])
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
def test_core_token_for_revocation_returns_none_for_unusable_values(oauth_stack, token):
    """Nothing to substitute, so the caller forwards the value as presented."""
    assert oauth_stack.indirect.core_token_for_revocation(KEY, token) is None


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
def test_core_token_for_revocation_refuses_unusable_bodies(oauth_stack, payload):
    """A body that yields no string token is refused, never guessed at.

    The nesting case is the #2218 guard: this parse runs BEFORE any MAC check,
    so json.loads sees caller-chosen nesting and can raise RecursionError
    where unwrap_refresh_token never can.
    """
    indirect = oauth_stack.indirect
    encoded = indirect._b64url_encode(payload)
    token = f"hamcp-rt-{encoded}.not-a-real-signature"

    assert len(token) <= indirect.MAX_REVOKE_ENVELOPE_LEN  # not the cap at work
    assert indirect.core_token_for_revocation(KEY, token) is None


def test_core_token_for_revocation_caps_the_unverified_parse(oauth_stack):
    """An oversized prefixed value is refused BEFORE it is decoded.

    Core's refresh tokens are short, so nothing legitimate approaches the cap;
    it exists because this parse is the one place an anonymous view hands
    attacker-chosen bytes to base64 and json.loads. It guards only that path —
    an envelope the proxy can still VERIFY is honoured at any size.
    """
    indirect = oauth_stack.indirect
    huge = "T" * indirect.MAX_REVOKE_ENVELOPE_LEN
    rotated = indirect.wrap_refresh_token(ROTATED_KEY, huge, "https://a.example", "cid")
    ours = indirect.wrap_refresh_token(KEY, huge, "https://a.example", "cid")

    assert len(rotated) > indirect.MAX_REVOKE_ENVELOPE_LEN
    assert indirect.core_token_for_revocation(KEY, rotated) is None
    assert indirect.core_token_for_revocation(KEY, ours) == huge


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
def test_rewrite_token_response_body_leaves_other_bodies_byte_identical(
    oauth_stack, body
):
    """Relay anything with no string refresh_token to wrap, byte for byte."""
    indirect = oauth_stack.indirect

    assert (
        indirect.rewrite_token_response_body(KEY, body, "https://a.example", "cid")
        is body
    )


async def test_ha_auth_code_leg_wraps_the_core_refresh_token(oauth_stack, monkeypatch):
    """A loopback client's code exchange comes back with a wrapped token."""
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    relay_session = _core_relay_session(
        b'{"access_token":"core","refresh_token":"core-refresh"}'
    )
    hass = _hass(oauth, oauth.MODE_HA_AUTH)
    hass.data[oauth.DOMAIN]["session"] = relay_session
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "authorization_code",
                "code": "code-1",
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:54321/callback",
            }
        )
    )

    assert response.status == 200
    body = json.loads(response.body)
    assert body["access_token"] == "core"
    assert indirect.unwrap_refresh_token(KEY, body["refresh_token"], client_id) == (
        "core-refresh",
        "http://127.0.0.1:54321",
    )


async def test_ha_auth_refresh_envelope_restores_core_token_and_identity(
    oauth_stack, monkeypatch
):
    """A redirect-less loopback refresh proxies with the envelope's pair."""
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )
    relay_session = _core_relay_session(b'{"access_token":"core"}')
    hass = _hass(oauth, oauth.MODE_HA_AUTH)
    hass.data[oauth.DOMAIN]["session"] = relay_session
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "refresh_token",
                "refresh_token": envelope,
                "client_id": client_id,
            }
        )
    )

    assert response.status == 200
    forwarded = relay_session.post.call_args.kwargs["data"]
    assert forwarded["client_id"] == "http://127.0.0.1:54321"
    assert forwarded["refresh_token"] == "core-refresh"


CIMD_CLIENT_ID = "https://app.example/cimd.json"
CIMD_SAME_ORIGIN_REDIRECT = "https://app.example/cb"
HYBRID_CIMD_REDIRECTS = [CIMD_SAME_ORIGIN_REDIRECT, "https://eu.example/cb"]


def _ha_auth_hass(oauth, relay_session, *, cimd_session=None):
    """A live ha_auth backend with a relay session bound for proxying."""
    hass = _hass(oauth, oauth.MODE_HA_AUTH)
    hass.data[oauth.DOMAIN]["session"] = relay_session
    if cimd_session is not None:
        hass.data[oauth.DOMAIN]["cimd_session"] = cimd_session
    return hass


def _pin_cimd_document(monkeypatch, indirect, redirect_uris: list[str]) -> None:
    """Serve ``redirect_uris`` for every CIMD lookup on both token legs."""

    async def fetch_redirects(_session, _client_id):
        return list(redirect_uris)

    monkeypatch.setattr(indirect, "fetch_cimd_redirects", fetch_redirects)
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )


async def test_ha_auth_code_leg_proxies_a_hybrid_cimd_same_origin_exchange(
    oauth_stack, monkeypatch
):
    """A hybrid CIMD identity presenting its same-origin redirect gets wrapped.

    The authorize leg's same-origin fast path returns the client_id without
    fetching, so this exchange used to 307 and hand back core's RAW refresh
    token — whose redirect-less refresh then derived UNREPRODUCIBLE (two web
    origins) and answered invalid_grant forever. The code leg now pays that one
    fetch, proxies, and records the identity in the token instead.
    """
    oauth, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    _pin_cimd_document(monkeypatch, indirect, HYBRID_CIMD_REDIRECTS)
    relay_session = _core_relay_session(
        b'{"access_token":"core","refresh_token":"core-refresh"}'
    )
    hass = _ha_auth_hass(oauth, relay_session, cimd_session=object())

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "authorization_code",
                "code": "code-1",
                "client_id": CIMD_CLIENT_ID,
                "redirect_uri": CIMD_SAME_ORIGIN_REDIRECT,
            }
        )
    )

    assert response.status == 200
    assert relay_session.post.call_args.kwargs["data"]["client_id"] == CIMD_CLIENT_ID
    body = json.loads(response.body)
    assert indirect.unwrap_refresh_token(
        KEY, body["refresh_token"], CIMD_CLIENT_ID
    ) == ("core-refresh", CIMD_CLIENT_ID)


async def test_ha_auth_refresh_of_a_hybrid_cimd_envelope_keeps_the_client_id(
    oauth_stack, monkeypatch
):
    """The refresh leg of that exchange proxies the untranslated client_id."""
    oauth, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    _pin_cimd_document(monkeypatch, indirect, HYBRID_CIMD_REDIRECTS)
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", CIMD_CLIENT_ID, CIMD_CLIENT_ID
    )
    relay_session = _core_relay_session(b'{"access_token":"core"}')
    hass = _ha_auth_hass(oauth, relay_session, cimd_session=object())

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "refresh_token",
                "refresh_token": envelope,
                "client_id": CIMD_CLIENT_ID,
            }
        )
    )

    assert response.status == 200
    forwarded = relay_session.post.call_args.kwargs["data"]
    assert forwarded["client_id"] == CIMD_CLIENT_ID
    assert forwarded["refresh_token"] == "core-refresh"


async def test_ha_auth_code_leg_still_307s_a_same_origin_only_cimd_client(
    oauth_stack, monkeypatch
):
    """A CIMD document with only same-origin redirects keeps the 307.

    Its redirect-less refresh re-derives PASSTHROUGH, so there is nothing to
    record and core must keep seeing the client's own address.
    """
    oauth, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    _pin_cimd_document(monkeypatch, indirect, [CIMD_SAME_ORIGIN_REDIRECT])
    relay_session = _core_relay_session(b'{"access_token":"core"}')
    hass = _ha_auth_hass(oauth, relay_session, cimd_session=object())

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "authorization_code",
                "code": "code-1",
                "client_id": CIMD_CLIENT_ID,
                "redirect_uri": CIMD_SAME_ORIGIN_REDIRECT,
            }
        )
    )

    assert response.status == 307
    assert response.headers["Location"] == "/auth/token"
    relay_session.post.assert_not_called()


async def test_ha_auth_refresh_leg_rewraps_a_rotated_core_refresh_token(
    oauth_stack, monkeypatch
):
    """A refresh that rotates core's token comes back wrapped again.

    ``rewrite_token_response_body`` runs on EVERY forwarded 200, so a core that
    starts rotating refresh tokens does not hand the client a bare one.
    """
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh-1", "http://127.0.0.1:54321", client_id
    )
    relay_session = _core_relay_session(
        b'{"access_token":"core-2","refresh_token":"core-refresh-2"}'
    )
    hass = _ha_auth_hass(oauth, relay_session)
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "refresh_token",
                "refresh_token": envelope,
                "client_id": client_id,
            }
        )
    )

    assert response.status == 200
    body = json.loads(response.body)
    assert indirect.unwrap_refresh_token(KEY, body["refresh_token"], client_id) == (
        "core-refresh-2",
        "http://127.0.0.1:54321",
    )


async def test_ha_auth_non_200_core_response_is_relayed_unwrapped(
    oauth_stack, monkeypatch
):
    """Only a 200 gets its refresh_token wrapped; errors relay byte for byte."""
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    relay_session = _core_relay_session(
        b'{"error":"invalid_grant","refresh_token":"core-refresh"}', status=400
    )
    hass = _ha_auth_hass(oauth, relay_session)
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "authorization_code",
                "code": "code-1",
                "client_id": client_id,
                "redirect_uri": "http://127.0.0.1:54321/callback",
            }
        )
    )

    assert response.status == 400
    assert json.loads(response.body)["refresh_token"] == "core-refresh"


async def test_ha_auth_revoke_unwraps_the_envelope_before_forwarding(
    oauth_stack, monkeypatch
):
    """Core must revoke ITS token, not the envelope we handed the client.

    ``/auth/token`` answers 200 to ``action=revoke`` even for a token it has
    never seen, so forwarding the envelope would report success while leaving
    the session live. Core's real revoke answer is an empty 200; the stub
    returns a token-shaped body instead to pin that the response rewrite is
    skipped on this path.
    """
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )
    relay_session = _core_relay_session(
        b'{"access_token":"core","refresh_token":"core-refresh"}'
    )
    hass = _ha_auth_hass(oauth, relay_session)
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(form={"action": "revoke", "token": envelope})
    )

    assert response.status == 200
    assert relay_session.post.call_args.kwargs["data"]["token"] == "core-refresh"
    assert json.loads(response.body)["refresh_token"] == "core-refresh"


async def test_ha_auth_revoke_of_a_plain_token_is_unchanged(oauth_stack):
    """A revoke carrying no envelope 307s to core exactly as it did before."""
    oauth, autoapprove = oauth_stack.oauth, oauth_stack.autoapprove
    relay_session = _core_relay_session(b"")
    hass = _ha_auth_hass(oauth, relay_session)

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(form={"action": "revoke", "token": "core-opaque-refresh-token"})
    )

    assert response.status == 307
    assert response.headers["Location"] == "/auth/token"
    relay_session.post.assert_not_called()


async def test_fast_path_passes_through_malformed_port_client_id(oauth_stack):
    """#2218 review: a client_id with an invalid port must pass through for
    core to reject — normalized_origin() reads parsed.port, which raises
    ValueError, so the fast path compares raw netlocs like the component."""
    resolved = await oauth_stack.indirect.resolve_forward_client_id(
        None,
        None,
        "https://client.example:99999/metadata.json",
        "https://client.example/cb",
    )

    assert resolved == "https://client.example:99999/metadata.json"


async def test_dcr_register_rejects_deeply_nested_json(oauth_stack):
    """#2218 review: json.loads raises RecursionError on a deeply nested
    body — malformed metadata answers 400, never a 500."""
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr

    payload = b"[" * 30000 + b"]" * 30000
    assert len(payload) < dcr.MAX_DCR_BODY_BYTES
    request = _raw_request(payload)
    response = await dcr.DcrRegisterView(_hass(oauth, oauth.MODE_HA_AUTH)).post(request)

    assert response.status == 400
    assert response.json_body["error"] == "invalid_client_metadata"
    # The description distinguishes the arms: both guards answer the same
    # error code, so only this pins that the PARSER rejected it.
    assert response.json_body["error_description"] == "body must be JSON"


async def test_dcr_register_reassembles_a_chunked_body(oauth_stack):
    """#2219 review round 3: a fragmented body must be read to EOF — a single
    StreamReader.read() can return early and truncate the document."""
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    body = json.dumps({"redirect_uris": ["https://a.example/cb"]}).encode()

    request = SimpleNamespace(content=SimpleNamespace(read=_reader(body, chunk=7)))
    response = await dcr.DcrRegisterView(_hass(oauth, oauth.MODE_HA_AUTH)).post(request)

    assert response.status == 201


async def test_dcr_register_rejects_oversized_body(oauth_stack):
    """#2219 review round 3: a conforming registration is a few KB, so the
    read is capped rather than riding HA's 16 MiB client_max_size."""
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    oversized = b"x" * (dcr.MAX_DCR_BODY_BYTES + 1)

    request = SimpleNamespace(content=SimpleNamespace(read=_reader(oversized)))
    response = await dcr.DcrRegisterView(_hass(oauth, oauth.MODE_HA_AUTH)).post(request)

    assert response.status == 400
    assert response.json_body["error"] == "invalid_client_metadata"
    assert response.json_body["error_description"] == "body is too large"


# ---------------------------------------------------------------------------
# ha_auth scoped RFC 7009 revocation endpoint (issue #2248)
# ---------------------------------------------------------------------------


async def test_scoped_revoke_forwards_the_unwrapped_envelope(oauth_stack, monkeypatch):
    """The envelope is swapped for core's own token before core sees it.

    Core's ``/auth/revoke`` answers 200 for a token it has never seen, so a
    client posting the envelope there directly would be told its session was
    revoked while it stayed live.
    """
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )
    relay_session = _core_relay_session(b"")
    hass = _ha_auth_hass(oauth, relay_session)
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await autoapprove.AutoApproveRevokeView(hass).post(
        _oauth_request(form={"token": envelope})
    )

    assert response.status == 200
    assert relay_session.post.call_args.args[0] == "https://core.example/auth/revoke"
    assert relay_session.post.call_args.kwargs["data"]["token"] == "core-refresh"


async def test_scoped_revoke_of_a_plain_token_307s_to_core(oauth_stack):
    """A token that is not ours reaches core unchanged, from the client."""
    oauth, autoapprove = oauth_stack.oauth, oauth_stack.autoapprove
    relay_session = _core_relay_session(b"")
    hass = _ha_auth_hass(oauth, relay_session)

    response = await autoapprove.AutoApproveRevokeView(hass).post(
        _oauth_request(form={"token": "core-opaque-refresh-token"})
    )

    assert response.status == 307
    assert response.headers["Location"] == "/auth/revoke"
    assert response.headers["Cache-Control"] == "no-store"
    relay_session.post.assert_not_called()


async def test_scoped_revoke_without_a_dcr_key_307s_to_core(oauth_stack):
    """No signing key means no envelope could have been minted, so there is
    nothing to unwrap and the revocation 307s like any other token."""
    oauth, autoapprove = oauth_stack.oauth, oauth_stack.autoapprove
    relay_session = _core_relay_session(b"")
    hass = _hass(oauth, oauth.MODE_HA_AUTH, dcr_key=None)
    hass.data[oauth.DOMAIN]["session"] = relay_session

    response = await autoapprove.AutoApproveRevokeView(hass).post(
        _oauth_request(form={"token": "hamcp-rt-whatever.sig"})
    )

    assert response.status == 307
    relay_session.post.assert_not_called()


@pytest.mark.parametrize("mode", ["MODE_NONE_AUTOAPPROVE", "MODE_LEGACY"])
async def test_scoped_revoke_404s_outside_ha_auth(oauth_stack, mode):
    """Only ha_auth hands out an envelope, so only ha_auth fronts revocation."""
    oauth, autoapprove = oauth_stack.oauth, oauth_stack.autoapprove
    hass = _hass(oauth, getattr(oauth, mode))

    response = await autoapprove.AutoApproveRevokeView(hass).post(
        _oauth_request(form={"token": "anything"})
    )

    assert response.status == 404
    assert response.json_body["error"] == "not_found"


async def test_scoped_revoke_transport_error_returns_temporarily_unavailable(
    oauth_stack, monkeypatch
):
    """A failed forward is a 503, not a fabricated revocation success.

    RFC 7009 2.2.1 gives that status a specific meaning on this endpoint --
    the token must be assumed to still exist -- and lets the server name the
    retry delay, so the client does not have to guess or give up.
    """
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )
    relay_session = SimpleNamespace(post=MagicMock(side_effect=TimeoutError()))
    hass = _ha_auth_hass(oauth, relay_session)
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await autoapprove.AutoApproveRevokeView(hass).post(
        _oauth_request(form={"token": envelope})
    )

    assert response.status == 503
    assert response.json_body["error"] == "temporarily_unavailable"
    assert response.headers["Retry-After"] == autoapprove._REVOKE_RETRY_AFTER


# ---------------------------------------------------------------------------
# Revoking an envelope the signing key can no longer verify (#2249 review)
# ---------------------------------------------------------------------------


# Both spellings core accepts a revocation in, and the core path each reaches.
REVOKE_SURFACES = {"token-action": "/auth/token", "scoped-revoke": "/auth/revoke"}


async def _post_revocation(autoapprove, hass, surface: str, token: str):
    """POST ``token`` as a revocation at one of the two surfaces."""
    if surface == "token-action":
        return await autoapprove.AutoApproveTokenView(hass).post(
            _oauth_request(form={"action": "revoke", "token": token})
        )
    return await autoapprove.AutoApproveRevokeView(hass).post(
        _oauth_request(form={"token": token})
    )


@pytest.mark.parametrize("surface", sorted(REVOKE_SURFACES))
@pytest.mark.parametrize("unverifiable", ["tampered", "rotated-key"])
async def test_revocation_forwards_an_unverifiable_envelope(
    oauth_stack, monkeypatch, surface, unverifiable
):
    """Core must see ITS token even when the envelope no longer verifies.

    Rotating the DCR signing key — which is what removing and re-adding the
    integration does — invalidates every envelope already in a client's hands.
    Treating those as "not ours" would 307 the ``hamcp-rt-`` string to core,
    whose revoke endpoint answers 200 for any token it cannot resolve: the
    client is told the session died while core's grant stays live for its full
    90 days (#2249 review). Tampering takes the same branch and is pinned with
    it, because forwarding an unverified body is exactly what has to be safe
    here — RFC 7009 authorizes the bearer, and core's endpoint is anonymous
    and idempotent.
    """
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    envelope = indirect.wrap_refresh_token(
        ROTATED_KEY if unverifiable == "rotated-key" else KEY,
        "core-refresh",
        "http://127.0.0.1:54321",
        dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"]),
    )
    if unverifiable == "tampered":
        body, _, signature = envelope.rpartition(".")
        envelope = f"{body}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"
    relay_session = _core_relay_session(b"")
    hass = _ha_auth_hass(oauth, relay_session)
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await _post_revocation(autoapprove, hass, surface, envelope)

    assert response.status == 200
    core_url = f"https://core.example{REVOKE_SURFACES[surface]}"
    assert relay_session.post.call_args.args[0] == core_url
    assert relay_session.post.call_args.kwargs["data"]["token"] == "core-refresh"


@pytest.mark.parametrize("surface", sorted(REVOKE_SURFACES))
@pytest.mark.parametrize(
    "blob",
    [
        pytest.param("nodot", id="no-separator"),
        pytest.param("###.not-a-real-signature", id="undecodable-base64"),
        pytest.param(b"not json", id="not-json"),
        pytest.param(b'["not","an","object"]', id="json-array"),
        pytest.param(b'{"v":1,"t":42}', id="int-t"),
    ],
)
async def test_revocation_307s_a_prefixed_value_carrying_no_token(
    oauth_stack, surface, blob
):
    """Our prefix over an unreadable body is not a token we can substitute.

    The best-effort unwrap gives up rather than inventing a value, so these
    reach core exactly as presented and core answers them — the proxy makes no
    outbound call at all. ``bytes`` are encoded into an envelope-shaped body;
    a ``str`` is the malformed blob verbatim.
    """
    oauth, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    if isinstance(blob, bytes):
        blob = f"{indirect._b64url_encode(blob)}.not-a-real-signature"
    relay_session = _core_relay_session(b"")
    hass = _ha_auth_hass(oauth, relay_session)

    response = await _post_revocation(autoapprove, hass, surface, f"hamcp-rt-{blob}")

    assert response.status == 307
    assert response.headers["Location"] == REVOKE_SURFACES[surface]
    relay_session.post.assert_not_called()


@pytest.mark.parametrize("surface", sorted(REVOKE_SURFACES))
async def test_revocation_307s_an_over_long_prefixed_value(oauth_stack, surface):
    """Past the cap the body is never decoded, so there is nothing to swap in.

    The value is a well-formed envelope under another key — only its SIZE
    stops it being parsed, which is the point of the cap on an anonymous view.
    """
    oauth, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    oversized = indirect.wrap_refresh_token(
        ROTATED_KEY,
        "T" * indirect.MAX_REVOKE_ENVELOPE_LEN,
        "http://127.0.0.1:54321",
        "cid",
    )
    relay_session = _core_relay_session(b"")
    hass = _ha_auth_hass(oauth, relay_session)

    response = await _post_revocation(autoapprove, hass, surface, oversized)

    assert len(oversized) > indirect.MAX_REVOKE_ENVELOPE_LEN
    assert response.status == 307
    relay_session.post.assert_not_called()


@pytest.mark.parametrize("surface", sorted(REVOKE_SURFACES))
async def test_revocation_503_names_the_retry_delay_on_both_surfaces(
    oauth_stack, monkeypatch, surface
):
    """RFC 7009 §2.2.1 attaches the retry contract to the REQUEST, not the URL.

    ``action=revoke`` on the token endpoint is the same revocation as one
    posted to the scoped view, so it cannot answer a barer 503 than the scoped
    view does (#2249 review).
    """
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    envelope = indirect.wrap_refresh_token(
        KEY,
        "core-refresh",
        "http://127.0.0.1:54321",
        dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"]),
    )
    relay_session = SimpleNamespace(post=MagicMock(side_effect=TimeoutError()))
    hass = _ha_auth_hass(oauth, relay_session)
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await _post_revocation(autoapprove, hass, surface, envelope)

    assert response.status == 503
    assert response.json_body["error"] == "temporarily_unavailable"
    assert response.headers["Retry-After"] == autoapprove._REVOKE_RETRY_AFTER


async def test_non_revocation_token_503_carries_no_retry_after(
    oauth_stack, monkeypatch
):
    """A refresh that core never answered gets a bare 503.

    RFC 6749 gives an ordinary token failure no retry contract, so the header
    belongs to revocation alone — the envelope here forces the same proxy leg,
    which is what makes this the exact counterpart of the test above.
    """
    oauth, dcr, indirect, autoapprove = (
        oauth_stack.oauth,
        oauth_stack.dcr,
        oauth_stack.indirect,
        oauth_stack.autoapprove,
    )
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )
    relay_session = SimpleNamespace(post=MagicMock(side_effect=TimeoutError()))
    hass = _ha_auth_hass(oauth, relay_session)
    monkeypatch.setattr(
        indirect, "core_token_base_url", lambda _hass: "https://core.example"
    )

    response = await autoapprove.AutoApproveTokenView(hass).post(
        _oauth_request(
            form={
                "grant_type": "refresh_token",
                "refresh_token": envelope,
                "client_id": client_id,
            }
        )
    )

    assert response.status == 503
    assert "Retry-After" not in response.headers


async def test_ha_auth_document_advertises_the_scoped_revocation_endpoint(
    oauth_stack,
):
    """#2248: the client holds an envelope, so revocation must come to us.

    None mode issues no refresh token at all, so its document keeps quiet —
    advertising a route that 404s there would be worse than silence.
    """
    oauth = oauth_stack.oauth
    base = "https://ha.example"
    ha_auth_doc = oauth_stack.auth_native.authorization_server_document(base)
    assert ha_auth_doc["revocation_endpoint"] == f"{base}{oauth.OAUTH_BASE}/revoke"
    assert ha_auth_doc["revocation_endpoint_auth_methods_supported"] == ["none"]

    none_doc = oauth_stack.autoapprove.authorization_server_document(base)
    assert "revocation_endpoint" not in none_doc


def test_register_autoapprove_views_binds_three_views(oauth_stack):
    """/revoke is advertised by the ha_auth document, so the bundle carries it."""
    oauth, autoapprove = oauth_stack.oauth, oauth_stack.autoapprove
    hass = _hass(oauth, oauth.MODE_HA_AUTH)
    hass.http.register_view = MagicMock()

    autoapprove.register_autoapprove_views(hass)
    autoapprove.register_autoapprove_views(hass)  # bind-once guard

    assert hass.http.register_view.call_count == 3
    bound = {type(call.args[0]) for call in hass.http.register_view.call_args_list}
    assert bound == {
        autoapprove.AutoApproveAuthorizeView,
        autoapprove.AutoApproveTokenView,
        autoapprove.AutoApproveRevokeView,
    }
