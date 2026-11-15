"""Stateless DCR client_id blobs and the registration endpoint (oauth_dcr).

The endpoint is bound by the component's own webhook registration on a real
``hass`` and every request goes through Core's HTTP server and router.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Coroutine
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator

import custom_components.ha_mcp_tools.mcp_webhook as mw
from custom_components.ha_mcp_tools import oauth_dcr
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK_ID,
    DOMAIN,
    OAUTH_BASE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)
from custom_components.ha_mcp_tools.oauth_dcr import (
    client_redirect_uris,
    mint_client_id,
)

KEY = b"k" * 32
OTHER_KEY = b"x" * 32
REGISTER_URL = f"{OAUTH_BASE}/register"
WEBHOOK_ID = "mcp_0123456789abcdef0123456789abcdef"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
GOOGLE_REDIRECT_URIS = [
    "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
    "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
]
JSON_HEADERS = {"Content-Type": "application/json"}

Registered = Callable[..., Coroutine[Any, Any, TestClient]]


def test_mint_and_verify_round_trip() -> None:
    """Round-trip redirect URIs through a signed stateless client ID."""
    cid = mint_client_id(KEY, [CALLBACK])
    assert cid.startswith("hamcp-dcr-")
    assert client_redirect_uris(KEY, cid) == [CALLBACK]


def test_wrong_key_rejects() -> None:
    """Reject a client ID verified with a different signing key."""
    cid = mint_client_id(KEY, ["https://example.com/cb"])
    assert client_redirect_uris(OTHER_KEY, cid) is None


def test_tampered_blob_rejects() -> None:
    """Reject a client ID whose signed blob was tampered with."""
    cid = mint_client_id(KEY, ["https://example.com/cb"])
    body, _, sig = cid.rpartition(".")
    assert client_redirect_uris(KEY, body + ".AAAA" + sig[4:]) is None


def test_non_dcr_client_id_rejects() -> None:
    """Reject URL-shaped, empty, and malformed non-DCR client IDs."""
    assert client_redirect_uris(KEY, "https://claude.ai") is None
    assert client_redirect_uris(KEY, "") is None
    assert client_redirect_uris(KEY, "hamcp-dcr-notablob") is None


@pytest.fixture
async def registered(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> AsyncIterator[Registered]:
    """Register the webhook surface on the real ``hass``; return a client."""
    assert await async_setup_component(hass, "webhook", {})

    async def register(
        mode: str = WEBHOOK_AUTH_HA, *, dcr_key: bytes | None = KEY
    ) -> TestClient:
        entry = MockConfigEntry(domain=DOMAIN, data={DATA_WEBHOOK_ID: WEBHOOK_ID})
        await mw.async_register_webhook(
            hass,
            entry,
            port=9584,
            secret_path="/private_x",
            auth_mode=mode,
            dcr_signing_key=dcr_key.hex() if dcr_key else None,
        )
        return await hass_client_no_auth()

    try:
        yield register
    finally:
        await mw.async_unregister_webhook(hass)


async def test_register_404s_when_no_dcr_key_live(registered: Registered) -> None:
    """Return 404 when no live mode exposes a DCR signing key."""
    client = await registered(WEBHOOK_AUTH_NONE, dcr_key=None)
    resp = await client.post(REGISTER_URL, json={"redirect_uris": [CALLBACK]})
    assert resp.status == 404


async def test_register_mints_verifiable_client_id(registered: Registered) -> None:
    """Return a verifiable public-client registration response."""
    client = await registered()
    resp = await client.post(
        REGISTER_URL,
        json={
            "redirect_uris": [CALLBACK],
            "client_name": "probe",
            "application_type": "web",
        },
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["token_endpoint_auth_method"] == "none"
    assert data["client_name"] == "probe"
    assert client_redirect_uris(KEY, data["client_id"]) == [CALLBACK]


async def test_register_rejects_bad_redirects(registered: Registered) -> None:
    """Reject empty, unsafe, fragmented, and oversized redirect lists."""
    client = await registered()
    for bad in (
        [],
        ["http://not-loopback.example/cb"],
        ["https://ok.example/cb#frag"],
        ["x" * 600],
    ):
        resp = await client.post(REGISTER_URL, json={"redirect_uris": bad})
        assert resp.status == 400, bad


async def test_register_rejects_deeply_nested_json_body(
    registered: Registered,
) -> None:
    """A deeply nested body is malformed metadata (400), never a 500.

    The payload stays under MAX_DCR_BODY_BYTES so it reaches the JSON parse.
    Whether the parser overflows is interpreter-dependent (3.14 parses it and
    the body is then refused as not an object), so only the 400 is pinned.
    """
    client = await registered()
    nesting = oauth_dcr.MAX_DCR_BODY_BYTES // 2 - 1
    payload = "[" * nesting + "]" * nesting

    resp = await client.post(REGISTER_URL, data=payload, headers=JSON_HEADERS)

    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_client_metadata"


async def test_register_answers_a_parser_stack_overflow_with_400(
    registered: Registered, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where ``json.loads`` gives up on nesting depends on the interpreter's
    stack, so the overflow is forced through the module's own ``json`` name:
    an anonymous endpoint calls it malformed metadata, never a 500."""
    client = await registered()
    overflowing = SimpleNamespace(
        loads=Mock(side_effect=RecursionError), dumps=json.dumps
    )
    monkeypatch.setattr(oauth_dcr, "json", overflowing)

    resp = await client.post(REGISTER_URL, data="[[]]", headers=JSON_HEADERS)

    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_client_metadata"
    assert body["error_description"] == "body must be JSON"


