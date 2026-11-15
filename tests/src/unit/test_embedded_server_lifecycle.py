"""What a start refuses before it touches anything, and the credential's lifetime (#2427).

A start that cannot proceed must fail before the package step: an install on a
Home Assistant the server cannot run on, or a credential created on the way,
would change the system for nothing. Removing the entry releases only what an
older release created for itself.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry, MockUser

from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import (
    CONF_ENTRY_TYPE,
    DATA_ADMIN_TOKEN,
    DATA_REFRESH_TOKEN_ID,
    DATA_SERVER_USER_ID,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    MIN_EMBEDDED_HOME_ASSISTANT_VERSION,
)

from ._embedded_server_core import (
    INDEX_VERSION,
    STABLE,
    PackageIndex,
    issue_admin_token,
    new_manager,
    pin,
)
from ._embedded_server_core import embedded_server_env as embedded_server_env
from ._embedded_server_core import package_index as package_index

pytestmark = pytest.mark.usefixtures("embedded_server_env")


async def test_a_start_without_a_secret_path_fails_before_anything_else(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER})
    entry.add_to_hass(hass)

    with pytest.raises(es.EmbeddedServerError, match="secret path missing"):
        await es.EmbeddedServerManager(hass, entry).async_start()

    assert package_index.calls == []


@pytest.mark.parametrize(
    ("version", "message"),
    [
        (
            "2025.9.4",
            rf"requires Home Assistant {re.escape(MIN_EMBEDDED_HOME_ASSISTANT_VERSION)} or newer",
        ),
        ("custom-build", "could not determine whether Home Assistant custom-build"),
    ],
    ids=["too old", "not a version"],
)
async def test_an_unsupported_home_assistant_is_refused_before_the_install(
    hass: HomeAssistant,
    package_index: PackageIndex,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    message: str,
) -> None:
    monkeypatch.setattr(es, "HA_VERSION", version)
    pin(hass, f"{STABLE}=={INDEX_VERSION}")
    mgr, _entry = await new_manager(hass)

    with pytest.raises(es.EmbeddedServerError, match=message) as err:
        await mgr.async_start()

    # Not "package": that repair offers a reinstall, which cannot fix this.
    assert err.value.kind == "start"
    assert package_index.calls == []
    package_index.requirements.assert_not_awaited()


async def test_a_missing_credential_asks_for_one_and_creates_no_account(
    hass: HomeAssistant, package_index: PackageIndex
) -> None:
    """Older releases created an administrator here; none is created now."""
    pin(hass, f"{STABLE}=={INDEX_VERSION}")
    package_index.dists[STABLE] = INDEX_VERSION
    users = len(await hass.auth.async_get_users())
    mgr, _entry = await new_manager(hass)

    with pytest.raises(es.EmbeddedServerError) as err:
        await mgr.async_start()

    assert err.value.kind == "token"
    assert len(await hass.auth.async_get_users()) == users


async def test_removing_the_entry_deletes_the_account_an_older_release_created(
    hass: HomeAssistant, hass_owner_user: MockUser
) -> None:
    user = await hass.auth.async_create_user(
        "HA-MCP server", group_ids=[GROUP_ID_ADMIN]
    )
    refresh_token = await hass.auth.async_create_refresh_token(
        user,
        client_name="HA-MCP",
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=365),
    )
    mgr, entry = await new_manager(
        hass,
        data={DATA_SERVER_USER_ID: user.id, DATA_REFRESH_TOKEN_ID: refresh_token.id},
    )
    stored = dict(entry.data)

    await mgr.async_revoke_credentials()

    assert await hass.auth.async_get_user(user.id) is None
    assert hass.auth.async_get_refresh_token(refresh_token.id) is None
    # Home Assistant removed the entry first; writing to it would raise.
    assert dict(entry.data) == stored


async def test_removing_the_entry_leaves_a_pasted_token_valid(
    hass: HomeAssistant,
) -> None:
    """A token the user pasted belongs to them, who revokes it in their profile."""
    token = await issue_admin_token(hass)
    mgr, _entry = await new_manager(hass, data={DATA_ADMIN_TOKEN: token})

    await mgr.async_revoke_credentials()

    assert hass.auth.async_validate_access_token(token) is not None
