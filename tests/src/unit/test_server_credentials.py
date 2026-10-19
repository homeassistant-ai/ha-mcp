"""How the in-process server gets its Home Assistant credential (#2427).

New setups use an administrator's long-lived access token, pasted in or issued
from the login of an account the user chose. Existing installs keep the user
and token an older component created, while they still work. Nothing creates an
administrator any more.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

from homeassistant.auth.models import (  # noqa: E402
    TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN as LLAT,
)

from custom_components.ha_mcp_tools import server_credentials as sc  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_ACCESS_TOKEN,
    DATA_ADMIN_TOKEN,
    DATA_ISSUED_TOKEN_ID,
    DATA_REFRESH_TOKEN_ID,
    DATA_SERVER_USER_ID,
)


def _user(
    uid: str = "u1", *, admin: bool = True, active: bool = True
) -> SimpleNamespace:
    return SimpleNamespace(id=uid, is_admin=admin, is_active=active)


def _rt(
    rt_id: str = "rt1",
    *,
    user: SimpleNamespace | None = None,
    token_type: str = LLAT,
) -> SimpleNamespace:
    return SimpleNamespace(id=rt_id, user=user or _user(), token_type=token_type)


def _hass(
    *,
    validated: SimpleNamespace | None = None,
    users: dict[str, SimpleNamespace] | None = None,
    refresh_tokens: dict[str, SimpleNamespace] | None = None,
) -> MagicMock:
    hass = MagicMock(name="hass")
    users = users or {}
    refresh_tokens = refresh_tokens or {}
    hass.auth.async_validate_access_token = MagicMock(return_value=validated)
    hass.auth.async_get_user = AsyncMock(side_effect=users.get)
    hass.auth.async_get_refresh_token = MagicMock(side_effect=refresh_tokens.get)
    hass.auth.async_create_access_token = MagicMock(return_value="minted")
    hass.auth.async_remove_refresh_token = MagicMock()
    hass.auth.async_remove_user = AsyncMock()
    hass.auth.async_create_user = AsyncMock()
    hass.auth.async_create_refresh_token = AsyncMock()
    return hass


def _login_hass(
    user: SimpleNamespace | None,
    *,
    valid: bool = True,
    names: tuple[str, ...] = (),
    local_only: bool = True,
) -> MagicMock:
    """A Home Assistant whose own auth provider knows ``user``'s login."""
    invalid = sys.modules["homeassistant.auth.providers.homeassistant"].InvalidAuth

    async def validate(_username: str, _password: str) -> None:
        if not valid:
            raise invalid

    provider = SimpleNamespace(
        type="homeassistant",
        async_validate_login=AsyncMock(side_effect=validate),
        async_get_or_create_credentials=AsyncMock(return_value="credentials"),
    )
    if user is not None:
        user.local_only = local_only
        user.refresh_tokens = {
            str(i): SimpleNamespace(client_name=n) for i, n in enumerate(names)
        }
    hass = _hass()
    hass.auth.auth_providers = [provider]
    hass.auth.async_get_user_by_credentials = AsyncMock(return_value=user)
    hass.auth.async_create_refresh_token = AsyncMock(
        return_value=SimpleNamespace(id="issued1")
    )
    return hass