async def test_register_survives_a_bogus_content_type_charset(
    registered: Registered,
) -> None:
    """An unknown Content-Type charset does not 500 the anonymous endpoint."""
    client = await registered()

    resp = await client.post(
        REGISTER_URL,
        data=b'{"redirect_uris": ["https://a.example/cb"]}',
        headers={"Content-Type": "application/json; charset=nope"},
    )

    assert resp.status == 201


async def test_register_rejects_oversized_body(registered: Registered) -> None:
    """A registration beyond the body cap is refused, not read in full."""
    client = await registered()

    resp = await client.post(
        REGISTER_URL,
        data=b'{"redirect_uris": ["https://a.example/cb"], "pad": "'
        + b"x" * (oauth_dcr.MAX_DCR_BODY_BYTES + 16)
        + b'"}',
        headers=JSON_HEADERS,
    )

    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_client_metadata"
    assert body["error_description"] == "body is too large"


async def test_register_reassembles_a_chunked_body(registered: Registered) -> None:
    """A fragmented body is read to EOF, not truncated at the first chunk."""
    client = await registered()
    body = b'{"redirect_uris": ["https://a.example/cb"], "client_name": "x"}'

    async def chunked() -> AsyncIterator[bytes]:
        for i in range(0, len(body), 7):
            yield body[i : i + 7]

    resp = await client.post(REGISTER_URL, data=chunked(), headers=JSON_HEADERS)

    assert resp.status == 201
    assert (await resp.json())["client_name"] == "x"


async def test_register_rejects_plain_invalid_json(registered: Registered) -> None:
    """The ordinary malformed-JSON arm answers the same 400."""
    client = await registered()

    resp = await client.post(
        REGISTER_URL, data=b"not json at all", headers=JSON_HEADERS
    )

    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid_client_metadata"
    assert body["error_description"] == "body must be JSON"


async def test_register_accepts_google_multi_origin_client(
    registered: Registered,
) -> None:
    """Accept Spark's two redirects and promise the refresh grant it now has."""
    client = await registered()

    resp = await client.post(REGISTER_URL, json={"redirect_uris": GOOGLE_REDIRECT_URIS})

    assert resp.status == 201
    data = await resp.json()
    assert data["grant_types"] == ["authorization_code", "refresh_token"]
    assert client_redirect_uris(KEY, data["client_id"]) == GOOGLE_REDIRECT_URIS


async def test_register_preserves_explicit_zero_port_in_web_origin(
    registered: Registered,
) -> None:
    """Round-trip an explicit port zero through the registration view."""
    client = await registered()
    redirect_uris = ["https://a.example/cb", "https://a.example:0/cb"]

    resp = await client.post(REGISTER_URL, json={"redirect_uris": redirect_uris})

    assert resp.status == 201
    assert client_redirect_uris(KEY, (await resp.json())["client_id"]) == redirect_uris


async def test_register_none_mode_advertises_authorization_code_only(
    registered: Registered,
) -> None:
    """Advertise only authorization_code for none-mode registrations."""
    client = await registered(WEBHOOK_AUTH_NONE)

    resp = await client.post(
        REGISTER_URL, json={"redirect_uris": ["https://a.example/cb"]}
    )

    assert resp.status == 201
    assert (await resp.json())["grant_types"] == ["authorization_code"]


@pytest.mark.parametrize(
    "redirect_uris",
    [
        ["https://a.example/cb"],
        ["http://127.0.0.1/callback"],
        ["https://a.example/cb", "http://localhost/callback"],
    ],
)
async def test_register_ha_auth_advertises_refresh_token(
    registered: Registered, redirect_uris: list[str]
) -> None:
    """Promise refresh in ha_auth mode, loopback registrations included."""
    client = await registered()

    resp = await client.post(REGISTER_URL, json={"redirect_uris": redirect_uris})

    assert resp.status == 201
    assert (await resp.json())["grant_types"] == [
        "authorization_code",
        "refresh_token",
    ]


def test_canonical_origin_url_rebrackets_ipv6() -> None:
    """IPv6 origins round-trip through normalize + canonicalize."""
    origin = oauth_dcr.normalized_origin("https://[2001:db8::1]/cb")
    assert origin == ("https", "2001:db8::1", 443)
    assert oauth_dcr.canonical_origin_url(origin) == "https://[2001:db8::1]"
    non_default = oauth_dcr.normalized_origin("https://[2001:db8::1]:8443/cb")
    assert non_default is not None
    assert oauth_dcr.canonical_origin_url(non_default) == "https://[2001:db8::1]:8443"
