"""Who the webhook lets through in ``ha_auth`` and ``legacy`` mode (#1527).

The component's own webhook is registered on a real ``hass`` with real users
and tokens; every request goes through Core's HTTP server.
"""

from __future__ import annotations

import pytest
from homeassistant.auth.models import TOKEN_TYPE_SYSTEM, User
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockUser
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.ha_mcp_tools.const import (
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)

from ._mcp_webhook_core import (
    READONLY_PATH,
    SECRET_PATH,
    WEBHOOK_ID,
    WEBHOOK_PATH,
    Register,
    Upstream,
    legacy_provider,
)
from ._mcp_webhook_core import (
    register as register,
)
from ._mcp_webhook_core import (
    upstream as upstream,
)

CLIENT_ID = "https://example.com/app"


async def _access_token(
    hass: HomeAssistant, user: User, client_id: str | None, token_type: str | None
) -> str:
    refresh = await hass.auth.async_create_refresh_token(
        user, client_id, token_type=token_type
    )
    return hass.auth.async_create_access_token(refresh)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_ha_auth_admin_bearer_is_relayed(
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    hass_access_token: str,
) -> None:
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()

    resp = await client.post(
        WEBHOOK_PATH, data=b"{}", headers=_bearer(hass_access_token)
    )

    assert resp.status == 200
    assert [seen.path for seen in upstream.requests] == [SECRET_PATH]


async def test_ha_auth_challenge_points_the_client_at_the_discovery_document(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()

    resp = await client.post(
        WEBHOOK_PATH,
        data=b"{}",
        headers={"Host": "example.nabu.casa", "X-Forwarded-Proto": "https"},
    )

    assert resp.status == 401
    www = resp.headers["WWW-Authenticate"]
    assert www.startswith("Bearer realm=")
    assert 'realm="HA-MCP"' in www
    assert (
        'resource_metadata="https://example.nabu.casa'
        f'/.well-known/oauth-protected-resource/api/webhook/{WEBHOOK_ID}"' in www
    )
    assert upstream.requests == []


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer nope",
        "Bearer    ",
        "Basic abc",
    ],
    ids=["invalid-token", "empty-token", "not-a-bearer-scheme"],
)
async def test_ha_auth_rejects_a_bad_authorization_header(
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    authorization: str,
) -> None:
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()

    resp = await client.post(
        WEBHOOK_PATH, data=b"{}", headers={"Authorization": authorization}
    )

    assert resp.status == 401
    assert upstream.requests == []


async def test_ha_auth_rejects_a_valid_token_sent_under_another_scheme(
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    hass_access_token: str,
) -> None:
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()

    resp = await client.post(
        WEBHOOK_PATH,
        data=b"{}",
        headers={"Authorization": f"Basic {hass_access_token}"},
    )

    assert resp.status == 401
    assert upstream.requests == []


async def test_ha_auth_rejects_a_household_login_that_is_not_an_administrator(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    hass_read_only_user: MockUser,
) -> None:
    """The server acts with its own administrator token, so accepting any valid
    login would hand every household member admin-equivalent control."""
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()
    token = await _access_token(hass, hass_read_only_user, CLIENT_ID, None)

    resp = await client.post(WEBHOOK_PATH, data=b"{}", headers=_bearer(token))

    assert resp.status == 401
    assert upstream.requests == []


async def test_ha_auth_rejects_a_deactivated_administrator(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    hass_admin_user: MockUser,
    hass_access_token: str,
) -> None:
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()
    await hass.auth.async_update_user(hass_admin_user, is_active=False)

    resp = await client.post(
        WEBHOOK_PATH, data=b"{}", headers=_bearer(hass_access_token)
    )

    assert resp.status == 401
    assert upstream.requests == []


async def test_ha_auth_rejects_a_system_generated_user(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()
    system_user = await hass.auth.async_create_system_user(
        "service", group_ids=["system-admin"]
    )
    token = await _access_token(hass, system_user, None, TOKEN_TYPE_SYSTEM)

    resp = await client.post(WEBHOOK_PATH, data=b"{}", headers=_bearer(token))

    assert resp.status == 401
    assert upstream.requests == []


async def test_legacy_bearer_issued_by_the_provider_is_relayed(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    await register(WEBHOOK_AUTH_LEGACY)
    client = await hass_client_no_auth()

    resp = await client.post(
        WEBHOOK_PATH,
        data=b"{}",
        headers=_bearer(legacy_provider().issue_access_token()),
    )

    assert resp.status == 200
    assert len(upstream.requests) == 1


async def test_legacy_missing_bearer_gets_a_401_challenge(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    await register(WEBHOOK_AUTH_LEGACY)
    client = await hass_client_no_auth()

    resp = await client.post(
        WEBHOOK_PATH, data=b"{}", headers={"Host": "example.nabu.casa"}
    )

    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"].startswith("Bearer realm=")
    assert upstream.requests == []


async def test_legacy_rejects_a_forged_token(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    await register(WEBHOOK_AUTH_LEGACY)
    client = await hass_client_no_auth()

    resp = await client.post(WEBHOOK_PATH, data=b"{}", headers=_bearer("forged.token"))

    assert resp.status == 401
    assert upstream.requests == []


async def test_legacy_refresh_token_is_not_an_access_bearer(
    register: Register, upstream: Upstream, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """A refresh token left in a client's bearer slot must not unlock the
    webhook."""
    await register(WEBHOOK_AUTH_LEGACY)
    client = await hass_client_no_auth()

    resp = await client.post(
        WEBHOOK_PATH,
        data=b"{}",
        headers=_bearer(legacy_provider().issue_refresh_token()),
    )

    assert resp.status == 401
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("auth_mode", "authorized"),
    [
        (WEBHOOK_AUTH_NONE, True),
        (WEBHOOK_AUTH_HA, False),
        (WEBHOOK_AUTH_LEGACY, False),
    ],
)
async def test_readonly_alias_is_gated_like_the_webhook(
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    auth_mode: str,
    authorized: bool,
) -> None:
    """The ``/readonly`` alias must not be a way around the auth gate."""
    await register(auth_mode)
    client = await hass_client_no_auth()

    resp = await client.post(READONLY_PATH, data=b"{}")

    assert (resp.status == 200) is authorized
    assert [seen.path for seen in upstream.requests] == (
        [f"{SECRET_PATH}/readonly"] if authorized else []
    )


async def test_readonly_alias_relays_a_valid_bearer(
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    hass_access_token: str,
) -> None:
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()

    resp = await client.post(
        READONLY_PATH, data=b"{}", headers=_bearer(hass_access_token)
    )

    assert resp.status == 200
    assert [seen.path for seen in upstream.requests] == [f"{SECRET_PATH}/readonly"]
