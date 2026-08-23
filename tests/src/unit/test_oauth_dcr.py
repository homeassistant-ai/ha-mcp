"""Unit tests for the stateless DCR client_id blobs (oauth_dcr)."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import oauth_dcr  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_WEBHOOK,
    DOMAIN,
)
from custom_components.ha_mcp_tools.oauth_dcr import (  # noqa: E402
    CFG_DCR_SIGNING_KEY,
    DcrRegisterView,
    client_redirect_uris,
    mint_client_id,
)

KEY = b"k" * 32
OTHER_KEY = b"x" * 32
GOOGLE_REDIRECT_URIS = [
    "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
    "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
]


def test_mint_and_verify_round_trip():
    """Round-trip redirect URIs through a signed stateless client ID."""
    uris = ["https://claude.ai/api/mcp/auth_callback"]
    cid = mint_client_id(KEY, uris)
    assert cid.startswith("hamcp-dcr-")
    assert client_redirect_uris(KEY, cid) == uris


def test_wrong_key_rejects():
    """Reject a client ID verified with a different signing key."""
    cid = mint_client_id(KEY, ["https://example.com/cb"])
    assert client_redirect_uris(OTHER_KEY, cid) is None


def test_tampered_blob_rejects():
    """Reject a client ID whose signed blob was tampered with."""
    cid = mint_client_id(KEY, ["https://example.com/cb"])
    body, _, sig = cid.rpartition(".")
    assert client_redirect_uris(KEY, body + ".AAAA" + sig[4:]) is None


def test_non_dcr_client_id_rejects():
    """Reject URL-shaped, empty, and malformed non-DCR client IDs."""
    assert client_redirect_uris(KEY, "https://claude.ai") is None
    assert client_redirect_uris(KEY, "") is None
    assert client_redirect_uris(KEY, "hamcp-dcr-notablob") is None


def _module_is(name: str, root: str) -> bool:
    """Return whether a module name is ``root`` or one of its children."""
    return name == root or name.startswith(f"{root}.")


@pytest.fixture
async def dcr_view_client_factory():
    """Build real aiohttp clients around a fake HA view registration.

    The shared embedded-component stubs replace ``aiohttp`` in ``sys.modules``.
    Load the real package only for this fixture, then restore the exact module
    table so this HTTP-level harness and the stub-driven component tests can
    coexist in the full unit suite.
    """
    package_roots = ("aiohttp", "yarl")
    saved_modules = {
        name: module
        for name, module in tuple(sys.modules.items())
        if any(_module_is(name, root) for root in package_roots)
    }
    for name in saved_modules:
        sys.modules.pop(name, None)

    clients = []
    stub_web = oauth_dcr.web
    try:
        aiohttp_web = importlib.import_module("aiohttp.web")
        test_utils = importlib.import_module("aiohttp.test_utils")
        oauth_dcr.web = aiohttp_web

        async def factory(
            *,
            dcr_key,
            autoapprove_provider=None,
            resource_server=None,
        ):
            cfg = {}
            if dcr_key is not None:
                cfg[CFG_DCR_SIGNING_KEY] = dcr_key
            if autoapprove_provider is not None:
                cfg["autoapprove_provider"] = autoapprove_provider
            if resource_server is not None:
                cfg["resource_server"] = resource_server

            app = aiohttp_web.Application()
            hass = SimpleNamespace(
                data={DOMAIN: {DATA_WEBHOOK: cfg}},
                http=SimpleNamespace(),
            )

            def register_view(view):
                app.router.add_post(view.url, view.post)

            hass.http.register_view = register_view
            hass.http.register_view(DcrRegisterView(hass))

            client = test_utils.TestClient(test_utils.TestServer(app))
            await client.start_server()
            clients.append(client)
            return client

        yield factory
    finally:
        for client in clients:
            await client.close()
        oauth_dcr.web = stub_web
        for name in tuple(sys.modules):
            if any(_module_is(name, root) for root in package_roots):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


async def test_register_404s_when_no_dcr_key_live(dcr_view_client_factory):
    """Return 404 when no live mode exposes a DCR signing key."""
    client = await dcr_view_client_factory(dcr_key=None)
    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
    )
    assert resp.status == 404


async def test_register_mints_verifiable_client_id(dcr_view_client_factory):
    """Return a verifiable public-client registration response."""
    client = await dcr_view_client_factory(dcr_key=KEY)
    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        json={
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "client_name": "probe",
            "application_type": "web",
        },
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["token_endpoint_auth_method"] == "none"
    assert data["client_name"] == "probe"
    assert client_redirect_uris(KEY, data["client_id"]) == [
        "https://claude.ai/api/mcp/auth_callback"
    ]


async def test_register_rejects_bad_redirects(dcr_view_client_factory):
    """Reject empty, unsafe, fragmented, and oversized redirect lists."""
    client = await dcr_view_client_factory(dcr_key=KEY)
    for bad in (
        [],
        ["http://not-loopback.example/cb"],
        ["https://ok.example/cb#frag"],
        ["x" * 600],
    ):
        resp = await client.post(
            "/api/ha_mcp_tools/oauth/register", json={"redirect_uris": bad}
        )
        assert resp.status == 400, bad


async def test_register_rejects_deeply_nested_json_body(dcr_view_client_factory):
    """#2218 review: json.loads raises RecursionError on a deeply nested body
    — malformed metadata answers 400, never a 500. The payload stays UNDER
    MAX_DCR_BODY_BYTES so it reaches the parse rather than being turned away
    by the size guard added later (#2219 review round 3)."""
    client = await dcr_view_client_factory(dcr_key=KEY)
    nesting = 10_000
    payload = "[" * nesting + "]" * nesting
    assert len(payload) < oauth_dcr.MAX_DCR_BODY_BYTES

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_client_metadata"
    # The description distinguishes the arms: both guards answer the same
    # error code, so only this pins that the PARSER rejected it.
    assert body["error_description"] == "body must be JSON"


async def test_register_survives_a_bogus_content_type_charset(
    dcr_view_client_factory,
):
    """#2219 review round 3: request.json() decodes via the Content-Type
    charset and raises LookupError on an unknown one, so a single header
    used to 500 this anonymous endpoint. Reading the bytes and parsing them
    as UTF-8 JSON (RFC 8259) ignores the parameter, so a well-formed body
    registers normally."""
    client = await dcr_view_client_factory(dcr_key=KEY)

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        data=b'{"redirect_uris": ["https://a.example/cb"]}',
        headers={"Content-Type": "application/json; charset=nope"},
    )

    assert resp.status == 201


