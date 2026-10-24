"""The OAuth consent flow binds a submitted token to the code it mints."""

import time
from types import SimpleNamespace

import pytest
from pydantic import AnyHttpUrl

from ha_mcp._vendor.mcp.server.auth.provider import AuthorizationParams, TokenError
from ha_mcp._vendor.mcp.shared.auth import OAuthClientInformationFull
from ha_mcp.auth.provider import (
    PENDING_AUTHORIZATION_EXPIRY_SECONDS,
    HomeAssistantAuthorizationCode,
    HomeAssistantOAuthProvider,
)

REDIRECT_URI = "http://localhost/cb"
STALE_CREATED_AT = time.time() - PENDING_AUTHORIZATION_EXPIRY_SECONDS - 1


@pytest.fixture
def provider() -> HomeAssistantOAuthProvider:
    return HomeAssistantOAuthProvider(base_url="http://localhost:8086")


def _consent_request(form: dict[str, object]) -> SimpleNamespace:
    """A consent-form POST carrying ``form`` as its body."""

    async def read_form() -> dict[str, object]:
        return form

    return SimpleNamespace(query_params={}, form=read_form)


def _pending(client_id: str, created_at: float | None = None) -> dict:
    return {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scopes": ["homeassistant"],
        "code_challenge": "test-challenge",
        "created_at": time.time() if created_at is None else created_at,
    }


def _code(
    client_id: str, code: str, expires_at: float
) -> HomeAssistantAuthorizationCode:
    return HomeAssistantAuthorizationCode(
        code=code,
        client_id=client_id,
        redirect_uri=AnyHttpUrl(REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
        scopes=["homeassistant"],
        expires_at=expires_at,
        code_challenge="challenge",
        ha_token="token",
    )


async def _register(provider: HomeAssistantOAuthProvider, client_id: str):
    client_info = OAuthClientInformationFull(
        client_id=client_id, redirect_uris=[REDIRECT_URI]
    )
    await provider.register_client(client_info)
    return client_info


async def _consent(
    provider: HomeAssistantOAuthProvider, client_id: str, txn_id: str, ha_token: str
) -> str:
    """Submit the consent form for ``txn_id`` and return the minted code."""
    provider.pending_authorizations[txn_id] = _pending(client_id)
    response = await provider._consent_post(
        _consent_request({"txn_id": txn_id, "ha_token": ha_token})
    )
    assert response.status_code == 303, response.body
    return response.headers["location"].split("code=")[1].split("&")[0]


@pytest.mark.asyncio
async def test_consent_post_rejects_expired_transaction(provider):
    """A consent form submitted after its window must not mint a code."""
    provider.pending_authorizations["stale-txn"] = _pending(
        "test-client", STALE_CREATED_AT
    )

    response = await provider._consent_post(
        _consent_request({"txn_id": "stale-txn", "ha_token": "token"})
    )

    assert response.status_code == 400
    assert b"expired" in response.body
    assert "stale-txn" not in provider.pending_authorizations
    assert not provider.auth_codes


@pytest.mark.asyncio
async def test_token_carries_the_credential_submitted_for_its_own_code(provider):
    """Two consents for one client must not swap credentials at exchange.

    The submitted LLAT used to be stored per client, so a second consent for
    the same client overwrote the first, the first code redeemed was issued
    the latest submitter's credential, and the other exchange failed.
    """
    client_info = await _register(provider, "shared-client")
    codes = {
        user: await _consent(provider, "shared-client", f"txn-{user}", f"llat-{user}")
        for user in ("first", "second")
    }

    for user in ("first", "second"):
        auth_code = await provider.load_authorization_code(client_info, codes[user])
        assert auth_code is not None
        token = await provider.exchange_authorization_code(client_info, auth_code)
        payload = provider._decode_token(token.access_token)
        assert payload is not None
        assert payload["ha_token"] == f"llat-{user}"


@pytest.mark.asyncio
async def test_padded_token_is_bound_without_its_whitespace(provider):
    """Whitespace around a pasted token must not reach Home Assistant."""
    client_info = await _register(provider, "pad-client")
    code = await _consent(provider, "pad-client", "txn", "  llat \n")

    auth_code = await provider.load_authorization_code(client_info, code)
    assert auth_code is not None
    token = await provider.exchange_authorization_code(client_info, auth_code)
    payload = provider._decode_token(token.access_token)
    assert payload is not None
    assert payload["ha_token"] == "llat"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ha_token",
    ["   ", SimpleNamespace(filename="token.txt")],
    ids=["whitespace-only", "file-upload"],
)
async def test_consent_post_rejects_a_token_that_is_not_text(provider, ha_token):
    """A blank or uploaded token field returns the user to the form, minting nothing."""
    provider.pending_authorizations["txn"] = _pending("test-client")

    response = await provider._consent_post(
        _consent_request({"txn_id": "txn", "ha_token": ha_token})
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert "/consent?" in location
    assert "error=" in location
    assert not provider.auth_codes
    assert "txn" in provider.pending_authorizations


@pytest.mark.asyncio
async def test_code_cannot_be_exchanged_twice(provider):
    """A redeemed code must not yield the user's credential a second time."""
    client_info = await _register(provider, "once-client")
    code = await _consent(provider, "once-client", "txn", "llat")
    auth_code = await provider.load_authorization_code(client_info, code)
    assert auth_code is not None
    await provider.exchange_authorization_code(client_info, auth_code)

    with pytest.raises(TokenError) as exc:
        await provider.exchange_authorization_code(client_info, auth_code)
    assert exc.value.error == "invalid_grant"


@pytest.mark.asyncio
async def test_code_is_not_loaded_for_another_client(provider):
    """One client's code must not be loadable, with its credential, by another."""
    owner = await _register(provider, "owner-client")
    other = await _register(provider, "other-client")
    code = await _consent(provider, "owner-client", "txn", "llat")

    assert await provider.load_authorization_code(other, code) is None
    assert await provider.load_authorization_code(owner, code) is not None


@pytest.mark.asyncio
async def test_expired_state_is_evicted_when_a_new_authorization_starts(provider):
    """Abandoned consent forms and unredeemed codes must not accumulate.

    Live entries must survive the sweep: a sweep that cleared everything would
    break another user's login in progress.
    """
    client_info = await _register(provider, "evict-client")
    provider.pending_authorizations["stale-txn"] = _pending(
        "evict-client", STALE_CREATED_AT
    )
    provider.pending_authorizations["live-txn"] = _pending("evict-client")
    provider.auth_codes["stale-code"] = _code(
        "evict-client", "stale-code", time.time() - 1
    )
    provider.auth_codes["live-code"] = _code(
        "evict-client", "live-code", time.time() + 60
    )

    await provider.authorize(
        client_info,
        AuthorizationParams(
            redirect_uri=AnyHttpUrl(REDIRECT_URI),
            redirect_uri_provided_explicitly=True,
            state="state",
            scopes=[],
            code_challenge="challenge",
        ),
    )

    assert "stale-txn" not in provider.pending_authorizations
    assert "stale-code" not in provider.auth_codes
    assert "live-txn" in provider.pending_authorizations
    assert "live-code" in provider.auth_codes
    assert len(provider.pending_authorizations) == 2


def test_authorization_code_repr_hides_the_token():
    """A logged or dumped code must not print the user's credential."""
    code = _code("c", "code", time.time() + 60)
    assert "token" not in repr(code)
