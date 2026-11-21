"""Unit tests for announcing the app's server to Core's MCP integration (#2307)."""

import logging
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from aiohasupervisor.models.discovery import SetDiscovery

from ha_mcp import app_discovery

from ._tools_addons_core import Supervisor, app_info
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, supervisor: Supervisor):
    """Run as the app, with the secret path start.py writes."""
    secret_file = tmp_path / "secret_path.txt"
    secret_file.write_text("/private_abc12345\n")
    monkeypatch.setattr(app_discovery, "SECRET_PATH_FILE", secret_file)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "app-token")
    monkeypatch.setenv("SUPERVISOR_BASE_URL", f"http://{supervisor.url}")
    return secret_file


async def _announce(supervisor: Supervisor, hostname: str = "local-ha-mcp-dev") -> list:
    supervisor.answer("GET", "/addons/self/info", app_info(hostname=hostname))
    supervisor.answer("POST", "/discovery", SetDiscovery(uuid=uuid4()).to_dict())
    await app_discovery.announce_mcp_discovery()
    return supervisor.sent("POST", "/discovery")


@pytest.mark.asyncio
async def test_posts_the_reachable_url(app, supervisor: Supervisor) -> None:
    posts = await _announce(supervisor)
    assert posts == [
        {
            "service": "mcp",
            "config": {"url": "http://local-ha-mcp-dev:9583/private_abc12345"},
        }
    ]


@pytest.mark.asyncio
async def test_a_custom_path_with_a_trailing_slash_is_announced_as_served(
    app, supervisor: Supervisor
) -> None:
    app.write_text("/my-custom-path/")
    posts = await _announce(supervisor)
    assert posts[0]["config"]["url"] == "http://local-ha-mcp-dev:9583/my-custom-path/"


@pytest.mark.asyncio
async def test_missing_hostname_skips(app, supervisor: Supervisor) -> None:
    assert await _announce(supervisor, hostname="") == []


@pytest.mark.asyncio
async def test_missing_secret_path_skips(app, supervisor: Supervisor) -> None:
    app.unlink()
    assert await _announce(supervisor) == []


@pytest.mark.asyncio
async def test_outside_the_app_nothing_is_announced(
    app, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    assert await _announce(supervisor) == []


@pytest.mark.asyncio
async def test_supervisor_failure_never_raises(
    app, supervisor: Supervisor, caplog: pytest.LogCaptureFixture
) -> None:
    supervisor.drop("GET", "/addons/self/info")
    with caplog.at_level(logging.WARNING, logger=app_discovery.__name__):
        await app_discovery.announce_mcp_discovery()
    assert supervisor.requests == [("GET", "/addons/self/info")]
    assert "Could not announce" in caplog.text


@pytest.mark.parametrize("flavor", ["homeassistant-addon", "homeassistant-addon-dev"])
def test_both_flavors_declare_the_service(flavor: str) -> None:
    config = yaml.safe_load((_REPO_ROOT / flavor / "config.yaml").read_text())
    assert "mcp" in config["discovery"]
