"""The Home Assistant credential the in-process server runs with (#2427).

New setups use a long-lived access token an administrator creates and pastes
in. Installs from before that keep the local administrator and token an older
release created, for as long as both still work. Nothing here creates an
account or a token: a missing or revoked credential stops the server and the
user is asked for a replacement token.

Accounts are never removed on the user's behalf, except the one an older
release created itself, and only when its config entry is deleted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import (
    DATA_ACCESS_TOKEN,
    DATA_ADMIN_TOKEN,
    DATA_REFRESH_TOKEN_ID,
    DATA_SERVER_USER_ID,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

# entry.data keys of the credential an older release provisioned.
_PROVISIONED_KEYS = (DATA_SERVER_USER_ID, DATA_REFRESH_TOKEN_ID, DATA_ACCESS_TOKEN)


class CredentialNeeded(Exception):
    """The server has no usable credential; ``reason`` is a translation key."""

    def __init__(self, reason: str) -> None:
        """Keep the reason the credential was refused."""
        super().__init__(reason)
        self.reason = reason


def token_problem(hass: HomeAssistant, token: str) -> str | None:
    """Why ``token`` cannot run the server, or None when it can.

    It must be a long-lived token, since a session token expires within the
    hour, and it must belong to an active administrator, because the server
    configures Home Assistant on the user's behalf.
    """
    from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN

    refresh_token = hass.auth.async_validate_access_token(token)
    if refresh_token is None:
        return "invalid_token"
    if refresh_token.token_type != TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
        return "token_not_long_lived"
    user = refresh_token.user
    if not (user.is_active and user.is_admin):
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
    return str(hass.auth.async_create_access_token(refresh_token))


def adopt_admin_token(
    hass: HomeAssistant, data: Mapping[str, Any], token: str
) -> dict[str, Any]:
    """Return ``data`` switched to ``token``.

    The token an older release provisioned is revoked so no unused
    administrator token is left behind. Its account stays, and so does its id,
    so removing the entry still deletes it.
    """
    rt_id = data.get(DATA_REFRESH_TOKEN_ID)
    refresh_token = hass.auth.async_get_refresh_token(rt_id) if rt_id else None
    if refresh_token is not None:
        hass.auth.async_remove_refresh_token(refresh_token)
    new = {
        k: v
        for k, v in data.items()
        if k not in (DATA_REFRESH_TOKEN_ID, DATA_ACCESS_TOKEN)
    }
    new[DATA_ADMIN_TOKEN] = token
    return new


def _token_owner_id(hass: HomeAssistant, token: Any) -> str | None:
    if not token:
        return None
    refresh_token = hass.auth.async_validate_access_token(str(token))
    return refresh_token.user.id if refresh_token is not None else None


async def async_release_credentials(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> dict[str, Any]:
    """Drop the credential when the entry is deleted; return the rest of ``data``.

    A supplied token belongs to the user, who revokes it in their profile.
    Only the account an older release created for itself is removed, and not
    even that one once the supplied token belongs to it.
    """
    rt_id = data.get(DATA_REFRESH_TOKEN_ID)
    if rt_id and (refresh_token := hass.auth.async_get_refresh_token(rt_id)):
        hass.auth.async_remove_refresh_token(refresh_token)
    user_id = data.get(DATA_SERVER_USER_ID)
    if (
        user_id
        and user_id != _token_owner_id(hass, data.get(DATA_ADMIN_TOKEN))
        and (user := await hass.auth.async_get_user(user_id))
    ):
        await hass.auth.async_remove_user(user)
    return {
        k: v for k, v in data.items() if k not in (*_PROVISIONED_KEYS, DATA_ADMIN_TOKEN)
    }
