"""The dev webhook proxy's unified OAuth endpoints, set up by real Core.

Core loads the dev ``mcp_proxy_dev`` component as a custom integration, sets
up its config entry from the file the app writes, and serves its views over
Home Assistant's real HTTP server. In ``ha_auth`` mode the proxy forwards to
Core's own ``/auth/token`` and ``/auth/revoke``, so ``core_auth`` runs a real
login flow for the codes and tokens those exchanges need. A client's metadata
document lives on the public internet, so ``cimd_web`` serves it through
Core's ``aioclient_mock``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import aiohttp
import pytest
from aiohttp import ClientResponse
from aiohttp.test_utils import TestClient
from homeassistant.auth import auth_manager_from_config
from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant.typing import ClientSessionGenerator

from tests.addon.webhook_proxy._auth_modes import HA_AUTH, proxy_config
from tests.addon.webhook_proxy._legacy_oauth import legacy_config
from tests.addon.webhook_proxy._proxy import (
    STATE_ROOTS,
    forget_package,
    make_fs_root,
    start_proxy,
    state_path,
)
from tests.addon.webhook_proxy._variants import VARIANTS

DEV = VARIANTS[1]
OAUTH_BASE = DEV.oauth_base
USERNAME = "proxy-user"
PASSWORD = "proxy-password"
KEY = b"k" * 32
GOOGLE_REDIRECT_URIS = [
    "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
    "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
]
LOOPBACK_CLIENT = "http://127.0.0.1:54321"
LOOPBACK_REDIRECT = f"{LOOPBACK_CLIENT}/callback"
# What every CIMD host's name resolves to in the tests: a public address.
PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture(scope="session")
def proxy_components_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A ``custom_components`` directory holding the dev proxy component."""
    root = tmp_path_factory.mktemp("proxy_dev_components")
    package = root / "custom_components"
    package.mkdir()
    (package / DEV.component).symlink_to(DEV.component_dir, target_is_directory=True)
    return root


@pytest.fixture
def fs_root(tmp_path: Path) -> Path:
    """Where the component's ``/config`` and ``/data`` files live."""
    return make_fs_root(tmp_path)


@pytest.fixture
def component(
    fs_root: Path, monkeypatch: pytest.MonkeyPatch, proxy_components_root: Path
) -> Iterator[ModuleType]:
    """The dev component package with every submodule loaded.

    Its ``/config`` and ``/data`` paths are redirected under ``fs_root`` and the
    app's heartbeat is fresh, as while the app runs.
    """
    monkeypatch.syspath_prepend(str(proxy_components_root))
    package_name = f"custom_components.{DEV.component}"
    forget_package(package_name)
    package = importlib.import_module(package_name)
    modules = [package] + [
        importlib.import_module(f"{package_name}.{path.stem}")
        for path in sorted(DEV.component_dir.glob("*.py"))
        if path.stem != "__init__"
    ]
    for module in modules:
        for attribute, value in list(vars(module).items()):
            if isinstance(value, Path) and str(value).startswith(STATE_ROOTS):
                monkeypatch.setattr(module, attribute, state_path(fs_root, value))
    heartbeat = vars(sys.modules[f"{package_name}.oauth"]).get("HEARTBEAT_FILE")
    assert isinstance(heartbeat, Path)
    heartbeat.touch()
    yield package
    forget_package(package_name)


def submodule(component: ModuleType, name: str) -> ModuleType:
    """The component's own submodule (loaded by the fixture)."""
    return importlib.import_module(f"{component.__name__}.{name}")


@dataclass
class Proxy:
    """A running proxy: Core's HTTP client plus the component's modules."""

    hass: HomeAssistant
    client: TestClient
    component: ModuleType

    def module(self, name: str) -> ModuleType:
        return submodule(self.component, name)

    @property
    def dcr_key(self) -> bytes:
        """The signing key the proxy minted for its client ids."""
        return self.component.DCR_SECRET_FILE.read_bytes()

    def url(self, endpoint: str) -> str:
        return f"{OAUTH_BASE}/{endpoint}"

    async def post(self, endpoint: str, form: dict[str, str]) -> ClientResponse:
        """POST ``form`` to a scoped endpoint without following redirects."""
        return await self.client.post(
            self.url(endpoint), data=form, allow_redirects=False
        )

    async def authorize(self, **query: str) -> ClientResponse:
        return await self.client.get(
            self.url("authorize"), params=query, allow_redirects=False
        )

    async def register(self, redirect_uris: list[str]) -> str:
        """Register a client through DCR and return its client_id."""
        response = await self.client.post(
            self.url("register"), json={"redirect_uris": redirect_uris}
        )
        assert response.status == 201, await response.text()
        return str((await response.json())["client_id"])


ProxyStarter = Callable[[str], Awaitable[Proxy]]


@pytest.fixture
async def start_proxy_in_mode(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    fs_root: Path,
) -> ProxyStarter:
    """Start the proxy in ``ha_auth``, ``none`` or ``legacy`` mode.

    Core's own listener is the test server, so the proxy's forward to
    ``/auth/token`` reaches the real Core views.
    """

    async def start(mode: str) -> Proxy:
        configs = {
            "ha_auth": proxy_config(oauth=HA_AUTH),
            "none": proxy_config(),
            "legacy": legacy_config(public_base_url="https://ha.example"),
        }
        await start_proxy(hass, DEV, fs_root, configs[mode])
        client = await hass_client_no_auth()
        hass.config.api = ApiConfig("127.0.0.1", "127.0.0.1", client.port, False)
        return Proxy(hass, client, component)

    return start


