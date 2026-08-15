"""A stopped add-on backend must take the dev proxy's OAuth surface dark."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPONENT_DIR = (
    REPO_ROOT / "homeassistant-addon-webhook-proxy-dev" / "mcp_proxy_dev"
)


@pytest.fixture
def oauth(monkeypatch):
    """Load the dev proxy OAuth module with its Home Assistant imports stubbed."""
    aiohttp = types.ModuleType("aiohttp")
    web = types.ModuleType("aiohttp.web")
    web.Request = MagicMock
    web.Response = MagicMock
    web.json_response = MagicMock(name="json_response")
    aiohttp.web = web

    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    http = types.ModuleType("homeassistant.components.http")
    http.HomeAssistantView = type("HomeAssistantView", (), {})
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = MagicMock

    package = types.ModuleType("mcp_proxy_dev")
    package.__path__ = [str(COMPONENT_DIR)]
    for name, module in {
        "aiohttp": aiohttp,
        "aiohttp.web": web,
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.http": http,
        "homeassistant.core": core,
        "mcp_proxy_dev": package,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        "mcp_proxy_dev.oauth", COMPONENT_DIR / "oauth.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _provider(oauth, mode: str):
    hass = MagicMock()
    hass.data = {
        oauth.DOMAIN: {
            "oauth_mode": mode,
            "target_url": "http://127.0.0.1:9583/private_x",
        }
    }
    return type(
        "Provider",
        (),
        {
            "_hass": hass,
            "base_url_for": lambda self, request: "https://ha.example",
            "authorization_server_url": lambda self, base: (
                f"{base}{oauth.OAUTH_BASE}"
            ),
        },
    )()


async def test_mode_none_when_backend_down(oauth, monkeypatch):
    """A bound discovery view behaves like an unregistered route when stopped."""

    async def _down(_hass):
        return False

    monkeypatch.setattr(oauth, "_backend_alive", _down)
    view = oauth.AuthorizationServerMetadataView(
        _provider(oauth, oauth.MODE_HA_AUTH)
    )

    await view.get(MagicMock())

    assert oauth.web.json_response.call_args.kwargs["status"] == 404


async def test_mode_served_when_backend_up(oauth, monkeypatch):
    """The existing mode dispatch still serves while the backend is reachable."""

    async def _up(_hass):
        return True

    monkeypatch.setattr(oauth, "_backend_alive", _up)
    view = oauth.AuthorizationServerMetadataView(
        _provider(oauth, oauth.MODE_LEGACY)
    )

    await view.get(MagicMock())

    body = oauth.web.json_response.call_args.args[0]
    assert body["issuer"] == "https://ha.example/api/mcp_proxy_dev/oauth"
    assert oauth.web.json_response.call_args.kwargs.get("status") in (None, 200)