class TestCredentialFromInput:
    async def test_a_login_issues_a_long_lived_token_for_that_account(self) -> None:
        # Nobody has to log in as the account (#2427), a local-only one included.
        account = _user("u7")
        hass = _login_hass(account)

        got = await sc.async_credential_from_input(
            hass, username="ha-mcp", password="pw"
        )

        assert got == ("minted", "issued1")
        call = hass.auth.async_create_refresh_token.await_args
        assert call.args == (account,)
        assert call.kwargs["token_type"] == LLAT
        assert call.kwargs["client_name"] == "HA-MCP Server"

    async def test_a_wrong_login_issues_nothing(self) -> None:
        hass = _login_hass(_user(), valid=False)
        with pytest.raises(sc.CredentialRefused) as err:
            await sc.async_credential_from_input(hass, username="x", password="bad")
        assert err.value.reason == "invalid_login"
        hass.auth.async_create_refresh_token.assert_not_awaited()

    @pytest.mark.parametrize(
        ("admin", "local_only"),
        [(False, True), (True, False)],
        ids=["not_an_administrator", "not_local_only"],
    )
    async def test_only_a_local_only_administrator_login_is_tried(
        self, admin: bool, local_only: bool
    ) -> None:
        # The forms are for an account made for HA-MCP; any other account,
        # the user's own included, is refused before its password is checked,
        # with the same error, so they never test another account's password.
        hass = _login_hass(_user(admin=admin), local_only=local_only)
        with pytest.raises(sc.CredentialRefused) as err:
            await sc.async_credential_from_input(hass, username="x", password="pw")
        assert err.value.reason == "invalid_login"
        hass.auth.auth_providers[0].async_validate_login.assert_not_awaited()
        hass.auth.async_create_refresh_token.assert_not_awaited()

    async def test_a_second_issued_token_does_not_collide_with_the_first(
        self,
    ) -> None:
        # Home Assistant refuses a second long-lived token with the same name.
        hass = _login_hass(_user(), names=("HA-MCP Server",))
        await sc.async_credential_from_input(hass, username="x", password="pw")
        call = hass.auth.async_create_refresh_token.await_args
        assert call.kwargs["client_name"] == "HA-MCP Server 2"

    async def test_a_pasted_token_is_used_without_a_login(self) -> None:
        hass = _login_hass(_user())
        hass.auth.async_validate_access_token = MagicMock(return_value=_rt())

        got = await sc.async_credential_from_input(
            hass, token="tok", username="x", password="pw"
        )

        assert got == ("tok", None)
        hass.auth.async_create_refresh_token.assert_not_awaited()

    async def test_an_empty_submission_asks_for_a_credential(self) -> None:
        with pytest.raises(sc.CredentialRefused) as err:
            await sc.async_credential_from_input(_login_hass(_user()))
        assert err.value.reason == "credentials_needed"


class TestTokenProblem:
    def test_an_administrators_long_lived_token_is_accepted(self) -> None:
        assert sc.token_problem(_hass(validated=_rt()), "tok") is None

    def test_an_unknown_or_revoked_token_is_refused(self) -> None:
        assert sc.token_problem(_hass(validated=None), "tok") == "invalid_token"

    def test_a_session_token_is_refused_because_it_expires(self) -> None:
        session = _rt(token_type="normal")
        assert sc.token_problem(_hass(validated=session), "tok") == (
            "token_not_long_lived"
        )

    def test_a_token_without_administrator_rights_is_refused(self) -> None:
        validated = _rt(user=_user(admin=False))
        assert sc.token_problem(_hass(validated=validated), "tok") == "token_not_admin"


