"""The dev proxy's ha_auth token forward reaches core's own listener (#2950)."""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import aiohttp
import pytest
from aiohttp import web
from aiohttp.base_protocol import BaseProtocol
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request
from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPONENT_DIR = REPO_ROOT / "homeassistant-addon-webhook-proxy-dev" / "mcp_proxy_dev"
PACKAGE = "webhook_proxy_dev_forward_tls_test"


class _CoreAnswer:
    status = 200
    content_type = "application/json"

    async def __aenter__(self) -> _CoreAnswer:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def read(self) -> bytes:
        return b'{"access_token":"core"}'


class _RelaySession:
    """Rejects a verified https hop: core's certificate names neither
    127.0.0.1 nor a private-CA host the container trusts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _CoreAnswer:
        self.calls.append((url, kwargs))
        if url.startswith("https://") and kwargs.get("ssl") is not False:
            raise aiohttp.ClientError("certificate verify failed")
        return _CoreAnswer()


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """The dev proxy's OAuth modules, loaded from its add-on directory, with
    the add-on's heartbeat file fresh."""
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(COMPONENT_DIR)]
    monkeypatch.setitem(sys.modules, PACKAGE, package)
    oauth = importlib.import_module(f"{PACKAGE}.oauth")
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    monkeypatch.setattr(oauth, "HEARTBEAT_FILE", heartbeat)
    yield SimpleNamespace(
        oauth=oauth,
        autoapprove=importlib.import_module(f"{PACKAGE}.oauth_autoapprove"),
        indirect=importlib.import_module(f"{PACKAGE}.oauth_indirect"),
    )
    for name in [n for n in sys.modules if n.startswith(f"{PACKAGE}.")]:
        del sys.modules[name]


def token_request(hass: HomeAssistant, form: dict[str, str]) -> web.Request:
    """A POST to the scoped token endpoint carrying ``form``."""
    body = urlencode(form).encode()
    payload = StreamReader(BaseProtocol(hass.loop), 2**16, loop=hass.loop)
    payload.feed_data(body)
    payload.feed_eof()
    return make_mocked_request(
        "POST",
        "/api/mcp_proxy_dev/oauth/token",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
        },
        payload=payload,
    )


@pytest.mark.parametrize(
    ("use_ssl", "host", "target"),
    [
        (False, "0.0.0.0", "http://127.0.0.1:9443"),
        (True, "0.0.0.0", "https://127.0.0.1:9443"),
        (True, "10.0.2.15", "https://10.0.2.15:9443"),
        (True, "::", "https://[::1]:9443"),
    ],
    ids=[
        "plain-http",
        "tls",
        "tls-restricted-listen-address",
        "tls-ipv6-wildcard-listen-address",
    ],
)
async def test_sign_in_reaches_core_listener(
    hass: HomeAssistant,
    proxy: SimpleNamespace,
    use_ssl: bool,
    host: str,
    target: str,
):
    """With http.ssl_certificate, every ha_auth sign-in answered 503: the
    forward verified a certificate issued for a name it could not trust. A
    listener bound to a LAN address only is reached there, not on loopback."""
    hass.config.api = ApiConfig("192.168.1.2", host, 9443, use_ssl)
    session = _RelaySession()
    key = b"k" * 32
    hass.data[proxy.oauth.DOMAIN] = {
        "oauth_mode": proxy.oauth.MODE_HA_AUTH,
        "dcr_signing_key": key,
        "session": session,
    }
    client_id = "https://client.example/metadata.json"
    # A signed refresh envelope is always proxied to core rather than 307'd.
    envelope = proxy.indirect.wrap_refresh_token(
        key, "core-refresh", client_id, client_id
    )

    response = await proxy.autoapprove.AutoApproveTokenView(hass).post(
        token_request(
            hass,
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": envelope,
            },
        )
    )

    assert response.status == 200  # 503 is the sign-in failing
    url, kwargs = session.calls[-1]
    assert url == f"{target}/auth/token"
    assert (kwargs.get("ssl") is False) == target.startswith("https://")
