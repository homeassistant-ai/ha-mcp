"""A stopped proxy add-on must take the dev proxy's OAuth surface dark."""

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request
from homeassistant.core import HomeAssistant

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPONENT_DIR = REPO_ROOT / "homeassistant-addon-webhook-proxy-dev" / "mcp_proxy_dev"


@pytest.fixture
def oauth(monkeypatch):
    """Load the dev proxy against real Home Assistant and aiohttp imports."""
    package = types.ModuleType("mcp_proxy_dev")
    package.__path__ = [str(COMPONENT_DIR)]
    monkeypatch.setitem(sys.modules, "mcp_proxy_dev", package)

    spec = importlib.util.spec_from_file_location(
        "mcp_proxy_dev.oauth", COMPONENT_DIR / "oauth.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _provider(oauth, hass: HomeAssistant, mode: str):
    hass.data[oauth.DOMAIN] = {
        "oauth_mode": mode,
        "target_url": "http://127.0.0.1:9583/private_x",
    }
    return oauth.OAuthProvider(
        hass,
        client_id="test-client-identifier",
        client_secret="test-secret",
        webhook_id="test-webhook",
        signing_key=b"x" * 32,
        public_base_url="https://ha.example",
    )


async def test_mode_none_when_addon_down(oauth, monkeypatch, hass: HomeAssistant):
    """A bound discovery view behaves like an unregistered route when stopped."""

    async def _down(_hass):
        return False

    monkeypatch.setattr(oauth, "_addon_alive", _down)
    view = oauth.AuthorizationServerMetadataView(
        _provider(oauth, hass, oauth.MODE_HA_AUTH)
    )

    response = await view.get(make_mocked_request("GET", "/"))

    assert response.status == 404


async def test_mode_served_when_addon_up(oauth, monkeypatch, hass: HomeAssistant):
    """The existing mode dispatch still serves while the heartbeat is fresh."""

    async def _up(_hass):
        return True

    monkeypatch.setattr(oauth, "_addon_alive", _up)
    view = oauth.AuthorizationServerMetadataView(
        _provider(oauth, hass, oauth.MODE_LEGACY)
    )

    response = await view.get(make_mocked_request("GET", "/"))

    body = json.loads(response.body)
    assert body["issuer"] == "https://ha.example/api/mcp_proxy_dev/oauth"
    assert response.status == 200


def test_heartbeat_fresh_only_for_recent_mtime(oauth, monkeypatch, tmp_path, freezer):
    """The liveness signal is the heartbeat file's mtime, not TCP reachability
    of target_url — that URL is the INDEPENDENT ha-mcp server add-on, which
    keeps accepting connections after this proxy add-on stops (#2218 review)."""
    freezer.move_to("2026-01-01 12:00:00")
    heartbeat = tmp_path / ".mcp_proxy_dev_heartbeat"
    monkeypatch.setattr(oauth, "HEARTBEAT_FILE", heartbeat)

    # Missing file (clean shutdown deleted it, or the add-on never ran).
    assert oauth._heartbeat_fresh() is False

    # Fresh touch — the add-on's keep-alive loop is running.
    heartbeat.touch()
    now = oauth.time.time()
    os.utime(heartbeat, (now, now))
    assert oauth._heartbeat_fresh() is True

    # Stale mtime — the add-on crashed without cleanup.
    stale = heartbeat.stat().st_mtime - (oauth._HEARTBEAT_MAX_AGE + 60)
    os.utime(heartbeat, (stale, stale))
    assert oauth._heartbeat_fresh() is False


async def test_addon_alive_caches_only_the_negative_verdict(
    oauth, monkeypatch, hass: HomeAssistant, freezer
):
    """A positive verdict re-stats every request so a clean shutdown's file
    deletion takes effect immediately (#2218 review); only the down verdict
    caches, bounding stat traffic from anonymous requests to a stopped
    install."""
    calls = []
    fresh = {"value": True}

    def _heartbeat_fresh():
        calls.append(True)
        return fresh["value"]

    monkeypatch.setattr(oauth, "_heartbeat_fresh", _heartbeat_fresh)
    monkeypatch.setitem(oauth._addon_down_state, "until", 0.0)

    assert await oauth._addon_alive(hass) is True
    assert await oauth._addon_alive(hass) is True
    assert len(calls) == 2  # alive verdicts are never cached

    fresh["value"] = False
    assert await oauth._addon_alive(hass) is False
    assert await oauth._addon_alive(hass) is False  # served from the down cache
    assert len(calls) == 3