async def test_register_rejects_oversized_body(dcr_view_client_factory):
    """#2219 review round 3: a conforming registration is a few KB, so the
    read is capped rather than riding HA's 16 MiB client_max_size."""
    client = await dcr_view_client_factory(dcr_key=KEY)

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        data=b'{"redirect_uris": ["https://a.example/cb"], "pad": "'
        + b"x" * (oauth_dcr.MAX_DCR_BODY_BYTES + 16)
        + b'"}',
        headers={"Content-Type": "application/json"},
    )

    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_client_metadata"
    assert body["error_description"] == "body is too large"


async def test_register_reassembles_a_chunked_body(dcr_view_client_factory):
    """#2219 review round 3: a fragmented body must be read to EOF — a single
    StreamReader.read() can return early and truncate the document."""
    client = await dcr_view_client_factory(dcr_key=KEY)
    body = b'{"redirect_uris": ["https://a.example/cb"], "client_name": "x"}'

    async def chunked():
        for i in range(0, len(body), 7):
            yield body[i : i + 7]

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        data=chunked(),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status == 201


async def test_register_rejects_plain_invalid_json(dcr_view_client_factory):
    """The ordinary malformed-JSON arm still answers the same 400."""
    client = await dcr_view_client_factory(dcr_key=KEY)

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        data=b"not json at all",
        headers={"Content-Type": "application/json"},
    )

    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_client_metadata"
    assert body["error_description"] == "body must be JSON"


