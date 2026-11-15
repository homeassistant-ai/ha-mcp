"""The legacy OAuth component: provider, view binding and credential lifecycle.

``custom_components/ha_mcp_tools/oauth_legacy.py``. The provider (signed tokens,
PKCE codes, client credentials) holds no Home Assistant object, so those tests
drive it directly with Core's ``freezer`` clock. ``bind_legacy_views`` runs
against Core's real HTTP component, and the credential lifecycle runs through
Core's own setup of a server entry (the server itself is replaced, see
``_server_entry``).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import const, oauth_legacy
from custom_components.ha_mcp_tools.const import DOMAIN, WEBHOOK_AUTH_LEGACY

from ._oauth_legacy_core import (
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    challenge_for,
    make_provider,
    pkce_pair,
)
from ._server_entry import add_server_entry, replace_server, set_up

# -- view binding and credential lifecycle ------------------------------


@pytest.fixture
async def register_view(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[MagicMock]:
    """Home Assistant's own view registration on its real HTTP component."""
    assert await async_setup_component(hass, "http", {})
    spy = MagicMock(wraps=hass.http.register_view)
    monkeypatch.setattr(hass.http, "register_view", spy)
    yield spy


def _bind(
    hass: HomeAssistant, secret: str = CLIENT_SECRET, key: bytes | str = b""
) -> tuple[oauth_legacy.LegacyOAuthProvider, bool]:
    return oauth_legacy.bind_legacy_views(
        hass, CLIENT_ID, secret, key or secrets.token_bytes(32)
    )


@pytest.mark.usefixtures("register_view")
class TestBindLegacyViews:
    def test_first_bind_registers_the_authorize_and_token_views(
        self, hass: HomeAssistant, register_view: MagicMock
    ) -> None:
        provider, _ = _bind(hass)

        assert provider.client_id == CLIENT_ID
        assert hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] == DOMAIN
        assert register_view.call_count == 2

    def test_first_bind_at_boot_needs_no_restart(self, hass: HomeAssistant) -> None:
        hass.set_state(CoreState.not_running)
        assert _bind(hass)[1] is False

    def test_first_bind_while_running_needs_a_restart(
        self, hass: HomeAssistant
    ) -> None:
        # A route registered after Core froze its router is not live until a
        # restart.
        assert _bind(hass)[1] is True

    def test_credential_change_alone_needs_a_restart(self, hass: HomeAssistant) -> None:
        # After a boot bind nothing else is pending, so the restart can only
        # come from the changed credentials.
        hass.set_state(CoreState.not_running)
        key = secrets.token_bytes(32)
        _bind(hass, key=key)

        assert _bind(hass, "rotated-secret", key)[1] is True

    def test_reload_after_a_boot_bind_reuses_the_provider_and_stays_live(
        self, hass: HomeAssistant, register_view: MagicMock
    ) -> None:
        # The pending state is remembered from the first bind, not recomputed
        # from the now-running Core.
        hass.set_state(CoreState.not_running)
        key = secrets.token_bytes(32)
        first, first_restart = _bind(hass, key=key)
        assert first_restart is False
        register_view.reset_mock()
        hass.set_state(CoreState.running)

        second, restart_needed = _bind(hass, key=key)

        assert second is first
        assert restart_needed is False
        register_view.assert_not_called()

    def test_reload_after_a_mid_session_bind_stays_restart_pending(
        self, hass: HomeAssistant, register_view: MagicMock
    ) -> None:
        # An unrelated reload before the restart must not clear the repair:
        # the views are still not live.
        key = secrets.token_bytes(32)
        first, first_restart = _bind(hass, key=key)
        assert first_restart is True
        register_view.reset_mock()

        second, restart_needed = _bind(hass, key=key)

        assert second is first
        assert restart_needed is True
        register_view.assert_not_called()

    def test_changed_credentials_keep_the_old_provider_until_a_restart(
        self, hass: HomeAssistant
    ) -> None:
        # aiohttp cannot rebind a registered view, so the old provider stays
        # authoritative.
        first, _ = _bind(hass)

        second, restart_needed = _bind(hass, "a-new-secret")

        assert second is first
        assert restart_needed is True

    def test_another_owner_of_the_routes_is_a_conflict(
        self, hass: HomeAssistant
    ) -> None:
        hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] = "webhook_proxy"
        with pytest.raises(oauth_legacy.LegacyOAuthRouteConflict):
            _bind(hass)

    def test_signing_key_may_be_the_hex_string_the_entry_stores(
        self, hass: HomeAssistant
    ) -> None:
        provider, _ = _bind(hass, key=secrets.token_hex(32))
        assert provider.issue_access_token()