class TestServerAccessToken:
    async def test_the_supplied_token_is_used_as_is(self) -> None:
        hass = _hass(validated=_rt())
        entry = SimpleNamespace(data={DATA_ADMIN_TOKEN: "user-token"})
        assert await sc.async_server_access_token(hass, entry) == "user-token"

    async def test_a_revoked_supplied_token_asks_for_a_new_one(self) -> None:
        hass = _hass(validated=None)
        entry = SimpleNamespace(data={DATA_ADMIN_TOKEN: "user-token"})
        with pytest.raises(sc.CredentialNeeded) as err:
            await sc.async_server_access_token(hass, entry)
        assert err.value.reason == "invalid_token"
        hass.auth.async_create_user.assert_not_awaited()

    async def test_an_existing_install_keeps_its_provisioned_token(self) -> None:
        user = _user()
        rt = _rt(user=user)
        hass = _hass(users={"u1": user}, refresh_tokens={"rt1": rt})
        entry = SimpleNamespace(
            data={DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}
        )
        assert await sc.async_server_access_token(hass, entry) == "minted"
        hass.auth.async_create_access_token.assert_called_once_with(rt)

    async def test_a_demoted_provisioned_account_asks_for_a_new_token(self) -> None:
        # An administrator can demote the account an older release created.
        user = _user(admin=False)
        hass = _hass(users={"u1": user}, refresh_tokens={"rt1": _rt(user=user)})
        entry = SimpleNamespace(
            data={DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}
        )
        with pytest.raises(sc.CredentialNeeded) as err:
            await sc.async_server_access_token(hass, entry)
        assert err.value.reason == "token_not_admin"
        hass.auth.async_create_access_token.assert_not_called()

    @pytest.mark.parametrize(
        ("users", "refresh_tokens"),
        [
            ({}, {"rt1": _rt()}),
            ({"u1": _user()}, {}),
            ({"u1": _user(active=False)}, {"rt1": _rt()}),
            ({"u1": _user()}, {"rt1": _rt(user=_user("someone-else"))}),
        ],
        ids=["user_deleted", "token_revoked", "user_disabled", "token_of_another"],
    )
    async def test_lost_provisioned_credentials_are_not_recreated(
        self,
        users: dict[str, SimpleNamespace],
        refresh_tokens: dict[str, SimpleNamespace],
    ) -> None:
        hass = _hass(users=users, refresh_tokens=refresh_tokens)
        entry = SimpleNamespace(
            data={DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}
        )
        with pytest.raises(sc.CredentialNeeded):
            await sc.async_server_access_token(hass, entry)
        hass.auth.async_create_user.assert_not_awaited()
        hass.auth.async_create_refresh_token.assert_not_awaited()

    async def test_an_entry_with_no_credential_asks_for_one(self) -> None:
        with pytest.raises(sc.CredentialNeeded) as err:
            await sc.async_server_access_token(_hass(), SimpleNamespace(data={}))
        assert err.value.reason == "missing_token"


class TestAdoptToken:
    def test_switching_to_a_supplied_token_retires_the_provisioned_one(self) -> None:
        rt = _rt()
        hass = _hass(refresh_tokens={"rt1": rt})
        data = {
            DATA_SERVER_USER_ID: "u1",
            DATA_REFRESH_TOKEN_ID: "rt1",
            DATA_ACCESS_TOKEN: "old",
            "webhook_id": "keep",
        }

        new = sc.adopt_admin_token(hass, data, "user-token")

        # The account id stays so removing the entry can still delete it.
        assert new == {
            DATA_SERVER_USER_ID: "u1",
            "webhook_id": "keep",
            DATA_ADMIN_TOKEN: "user-token",
        }
        hass.auth.async_remove_refresh_token.assert_called_once_with(rt)

    def test_a_token_of_the_provisioned_account_hands_the_account_over(self) -> None:
        # The administrator gave the old account a login and took a token from
        # it; the account is theirs now, so its id must not linger as "ours".
        account = _user("u1")
        hass = _hass(
            refresh_tokens={"rt1": _rt(user=account)},
            validated=_rt("rt2", user=account),
        )

        new = sc.adopt_admin_token(
            hass, {DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}, "tok"
        )

        assert new == {DATA_ADMIN_TOKEN: "tok"}

    def test_a_token_minted_from_the_provisioned_one_is_not_revoked(self) -> None:
        # The replacement is an access token of the stored refresh token (say a
        # legacy token recovered from storage): revoking that refresh token
        # would invalidate the very token being adopted.
        account = _user("u1")
        rt = _rt(user=account)
        hass = _hass(refresh_tokens={"rt1": rt}, validated=rt)

        new = sc.adopt_admin_token(
            hass, {DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}, "tok"
        )

        hass.auth.async_remove_refresh_token.assert_not_called()
        assert new == {DATA_ADMIN_TOKEN: "tok"}

    def test_switching_revokes_a_token_issued_from_a_login(self) -> None:
        # The component made it, so a switch must not leave it behind.
        issued = _rt("i1")
        hass = _hass(refresh_tokens={"i1": issued})

        new = sc.adopt_admin_token(
            hass, {DATA_ADMIN_TOKEN: "old", DATA_ISSUED_TOKEN_ID: "i1"}, "tok", "i2"
        )

        hass.auth.async_remove_refresh_token.assert_called_once_with(issued)
        assert new == {DATA_ADMIN_TOKEN: "tok", DATA_ISSUED_TOKEN_ID: "i2"}

    def test_switching_never_removes_an_account(self) -> None:
        hass = _hass(refresh_tokens={"rt1": _rt()})
        sc.adopt_admin_token(
            hass, {DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}, "tok"
        )
        hass.auth.async_remove_user.assert_not_awaited()


