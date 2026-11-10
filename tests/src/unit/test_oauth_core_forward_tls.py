"""The ha_auth token forward reaches a core that serves TLS itself (#2950)."""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest
from homeassistant.helpers import network

import custom_components.ha_mcp_tools.oauth_autoapprove as aa


class _CoreAnswer:
    status = 200
    content_type = "application/json"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self):
        return b'{"access_token":"core"}'


class _RelaySession:
    """Rejects a verified https hop: core's certificate names neither
    127.0.0.1 nor a private-CA host the container trusts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.startswith("https://") and kwargs.get("ssl") is not False:
            raise aiohttp.ClientError("certificate verify failed")
        return _CoreAnswer()


def tls_core(monkeypatch, *, api=None, configured_url=None):
    """A core serving TLS on :9443. ``get_url`` answers with the configured
    URL because the pre-#2950 forward consulted it: that keeps the path
    reachable when the fix is reverted, so these tests fail instead of
    erroring."""

    def get_url(_hass, **_kwargs):
        if configured_url is None:
            raise network.NoURLAvailableError
        return configured_url

    monkeypatch.setattr(network, "get_url", get_url)
    api = api or SimpleNamespace(use_ssl=True, port=9443)
    return SimpleNamespace(config=SimpleNamespace(api=api)), _RelaySession()


@pytest.mark.parametrize(
    "configured_url",
    [None, "https://homeassistant.local:9443"],
    ids=["no-https-url", "private-ca-internal-url"],
)
async def test_sign_in_reaches_a_tls_core(monkeypatch, configured_url):
    """With http.ssl_certificate, every ha_auth sign-in answered 503: the
    forward verified a certificate issued for a name it could not trust."""
    hass, session = tls_core(monkeypatch, configured_url=configured_url)

    result = await aa._forward_to_core(
        hass, {"session": session}, "/auth/token", {"grant_type": "authorization_code"}
    )

    assert isinstance(result, tuple)  # a web.Response here is the user's 503
    url, kwargs = session.calls[-1]
    assert url == "https://127.0.0.1:9443/auth/token"
    assert kwargs.get("ssl") is False
