"""The Home Assistant credential the in-process server runs with (#2427).

New setups use an administrator's long-lived access token: one the user
pastes in, or one issued here from the login of a local-only administrator
account the user made for the server. Installs from before that keep the local
administrator and token an older release created, for as long as both still
work. Nothing here creates an account: a missing or revoked credential stops
the server and the user is asked for a replacement.

Accounts are never removed on the user's behalf, except the one an older
release created itself, and only when its config entry is deleted.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from .const import (
    DATA_ACCESS_TOKEN,
    DATA_ADMIN_TOKEN,
    DATA_ISSUED_TOKEN_ID,
    DATA_REFRESH_TOKEN_ID,
    DATA_SERVER_USER_ID,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

# entry.data keys of the credential an older release provisioned.

# Why a token cannot run the server: the setup / repair error translation keys.
TokenProblem = Literal["invalid_token", "token_not_long_lived", "token_not_admin"]
# Why a submitted token or login was refused: the form error translation keys.
CredentialProblem = TokenProblem | Literal["invalid_login", "credentials_needed"]

# The login fields of the setup, Configure and token-repair forms; never stored.
ADMIN_USERNAME = "admin_username"
ADMIN_PASSWORD = "admin_password"

_ISSUED_TOKEN_NAME = "HA-MCP Server"
# The lifespan the profile page gives a token made there.
_ISSUED_TOKEN_LIFESPAN = timedelta(days=3650)


class _CredentialError(Exception):
    """A credential problem; ``reason`` is its translation key."""

    def __init__(self, reason: str) -> None:
        """Keep the reason the credential was refused."""
        super().__init__(reason)
        self.reason = reason


class CredentialRefused(_CredentialError):
    """A submitted token or login cannot run the server (a CredentialProblem)."""


class CredentialNeeded(_CredentialError):
    """The server has no usable credential.

    ``reason`` is a :data:`TokenProblem`, or ``"missing_token"`` when an older
    release's provisioned credential is gone.
    """


def token_problem(hass: HomeAssistant, token: str) -> TokenProblem | None:
    """Why ``token`` cannot run the server, or None when it can.

    It must be a long-lived token, since a session token expires within the
    hour, and it must belong to an active administrator, because the server
    configures Home Assistant on the user's behalf.
    """
    from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN

    refresh_token = hass.auth.async_validate_access_token(token)
    if refresh_token is None:
        # Also a deactivated account's token: deactivating removes its tokens.
        return "invalid_token"
    if refresh_token.token_type != TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
        return "token_not_long_lived"
    if not refresh_token.user.is_admin:
        return "token_not_admin"
    return None


async def async_server_access_token(hass: HomeAssistant, entry: Any) -> str:
    """The access token to start the server with; raises CredentialNeeded."""
    token = entry.data.get(DATA_ADMIN_TOKEN)
    if token:
        problem = token_problem(hass, str(token))
        if problem is not None:
            raise CredentialNeeded(problem)
        return str(token)

    user_id = entry.data.get(DATA_SERVER_USER_ID)
    rt_id = entry.data.get(DATA_REFRESH_TOKEN_ID)
    user = await hass.auth.async_get_user(user_id) if user_id else None
    refresh_token = hass.auth.async_get_refresh_token(rt_id) if rt_id else None
    if (
        user is None
        or not user.is_active
        or refresh_token is None
        or refresh_token.user.id != user.id
    ):
        raise CredentialNeeded("missing_token")
    if not user.is_admin:
        # An administrator can demote the account an older release created.
        raise CredentialNeeded("token_not_admin")
    return str(hass.auth.async_create_access_token(refresh_token))


async def async_credential_from_input(
    hass: HomeAssistant, *, token: str = "", username: str = "", password: str = ""
) -> tuple[str, str | None]:
    """The token to run the server with, and its refresh-token id if issued here.

    A pasted token wins. Otherwise the login must belong to a local-only
    administrator account, the kind the forms ask the user to make for the
    server; Home Assistant checks its password and a long-lived token is issued
    for it, so nobody has to log in as it. Its token works because the server
    reaches Home Assistant over loopback. Raises CredentialRefused.
    """
    if token:
        if (problem := token_problem(hass, token)) is not None:
            raise CredentialRefused(problem)
        return token, None
    if not (username and password):
        raise CredentialRefused("credentials_needed")
    from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    from homeassistant.auth.providers.homeassistant import InvalidAuth

    provider = next(
        (p for p in hass.auth.auth_providers if p.type == "homeassistant"), None
    )
    if provider is None:
        raise CredentialRefused("invalid_login")
    credentials = await provider.async_get_or_create_credentials({"username": username})
    user = await hass.auth.async_get_user_by_credentials(credentials)
    # Any other account is refused before its password is checked, with the
    # same error, so these forms never test another account's password and
    # never issue a token that works from outside the local network.
    if user is None or not (user.is_active and user.is_admin and user.local_only):
        raise CredentialRefused("invalid_login")
    try:
        await provider.async_validate_login(username, password)
    except InvalidAuth:
        raise CredentialRefused("invalid_login") from None
    # Home Assistant allows one long-lived token per name and account.
    taken = {rt.client_name for rt in user.refresh_tokens.values()}
    name, n = _ISSUED_TOKEN_NAME, 1
    while name in taken:
        n += 1
        name = f"{_ISSUED_TOKEN_NAME} {n}"
    refresh_token = await hass.auth.async_create_refresh_token(
        user,
        client_name=name,
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=_ISSUED_TOKEN_LIFESPAN,
    )
    return hass.auth.async_create_access_token(refresh_token), refresh_token.id


def credential_fields(
    user_input: Mapping[str, Any], token_field: str
) -> dict[str, str]:
    """The token and login a form submitted, as keyword arguments."""
    return {
        "token": str(user_input.get(token_field) or "").strip(),
        "username": str(user_input.get(ADMIN_USERNAME) or "").strip(),
        "password": str(user_input.get(ADMIN_PASSWORD) or ""),
    }


def credential_schema(
    vol: Any, token_field: str, values: Mapping[str, Any]
) -> dict[Any, Any]:
    """Form fields: a pasted token, or the login of an account made for HA-MCP.

    ``vol`` is the caller's voluptuous, the one its own schema is built with.
    The login fields never autofill: a browser would offer the administrator's
    own saved login, which does not belong there.
    """
    from homeassistant.helpers.selector import (
        TextSelector,
        TextSelectorConfig,
        TextSelectorType,
    )

    return {
        vol.Optional(token_field): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(
            ADMIN_USERNAME,
            description={"suggested_value": values.get(ADMIN_USERNAME)},
        ): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="off")
        ),
        vol.Optional(ADMIN_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="new-password"
            )
        ),
    }


def error_field(fields: Mapping[str, str], token_field: str) -> str:
    """Where a refusal shows: on the token field for a token, else the form."""
    return token_field if fields["token"] else "base"


def _revoke(hass: HomeAssistant, rt_id: Any, keep: Any = None) -> None:
    refresh_token = hass.auth.async_get_refresh_token(rt_id) if rt_id else None
    if refresh_token is not None and (keep is None or keep.id != refresh_token.id):
        hass.auth.async_remove_refresh_token(refresh_token)


def adopt_admin_token(
    hass: HomeAssistant,
    data: Mapping[str, Any],
    token: str,
    issued_id: str | None = None,
) -> dict[str, Any]:
    """Return ``data`` switched to ``token``.

    The token an older release provisioned is revoked so no unused
    administrator token is left behind, unless it is the one ``token`` was
    minted from. Its account stays, and so does its id, so removing the entry
    still deletes it, unless ``token`` belongs to that very account: an
    administrator who took a token from it has made it their own, so its id is
    dropped here, while the token is known to be valid, rather than inferred at
    removal time from a token that may be revoked by then. A token issued here
    from a login is revoked on a switch too; ``issued_id`` marks the new token
    when it was issued here.
    """
    # Identify the replacement before revoking anything: it may be backed by
    # the very refresh token an older release provisioned.
    supplied = hass.auth.async_validate_access_token(token)
    owner_id = supplied.user.id if supplied is not None else None
    _revoke(hass, data.get(DATA_REFRESH_TOKEN_ID), keep=supplied)
    _revoke(hass, data.get(DATA_ISSUED_TOKEN_ID), keep=supplied)
    new = {
        k: v
        for k, v in data.items()
        if k not in (DATA_REFRESH_TOKEN_ID, DATA_ACCESS_TOKEN, DATA_ISSUED_TOKEN_ID)
        and not (k == DATA_SERVER_USER_ID and v == owner_id)
    }
    new[DATA_ADMIN_TOKEN] = token
    if issued_id:
        new[DATA_ISSUED_TOKEN_ID] = issued_id
    return new


def _token_owner_id(hass: HomeAssistant, token: Any) -> str | None:
    if not token:
        return None
    refresh_token = hass.auth.async_validate_access_token(str(token))
    return refresh_token.user.id if refresh_token is not None else None


async def async_release_credentials(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> None:
    """Drop the credential when the entry is deleted.

    A pasted token belongs to the user, who revokes it in their profile; one
    issued here from a login is revoked. Only the account an older release
    created for itself is removed, and not even that one once the supplied
    token belongs to it.
    """
    _revoke(hass, data.get(DATA_REFRESH_TOKEN_ID))
    _revoke(hass, data.get(DATA_ISSUED_TOKEN_ID))
    user_id = data.get(DATA_SERVER_USER_ID)
    if (
        user_id
        and user_id != _token_owner_id(hass, data.get(DATA_ADMIN_TOKEN))
        and (user := await hass.auth.async_get_user(user_id))
    ):
        await hass.auth.async_remove_user(user)
