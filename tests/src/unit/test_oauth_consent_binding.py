"""The OAuth consent flow binds a submitted token to the code it mints."""

import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from ha_mcp.auth.provider import (
    HomeAssistantAuthorizationCode,
    HomeAssistantOAuthProvider,
)


@pytest.fixture
def provider() -> HomeAssistantOAuthProvider:
    return HomeAssistantOAuthProvider(base_url="http://localhost:8086")


@pytest.fixture
def consent_request():
    """Build a consent-form POST carrying ``form`` as its body."""

    def build(form: dict[str, str]) -> SimpleNamespace:
        async def read_form() -> dict[str, str]:
            return form

        return SimpleNamespace(query_params={}, form=read_form)

    return build


@pytest.mark.asyncio
async def test_consent_post_rejects_expired_transaction(provider, consent_request):
    """A consent form submitted after its window must not mint a code."""
    txn_id = "stale-txn"
    provider.pending_authorizations[txn_id] = {
        "client_id": "test-client",
        "redirect_uri": "http://localhost/cb",
        "code_challenge": "test-challenge",
        "created_at": time.time() - 400,  # More than 5 minutes ago
    }

    response = await provider._consent_post(
        consent_request({"txn_id": txn_id, "ha_token": "token"})
    )

    assert response.status_code == 400
    assert b"expired" in response.body
    assert txn_id not in provider.pending_authorizations
    assert not provider.auth_codes


@pytest.mark.asyncio
async def test_token_carries_the_credential_submitted_for_its_own_code(
    provider, consent_request
):
    """Two consents for one client must not swap credentials at exchange.

    The submitted LLAT used to be stored per client, so a second consent
    for the same client overwrote the first and whichever code was
    redeemed first was issued the other submitter's credential.
    """
    from ha_mcp._vendor.mcp.shared.auth import OAuthClientInformationFull

    client_info = OAuthClientInformationFull(
        client_id="shared-client",
        redirect_uris=["http://localhost/cb"],
    )
    await provider.register_client(client_info)

    codes: dict[str, str] = {}
    for user in ("first", "second"):
        txn_id = f"txn-{user}"
        provider.pending_authorizations[txn_id] = {
            "client_id": "shared-client",
            "redirect_uri": "http://localhost/cb",
            "scopes": ["homeassistant"],
            "code_challenge": "test-challenge",
            "created_at": time.time(),
        }
        response = await provider._consent_post(
            consent_request({"txn_id": txn_id, "ha_token": f"llat-{user}"})
        )
        location = urlparse(response.headers["location"])
        codes[user] = parse_qs(location.query)["code"][0]

    for user in ("first", "second"):
        auth_code = await provider.load_authorization_code(client_info, codes[user])
        assert auth_code is not None
        token = await provider.exchange_authorization_code(client_info, auth_code)
        payload = provider._decode_token(token.access_token)
        assert payload is not None
        assert payload["ha_token"] == f"llat-{user}"


@pytest.mark.asyncio
async def test_expired_state_is_evicted_when_a_new_authorization_starts(provider):
    """Abandoned consent forms and unredeemed codes must not accumulate."""
    from pydantic import AnyHttpUrl

    from ha_mcp._vendor.mcp.server.auth.provider import AuthorizationParams
    from ha_mcp._vendor.mcp.shared.auth import OAuthClientInformationFull

    client_info = OAuthClientInformationFull(
        client_id="evict-client",
        redirect_uris=["http://localhost/cb"],
    )
    await provider.register_client(client_info)
    provider.pending_authorizations["stale-txn"] = {
        "client_id": "evict-client",
        "redirect_uri": "http://localhost/cb",
        "created_at": time.time() - 400,  # More than 5 minutes ago
    }
    provider.auth_codes["stale-code"] = HomeAssistantAuthorizationCode(
        code="stale-code",
        client_id="evict-client",
        redirect_uri=AnyHttpUrl("http://localhost/cb"),
        redirect_uri_provided_explicitly=True,
        scopes=[],
        expires_at=time.time() - 1,
        code_challenge="challenge",
        ha_token="stale-token",
    )

    await provider.authorize(
        client_info,
        AuthorizationParams(
            redirect_uri=AnyHttpUrl("http://localhost/cb"),
            redirect_uri_provided_explicitly=True,
            state="state",
            scopes=[],
            code_challenge="challenge",
        ),
    )

    assert "stale-txn" not in provider.pending_authorizations
    assert "stale-code" not in provider.auth_codes
    assert len(provider.pending_authorizations) == 1
