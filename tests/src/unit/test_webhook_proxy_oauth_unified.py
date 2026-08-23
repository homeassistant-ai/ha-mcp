"""Proxy ports of the unified OAuth, DCR, and CIMD regression tests."""

from __future__ import annotations

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
    """An explicit port zero remains distinct from the HTTPS default port.

    Port 0 is falsy, so a normalizer applying the scheme default with ``or``
    would collapse these two into one origin. The registration round-trips both
    URIs and ``normalized_origin`` keeps them apart, which is what the
    authorize-leg translation keys off.
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
    assert dcr.normalized_origin("https://a.example:0/cb") == ("https", "a.example", 0)
    assert dcr.normalized_origin("https://a.example/cb") == ("https", "a.example", 443)


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
    """#2248: the envelope survives a round trip and never crosses the blob.

    Both are HMAC-signed under the DCR key; the envelope's MAC covers its
    prefix and the blob's does not, so neither verifies as the other.
    """
    dcr, indirect = oauth_stack.dcr, oauth_stack.indirect
    client_id = dcr.mint_client_id(KEY, ["http://127.0.0.1/callback"])
    envelope = indirect.wrap_refresh_token(
        KEY, "core-refresh", "http://127.0.0.1:54321", client_id
    )

    assert indirect.unwrap_refresh_token(KEY, envelope, client_id) == (
        "core-refresh",
        "http://127.0.0.1:54321",
    )
    assert indirect.unwrap_refresh_token(KEY, envelope, "other-client") is None
    assert indirect.unwrap_refresh_token(KEY, client_id, client_id) is None
    assert dcr.client_redirect_uris(KEY, envelope) is None


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