class TestReleaseOnRemove:
    async def test_a_token_issued_from_a_login_is_revoked(self) -> None:
        issued = _rt("i1")
        hass = _hass(refresh_tokens={"i1": issued})

        await sc.async_release_credentials(
            hass, {DATA_ADMIN_TOKEN: "tok", DATA_ISSUED_TOKEN_ID: "i1"}
        )

        hass.auth.async_remove_refresh_token.assert_called_once_with(issued)
        hass.auth.async_remove_user.assert_not_awaited()

    async def test_a_supplied_token_and_its_account_are_left_alone(self) -> None:
        hass = _hass()
        data = {DATA_ADMIN_TOKEN: "user-token", "webhook_id": "w"}

        await sc.async_release_credentials(hass, data)

        hass.auth.async_remove_user.assert_not_awaited()
        hass.auth.async_remove_refresh_token.assert_not_called()

    async def test_the_account_an_older_release_created_is_removed(self) -> None:
        user = _user()
        rt = _rt(user=user)
        hass = _hass(users={"u1": user}, refresh_tokens={"rt1": rt})

        await sc.async_release_credentials(
            hass, {DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}
        )

        hass.auth.async_remove_refresh_token.assert_called_once_with(rt)
        hass.auth.async_remove_user.assert_awaited_once_with(user)

    async def test_switching_tokens_does_not_orphan_the_old_account(self) -> None:
        old_account = _user("u1")
        hass = _hass(
            users={"u1": old_account},
            refresh_tokens={"rt1": _rt(user=old_account)},
            validated=_rt("rt2", user=_user("owner")),
        )
        switched = sc.adopt_admin_token(
            hass, {DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}, "tok"
        )

        await sc.async_release_credentials(hass, switched)

        hass.auth.async_remove_user.assert_awaited_once_with(old_account)

    async def test_a_handed_over_account_survives_its_tokens_revocation(self) -> None:
        # Adopt a token of the provisioned account, then revoke it before
        # removing the entry: ownership was recorded at adoption, not inferred
        # from a token that no longer validates.
        account = _user("u1")
        hass = _hass(
            users={"u1": account},
            refresh_tokens={"rt1": _rt(user=account)},
            validated=_rt("rt2", user=account),
        )
        switched = sc.adopt_admin_token(
            hass, {DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}, "tok"
        )
        hass.auth.async_validate_access_token.return_value = None

        await sc.async_release_credentials(hass, switched)

        hass.auth.async_remove_user.assert_not_awaited()

    async def test_the_account_behind_the_supplied_token_is_never_removed(
        self,
    ) -> None:
        # An administrator may have given the old account a login and taken a
        # token from it; that account is theirs now.
        account = _user("u1")
        hass = _hass(users={"u1": account}, validated=_rt("rt2", user=account))

        await sc.async_release_credentials(
            hass, {DATA_SERVER_USER_ID: "u1", DATA_ADMIN_TOKEN: "tok"}
        )

        hass.auth.async_remove_user.assert_not_awaited()
