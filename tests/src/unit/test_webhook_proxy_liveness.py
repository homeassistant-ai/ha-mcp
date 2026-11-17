"""A stopped proxy add-on must take the dev proxy's OAuth surface dark."""

import importlib.util
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPONENT_DIR = REPO_ROOT / "homeassistant-addon-webhook-proxy-dev" / "mcp_proxy_dev"


@pytest.fixture
def oauth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> types.ModuleType:
    """Load the dev proxy against real Home Assistant and aiohttp imports, with
    its heartbeat file under ``tmp_path``."""
    package = types.ModuleType("mcp_proxy_dev")
    package.__path__ = [str(COMPONENT_DIR)]
    monkeypatch.setitem(sys.modules, "mcp_proxy_dev", package)

    spec = importlib.util.spec_from_file_location(
        "mcp_proxy_dev.oauth", COMPONENT_DIR / "oauth.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "HEARTBEAT_FILE", tmp_path / ".mcp_proxy_dev_heartbeat")
    return module


async def _beat(oauth: types.ModuleType, hass: HomeAssistant) -> None:
    """The add-on's keep-alive touch."""
    await hass.async_add_executor_job(oauth.HEARTBEAT_FILE.touch)


async def _metadata_status(oauth: types.ModuleType, hass: HomeAssistant) -> int:
    """What a client asking the bound discovery view gets."""
    hass.data[oauth.DOMAIN] = {
        "oauth_mode": oauth.MODE_LEGACY,
        "target_url": "http://127.0.0.1:9583/private_x",
    }
    provider = oauth.OAuthProvider(
        hass,
        client_id="test-client-identifier",
        client_secret="test-secret",
        webhook_id="test-webhook",
        signing_key=b"x" * 32,
        public_base_url="https://ha.example",
    )
    view = oauth.AuthorizationServerMetadataView(provider)
    response = await view.get(make_mocked_request("GET", "/"))
    if response.status == 200:
        body = json.loads(response.body)
        assert body["issuer"] == "https://ha.example/api/mcp_proxy_dev/oauth"
    return int(response.status)


async def test_oauth_serves_only_while_the_heartbeat_is_fresh(
    oauth: types.ModuleType, hass: HomeAssistant
) -> None:
    """The liveness signal is the heartbeat file's mtime, not TCP reachability
    of target_url — that URL is the INDEPENDENT ha-mcp server add-on, which
    keeps accepting connections after this proxy add-on stops (#2218 review).
    A bound view behaves like an unregistered route when the add-on is down."""
    # The add-on's keep-alive loop is running.
    await _beat(oauth, hass)
    assert await _metadata_status(oauth, hass) == 200

    # One missed 30-second touch is a slow loop, not a stopped add-on.
    minute_ago = time.time() - 60
    await hass.async_add_executor_job(
        os.utime, oauth.HEARTBEAT_FILE, (minute_ago, minute_ago)
    )
    assert await _metadata_status(oauth, hass) == 200

    # The add-on crashed without cleanup a day ago.
    day_ago = time.time() - 86400
    await hass.async_add_executor_job(
        os.utime, oauth.HEARTBEAT_FILE, (day_ago, day_ago)
    )
    assert await _metadata_status(oauth, hass) == 404


async def test_a_clean_stop_goes_dark_at_once_and_a_restart_is_seen_soon(
    oauth: types.ModuleType, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A clean shutdown's file deletion takes effect on the next request (an
    up verdict is never reused, #2218 review); a down verdict is held briefly
    to bound stat traffic from anonymous requests to a stopped install, but
    for less than the 2 s the starting add-on waits between its probes of
    this view (#2219)."""
    await _beat(oauth, hass)
    assert await _metadata_status(oauth, hass) == 200

    await hass.async_add_executor_job(oauth.HEARTBEAT_FILE.unlink)
    assert await _metadata_status(oauth, hass) == 404

    await _beat(oauth, hass)
    assert await _metadata_status(oauth, hass) == 404

    freezer.tick(2)
    assert await _metadata_status(oauth, hass) == 200
