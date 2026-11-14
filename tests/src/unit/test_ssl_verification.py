"""The HA_VERIFY_SSL toggle, against Core served with a self-signed certificate.

Verification on, the REST and WebSocket clients refuse the certificate and
name the switch that would accept it; verification off, from the argument,
the environment or the pooled factory, they reach Core. Each assertion is on
what the client could or could not do against real Core over TLS.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.config import reset_global_settings
from ha_mcp.tools.helpers import get_connected_ws_client

from ._tls_core import tls_core

WS_LOGGER = "ha_mcp.client.websocket_client"
REST_LOGGER = "ha_mcp.client.rest_client"


@pytest.fixture(autouse=True)
def _reset_settings_singleton(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test reads ``HA_VERIFY_SSL`` afresh; unset unless it sets one."""
    monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
    reset_global_settings()
    yield
    reset_global_settings()


@pytest.fixture
async def tls_url(
    hass: HomeAssistant, tmp_path: Path, socket_enabled: None
) -> AsyncIterator[str]:
    """The base URL of Core served over TLS with a self-signed certificate."""
    async with tls_core(hass, tmp_path) as url:
        yield url


async def test_rest_client_refuses_a_self_signed_core_by_default_and_names_the_switch(
    tls_url: str, hass_access_token: str
) -> None:
    """Verification is on unless turned off, and the failure says how."""
    client = HomeAssistantClient(base_url=tls_url, token=hass_access_token)
    try:
        assert client.verify_ssl is True
        with pytest.raises(HomeAssistantConnectionError) as excinfo:
            await client.get_config()
    finally:
        await client.close()

    assert "HA_VERIFY_SSL=false" in str(excinfo.value)


@pytest.mark.parametrize("source", ["argument", "environment", "oauth"])
async def test_rest_client_with_verification_off_reaches_a_self_signed_core(
    source: str,
    tls_url: str,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``verify_ssl=False`` from any source reaches Core.

    ``environment`` builds the client from settings alone; ``oauth`` passes
    the URL and token but no ``verify_ssl``, so the setting must still apply.
    """
    if source == "argument":
        client = HomeAssistantClient(
            base_url=tls_url, token=hass_access_token, verify_ssl=False
        )
    else:
        monkeypatch.setenv("HA_VERIFY_SSL", "false")
        if source == "environment":
            monkeypatch.setenv("HOMEASSISTANT_URL", tls_url)
            monkeypatch.setenv("HOMEASSISTANT_TOKEN", hass_access_token)
            client = HomeAssistantClient()
        else:
            client = HomeAssistantClient(base_url=tls_url, token=hass_access_token)
    try:
        config = await client.get_config()
    finally:
        await client.close()

    assert config["state"] == "RUNNING"


@pytest.mark.parametrize("verify_ssl", [False, True])
async def test_rest_client_warns_only_when_verification_is_off(
    verify_ssl: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Turning verification off is announced once, at construction."""
    with caplog.at_level(logging.WARNING, logger=REST_LOGGER):
        client = HomeAssistantClient(
            base_url="https://ha.local:8123", token="t", verify_ssl=verify_ssl
        )
    await client.close()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert bool(warnings) is not verify_ssl, warnings


async def test_websocket_refuses_a_self_signed_core_by_default(
    tls_url: str, hass_access_token: str, caplog: pytest.LogCaptureFixture
) -> None:
    """``wss://`` verifies the certificate; the refusal names the SSL error.

    Self-signed / hostname-mismatch certs are a common real-world cause, so
    the surfaced reason must name the SSL error and the log the switch.
    """
    client = HomeAssistantWebSocketClient(tls_url, hass_access_token)
    with caplog.at_level(logging.ERROR, logger=WS_LOGGER):
        assert await client.connect() is False

    assert client.last_connect_error is not None
    assert "SSLCertVerificationError" in client.last_connect_error
    assert "self-signed" in client.last_connect_error
    assert any("HA_VERIFY_SSL=false" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("path", ["", "/api"], ids=["root", "with_path"])
async def test_websocket_with_verification_off_reaches_a_self_signed_core(
    path: str, tls_url: str, hass_access_token: str
) -> None:
    """``verify_ssl=False`` reaches Core over ``wss://``.

    A path-bearing ``https`` URL (proxy setups) maps to ``<path>/websocket``;
    it must get the same TLS settings, and ``/api/websocket`` is Core's.
    """
    client = HomeAssistantWebSocketClient(
        f"{tls_url}{path}", hass_access_token, verify_ssl=False
    )
    try:
        assert await client.connect() is True, client.last_connect_error
    finally:
        await client.disconnect()


async def test_plain_websocket_url_connects_without_tls(
    core_client: HomeAssistantClient, hass_access_token: str
) -> None:
    """A ``ws://`` URL (e.g. the Supervisor proxy) gets no TLS context.

    Verification is on here: a TLS context on a plain socket would refuse it.
    """
    client = HomeAssistantWebSocketClient(
        core_client.base_url, hass_access_token, verify_ssl=True
    )
    try:
        assert await client.connect() is True, client.last_connect_error
    finally:
        await client.disconnect()


async def test_verification_off_warning_is_logged_once_per_client(
    tls_url: str, hass_access_token: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Reconnect storms shouldn't spam the warning."""
    client = HomeAssistantWebSocketClient(tls_url, hass_access_token, verify_ssl=False)
    with caplog.at_level(logging.WARNING, logger=WS_LOGGER):
        for _ in range(3):
            assert await client.connect() is True
            await client.disconnect()

    ssl_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "TLS verification disabled" in r.getMessage()
    ]
    assert len(ssl_warnings) == 1, ssl_warnings


async def test_pooled_websocket_follows_the_environment(
    tls_url: str, hass_access_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool builds its clients from (url, token) alone: the env decides.

    A factory that hard-coded verification on would refuse this Core.
    """
    monkeypatch.setenv("HA_VERIFY_SSL", "false")
    try:
        client = await websocket_manager.get_client(
            url=tls_url, token=hass_access_token
        )
        assert client.is_connected
    finally:
        await websocket_manager.disconnect()


def test_unreadable_settings_keep_verification_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad env var elsewhere must not silently flip TLS verification off."""
    monkeypatch.setenv("HA_VERIFY_SSL", "neither-true-nor-false")

    with caplog.at_level(logging.WARNING, logger=WS_LOGGER):
        client = HomeAssistantWebSocketClient("https://ha.local:8123", "t")

    assert client.verify_ssl is True
    assert any("Could not load settings" in r.getMessage() for r in caplog.records)


async def test_connect_helper_passes_verification_off_to_the_websocket(
    tls_url: str, hass_access_token: str
) -> None:
    """``get_connected_ws_client(verify_ssl=False)`` reaches a self-signed Core."""
    ws_client, error = await get_connected_ws_client(
        tls_url, hass_access_token, verify_ssl=False
    )
    try:
        assert error is None
        assert ws_client is not None
    finally:
        if ws_client is not None:
            await ws_client.disconnect()
