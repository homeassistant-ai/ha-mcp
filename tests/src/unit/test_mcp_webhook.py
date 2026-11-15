"""Webhook auth gate and OAuth discovery documents of the component's own webhook.

Who the webhook lets through in ``ha_auth`` and ``legacy`` mode (#1527), and the
RFC 8414 / RFC 9728 discovery documents it serves (#1527, #1969). The webhook is
registered on a real ``hass``; every request goes through Core's HTTP server.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.auth.models import TOKEN_TYPE_SYSTEM, User
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockUser
from pytest_homeassistant.typing import ClientSessionGenerator

import custom_components.ha_mcp_tools.mcp_webhook as mw
from custom_components.ha_mcp_tools.const import (
    OAUTH_BASE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)

from ._mcp_webhook_core import (
    PUBLIC_HEADERS,
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

AS_URL = f"{OAUTH_BASE}/authorization-server"
AS_URLS = [
    AS_URL,
    f"/.well-known/oauth-authorization-server{OAUTH_BASE}",
    f"/.well-known/openid-configuration{OAUTH_BASE}",
    f"{OAUTH_BASE}/.well-known/openid-configuration",
    f"{OAUTH_BASE}/.well-known/oauth-authorization-server",
]
PUBLIC = "https://abc.ui.nabu.casa"
AUTH_MODES = [WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY, WEBHOOK_AUTH_NONE]


def _prm_url(webhook_id: str) -> str:
    return f"/.well-known/oauth-protected-resource/api/webhook/{webhook_id}"


async def _document(client: Any, url: str = AS_URL) -> dict[str, Any]:
    resp = await client.get(url, headers=PUBLIC_HEADERS)
    assert resp.status == 200
    document: dict[str, Any] = await resp.json()
    return document


async def _access_token(
    hass: HomeAssistant, user: User, client_id: str | None, token_type: str | None
) -> str:
    refresh = await hass.auth.async_create_refresh_token(
        user, client_id, token_type=token_type
    )
    return hass.auth.async_create_access_token(refresh)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- auth gate ------------------------------------------------------------


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


# -- discovery documents --------------------------------------------------


async def test_document_urls_follow_the_forwarded_public_host(
    register: Register, hass_client_no_auth: ClientSessionGenerator
) -> None:
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()

    resp = await client.get(
        AS_URL,
        headers={
            "Host": "internal:8123",
            "X-Forwarded-Host": "abc.ui.nabu.casa",
            "X-Forwarded-Proto": "https",
        },
    )

    assert (await resp.json())["issuer"] == f"{PUBLIC}{OAUTH_BASE}"


async def test_document_urls_fall_back_to_the_host_the_client_called(
    register: Register, hass_client_no_auth: ClientSessionGenerator
) -> None:
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()

    resp = await client.get(AS_URL)

    assert (await resp.json())["issuer"] == (
        f"http://{client.host}:{client.port}{OAUTH_BASE}"
    )


async def test_ha_auth_document_advertises_what_claude_selects_a_client_by(
    register: Register, hass_client_no_auth: ClientSessionGenerator
) -> None:
    await register(WEBHOOK_AUTH_HA)
    doc = await _document(await hass_client_no_auth())

    assert doc["issuer"] == f"{PUBLIC}{OAUTH_BASE}"
    assert doc["authorization_endpoint"] == f"{PUBLIC}{OAUTH_BASE}/authorize"
    assert doc["token_endpoint"] == f"{PUBLIC}{OAUTH_BASE}/token"
    assert doc["registration_endpoint"] == f"{PUBLIC}{OAUTH_BASE}/register"
    assert doc["response_types_supported"] == ["code"]
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert doc["client_id_metadata_document_supported"] is True


@pytest.mark.parametrize("url", AS_URLS)
async def test_every_location_a_client_probes_serves_the_document(
    register: Register, hass_client_no_auth: ClientSessionGenerator, url: str
) -> None:
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()

    doc = await _document(client, url)

    assert doc == await _document(client, AS_URL)


async def test_legacy_document_advertises_client_secret_auth(
    register: Register, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """Reverting the mode dispatch to always serve the ha_auth document would
    leave a client that needs a pasted credential with no way to sign in."""
    await register(WEBHOOK_AUTH_LEGACY)
    doc = await _document(await hass_client_no_auth())

    assert doc["authorization_endpoint"] == f"{PUBLIC}{OAUTH_BASE}/authorize"
    assert doc["token_endpoint"] == f"{PUBLIC}{OAUTH_BASE}/token"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert set(doc["token_endpoint_auth_methods_supported"]) == {
        "client_secret_basic",
        "client_secret_post",
    }
    assert doc["grant_types_supported"] == ["authorization_code", "refresh_token"]


async def test_none_mode_document_fixes_what_home_assistants_root_document_lacks(
    register: Register, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """#1969: claude.ai's discovery otherwise lands on Home Assistant's root
    document, which omits the ``none`` auth method and a registration endpoint."""
    await register(WEBHOOK_AUTH_NONE)
    doc = await _document(await hass_client_no_auth())

    assert doc["issuer"] == f"{PUBLIC}{OAUTH_BASE}"
    assert doc["authorization_endpoint"] == f"{PUBLIC}{OAUTH_BASE}/authorize"
    assert doc["token_endpoint"] == f"{PUBLIC}{OAUTH_BASE}/token"
    assert doc["registration_endpoint"] == f"{PUBLIC}{OAUTH_BASE}/register"
    assert doc["response_types_supported"] == ["code"]
    assert doc["grant_types_supported"] == ["authorization_code"]
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert doc["client_id_metadata_document_supported"] is True


@pytest.mark.parametrize("mode", AUTH_MODES)
async def test_active_auth_mode_names_the_registered_mode(
    hass: HomeAssistant, register: Register, mode: str
) -> None:
    assert mw.active_auth_mode(hass) is None

    await register(mode)

    assert mw.active_auth_mode(hass) == mode


@pytest.mark.parametrize("mode", AUTH_MODES)
async def test_documents_stop_when_the_entry_is_unloaded(
    hass: HomeAssistant,
    register: Register,
    hass_client_no_auth: ClientSessionGenerator,
    mode: str,
) -> None:
    """The views stay bound until Home Assistant restarts, so they must answer
    404 rather than advertise a webhook that no longer exists."""
    await register(mode)
    client = await hass_client_no_auth()
    assert (await client.get(AS_URL, headers=PUBLIC_HEADERS)).status == 200

    await mw.async_unregister_webhook(hass)

    assert mw.active_auth_mode(hass) is None
    assert (await client.get(AS_URL, headers=PUBLIC_HEADERS)).status == 404
    assert (await client.get(_prm_url(WEBHOOK_ID))).status == 404


@pytest.mark.parametrize("mode", [WEBHOOK_AUTH_HA, WEBHOOK_AUTH_NONE])
async def test_documents_stay_off_while_remote_access_is_disabled(
    hass: HomeAssistant,
    register: Register,
    hass_client_no_auth: ClientSessionGenerator,
    mode: str,
) -> None:
    """Local-only mode has no webhook, so nothing may advertise one."""
    await register(mode)
    client = await hass_client_no_auth()
    assert (await client.get(AS_URL, headers=PUBLIC_HEADERS)).status == 200
    await mw.async_unregister_webhook(hass)

    await register(mode, register_endpoint=False)

    assert mw.active_auth_mode(hass) is None
    assert (await client.get(AS_URL, headers=PUBLIC_HEADERS)).status == 404
    assert (await client.get(_prm_url(WEBHOOK_ID))).status == 404


async def test_resource_document_follows_a_recreated_entry_to_its_new_id(
    hass: HomeAssistant, register: Register, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """Removing and re-adding the entry mints a new webhook id in the same Home
    Assistant session: the already-bound view must serve the new id and stop
    answering the old one, so whoever held it never learns the new one."""
    await register(WEBHOOK_AUTH_HA)
    client = await hass_client_no_auth()
    first = await client.get(_prm_url(WEBHOOK_ID), headers=PUBLIC_HEADERS)
    assert first.status == 200
    await mw.async_unregister_webhook(hass)

    await register(WEBHOOK_AUTH_HA, webhook_id="mcp_second_entry_id")

    new = await client.get(_prm_url("mcp_second_entry_id"), headers=PUBLIC_HEADERS)
    assert new.status == 200
    assert (await new.json())["resource"] == f"{PUBLIC}/api/webhook/mcp_second_entry_id"
    assert (
        await client.get(_prm_url(WEBHOOK_ID), headers=PUBLIC_HEADERS)
    ).status == 404


async def test_switching_modes_serves_each_mode_document_without_rebinding(
    hass: HomeAssistant, register: Register, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """aiohttp cannot rebind a view: a none -> ha_auth -> legacy -> none cycle in
    one Home Assistant session must reuse the bound views, and each document
    must be the one for the mode that is live now."""
    client = await hass_client_no_auth()
    for mode in (
        WEBHOOK_AUTH_NONE,
        WEBHOOK_AUTH_HA,
        WEBHOOK_AUTH_LEGACY,
        WEBHOOK_AUTH_NONE,
        WEBHOOK_AUTH_HA,
    ):
        await register(mode)
        doc = await _document(client)
        assert ("refresh_token" in doc["grant_types_supported"]) == (
            mode != WEBHOOK_AUTH_NONE
        )
        assert (
            "client_secret_basic" in doc["token_endpoint_auth_methods_supported"]
        ) == (mode == WEBHOOK_AUTH_LEGACY)
        await mw.async_unregister_webhook(hass)


async def test_no_fixed_path_document_reveals_the_webhook_id_in_any_mode(
    hass: HomeAssistant, register: Register, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """SECURITY (#1976): a document reachable without already holding the id must
    never carry it; a fixed-path protected-resource document once did, and a
    later switch back to none mode, where the id is the only credential, turned
    that into a credential disclosure."""
    client = await hass_client_no_auth()
    fixed_urls = [
        *AS_URLS,
        "/.well-known/oauth-protected-resource",
        f"/.well-known/oauth-protected-resource{OAUTH_BASE}",
        f"{OAUTH_BASE}/.well-known/oauth-protected-resource",
    ]
    for mode in AUTH_MODES:
        await register(mode)
        assert mw.active_auth_mode(hass) == mode
        assert (await client.get(AS_URL, headers=PUBLIC_HEADERS)).status == 200
        for url in fixed_urls:
            resp = await client.get(url, headers=PUBLIC_HEADERS)
            assert WEBHOOK_ID not in await resp.text(), url
        await mw.async_unregister_webhook(hass)
