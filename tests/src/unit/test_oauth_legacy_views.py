"""The legacy ``/authorize`` and ``/token`` endpoints, over Core's HTTP server.

The webhook is registered in legacy mode through the component's own entry
point (``_oauth_legacy_core.legacy_webhook``) and every request goes through
Core's router and aiohttp's own request parsing.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from aiohttp import BasicAuth, FormData
from aiohttp.test_utils import TestClient
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator

import custom_components.ha_mcp_tools.mcp_webhook as mw
from custom_components.ha_mcp_tools import oauth_legacy
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK_ID,
    DOMAIN,
    OAUTH_BASE,
    WEBHOOK_AUTH_NONE,
)

from ._oauth_legacy_core import (
    CLIENT_ID,
    CLIENT_SECRET,
    EXPECTED_ISSUER,
    PUBLIC_HEADERS,
    REDIRECT_URI,
    WEBHOOK_ID,
    approve_form,
    authorize_query,
    challenge_for,
    legacy_webhook,
    pkce_pair,
    query_param,
)

AUTHORIZE = oauth_legacy.AUTHORIZE_PATH
TOKEN = oauth_legacy.TOKEN_PATH
UNDECODABLE = {"Content-Type": "application/x-www-form-urlencoded; charset=nope"}


@pytest.fixture
async def provider(
    hass: HomeAssistant,
) -> AsyncIterator[oauth_legacy.LegacyOAuthProvider]:
    async with legacy_webhook(hass) as bound:
        yield bound


@pytest.fixture
async def client(
    provider: oauth_legacy.LegacyOAuthProvider,
    hass_client_no_auth: ClientSessionGenerator,
) -> TestClient:
    return await hass_client_no_auth()


async def _approve(
    client: TestClient, challenge: str, **overrides: str
) -> tuple[int, str]:
    """Post the consent form; the redirect status and its ``Location``."""
    response = await client.post(
        AUTHORIZE,
        data=approve_form(challenge, **overrides),
        headers=PUBLIC_HEADERS,
        allow_redirects=False,
    )
    return response.status, response.headers.get("Location", "")


async def _code(client: TestClient, challenge: str, **overrides: str) -> str:
    """An authorization code issued to a user who clicked Allow."""
    status, location = await _approve(client, challenge, **overrides)
    assert status == 302
    return query_param(location, "code")


def _token_form(code: str, verifier: str, **overrides: str) -> dict[str, str]:
    form = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }
    form.update(overrides)
    return form


def _refresh_form(refresh_token: str) -> dict[str, str]:
    return {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }


async def _error(response: Any, status: int) -> str:
    """The OAuth ``error`` of a JSON error reply with ``status``."""
    assert response.status == status
    body: dict[str, str] = await response.json()
    return body["error"]


class TestAuthorizeGet:
    async def test_consent_page_is_served(self, client: TestClient) -> None:
        response = await client.get(AUTHORIZE, params=authorize_query())

        assert response.status == 200
        assert REDIRECT_URI in await response.text()

    async def test_route_is_hidden_once_legacy_is_not_the_live_mode(
        self, hass: HomeAssistant, client: TestClient
    ) -> None:
        # The root views stay bound after a mode switch; they must stop serving.
        await mw.async_unregister_webhook(hass)
        entry = MockConfigEntry(domain=DOMAIN, data={DATA_WEBHOOK_ID: WEBHOOK_ID})
        await mw.async_register_webhook(
            hass,
            entry,
            port=9584,
            secret_path="/private_x",
            auth_mode=WEBHOOK_AUTH_NONE,
        )

        response = await client.get(AUTHORIZE, params=authorize_query())

        assert response.status == 404
        assert await response.text() == "not found"

    @pytest.mark.parametrize(
        ("override", "expected_text"),
        [
            ({"client_id": "someone-else"}, "invalid client_id"),
            ({"redirect_uri": "http://insecure.example.com/cb"}, "redirect_uri"),
            ({"code_challenge_method": "plain"}, "S256"),
            ({"response_type": "token"}, "unsupported_response_type"),
            ({"code_challenge": "short"}, "code_challenge"),
        ],
        ids=[
            "wrong-client-id",
            "insecure-redirect",
            "plain-challenge-method",
            "token-response-type",
            "short-challenge",
        ],
    )
    async def test_invalid_request_is_refused_with_a_400(
        self,
        client: TestClient,
        override: dict[str, str],
        expected_text: str,
    ) -> None:
        response = await client.get(AUTHORIZE, params=authorize_query(**override))

        assert response.status == 400
        assert expected_text in await response.text()

    async def test_consent_page_shows_the_redirect_domain_and_escapes_it(
        self, client: TestClient
    ) -> None:
        # An attacker-chosen redirect_uri must not inject markup into the page
        # an administrator is asked to approve.
        redirect = 'https://evil.example.com/cb?x="><img src=x onerror=alert(1)>'

        response = await client.get(
            AUTHORIZE, params=authorize_query(redirect_uri=redirect)
        )

        text = await response.text()
        assert response.status == 200
        assert "evil.example.com" in text
        assert "<img" not in text
        assert "&lt;img" in text


class TestRedirectUriValidation:
    @pytest.mark.parametrize(
        "redirect_uri",
        [
            REDIRECT_URI,
            "http://localhost:8765/cb",
            "http://127.0.0.1:8765/cb",
            "http://127.9.9.9/cb",
            "http://[::1]:8765/cb",
        ],
        ids=["https", "localhost", "loopback-v4", "loopback-v4-range", "loopback-v6"],
    )
    async def test_https_and_loopback_callbacks_are_accepted(
        self, client: TestClient, redirect_uri: str
    ) -> None:
        # RFC 8252 section 7.3: native and CLI clients receive the code on an
        # http loopback callback.
        response = await client.get(
            AUTHORIZE, params=authorize_query(redirect_uri=redirect_uri)
        )
        assert response.status == 200

    @pytest.mark.parametrize(
        "redirect_uri",
        [
            "http://client.example.com/cb",
            f"{REDIRECT_URI}#frag",
            "https:///cb",
            "",
            "https://x.example.com:999999/cb",
            "https://x.example.com:abc/cb",
        ],
        ids=[
            "plain-http",
            "fragment",
            "no-host",
            "empty",
            "port-out-of-range",
            "port-not-a-number",
        ],
    )
    async def test_other_callbacks_are_refused_with_a_400(
        self, client: TestClient, redirect_uri: str
    ) -> None:
        response = await client.get(
            AUTHORIZE, params=authorize_query(redirect_uri=redirect_uri)
        )
        assert response.status == 400

    async def test_out_of_range_port_on_the_consent_post_is_a_400_not_a_500(
        self, client: TestClient
    ) -> None:
        # The crafted port must be refused at validation, before the redirect
        # URL is built, or the anonymous POST would answer 500.
        status, _ = await _approve(
            client,
            pkce_pair()[1],
            redirect_uri="https://client.example.com:999999/cb",
        )
        assert status == 400


class TestAuthorizePost:
    async def test_allow_redirects_with_a_code_the_token_endpoint_redeems(
        self, client: TestClient, provider: oauth_legacy.LegacyOAuthProvider
    ) -> None:
        verifier, challenge = pkce_pair()

        status, location = await _approve(client, challenge)

        assert status == 302
        assert location.startswith(f"{REDIRECT_URI}?")
        assert query_param(location, "state") == "xyz"
        # RFC 9207 section 2: the success response names the issuer.
        assert query_param(location, "iss") == EXPECTED_ISSUER
        code = query_param(location, "code")
        assert provider.consume_code(code, REDIRECT_URI, verifier) is True

    async def test_deny_redirects_with_access_denied(self, client: TestClient) -> None:
        status, location = await _approve(client, pkce_pair()[1], action="deny")

        assert status == 302
        assert query_param(location, "error") == "access_denied"
        # RFC 9207 section 2 covers error responses too.
        assert query_param(location, "iss") == EXPECTED_ISSUER

    async def test_redirect_issuer_equals_the_discovery_documents_issuer(
        self, client: TestClient
    ) -> None:
        # RFC 9207 section 2 rejects any difference, and the two are built in
        # different modules.
        _, location = await _approve(client, pkce_pair()[1])

        document = await (
            await client.get(
                f"/.well-known/oauth-authorization-server{OAUTH_BASE}",
                headers=PUBLIC_HEADERS,
            )
        ).json()

        assert document["issuer"] == query_param(location, "iss")
        # RFC 9207 section 3: the document tells clients to expect ``iss``.
        assert document["authorization_response_iss_parameter_supported"] is True

    async def test_challenge_with_a_trailing_newline_is_a_400(
        self, client: TestClient
    ) -> None:
        # Core's security filter refuses a newline in a query string, so only
        # the form post can carry one; the 43-character shape check must not
        # let it through.
        status, _ = await _approve(client, "a" * 43 + "\n")
        assert status == 400

    async def test_unknown_action_is_a_400(self, client: TestClient) -> None:
        status, _ = await _approve(client, pkce_pair()[1], action="bogus")
        assert status == 400

    async def test_full_code_store_redirects_temporarily_unavailable(
        self, client: TestClient, provider: oauth_legacy.LegacyOAuthProvider
    ) -> None:
        # RFC 6749 section 4.1.2.1: a full store is an error redirect, not a
        # silent failure or a 500.
        _, challenge = pkce_pair()
        for _ in range(oauth_legacy.MAX_PENDING_CODES):
            provider.issue_code(REDIRECT_URI, challenge)

        status, location = await _approve(client, challenge)

        assert status == 302
        assert query_param(location, "error") == "temporarily_unavailable"
        assert query_param(location, "state") == "xyz"
        assert query_param(location, "iss") == EXPECTED_ISSUER

    async def test_undecodable_body_is_a_400_not_a_500(
        self, client: TestClient
    ) -> None:
        # aiohttp raises LookupError for an unknown charset; on an anonymous
        # view an unguarded parse would turn one header into a 500.
        response = await client.post(AUTHORIZE, data=b"a=b", headers=UNDECODABLE)
        assert response.status == 400

    async def test_post_is_hidden_once_legacy_is_not_the_live_mode(
        self, hass: HomeAssistant, client: TestClient
    ) -> None:
        await mw.async_unregister_webhook(hass)

        status, _ = await _approve(client, pkce_pair()[1])

        assert status == 404


class TestTokenAuthorizationCodeGrant:
    async def test_code_is_exchanged_for_tokens_that_must_not_be_cached(
        self, client: TestClient, provider: oauth_legacy.LegacyOAuthProvider
    ) -> None:
        verifier, challenge = pkce_pair()
        code = await _code(client, challenge)

        response = await client.post(TOKEN, data=_token_form(code, verifier))

        body = await response.json()
        assert response.status == 200
        assert body["token_type"] == "Bearer"
        assert provider.validate_access_token(body["access_token"]) is True
        assert provider.validate_refresh_token(body["refresh_token"]) is True
        # RFC 6749 section 5.1: the body carries credentials.
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"

    async def test_unknown_code_is_invalid_grant(self, client: TestClient) -> None:
        response = await client.post(TOKEN, data=_token_form("bogus", "x" * 43))
        assert await _error(response, 400) == "invalid_grant"

    async def test_code_cannot_be_redeemed_twice(self, client: TestClient) -> None:
        verifier, challenge = pkce_pair()
        code = await _code(client, challenge)
        assert (
            await client.post(TOKEN, data=_token_form(code, verifier))
        ).status == 200

        again = await client.post(TOKEN, data=_token_form(code, verifier))

        assert await _error(again, 400) == "invalid_grant"

    async def test_wrong_verifier_is_invalid_grant(self, client: TestClient) -> None:
        _, challenge = pkce_pair()
        code = await _code(client, challenge)

        response = await client.post(TOKEN, data=_token_form(code, pkce_pair()[0]))

        assert await _error(response, 400) == "invalid_grant"

    async def test_other_redirect_uri_is_invalid_grant(
        self, client: TestClient
    ) -> None:
        verifier, challenge = pkce_pair()
        code = await _code(client, challenge)

        response = await client.post(
            TOKEN,
            data=_token_form(
                code, verifier, redirect_uri="https://other.example.com/cb"
            ),
        )

        assert await _error(response, 400) == "invalid_grant"

    async def test_expired_code_is_invalid_grant(
        self, client: TestClient, freezer: FrozenDateTimeFactory
    ) -> None:
        verifier, challenge = pkce_pair()
        code = await _code(client, challenge)

        freezer.tick(timedelta(seconds=oauth_legacy.AUTH_CODE_TTL + 1))
        response = await client.post(TOKEN, data=_token_form(code, verifier))

        assert await _error(response, 400) == "invalid_grant"

    @pytest.mark.parametrize(
        "verifier",
        ["short", "a" * 42 + "!", "a" * 43 + "\n"],
        ids=["short", "char", "newline"],
    )
    async def test_malformed_verifier_is_invalid_grant_though_it_matches_the_challenge(
        self, client: TestClient, verifier: str
    ) -> None:
        code = await _code(client, challenge_for(verifier))

        response = await client.post(TOKEN, data=_token_form(code, verifier))

        assert await _error(response, 400) == "invalid_grant"

    async def test_missing_verifier_is_invalid_request(
        self, client: TestClient
    ) -> None:
        # A real code exists, so only the missing-parameter guard can answer.
        _, challenge = pkce_pair()
        code = await _code(client, challenge)
        form = _token_form(code, "")
        del form["code_verifier"]

        response = await client.post(TOKEN, data=form)

        assert await _error(response, 400) == "invalid_request"


class TestTokenRefreshGrant:
    async def test_refresh_token_is_exchanged_for_new_tokens(
        self, client: TestClient, provider: oauth_legacy.LegacyOAuthProvider
    ) -> None:
        refresh = provider.issue_refresh_token()

        response = await client.post(TOKEN, data=_refresh_form(refresh))

        body = await response.json()
        assert response.status == 200
        assert provider.validate_access_token(body["access_token"]) is True
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"

    async def test_empty_refresh_token_is_invalid_grant(
        self, client: TestClient
    ) -> None:
        response = await client.post(TOKEN, data=_refresh_form(""))
        assert await _error(response, 400) == "invalid_grant"

    async def test_expired_refresh_token_is_invalid_grant(
        self,
        client: TestClient,
        provider: oauth_legacy.LegacyOAuthProvider,
        freezer: FrozenDateTimeFactory,
    ) -> None:
        refresh = provider.issue_refresh_token()

        freezer.tick(timedelta(seconds=oauth_legacy.REFRESH_TOKEN_TTL + 1))
        response = await client.post(TOKEN, data=_refresh_form(refresh))

        assert await _error(response, 400) == "invalid_grant"


class TestTokenClientAuthentication:
    async def test_wrong_secret_is_invalid_client(self, client: TestClient) -> None:
        form = _refresh_form("anything")
        form["client_secret"] = "wrong"

        response = await client.post(TOKEN, data=form)

        assert await _error(response, 401) == "invalid_client"
        assert "Basic" in response.headers["WWW-Authenticate"]

    async def test_unsupported_grant_type_is_refused_after_the_client_passes(
        self, client: TestClient
    ) -> None:
        form = _refresh_form("anything")
        form["grant_type"] = "client_credentials"

        response = await client.post(TOKEN, data=form)

        assert await _error(response, 400) == "unsupported_grant_type"

    async def test_file_upload_in_place_of_credentials_is_a_401_not_a_500(
        self, client: TestClient
    ) -> None:
        # A multipart file field is truthy but not a string; unwrapped, it would
        # slip past the emptiness guard and raise AttributeError.
        form = FormData()
        form.add_field("client_id", b"bytes-id", filename="id.bin")
        form.add_field("client_secret", b"bytes-secret", filename="secret.bin")

        response = await client.post(TOKEN, data=form)

        assert await _error(response, 401) == "invalid_client"

    async def test_undecodable_body_is_a_400_not_a_500(
        self, client: TestClient
    ) -> None:
        response = await client.post(TOKEN, data=b"a=b", headers=UNDECODABLE)
        assert await _error(response, 400) == "invalid_request"

    async def test_token_endpoint_is_hidden_once_legacy_is_not_the_live_mode(
        self, hass: HomeAssistant, client: TestClient
    ) -> None:
        await mw.async_unregister_webhook(hass)

        response = await client.post(TOKEN, data=_refresh_form("anything"))

        assert await _error(response, 404) == "not_found"


class TestBasicAuthCredentials:
    """Credentials in a Basic header, as MCP clients send them."""

    @staticmethod
    async def _probe(client: TestClient, authorization: str) -> Any:
        """A grant type no client uses: only a refused client fails earlier."""
        return await client.post(
            TOKEN,
            data={"grant_type": "unsupported_probe"},
            headers={"Authorization": authorization},
        )

    async def test_header_credentials_authenticate(self, client: TestClient) -> None:
        header = BasicAuth(CLIENT_ID, CLIENT_SECRET).encode()
        response = await self._probe(client, header)
        assert await _error(response, 400) == "unsupported_grant_type"

    async def test_form_body_credentials_authenticate(self, client: TestClient) -> None:
        form = _refresh_form("anything")
        form["grant_type"] = "unsupported_probe"
        response = await client.post(TOKEN, data=form)
        assert await _error(response, 400) == "unsupported_grant_type"

    @pytest.mark.parametrize(
        "header",
        [
            "Basic not-valid-base64!!!",
            f"Basic {base64.b64encode(b'no-colon-here').decode()}",
        ],
        ids=["malformed-base64", "no-colon"],
    )
    async def test_unreadable_header_is_invalid_client(
        self, client: TestClient, header: str
    ) -> None:
        response = await self._probe(client, header)
        assert await _error(response, 401) == "invalid_client"

    @pytest.mark.parametrize(
        ("client_id", "secret", "sent"),
        [
            ("c@id", "p@ss/word", b"c%40id:p%40ss%2Fword"),
            ("c id", "p+ss word", b"c+id:p%2Bss word"),
            ("client+id", "se%41cret+x", b"client+id:se%41cret+x"),
        ],
        ids=["percent-encoded", "plus-is-space", "raw-unencoded"],
    )
    async def test_credentials_authenticate_encoded_or_raw(
        self,
        hass: HomeAssistant,
        hass_client_no_auth: ClientSessionGenerator,
        client_id: str,
        secret: str,
        sent: bytes,
    ) -> None:
        # RFC 6749 section 2.3.1 form-urlencodes Basic credentials, so "+" is a
        # space and a literal "+" arrives as "%2B". Many clients skip the
        # encoding, so a configured "+" or "%XX" must also match when sent raw.
        async with legacy_webhook(hass, client_id=client_id, client_secret=secret):
            client = await hass_client_no_auth()
            header = f"Basic {base64.b64encode(sent).decode()}"

            response = await self._probe(client, header)

            assert await _error(response, 400) == "unsupported_grant_type"