class TestLegacyCredentialsActive:
    """The check the startup log and Configure screen use to withhold rotated
    credentials while the rotation is pending a restart."""

    def test_false_when_never_bound(self, hass: HomeAssistant) -> None:
        active = oauth_legacy.legacy_credentials_active(
            hass, CLIENT_ID, CLIENT_SECRET, secrets.token_bytes(32)
        )
        assert active is False

    def test_false_when_another_integration_owns_the_routes(
        self, hass: HomeAssistant
    ) -> None:
        hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] = "webhook_proxy_addon"
        hass.data[oauth_legacy.OAUTH_ROUTE_KEY_FINGERPRINT] = "anything"
        active = oauth_legacy.legacy_credentials_active(
            hass, CLIENT_ID, CLIENT_SECRET, secrets.token_bytes(32)
        )
        assert active is False

    @pytest.mark.usefixtures("register_view")
    def test_true_for_the_bound_credentials(self, hass: HomeAssistant) -> None:
        key = secrets.token_bytes(32)
        _bind(hass, key=key)

        assert oauth_legacy.legacy_credentials_active(
            hass, CLIENT_ID, CLIENT_SECRET, key
        )

    @pytest.mark.usefixtures("register_view")
    def test_false_for_rotated_credentials_until_the_rebind(
        self, hass: HomeAssistant
    ) -> None:
        # The reuse branch keeps the OLD provider bound after a change.
        key = secrets.token_bytes(32)
        _bind(hass, key=key)

        assert not oauth_legacy.legacy_credentials_active(
            hass, CLIENT_ID, "rotated-secret", key
        )

    @pytest.mark.usefixtures("register_view")
    def test_hex_string_signing_key_matches_the_bound_bytes(
        self, hass: HomeAssistant
    ) -> None:
        key = secrets.token_bytes(32)
        _bind(hass, key=key)

        assert oauth_legacy.legacy_credentials_active(
            hass, CLIENT_ID, CLIENT_SECRET, key.hex()
        )

    def test_scoped_provider_credentials_are_active_when_a_foreign_owner_holds_root(
        self, hass: HomeAssistant
    ) -> None:
        hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] = "mcp_proxy_dev"
        key = secrets.token_bytes(32)
        oauth_legacy.build_unbound_legacy_provider(hass, CLIENT_ID, CLIENT_SECRET, key)

        assert oauth_legacy.legacy_credentials_active(
            hass, CLIENT_ID, CLIENT_SECRET, key
        )
        assert not oauth_legacy.legacy_credentials_active(
            hass, CLIENT_ID, "different-secret", key
        )

    def test_unloaded_scoped_provider_no_longer_exposes_its_credentials(
        self, hass: HomeAssistant
    ) -> None:
        hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] = "mcp_proxy_dev"
        key = secrets.token_bytes(32)
        oauth_legacy.build_unbound_legacy_provider(hass, CLIENT_ID, CLIENT_SECRET, key)

        oauth_legacy.clear_scoped_legacy_credentials(hass)

        assert not oauth_legacy.legacy_credentials_active(
            hass, CLIENT_ID, CLIENT_SECRET, key
        )


OLD_KEY = "aa" * 32


