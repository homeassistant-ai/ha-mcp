"""The ha_auth webhook gate: bearers are Home Assistant access tokens.

Tokens come from Core's own auth (a real user, refresh token and access
token), are sent over Home Assistant's HTTP server to the webhook, and the
relayed call lands on the ``upstream`` stand-in for the ha-mcp server app.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._auth_modes import (
    HA_AUTH,
    WEBHOOK_ID,
    prm_url,
    proxy_config,
    submodule,
    token_core_cannot_validate,
)
from ._proxy import Upstream, start_proxy, state_path, text_of, wait_until
from ._variants import Variant

WEBHOOK = f"/api/webhook/{WEBHOOK_ID}"


async def _start_ha_auth(
    hass: HomeAssistant,
    fs_root: Path,
    variant: Variant,
    upstream: Upstream,
    *,
    debug_logging: bool = False,
) -> None:
    config = proxy_config(target_url=upstream.target_url(), oauth=HA_AUTH)
    if debug_logging:
        config["debug_logging"] = True
    await start_proxy(hass, variant, fs_root, config)


@pytest.mark.parametrize("scheme", ["Bearer", "bEaReR"])
async def test_a_home_assistant_access_token_authenticates_without_reaching_upstream(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    hass_access_token: str,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    scheme: str,
) -> None:
    """A valid Home Assistant access token authenticates the request, with the
    auth scheme matched case-insensitively (RFC 7235); the token itself never
    reaches the MCP server."""
    await _start_ha_auth(hass, fs_root, variant, upstream)
    client = await hass_client_no_auth()

    response = await client.post(
        WEBHOOK,
        data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        headers={"Authorization": f"{scheme} {hass_access_token}"},
    )

    assert response.status == 200
    assert len(upstream.requests) == 1
    assert "authorization" not in {key.lower() for key in upstream.requests[0].headers}


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param("Bearer not-a-home-assistant-token", id="rejected-token"),
        # Core's own auth middleware validates a canonical "Bearer" header
        # first and would answer 500 itself; any other casing reaches the gate.
        pytest.param(f"bearer {token_core_cannot_validate()}", id="validator-raises"),
    ],
)
async def test_unaccepted_bearer_gets_the_discovery_challenge(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    authorization: str,
) -> None:
    """A bearer Home Assistant does not accept, including one that makes its
    validator raise, gets the 401 challenge pointing at the discovery
    document (never a 500), and nothing reaches the MCP server."""
    await _start_ha_auth(hass, fs_root, variant, upstream)
    client = await hass_client_no_auth()

    response = await client.post(
        WEBHOOK,
        data=b"{}",
        headers={"Authorization": authorization, "Host": "ha.example"},
    )

    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == (
        f'Bearer realm="MCP Proxy", resource_metadata="http://ha.example{prm_url()}"'
    )
    assert upstream.requests == []


async def test_missing_bearer_is_challenged_without_asking_home_assistant(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """The discovery probe carries no bearer: it gets the 401 challenge and
    Home Assistant's token validator is never consulted for it."""
    await _start_ha_auth(hass, fs_root, variant, upstream)
    client = await hass_client_no_auth()

    with patch.object(
        hass.auth,
        "async_validate_access_token",
        wraps=hass.auth.async_validate_access_token,
    ) as validator:
        response = await client.post(WEBHOOK, data=b"{}")

    assert response.status == 401
    validator.assert_not_called()
    assert upstream.requests == []


async def test_rejection_reason_names_the_cause_never_the_token(
    hass: HomeAssistant,
    hass_access_token: str,
    component: ModuleType,
) -> None:
    """The reason the add-on log gives for a rejected bearer tells apart no
    usable bearer, an empty one, one Home Assistant rejected and one its
    validator raised on (#1714's OIDC diagnosis), and never echoes the token.
    Junk headers are refused without asking Home Assistant."""
    auth_native = submodule(component, "auth_native")
    server = auth_native.ResourceServer(hass, WEBHOOK_ID)
    crafted = token_core_cannot_validate()
    cases = [
        (None, False, "no bearer header"),
        ("Basic dXNlcjpwYXNzd29yZA==", False, "no bearer header"),
        ("Bearer    ", False, "empty bearer token"),
        ("Bearer sekret-tok", False, "token rejected by hass.auth (returned None)"),
        (f"Bearer {crafted}", False, "validator raised TypeError"),
        (f"Bearer {hass_access_token}", True, "valid"),
    ]

    with patch.object(
        hass.auth,
        "async_validate_access_token",
        wraps=hass.auth.async_validate_access_token,
    ) as validator:
        for header, authorized, reason in cases:
            headers = {} if header is None else {"Authorization": header}
            request = make_mocked_request("POST", "/api/webhook/x", headers=headers)
            assert await server.validate_request_detailed(request) == (
                authorized,
                reason,
            ), header
            assert await server.validate_request(request) is authorized

    consulted = [call.args[0] for call in validator.call_args_list]
    assert set(consulted) == {"sekret-tok", crafted, hass_access_token}


@pytest.fixture
def _restore_proxy_loggers(component: ModuleType) -> Iterator[None]:
    """The debug toggle raises these loggers process-wide; undo it after."""
    loggers = [
        logging.getLogger(component.__name__),
        logging.getLogger("homeassistant.components.webhook"),
    ]
    levels = [logger.level for logger in loggers]
    yield
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)


@pytest.mark.usefixtures("_restore_proxy_loggers")
async def test_inbound_debug_log_says_why_a_bearer_was_rejected(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """With "Log inbound requests" on, the 401 line in the app's log names
    why ha_auth rejected the bearer, and never the token itself."""
    await _start_ha_auth(hass, fs_root, variant, upstream, debug_logging=True)
    client = await hass_client_no_auth()

    response = await client.post(
        WEBHOOK, data=b"{}", headers={"Authorization": "Bearer sekret-tok"}
    )
    mirror = state_path(fs_root, variant.inbound_log)
    await wait_until(hass, lambda: "401 Unauthorized" in text_of(mirror))

    assert response.status == 401
    mirrored = mirror.read_text()
    assert "token rejected by hass.auth (returned None)" in mirrored
    assert "sekret-tok" not in mirrored


async def test_gate_awaits_a_coroutine_validator(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    hass_access_token: str,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """Core's validator is synchronous today; if a Core release makes it a
    coroutine, the gate must await it rather than read the coroutine object
    as an accepted token."""
    await _start_ha_auth(hass, fs_root, variant, upstream)
    client = await hass_client_no_auth()
    # Core's own answers, delivered the way a coroutine validator would.
    validator = AsyncMock(side_effect=hass.auth.async_validate_access_token)

    with patch.object(hass.auth, "async_validate_access_token", validator):
        accepted = await client.post(
            WEBHOOK,
            data=b"{}",
            headers={"Authorization": f"bEaReR {hass_access_token}"},
        )
        rejected = await client.post(
            WEBHOOK, data=b"{}", headers={"Authorization": "bEaReR not-a-token"}
        )

    assert (accepted.status, rejected.status) == (200, 401)
    assert len(upstream.requests) == 1
