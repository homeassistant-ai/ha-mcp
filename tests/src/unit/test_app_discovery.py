"""Unit tests for announcing the app's server to Core's MCP integration (#2307)."""

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from aiohasupervisor.models.discovery import SetDiscovery

from ha_mcp import app_discovery

from ._haos_build_core import core_info
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


async def _announce(
    supervisor: Supervisor, core_version: str = "2026.10.0", hostname: str = "local-ha-mcp-dev"
) -> list:
    supervisor.answer(
        "GET", "/core/info",
        core_info(core_version, version_latest=core_version, update_available=False),
    )
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
    assert (
        posts[0]["config"]["url"] == "http://local-ha-mcp-dev:9583/my-custom-path/"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2026.9.4", "landingpage", "None"])
async def test_older_or_unknown_core_is_not_announced_to(
    app, supervisor: Supervisor, version: str
) -> None:
    assert await _announce(supervisor, core_version=version) == []


@pytest.mark.parametrize(
    "version", ["2026.10.0", "2026.10.0b0", "2026.11.0.dev202610010000", "2027.1.0"]
)
def test_supported_core_versions(version: str) -> None:
    assert app_discovery.core_supports_mcp_discovery(version) is True


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
    supervisor.drop("GET", "/core/info")
    await app_discovery.announce_mcp_discovery()
    assert "Could not announce" in caplog.text


@pytest.mark.parametrize("flavor", ["homeassistant-addon", "homeassistant-addon-dev"])
def test_both_flavors_declare_the_service(flavor: str) -> None:
    config = yaml.safe_load((_REPO_ROOT / flavor / "config.yaml").read_text())
    assert "mcp" in config["discovery"]