@pytest.fixture
async def server(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """Core's HTTP component up, so entry setup can bind the OAuth routes."""
    assert await async_setup_component(hass, "http", {})
    return replace_server(monkeypatch)


def _legacy_options(**options: object) -> dict[str, object]:
    return {const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY, **options}


def _stored_credentials() -> dict[str, str]:
    return {
        const.DATA_OAUTH_CLIENT_ID: "hamcp-old",
        const.DATA_OAUTH_CLIENT_SECRET: "old-secret",
        const.DATA_OAUTH_SIGNING_KEY: OLD_KEY,
    }


@pytest.mark.usefixtures("server")
class TestLegacyCredentialLifecycle:
    """Setup of a legacy-mode server entry mints, keeps and rotates the client
    credentials; every change rotates the signing key to revoke old tokens."""

    async def test_first_setup_mints_the_client_id_secret_and_signing_key(
        self, hass: HomeAssistant
    ) -> None:
        entry = add_server_entry(hass, options=_legacy_options())

        assert await set_up(hass, entry)

        assert entry.data[const.DATA_OAUTH_CLIENT_ID].startswith("hamcp-")
        assert entry.data[const.DATA_OAUTH_CLIENT_SECRET]
        # The entry data must stay JSON serializable: the key is a hex string.
        assert len(bytes.fromhex(entry.data[const.DATA_OAUTH_SIGNING_KEY])) == 32

    async def test_reload_keeps_the_minted_credentials(
        self, hass: HomeAssistant
    ) -> None:
        entry = add_server_entry(hass, options=_legacy_options())
        assert await set_up(hass, entry)
        minted = dict(entry.data)

        assert await hass.config_entries.async_reload(entry.entry_id)

        assert dict(entry.data) == minted

    async def test_regenerate_mints_new_credentials_and_rotates_the_key(
        self, hass: HomeAssistant
    ) -> None:
        entry = add_server_entry(
            hass,
            data=_stored_credentials(),
            options=_legacy_options(**{const.OPT_OAUTH_REGENERATE: True}),
        )

        assert await set_up(hass, entry)

        assert entry.data[const.DATA_OAUTH_CLIENT_ID] != "hamcp-old"
        assert entry.data[const.DATA_OAUTH_CLIENT_SECRET] != "old-secret"
        assert entry.data[const.DATA_OAUTH_SIGNING_KEY] != OLD_KEY
        assert entry.options[const.OPT_OAUTH_REGENERATE] is False

    async def test_client_id_override_is_adopted_and_rotates_the_key(
        self, hass: HomeAssistant
    ) -> None:
        # Rotating the key stops a re-used former client_id (A, B, A) from
        # resurrecting its old tokens.
        entry = add_server_entry(
            hass,
            data=_stored_credentials(),
            options=_legacy_options(**{const.OPT_OAUTH_CLIENT_ID: "my-custom-client"}),
        )

        assert await set_up(hass, entry)

        assert entry.data[const.DATA_OAUTH_CLIENT_ID] == "my-custom-client"
        assert entry.data[const.DATA_OAUTH_SIGNING_KEY] != OLD_KEY
        # The cleartext override must not stay readable in the entry options.
        assert entry.options[const.OPT_OAUTH_CLIENT_ID] == ""

    async def test_client_secret_override_is_adopted_and_rotates_the_key(
        self, hass: HomeAssistant
    ) -> None:
        # Token validation never involves the secret, so only a new key evicts
        # a holder of the old one.
        entry = add_server_entry(
            hass,
            data=_stored_credentials(),
            options=_legacy_options(**{const.OPT_OAUTH_CLIENT_SECRET: "my-secret"}),
        )

        assert await set_up(hass, entry)

        assert entry.data[const.DATA_OAUTH_CLIENT_SECRET] == "my-secret"
        assert entry.data[const.DATA_OAUTH_SIGNING_KEY] != OLD_KEY
        assert entry.options[const.OPT_OAUTH_CLIENT_SECRET] == ""

    async def test_override_equal_to_the_current_value_is_still_cleared(
        self, hass: HomeAssistant
    ) -> None:
        entry = add_server_entry(
            hass,
            data=_stored_credentials(),
            options=_legacy_options(**{const.OPT_OAUTH_CLIENT_ID: "hamcp-old"}),
        )

        assert await set_up(hass, entry)

        assert entry.options[const.OPT_OAUTH_CLIENT_ID] == ""
        assert entry.data[const.DATA_OAUTH_CLIENT_ID] == "hamcp-old"
        assert entry.data[const.DATA_OAUTH_SIGNING_KEY] == OLD_KEY


# -- provider ------------------------------------------------------------


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
