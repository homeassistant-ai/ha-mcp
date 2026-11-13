"""How the in-process server gets its Home Assistant credential (#2427).

New setups use an administrator's long-lived access token, pasted in or issued
from the login of an account the user chose. Existing installs keep the user
and token an older component created, while they still work. Nothing creates an
administrator any more.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from homeassistant.auth.models import (
    GROUP_ID_ADMIN,
    TOKEN_TYPE_NORMAL,
)
from homeassistant.auth.models import (
    TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN as LLAT,
)
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry, register_auth_provider

from custom_components.ha_mcp_tools import server_credentials as sc
from custom_components.ha_mcp_tools.const import (
    DATA_ACCESS_TOKEN,
    DATA_ADMIN_TOKEN,
    DATA_ISSUED_TOKEN_ID,
    DATA_REFRESH_TOKEN_ID,
    DATA_SERVER_USER_ID,
)

if TYPE_CHECKING:
    from homeassistant.auth.models import RefreshToken, User


async def _create_admin_user(
    hass: HomeAssistant,
    name: str = "Admin",
    *,
    local_only: bool = True,
    active: bool = True,
) -> User:
    """Create an administrator account."""
    if not await hass.auth.async_get_users():
        await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    user = await hass.auth.async_create_user(
        name,
        group_ids=[GROUP_ID_ADMIN],
        local_only=local_only,
    )
    if not active:
        await hass.auth.async_update_user(user, is_active=False)
    return user


async def _create_regular_user(
    hass: HomeAssistant,
    name: str = "User",
    *,
    local_only: bool = True,
    active: bool = True,
) -> User:
    """Create a non-administrator account."""
    if not await hass.auth.async_get_users():
        await hass.auth.async_create_user("Owner", group_ids=[GROUP_ID_ADMIN])
    user = await hass.auth.async_create_user(
        name,
        group_ids=[],
        local_only=local_only,
    )
    if not active:
        await hass.auth.async_update_user(user, is_active=False)
    return user


async def _create_llat(
    hass: HomeAssistant,
    user: User,
    *,
    client_name: str = "Test Token",
) -> tuple[RefreshToken, str]:
    """Create a long-lived refresh token and mint its access token."""
    rt = await hass.auth.async_create_refresh_token(
        user,
        client_name=client_name,
        token_type=LLAT,
    )
    token = hass.auth.async_create_access_token(rt)
    return rt, token


async def _create_login_user(
    hass: HomeAssistant,
    username: str = "ha-mcp-user",
    password: str = "pw",
    *,
    admin: bool = True,
    local_only: bool = True,
) -> User:
    """Create a user with Home Assistant auth provider credentials."""
    provider = cast(
        HassAuthProvider | None,
        next((p for p in hass.auth.auth_providers if p.type == "homeassistant"), None),
    )
    if provider is None:
        provider = cast(
            HassAuthProvider,
            await register_auth_provider(hass, {"type": "homeassistant"}),
        )
    await provider.async_initialize()
    assert provider.data is not None
    if any(u["username"] == username for u in provider.data.users):
        await provider.async_change_password(username, password)
    else:
        await provider.async_add_auth(username, password)
    cred = await provider.async_get_or_create_credentials({"username": username})
    user = (
        await _create_admin_user(hass, username, local_only=local_only)
        if admin
        else await _create_regular_user(hass, username, local_only=local_only)
    )
    await hass.auth.async_link_user(user, cred)
    return user


class TestCredentialFromInput:
    async def test_a_login_issues_a_long_lived_token_for_that_account(
        self, hass: HomeAssistant
    ) -> None:
        # Nobody has to log in as the account (#2427), a local-only one included.
        user = await _create_login_user(hass, "ha-mcp", "pw")

        token, rt_id = await sc.async_credential_from_input(
            hass, username="ha-mcp", password="pw"
        )

        assert token
        assert rt_id is not None
        rt = hass.auth.async_get_refresh_token(rt_id)
        assert rt is not None
        assert rt.user.id == user.id
        assert rt.token_type == LLAT
        assert rt.client_name == "HA-MCP Server"

    async def test_a_wrong_login_issues_nothing(self, hass: HomeAssistant) -> None:
        user = await _create_login_user(hass, "x", "correct_pw")
        with pytest.raises(sc.CredentialRefused) as err:
            await sc.async_credential_from_input(hass, username="x", password="bad")
        assert err.value.reason == "invalid_login"
        assert len(user.refresh_tokens) == 0

    @pytest.mark.parametrize(
        ("admin", "local_only"),
        [(False, True), (True, False)],
        ids=["not_an_administrator", "not_local_only"],
    )
    async def test_only_a_local_only_administrator_login_is_tried(
        self, hass: HomeAssistant, admin: bool, local_only: bool
    ) -> None:
        # The forms are for an account made for HA-MCP; any other account,
        # the user's own included, is refused before its password is checked,
        # with the same error, so they never test another account's password.
        user = await _create_login_user(
            hass, "x", "pw", admin=admin, local_only=local_only
        )
        with pytest.raises(sc.CredentialRefused) as err:
            await sc.async_credential_from_input(hass, username="x", password="pw")
        assert err.value.reason == "invalid_login"
        assert len(user.refresh_tokens) == 0

    async def test_a_second_issued_token_does_not_collide_with_the_first(
        self, hass: HomeAssistant
    ) -> None:
        # Home Assistant refuses a second long-lived token with the same name.
        user = await _create_login_user(hass, "x", "pw")
        await hass.auth.async_create_refresh_token(
            user, client_name="HA-MCP Server", token_type=LLAT
        )

        _, rt2_id = await sc.async_credential_from_input(
            hass, username="x", password="pw"
        )

        assert rt2_id is not None
        rt2 = hass.auth.async_get_refresh_token(rt2_id)
        assert rt2 is not None
        assert rt2.client_name == "HA-MCP Server 2"

    async def test_a_pasted_token_is_used_without_a_login(
        self, hass: HomeAssistant
    ) -> None:
        user = await _create_admin_user(hass, "user")
        _, tok = await _create_llat(hass, user)
        initial_count = len(user.refresh_tokens)

        got = await sc.async_credential_from_input(
            hass, token=tok, username="x", password="pw"
        )

        assert got == (tok, None)
        assert len(user.refresh_tokens) == initial_count

    async def test_an_empty_submission_asks_for_a_credential(
        self, hass: HomeAssistant
    ) -> None:
        with pytest.raises(sc.CredentialRefused) as err:
            await sc.async_credential_from_input(hass)
        assert err.value.reason == "credentials_needed"


class TestTokenProblem:
    async def test_an_administrators_long_lived_token_is_accepted(
        self, hass: HomeAssistant
    ) -> None:
        user = await _create_admin_user(hass)
        _, tok = await _create_llat(hass, user)
        assert sc.token_problem(hass, tok) is None

    def test_an_unknown_or_revoked_token_is_refused(self, hass: HomeAssistant) -> None:
        assert sc.token_problem(hass, "unknown-tok") == "invalid_token"

    async def test_a_session_token_is_refused_because_it_expires(
        self, hass: HomeAssistant
    ) -> None:
        user = await _create_admin_user(hass)
        rt = await hass.auth.async_create_refresh_token(
            user, client_id="https://my-app.com", token_type=TOKEN_TYPE_NORMAL
        )
        tok = hass.auth.async_create_access_token(rt)
        assert sc.token_problem(hass, tok) == "token_not_long_lived"

    async def test_a_token_without_administrator_rights_is_refused(
        self, hass: HomeAssistant
    ) -> None:
        user = await _create_regular_user(hass)
        _, tok = await _create_llat(hass, user)
        assert sc.token_problem(hass, tok) == "token_not_admin"


class TestServerAccessToken:
    async def test_the_supplied_token_is_used_as_is(self, hass: HomeAssistant) -> None:
        user = await _create_admin_user(hass)
        _, tok = await _create_llat(hass, user)
        entry = MockConfigEntry(data={DATA_ADMIN_TOKEN: tok})
        assert await sc.async_server_access_token(hass, entry) == tok

    async def test_a_revoked_supplied_token_asks_for_a_new_one(
        self, hass: HomeAssistant
    ) -> None:
        entry = MockConfigEntry(data={DATA_ADMIN_TOKEN: "revoked-user-token"})
        with pytest.raises(sc.CredentialNeeded) as err:
            await sc.async_server_access_token(hass, entry)
        assert err.value.reason == "invalid_token"

    async def test_an_existing_install_keeps_its_provisioned_token(
        self, hass: HomeAssistant
    ) -> None:
        user = await _create_admin_user(hass)
        rt, _ = await _create_llat(hass, user)
        entry = MockConfigEntry(
            data={DATA_SERVER_USER_ID: user.id, DATA_REFRESH_TOKEN_ID: rt.id}
        )
        token = await sc.async_server_access_token(hass, entry)
        validated = hass.auth.async_validate_access_token(token)
        assert validated is not None
        assert validated.id == rt.id

    async def test_a_demoted_provisioned_account_asks_for_a_new_token(
        self, hass: HomeAssistant
    ) -> None:
        # An administrator can demote the account an older release created.
        user = await _create_regular_user(hass)
        rt, _ = await _create_llat(hass, user)
        entry = MockConfigEntry(
            data={DATA_SERVER_USER_ID: user.id, DATA_REFRESH_TOKEN_ID: rt.id}
        )
        with pytest.raises(sc.CredentialNeeded) as err:
            await sc.async_server_access_token(hass, entry)
        assert err.value.reason == "token_not_admin"

    @pytest.mark.parametrize(
        "case",
        ["user_deleted", "token_revoked", "user_disabled", "token_of_another"],
    )
    async def test_lost_provisioned_credentials_are_not_recreated(
        self, hass: HomeAssistant, case: str
    ) -> None:
        user = await _create_admin_user(hass, f"user-{case}")
        rt, _ = await _create_llat(hass, user)
        entry_user_id = user.id
        entry_rt_id = rt.id

        if case == "user_deleted":
            await hass.auth.async_remove_user(user)
        elif case == "token_revoked":
            hass.auth.async_remove_refresh_token(rt)
        elif case == "user_disabled":
            await hass.auth.async_update_user(user, is_active=False)
        elif case == "token_of_another":
            other = await _create_admin_user(hass, f"other-{case}")
            entry_user_id = other.id

        entry = MockConfigEntry(
            data={
                DATA_SERVER_USER_ID: entry_user_id,
                DATA_REFRESH_TOKEN_ID: entry_rt_id,
            }
        )
        with pytest.raises(sc.CredentialNeeded) as err:
            await sc.async_server_access_token(hass, entry)
        assert err.value.reason == "missing_token"

    async def test_an_entry_with_no_credential_asks_for_one(
        self, hass: HomeAssistant
    ) -> None:
        with pytest.raises(sc.CredentialNeeded) as err:
            await sc.async_server_access_token(hass, MockConfigEntry(data={}))
        assert err.value.reason == "missing_token"


class TestAdoptToken:
    async def test_switching_to_a_supplied_token_retires_the_provisioned_one(
        self, hass: HomeAssistant
    ) -> None:
        old_user = await _create_admin_user(hass, "old-user")
        old_rt, _ = await _create_llat(hass, old_user)
        new_user = await _create_admin_user(hass, "new-user")
        new_rt, new_tok = await _create_llat(hass, new_user)
        data = {
            DATA_SERVER_USER_ID: old_user.id,
            DATA_REFRESH_TOKEN_ID: old_rt.id,
            DATA_ACCESS_TOKEN: "old",
            "webhook_id": "keep",
        }

        new = sc.adopt_admin_token(hass, data, new_tok)

        # The account id stays so removing the entry can still delete it.
        assert new == {
            DATA_SERVER_USER_ID: old_user.id,
            "webhook_id": "keep",
            DATA_ADMIN_TOKEN: new_tok,
        }
        assert hass.auth.async_get_refresh_token(old_rt.id) is None
        assert hass.auth.async_get_refresh_token(new_rt.id) is not None

    async def test_a_token_of_the_provisioned_account_hands_the_account_over(
        self, hass: HomeAssistant
    ) -> None:
        # The administrator gave the old account a login and took a token from
        # it; the account is theirs now, so its id must not linger as "ours".
        account = await _create_admin_user(hass, "u1")
        rt1, _ = await _create_llat(hass, account, client_name="rt1")
        rt2, tok2 = await _create_llat(hass, account, client_name="tok")

        new = sc.adopt_admin_token(
            hass,
            {DATA_SERVER_USER_ID: account.id, DATA_REFRESH_TOKEN_ID: rt1.id},
            tok2,
        )

        assert new == {DATA_ADMIN_TOKEN: tok2}
        assert hass.auth.async_get_refresh_token(rt1.id) is None
        assert hass.auth.async_get_refresh_token(rt2.id) is not None

    async def test_a_token_minted_from_the_provisioned_one_is_not_revoked(
        self, hass: HomeAssistant
    ) -> None:
        # The replacement is an access token of the stored refresh token (say a
        # legacy token recovered from storage): revoking that refresh token
        # would invalidate the very token being adopted.
        account = await _create_admin_user(hass, "u1")
        rt, tok = await _create_llat(hass, account)

        new = sc.adopt_admin_token(
            hass,
            {DATA_SERVER_USER_ID: account.id, DATA_REFRESH_TOKEN_ID: rt.id},
            tok,
        )

        assert hass.auth.async_get_refresh_token(rt.id) is not None
        assert new == {DATA_ADMIN_TOKEN: tok}

    async def test_switching_revokes_a_token_issued_from_a_login(
        self, hass: HomeAssistant
    ) -> None:
        # The component made it, so a switch must not leave it behind.
        user = await _create_admin_user(hass, "user")
        issued, _ = await _create_llat(hass, user, client_name="i1")
        _, new_tok = await _create_llat(hass, user, client_name="i2")

        new = sc.adopt_admin_token(
            hass,
            {DATA_ADMIN_TOKEN: "old", DATA_ISSUED_TOKEN_ID: issued.id},
            new_tok,
            "i2",
        )

        assert hass.auth.async_get_refresh_token(issued.id) is None
        assert new == {DATA_ADMIN_TOKEN: new_tok, DATA_ISSUED_TOKEN_ID: "i2"}

    async def test_switching_never_removes_an_account(
        self, hass: HomeAssistant
    ) -> None:
        user = await _create_admin_user(hass, "u1")
        rt, _ = await _create_llat(hass, user)
        _, new_tok = await _create_llat(hass, user, client_name="new")

        sc.adopt_admin_token(
            hass,
            {DATA_SERVER_USER_ID: user.id, DATA_REFRESH_TOKEN_ID: rt.id},
            new_tok,
        )

        assert await hass.auth.async_get_user(user.id) is not None


class TestReleaseOnRemove:
    async def test_a_token_issued_from_a_login_is_revoked(
        self, hass: HomeAssistant
    ) -> None:
        user = await _create_admin_user(hass, "user")
        issued, _ = await _create_llat(hass, user)

        await sc.async_release_credentials(
            hass, {DATA_ADMIN_TOKEN: "tok", DATA_ISSUED_TOKEN_ID: issued.id}
        )

        assert hass.auth.async_get_refresh_token(issued.id) is None
        assert await hass.auth.async_get_user(user.id) is not None

    async def test_a_supplied_token_and_its_account_are_left_alone(
        self, hass: HomeAssistant
    ) -> None:
        user = await _create_admin_user(hass, "user")
        _, tok = await _create_llat(hass, user)
        data = {DATA_ADMIN_TOKEN: tok, "webhook_id": "w"}

        await sc.async_release_credentials(hass, data)

        assert await hass.auth.async_get_user(user.id) is not None
        assert hass.auth.async_validate_access_token(tok) is not None

    async def test_the_account_an_older_release_created_is_removed(
        self, hass: HomeAssistant
    ) -> None:
        user = await _create_admin_user(hass, "old-provisioned")
        rt, _ = await _create_llat(hass, user)

        await sc.async_release_credentials(
            hass, {DATA_SERVER_USER_ID: user.id, DATA_REFRESH_TOKEN_ID: rt.id}
        )

        assert hass.auth.async_get_refresh_token(rt.id) is None
        assert await hass.auth.async_get_user(user.id) is None

    async def test_switching_tokens_does_not_orphan_the_old_account(
        self, hass: HomeAssistant
    ) -> None:
        old_account = await _create_admin_user(hass, "old-acc")
        old_rt, _ = await _create_llat(hass, old_account)
        new_user = await _create_admin_user(hass, "new-acc")
        _, new_tok = await _create_llat(hass, new_user)

        switched = sc.adopt_admin_token(
            hass,
            {DATA_SERVER_USER_ID: old_account.id, DATA_REFRESH_TOKEN_ID: old_rt.id},
            new_tok,
        )

        await sc.async_release_credentials(hass, switched)

        assert await hass.auth.async_get_user(old_account.id) is None

    async def test_a_handed_over_account_survives_its_tokens_revocation(
        self, hass: HomeAssistant
    ) -> None:
        # Adopt a token of the provisioned account, then revoke it before
        # removing the entry: ownership was recorded at adoption, not inferred
        # from a token that no longer validates.
        account = await _create_admin_user(hass, "handed-over")
        rt1, _ = await _create_llat(hass, account, client_name="rt1")
        rt2, tok2 = await _create_llat(hass, account, client_name="tok2")

        switched = sc.adopt_admin_token(
            hass,
            {DATA_SERVER_USER_ID: account.id, DATA_REFRESH_TOKEN_ID: rt1.id},
            tok2,
        )
        hass.auth.async_remove_refresh_token(rt2)

        await sc.async_release_credentials(hass, switched)

        assert await hass.auth.async_get_user(account.id) is not None

    async def test_the_account_behind_the_supplied_token_is_never_removed(
        self, hass: HomeAssistant
    ) -> None:
        # An administrator may have given the old account a login and taken a
        # token from it; that account is theirs now.
        account = await _create_admin_user(hass, "supplied-acc")
        _, tok = await _create_llat(hass, account)

        await sc.async_release_credentials(
            hass, {DATA_SERVER_USER_ID: account.id, DATA_ADMIN_TOKEN: tok}
        )

        assert await hass.auth.async_get_user(account.id) is not None
