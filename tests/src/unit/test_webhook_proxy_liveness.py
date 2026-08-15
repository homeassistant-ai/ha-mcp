"""A stopped proxy add-on must take the dev proxy's OAuth surface dark."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPONENT_DIR = REPO_ROOT / "homeassistant-addon-webhook-proxy-dev" / "mcp_proxy_dev"


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
            "authorization_server_url": lambda self, base: f"{base}{oauth.OAUTH_BASE}",
        },
    )()


async def test_mode_none_when_addon_down(oauth, monkeypatch):
    """A bound discovery view behaves like an unregistered route when stopped."""

    async def _down(_hass):
        return False

    monkeypatch.setattr(oauth, "_addon_alive", _down)
    view = oauth.AuthorizationServerMetadataView(_provider(oauth, oauth.MODE_HA_AUTH))

    await view.get(MagicMock())

    assert oauth.web.json_response.call_args.kwargs["status"] == 404


async def test_mode_served_when_addon_up(oauth, monkeypatch):
    """The existing mode dispatch still serves while the heartbeat is fresh."""

    async def _up(_hass):
        return True

    monkeypatch.setattr(oauth, "_addon_alive", _up)
    view = oauth.AuthorizationServerMetadataView(_provider(oauth, oauth.MODE_LEGACY))

    await view.get(MagicMock())

    body = oauth.web.json_response.call_args.args[0]
    assert body["issuer"] == "https://ha.example/api/mcp_proxy_dev/oauth"
    assert oauth.web.json_response.call_args.kwargs.get("status") in (None, 200)


def test_heartbeat_fresh_only_for_recent_mtime(oauth, monkeypatch, tmp_path):
    """The liveness signal is the heartbeat file's mtime, not TCP reachability
    of target_url — that URL is the INDEPENDENT ha-mcp server add-on, which
    keeps accepting connections after this proxy add-on stops (#2218 review)."""
    import os

    heartbeat = tmp_path / ".mcp_proxy_dev_heartbeat"
    monkeypatch.setattr(oauth, "HEARTBEAT_FILE", heartbeat)

    # Missing file (clean shutdown deleted it, or the add-on never ran).
    assert oauth._heartbeat_fresh() is False

    # Fresh touch — the add-on's keep-alive loop is running.
    heartbeat.touch()
    assert oauth._heartbeat_fresh() is True

    # Stale mtime — the add-on crashed without cleanup.
    stale = heartbeat.stat().st_mtime - (oauth._HEARTBEAT_MAX_AGE + 60)
    os.utime(heartbeat, (stale, stale))
    assert oauth._heartbeat_fresh() is False


async def test_addon_alive_caches_only_the_negative_verdict(oauth, monkeypatch):
    """A positive verdict re-stats every request so a clean shutdown's file
    deletion takes effect immediately (#2218 review); only the down verdict
    caches, bounding stat traffic from anonymous requests to a stopped
    install."""
    calls = []
    fresh = {"value": True}

    async def _executor_job(func):
        calls.append(func)
        return func()

    hass = MagicMock()
    hass.async_add_executor_job = _executor_job
    monkeypatch.setattr(oauth, "_heartbeat_fresh", lambda: fresh["value"])
    monkeypatch.setattr(oauth, "_addon_down_until", 0.0)

    assert await oauth._addon_alive(hass) is True
    assert await oauth._addon_alive(hass) is True
    assert len(calls) == 2  # alive verdicts are never cached

    fresh["value"] = False
    assert await oauth._addon_alive(hass) is False
    assert await oauth._addon_alive(hass) is False  # served from the down cache
    assert len(calls) == 3
