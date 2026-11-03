"""The dev proxy's ha_auth token forward reaches core's own listener (#2950)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from .test_oauth_core_forward_tls import tls_core
from .test_webhook_proxy_oauth_unified import oauth_stack  # noqa: F401


@pytest.mark.parametrize(
    ("api", "target"),
    [
        (SimpleNamespace(use_ssl=False, port=9443), "http://127.0.0.1:9443"),
        (SimpleNamespace(use_ssl=True, port=9443), "https://127.0.0.1:9443"),
        (
            SimpleNamespace(use_ssl=True, port=9443, host="10.0.2.15"),
            "https://10.0.2.15:9443",
        ),
        (SimpleNamespace(use_ssl=True, port=9443, host="::"), "https://[::1]:9443"),
    ],
    ids=[
        "plain-http",
        "tls",
        "tls-restricted-listen-address",
        "tls-ipv6-wildcard-listen-address",
    ],
)
async def test_sign_in_reaches_core_listener(
    oauth_stack,  # noqa: F811
    monkeypatch,
    api,
    target,
):
    """With http.ssl_certificate, every ha_auth sign-in answered 503: the
    forward verified a certificate issued for a name it could not trust. A
    listener bound to a LAN address only is reached there, not on loopback."""
    hass, session = tls_core(monkeypatch, api=api)

    result = await oauth_stack.autoapprove._forward_to_core(
        hass, {"session": session}, "/auth/token", {"grant_type": "authorization_code"}
    )

    assert isinstance(result, tuple)  # a web.Response here is the user's 503
    url, kwargs = session.calls[-1]
    assert url == f"{target}/auth/token"
    assert (kwargs.get("ssl") is False) == target.startswith("https://")
