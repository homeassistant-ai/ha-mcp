"""The legacy ``/token`` endpoint: client authentication, PKCE and refresh."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._legacy_oauth import (
    CLIENT_ID,
    CLIENT_SECRET,
    VERIFIER,
    basic_auth,
    exchange_code,
    issue_code,
    obtain_tokens,
    request_token,
    set_up_legacy,
    token_claims,
)
from ._variants import Variant


async def test_wrong_client_credentials_get_invalid_client_with_restart_hint(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """A client still holding credentials from before a regeneration is the
    case a Home Assistant restart fixes (#1694), so the 401 says so and asks
    for Basic credentials (RFC 6749 §5.2)."""
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()

    response = await request_token(
        client, {"grant_type": "authorization_code"}, basic_auth("wrong", "pw")
    )

    assert response.status == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic ")
    body = await response.json()
    assert body["error"] == "invalid_client"
    assert "restart Home Assistant" in body["error_description"]


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [
        ("wrong-client-id-123456", CLIENT_SECRET),
        (CLIENT_ID, "wrong"),
        ("", CLIENT_SECRET),
        (CLIENT_ID, ""),
    ],
    ids=["wrong-id", "wrong-secret", "blank-id", "blank-secret"],
)
async def test_form_credentials_must_match_the_configured_client(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    client_id: str,
    client_secret: str,
) -> None:
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()

    response = await request_token(
        client,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={},
    )

    assert response.status == 401
    assert (await response.json())["error"] == "invalid_client"


async def test_configured_credentials_with_reserved_characters_authenticate_raw(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """#2218: a configured id or secret may hold ``+`` or ``%XX``. Many clients
    put them into Basic auth without form-encoding first, and decoding alone
    would turn ``+`` into a space and lock such a client out."""
    client_id, client_secret = "configured-client+id", "se%41cret+x"
    await set_up_legacy(
        hass, variant, fs_root, client_id=client_id, client_secret=client_secret
    )
    client = await hass_client_no_auth()

    response = await request_token(
        client,
        {"grant_type": "unsupported_probe"},
        basic_auth(client_id, client_secret),
    )

    # Authentication passed; only the probe grant is refused, after the gate.
    assert (await response.json())["error"] == "unsupported_grant_type"


async def test_unsupported_grant_is_a_client_error_without_restart_hint(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()

    response = await request_token(client, {"grant_type": "client_credentials"})

    assert response.status == 400
    assert await response.json() == {"error": "unsupported_grant_type"}


async def test_code_grant_without_code_fields_is_invalid_request(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()

    response = await request_token(client, {"grant_type": "authorization_code"})

    assert response.status == 400
    assert (await response.json())["error"] == "invalid_request"


async def test_code_exchange_reports_the_access_token_lifetime(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """A client refreshes by ``expires_in``; it must match the lifetime the
    access token really has, or the client keeps sending an expired token."""
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()

    tokens = await obtain_tokens(client)

    assert tokens["token_type"] == "Bearer"
    assert tokens["refresh_token"]
    claims = token_claims(tokens["access_token"])
    assert tokens["expires_in"] == claims["exp"] - claims["iat"]


async def test_authorization_code_redeems_only_once(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()
    code = await issue_code(client)
    assert (await exchange_code(client, code)).status == 200

    replay = await exchange_code(client, code)

    assert replay.status == 400
    assert (await replay.json())["error"] == "invalid_grant"


async def test_wrong_verifier_burns_the_code(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """Whoever intercepted the code gets one guess at the verifier; after it
    the code is gone, even for the verifier's holder."""
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()
    code = await issue_code(client)

    guess = await exchange_code(client, code, verifier="z" * len(VERIFIER))
    retry = await exchange_code(client, code)

    assert (await guess.json())["error"] == "invalid_grant"
    assert (await retry.json())["error"] == "invalid_grant"


async def test_code_is_bound_to_its_redirect_uri(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()
    code = await issue_code(client)

    response = await exchange_code(client, code, redirect_uri="https://attacker.com/cb")

    assert response.status == 400
    assert (await response.json())["error"] == "invalid_grant"


@pytest.mark.parametrize(
    "verifier",
    ["tooshort", "X" * 129, "a" * 42 + " "],
    ids=["short", "long", "space"],
)
async def test_malformed_verifier_is_refused_even_when_it_matches_the_challenge(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    verifier: str,
) -> None:
    """RFC 7636 §4.1 fixes the verifier at 43-128 unreserved characters. A
    client whose challenge was made from an out-of-spec verifier is refused
    rather than having the junk hashed and accepted."""
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()
    code = await issue_code(client, verifier=verifier)

    response = await exchange_code(client, code, verifier=verifier)

    assert response.status == 400
    assert (await response.json())["error"] == "invalid_grant"


async def test_refresh_grant_issues_a_working_access_token(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()
    tokens = await obtain_tokens(client)

    response = await request_token(
        client,
        {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )

    assert response.status == 200
    refreshed = await response.json()
    assert refreshed["refresh_token"]
    provider = hass.data[variant.domain]["oauth"]
    assert provider.validate_access_token(refreshed["access_token"]) is True


@pytest.mark.parametrize("presented", ["garbage", "access"])
async def test_refresh_grant_needs_a_refresh_token(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    presented: str,
) -> None:
    """Neither a forged value nor the short-lived access token renews a
    session, and the refusal is a client error that carries no restart hint."""
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()
    refresh_token = (
        "garbage.token"
        if presented == "garbage"
        else (await obtain_tokens(client))["access_token"]
    )

    response = await request_token(
        client, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )

    assert response.status == 400
    assert await response.json() == {"error": "invalid_grant"}
