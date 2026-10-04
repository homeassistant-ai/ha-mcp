"""How the in-process server gets its Home Assistant credential (#2427).

New setups use an administrator's long-lived access token. Existing installs
keep the user and token an older component created, while they still work.
Nothing creates an administrator or a refresh token on its own any more.
"""

from __future__ import annotations

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
    DATA_REFRESH_TOKEN_ID,
    DATA_SERVER_USER_ID,
)


def _user(uid: str = "u1", *, admin: bool = True, active: bool = True):
    return SimpleNamespace(id=uid, is_admin=admin, is_active=active)


def _rt(rt_id: str = "rt1", *, user=None, token_type: str = LLAT):
    return SimpleNamespace(id=rt_id, user=user or _user(), token_type=token_type)


def _hass(*, validated=None, users=None, refresh_tokens=None) -> MagicMock:
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

    @pytest.mark.parametrize(
        "user", [_user(admin=False), _user(active=False)], ids=["non_admin", "inactive"]
    )
    def test_a_token_without_administrator_rights_is_refused(self, user) -> None:
        validated = _rt(user=user)
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
        self, users, refresh_tokens
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

    def test_switching_never_removes_an_account(self) -> None:
        hass = _hass(refresh_tokens={"rt1": _rt()})
        sc.adopt_admin_token(
            hass, {DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}, "tok"
        )
        hass.auth.async_remove_user.assert_not_awaited()


class TestReleaseOnRemove:
    async def test_a_supplied_token_and_its_account_are_left_alone(self) -> None:
        hass = _hass()
        data = {DATA_ADMIN_TOKEN: "user-token", "webhook_id": "w"}

        remaining = await sc.async_release_credentials(hass, data)

        assert remaining == {"webhook_id": "w"}
        hass.auth.async_remove_user.assert_not_awaited()
        hass.auth.async_remove_refresh_token.assert_not_called()

    async def test_the_account_an_older_release_created_is_removed(self) -> None:
        user = _user()
        rt = _rt(user=user)
        hass = _hass(users={"u1": user}, refresh_tokens={"rt1": rt})

        remaining = await sc.async_release_credentials(
            hass, {DATA_SERVER_USER_ID: "u1", DATA_REFRESH_TOKEN_ID: "rt1"}
        )

        assert remaining == {}
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
