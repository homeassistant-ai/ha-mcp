"""The legacy discovery documents MCP clients fetch over HTTP.

The base URL in every document decides where a client sends the user's
credentials, so it is pinned to the operator's public URL when one is
configured and never taken from a forged ``Host`` header.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._legacy_oauth import PUBLIC_BASE_URL, WEBHOOK_ID, get_json, set_up_legacy
from ._variants import Variant

PRM_PATH = f"/.well-known/oauth-protected-resource/api/webhook/{WEBHOOK_ID}"


async def test_protected_resource_document_ignores_forged_host_headers(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """With a public URL configured, a request whose Host and X-Forwarded-*
    headers name an attacker's origin must still get the operator's URLs."""
    await set_up_legacy(hass, variant, fs_root, public_base_url=PUBLIC_BASE_URL)

    client = await hass_client_no_auth()
    document = await get_json(
        client,
        PRM_PATH,
        {
            "Host": "evil.example",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "http",
        },
    )

    assert document["resource"] == f"{PUBLIC_BASE_URL}/api/webhook/{WEBHOOK_ID}"
    assert document["authorization_servers"] == [
        f"{PUBLIC_BASE_URL}{variant.oauth_base}"
    ]
    assert document["bearer_methods_supported"] == ["header"]


async def test_public_url_with_trailing_slash_does_not_double_the_path(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    await set_up_legacy(hass, variant, fs_root, public_base_url=f"{PUBLIC_BASE_URL}/")

    client = await hass_client_no_auth()
    document = await get_json(client, PRM_PATH)

    assert document["resource"] == f"{PUBLIC_BASE_URL}/api/webhook/{WEBHOOK_ID}"


@pytest.mark.parametrize(
    ("headers", "base"),
    [
        (
            {
                "Host": "internal.example",
                "X-Forwarded-Host": "real.example",
                "X-Forwarded-Proto": "https",
            },
            "https://real.example",
        ),
        # The test client speaks plain HTTP, so that is the request's scheme.
        ({"Host": "host.example"}, "http://host.example"),
    ],
    ids=["forwarded", "host"],
)
async def test_unpinned_document_names_the_origin_the_client_used(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    headers: dict[str, str],
    base: str,
) -> None:
    """Without a public URL the documents follow the origin the client reached,
    preferring the reverse proxy's X-Forwarded-* headers to the Host header."""
    await set_up_legacy(hass, variant, fs_root)

    client = await hass_client_no_auth()
    document = await get_json(client, PRM_PATH, headers)

    assert document["resource"] == f"{base}/api/webhook/{WEBHOOK_ID}"


async def test_authorization_server_document_advertises_the_legacy_flow(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """RFC 8414 fields a client needs to run the authorization-code flow with
    PKCE against this server, at its own scoped endpoints."""
    await set_up_legacy(hass, variant, fs_root, public_base_url=PUBLIC_BASE_URL)

    client = await hass_client_no_auth()
    document = await get_json(client, f"{variant.oauth_base}/authorization-server")

    issuer = f"{PUBLIC_BASE_URL}{variant.oauth_base}"
    assert document["issuer"] == issuer
    assert document["authorization_endpoint"] == f"{issuer}/authorize"
    assert document["token_endpoint"] == f"{issuer}/token"
    assert "code" in document["response_types_supported"]
    assert {"authorization_code", "refresh_token"} <= set(
        document["grant_types_supported"]
    )
    assert "S256" in document["code_challenge_methods_supported"]
    assert {"client_secret_basic", "client_secret_post"} <= set(
        document["token_endpoint_auth_methods_supported"]
    )
    # RFC 9207 §3: without this the client treats the redirect's ``iss`` as
    # unsupported.
    assert document["authorization_response_iss_parameter_supported"] is True


@pytest.mark.parametrize(
    "location",
    [
        "/.well-known/oauth-authorization-server{base}",
        "/.well-known/openid-configuration{base}",
        "{base}/.well-known/openid-configuration",
        "{base}/.well-known/oauth-authorization-server",
    ],
    ids=["rfc8414", "oidc-prefixed", "oidc-suffixed", "rfc8414-suffixed"],
)
async def test_well_known_locations_serve_the_authorization_server_document(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    location: str,
) -> None:
    """#1714: claude.ai probes these locations and caches what it finds; a 404
    there sent it to Core's own document, where the proxy's client id can never
    work. Each must serve the canonical document, which offers no dynamic
    registration because the proxy has one fixed client."""
    await set_up_legacy(hass, variant, fs_root, public_base_url=PUBLIC_BASE_URL)
    client = await hass_client_no_auth()
    canonical = await get_json(client, f"{variant.oauth_base}/authorization-server")

    document = await get_json(client, location.format(base=variant.oauth_base))

    assert document == canonical
    assert "registration_endpoint" not in document