@dataclass
class CoreAuth:
    """Core's own login flow and token endpoint, behind the test client."""

    hass: HomeAssistant
    client: TestClient

    async def code_for(self, client_id: str, redirect_uri: str) -> str:
        """Sign in through Core's login flow and return its authorization code."""
        flow = await self.client.post(
            "/auth/login_flow",
            json={
                "client_id": client_id,
                "handler": ["insecure_example", None],
                "redirect_uri": redirect_uri,
            },
        )
        assert flow.status == 200, await flow.text()
        done = await self.client.post(
            f"/auth/login_flow/{(await flow.json())['flow_id']}",
            json={"client_id": client_id, "username": USERNAME, "password": PASSWORD},
        )
        assert done.status == 200, await done.text()
        return str((await done.json())["result"])

    async def tokens_for(self, client_id: str, redirect_uri: str) -> dict[str, Any]:
        """Sign in and redeem the code at Core itself, bypassing the proxy."""
        code = await self.code_for(client_id, redirect_uri)
        response = await self.client.post(
            "/auth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
            },
        )
        assert response.status == 200, await response.text()
        tokens: dict[str, Any] = await response.json()
        return tokens

    def refresh_token_exists(self, token: str) -> bool:
        return self.hass.auth.async_get_refresh_token_by_token(token) is not None


@pytest.fixture
async def core_auth(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> CoreAuth:
    """Core's auth component with one real user, served over HTTP."""
    hass.auth = await auth_manager_from_config(
        hass,
        [
            {
                "type": "insecure_example",
                "users": [
                    {"username": USERNAME, "password": PASSWORD, "name": "Proxy User"}
                ],
            }
        ],
        [],
    )
    assert await async_setup_component(hass, "auth", {})
    return CoreAuth(hass, await hass_client_no_auth())


@dataclass
class WrappedSession:
    """A sign-in through the proxy: the client, its envelope and core's token."""

    client_id: str
    envelope: str
    core_refresh_token: str


async def sign_in_through_proxy(
    proxy: Proxy, core_auth: CoreAuth, redirect_uri: str = LOOPBACK_REDIRECT
) -> WrappedSession:
    """Register, sign in at Core and redeem the code through the proxy."""
    client_id = await proxy.register([redirect_uri])
    origin = redirect_uri.rpartition("/")[0]
    code = await core_auth.code_for(origin, redirect_uri)
    response = await proxy.post(
        "token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
        },
    )
    assert response.status == 200, await response.text()
    envelope = (await response.json())["refresh_token"]
    unwrapped = proxy.module("oauth_indirect").unwrap_refresh_token(
        proxy.dcr_key, envelope, client_id
    )
    assert isinstance(unwrapped, tuple)
    return WrappedSession(client_id, envelope, unwrapped[0])


@dataclass
class CimdWeb:
    """The public internet's view of client metadata documents."""

    mocker: AiohttpClientMocker
    session: aiohttp.ClientSession
    dns: AsyncMock

    @property
    def fetches(self) -> int:
        """How many documents were fetched since the last ``serve``."""
        return self.mocker.call_count

    def serve(
        self, client_id: str, redirect_uris: list[str] | None, **kwargs: Any
    ) -> None:
        """Publish the client's document at its address, replacing earlier ones."""
        self.mocker.clear_requests()
        url = f"https://{PUBLIC_ADDRESS}{urlparse(client_id).path}"
        if redirect_uris is None:
            self.mocker.get(url, **kwargs)
            return
        document = {
            "client_id": client_id,
            "client_name": "Client",
            "redirect_uris": redirect_uris,
        }
        self.mocker.get(url, text=json.dumps(document), **kwargs)

    async def attach(self, proxy: Proxy, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make the proxy's metadata lookups use this web."""
        data = proxy.hass.data[DEV.domain]
        await data["cimd_session"].close()
        monkeypatch.setitem(data, "cimd_session", self.session)


@pytest.fixture
async def cimd_web(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[CimdWeb]:
    """Name resolution answers a public address; documents come from ``serve``."""
    dns = AsyncMock(
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_ADDRESS, 443))
        ]
    )
    monkeypatch.setattr(hass.loop, "getaddrinfo", dns)
    session = aioclient_mock.create_session(hass.loop)
    yield CimdWeb(aioclient_mock, session, dns)
    await session.close()


@pytest.fixture
def relay_posts() -> Iterator[MagicMock]:
    """Every POST the proxy's relay session makes toward Core."""
    with patch.object(
        aiohttp.ClientSession,
        "post",
        autospec=True,
        side_effect=aiohttp.ClientSession.post,
    ) as spy:
        yield spy


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def hand_signed_envelope(key: bytes, payload: object) -> str:
    """Sign an arbitrary envelope payload under ``key`` the way the proxy does."""
    body = b64url(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(key, f"hamcp-rt-{body}".encode("ascii"), hashlib.sha256)
    return f"hamcp-rt-{body}.{b64url(signature.digest())}"
