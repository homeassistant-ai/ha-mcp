"""The legacy OAuth provider: signed tokens, PKCE codes and client credentials.

``custom_components/ha_mcp_tools/oauth_legacy.py``. The provider holds no Home
Assistant object, so these tests drive it directly; the clock is Core's
``freezer``.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from freezegun.api import FrozenDateTimeFactory

from custom_components.ha_mcp_tools import oauth_legacy
from custom_components.ha_mcp_tools.const import WEBHOOK_AUTH_LEGACY

from ._oauth_legacy_core import (
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    challenge_for,
    make_provider,
    pkce_pair,
)


class TestTokenCodec:
    def test_issued_access_token_is_accepted(self) -> None:
        provider = make_provider()
        assert provider.validate_access_token(provider.issue_access_token()) is True

    def test_issued_refresh_token_is_accepted(self) -> None:
        provider = make_provider()
        assert provider.validate_refresh_token(provider.issue_refresh_token()) is True

    def test_access_token_cannot_be_used_to_refresh(self) -> None:
        provider = make_provider()
        assert provider.validate_refresh_token(provider.issue_access_token()) is False

    def test_refresh_token_cannot_be_used_as_access_token(self) -> None:
        provider = make_provider()
        assert provider.validate_access_token(provider.issue_refresh_token()) is False

    def test_forged_signature_is_rejected(self) -> None:
        provider = make_provider()
        body, sig = provider.issue_access_token().rsplit(".", 1)
        forged_char = "A" if sig[0] != "A" else "B"
        assert provider.validate_access_token(f"{body}.{forged_char}{sig[1:]}") is False

    def test_edited_body_is_rejected(self) -> None:
        provider = make_provider()
        body, sig = provider.issue_access_token().rsplit(".", 1)
        assert provider.validate_access_token(f"{body}extra.{sig}") is False

    def test_token_without_a_signature_is_rejected(self) -> None:
        assert make_provider().validate_access_token("not-a-real-token") is False

    def test_token_is_valid_until_its_lifetime_ends(
        self, freezer: FrozenDateTimeFactory
    ) -> None:
        provider = make_provider()
        token = provider.issue_access_token()

        freezer.tick(timedelta(seconds=oauth_legacy.ACCESS_TOKEN_TTL - 1))
        assert provider.validate_access_token(token) is True

        freezer.tick(timedelta(seconds=1))
        assert provider.validate_access_token(token) is False

    def test_refresh_token_is_valid_until_its_lifetime_ends(
        self, freezer: FrozenDateTimeFactory
    ) -> None:
        provider = make_provider()
        token = provider.issue_refresh_token()

        freezer.tick(timedelta(seconds=oauth_legacy.REFRESH_TOKEN_TTL - 1))
        assert provider.validate_refresh_token(token) is True

        freezer.tick(timedelta(seconds=1))
        assert provider.validate_refresh_token(token) is False

    def test_token_of_a_rotated_client_id_is_revoked(self) -> None:
        key = secrets.token_bytes(32)
        token = make_provider(
            client_id="old-client", signing_key=key
        ).issue_access_token()
        rotated = make_provider(client_id="new-client", signing_key=key)
        assert rotated.validate_access_token(token) is False

    def test_non_ascii_bearer_is_rejected_not_raised(self) -> None:
        # A bearer with a non-ASCII char before the dot must not escape the
        # webhook gate as an exception.
        assert make_provider().validate_access_token("é.c2ln") is False


class TestProviderConstruction:
    def test_empty_client_id_is_refused(self) -> None:
        with pytest.raises(ValueError, match="client_id"):
            oauth_legacy.LegacyOAuthProvider(
                "", CLIENT_SECRET, secrets.token_bytes(32), lambda: WEBHOOK_AUTH_LEGACY
            )

    def test_empty_client_secret_is_refused(self) -> None:
        with pytest.raises(ValueError, match="client_secret"):
            oauth_legacy.LegacyOAuthProvider(
                CLIENT_ID, "", secrets.token_bytes(32), lambda: WEBHOOK_AUTH_LEGACY
            )

    def test_short_signing_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 32 bytes"):
            oauth_legacy.LegacyOAuthProvider(
                CLIENT_ID, CLIENT_SECRET, b"too-short", lambda: WEBHOOK_AUTH_LEGACY
            )


class TestValidateBearer:
    @staticmethod
    def _with_header(value: str | None) -> web.Request:
        headers = {} if value is None else {"Authorization": value}
        return make_mocked_request("POST", "/api/webhook/x", headers=headers)

    def test_access_token_in_a_bearer_header_is_accepted(self) -> None:
        provider = make_provider()
        request = self._with_header(f"Bearer {provider.issue_access_token()}")
        assert provider.validate_bearer(request) is True

    def test_request_without_an_authorization_header_is_refused(self) -> None:
        assert make_provider().validate_bearer(self._with_header(None)) is False

    def test_basic_scheme_is_refused(self) -> None:
        assert (
            make_provider().validate_bearer(self._with_header("Basic abc123")) is False
        )

    def test_refresh_token_cannot_authorize_a_request(self) -> None:
        provider = make_provider()
        request = self._with_header(f"Bearer {provider.issue_refresh_token()}")
        assert provider.validate_bearer(request) is False


class TestPKCECodes:
    def test_code_is_redeemed_with_its_verifier(self) -> None:
        provider = make_provider()
        verifier, challenge = pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        assert code is not None
        assert provider.consume_code(code, REDIRECT_URI, verifier) is True

    def test_code_is_refused_for_another_verifier(self) -> None:
        provider = make_provider()
        _, challenge = pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        assert code is not None
        assert provider.consume_code(code, REDIRECT_URI, pkce_pair()[0]) is False

    def test_code_works_only_once(self) -> None:
        provider = make_provider()
        verifier, challenge = pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        assert code is not None
        assert provider.consume_code(code, REDIRECT_URI, verifier) is True
        assert provider.consume_code(code, REDIRECT_URI, verifier) is False

    def test_code_expires(self, freezer: FrozenDateTimeFactory) -> None:
        provider = make_provider()
        verifier, challenge = pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        assert code is not None

        freezer.tick(timedelta(seconds=oauth_legacy.AUTH_CODE_TTL + 1))

        assert provider.consume_code(code, REDIRECT_URI, verifier) is False

    def test_code_is_refused_for_another_redirect_uri(self) -> None:
        provider = make_provider()
        verifier, challenge = pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        assert code is not None
        other = "https://other.example.com/cb"
        assert provider.consume_code(code, other, verifier) is False

    def test_unknown_code_is_refused(self) -> None:
        verifier, _ = pkce_pair()
        provider = make_provider()
        assert provider.consume_code("never-issued", REDIRECT_URI, verifier) is False

    @pytest.mark.parametrize(
        "verifier",
        [
            "short",
            "a" * 42 + "!",
            "a" * 43 + "\n",
            "a" * (oauth_legacy.PKCE_VERIFIER_MAX + 1),
        ],
        ids=["too-short", "bad-character", "trailing-newline", "over-max-length"],
    )
    def test_malformed_verifier_is_refused_though_it_matches_the_challenge(
        self, verifier: str
    ) -> None:
        # The challenge is derived from the malformed verifier itself, so the
        # S256 check would pass and only the RFC 7636 shape check refuses it.
        provider = make_provider()
        code = provider.issue_code(REDIRECT_URI, challenge_for(verifier))
        assert code is not None
        assert provider.consume_code(code, REDIRECT_URI, verifier) is False

    def test_malformed_verifier_does_not_burn_the_code(self) -> None:
        provider = make_provider()
        verifier, challenge = pkce_pair()
        code = provider.issue_code(REDIRECT_URI, challenge)
        assert code is not None
        assert provider.consume_code(code, REDIRECT_URI, "short") is False
        assert provider.consume_code(code, REDIRECT_URI, verifier) is True

    def test_new_codes_are_refused_when_the_pending_store_is_full(self) -> None:
        provider = make_provider()
        _, challenge = pkce_pair()
        for _ in range(oauth_legacy.MAX_PENDING_CODES):
            assert provider.issue_code(REDIRECT_URI, challenge) is not None
        assert provider.issue_code(REDIRECT_URI, challenge) is None

    def test_expired_codes_make_room_in_a_full_store(
        self, freezer: FrozenDateTimeFactory
    ) -> None:
        # Without the prune, a store full of dead codes refuses issuance forever.
        provider = make_provider()
        _, challenge = pkce_pair()
        for _ in range(oauth_legacy.MAX_PENDING_CODES):
            assert provider.issue_code(REDIRECT_URI, challenge) is not None
        assert provider.issue_code(REDIRECT_URI, challenge) is None

        freezer.tick(timedelta(seconds=oauth_legacy.AUTH_CODE_TTL + 1))

        assert provider.issue_code(REDIRECT_URI, challenge) is not None


class TestAuthenticateClient:
    def test_correct_credentials_are_accepted(self) -> None:
        assert make_provider().authenticate_client(CLIENT_ID, CLIENT_SECRET) is True

    def test_wrong_secret_is_refused(self) -> None:
        assert make_provider().authenticate_client(CLIENT_ID, "wrong-secret") is False

    def test_wrong_client_id_is_refused(self) -> None:
        assert make_provider().authenticate_client("wrong", CLIENT_SECRET) is False

    def test_missing_client_id_is_refused(self) -> None:
        assert make_provider().authenticate_client(None, CLIENT_SECRET) is False

    def test_missing_client_secret_is_refused(self) -> None:
        assert make_provider().authenticate_client(CLIENT_ID, None) is False


class TestIsActive:
    def test_active_while_legacy_is_the_live_mode(self) -> None:
        assert make_provider().is_active() is True

    def test_inactive_while_another_mode_is_live(self) -> None:
        provider = oauth_legacy.LegacyOAuthProvider(
            CLIENT_ID, CLIENT_SECRET, secrets.token_bytes(32), lambda: "ha_auth"
        )
        assert provider.is_active() is False

    def test_inactive_while_no_mode_is_live(self) -> None:
        provider = oauth_legacy.LegacyOAuthProvider(
            CLIENT_ID, CLIENT_SECRET, secrets.token_bytes(32), lambda: None
        )
        assert provider.is_active() is False