async def test_register_accepts_google_multi_origin_client(dcr_view_client_factory):
    """Accept Spark's two redirects and promise the refresh grant it now has.

    #2248: the signed refresh envelope records which of the two origins core
    bound a grant to, so a multi-origin registration is refreshable and the
    registration response says so.
    """
    client = await dcr_view_client_factory(dcr_key=KEY, resource_server=object())

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        json={"redirect_uris": GOOGLE_REDIRECT_URIS},
    )

    assert resp.status == 201
    data = await resp.json()
    assert data["grant_types"] == ["authorization_code", "refresh_token"]
    assert client_redirect_uris(KEY, data["client_id"]) == GOOGLE_REDIRECT_URIS


async def test_register_preserves_explicit_zero_port_in_web_origin(
    dcr_view_client_factory,
):
    """Round-trip an explicit port zero through the registration view.

    The origin identity itself is pinned next to the other translation unit
    tests, by
    test_oauth_ha_auth.test_normalized_origin_keeps_an_explicit_zero_port_distinct.
    """
    client = await dcr_view_client_factory(dcr_key=KEY, resource_server=object())
    redirect_uris = ["https://a.example/cb", "https://a.example:0/cb"]

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        json={"redirect_uris": redirect_uris},
    )

    assert resp.status == 201
    assert client_redirect_uris(KEY, (await resp.json())["client_id"]) == redirect_uris


async def test_register_none_mode_advertises_authorization_code_only(
    dcr_view_client_factory,
):
    """Advertise only authorization_code for none-mode registrations."""
    client = await dcr_view_client_factory(
        dcr_key=KEY,
        autoapprove_provider=object(),
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        json={"redirect_uris": ["https://a.example/cb"]},
    )

    assert resp.status == 201
    assert (await resp.json())["grant_types"] == ["authorization_code"]


async def test_register_ha_auth_advertises_refresh_token(dcr_view_client_factory):
    """Advertise refresh_token when ha_auth forwards refresh grants to core."""
    client = await dcr_view_client_factory(
        dcr_key=KEY,
        resource_server=object(),
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        json={"redirect_uris": ["https://a.example/cb"]},
    )

    assert resp.status == 201
    assert (await resp.json())["grant_types"] == [
        "authorization_code",
        "refresh_token",
    ]


@pytest.mark.parametrize(
    "redirect_uris",
    [
        ["http://127.0.0.1/callback"],
        ["https://a.example/cb", "http://localhost/callback"],
    ],
)
async def test_register_ha_auth_advertises_refresh_for_loopback_registrations(
    dcr_view_client_factory,
    redirect_uris,
):
    """Promise refresh even when a loopback authorization may be selected.

    #2248 inverted this: the token leg records the ephemeral loopback origin
    core bound the grant to in the refresh token itself, so the registration
    shape no longer decides whether refresh works.
    """
    client = await dcr_view_client_factory(
        dcr_key=KEY,
        resource_server=object(),
    )

    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        json={"redirect_uris": redirect_uris},
    )

    assert resp.status == 201
    assert (await resp.json())["grant_types"] == [
        "authorization_code",
        "refresh_token",
    ]


def test_canonical_origin_url_rebrackets_ipv6():
    """#2213 review: IPv6 origins round-trip through normalize + canonicalize."""
    origin = oauth_dcr.normalized_origin("https://[2001:db8::1]/cb")
    assert origin == ("https", "2001:db8::1", 443)
    assert oauth_dcr.canonical_origin_url(origin) == "https://[2001:db8::1]"
    non_default = oauth_dcr.normalized_origin("https://[2001:db8::1]:8443/cb")
    assert oauth_dcr.canonical_origin_url(non_default) == "https://[2001:db8::1]:8443"
