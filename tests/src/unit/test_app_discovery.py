"""Unit tests for announcing the app's server to Core's MCP integration (#2307)."""

import json
from pathlib import Path

import httpx
import pytest
import yaml

from ha_mcp import app_discovery

_REPO_ROOT = Path(__file__).resolve().parents[3]


class _FakeSupervisor:
    """Answers the Supervisor endpoints discovery reads and records POSTs."""

    def __init__(
        self, core_version: str = "2026.10.0", hostname: str = "local-ha-mcp-dev"
    ) -> None:
        self.data = {
            "/core/info": {"version": core_version},
            "/addons/self/info": {"hostname": hostname},
        }
        self.posts: list[tuple[str, dict]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.posts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"result": "ok", "data": {"uuid": "abc"}})
        return httpx.Response(
            200, json={"result": "ok", "data": self.data[request.url.path]}
        )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run as the app, with the secret path start.py writes."""
    secret_file = tmp_path / "secret_path.txt"
    secret_file.write_text("/private_abc12345\n")
    monkeypatch.setattr(app_discovery, "SECRET_PATH_FILE", secret_file)
    monkeypatch.setattr(app_discovery, "is_running_in_addon", lambda: True)
    return secret_file


def _serve(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def client(**_: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://supervisor", transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(app_discovery, "make_supervisor_httpx_client", client)


async def _announce(
    monkeypatch: pytest.MonkeyPatch, supervisor: _FakeSupervisor
) -> list[tuple[str, dict]]:
    _serve(monkeypatch, supervisor.handle)
    await app_discovery.announce_mcp_discovery()
    return supervisor.posts


@pytest.mark.asyncio
async def test_posts_the_reachable_url(app, monkeypatch: pytest.MonkeyPatch) -> None:
    posts = await _announce(monkeypatch, _FakeSupervisor())
    assert posts == [
        (
            "/discovery",
            {
                "service": "mcp",
                "config": {"url": "http://local-ha-mcp-dev:9583/private_abc12345"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_a_custom_path_with_a_trailing_slash_is_announced_as_served(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.write_text("/my-custom-path/")
    posts = await _announce(monkeypatch, _FakeSupervisor())
    assert (
        posts[0][1]["config"]["url"] == "http://local-ha-mcp-dev:9583/my-custom-path/"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2026.9.4", "landingpage", "None"])
async def test_older_or_unknown_core_is_not_announced_to(
    app, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    assert await _announce(monkeypatch, _FakeSupervisor(core_version=version)) == []


@pytest.mark.parametrize(
    "version", ["2026.10.0", "2026.10.0b0", "2026.11.0.dev202610010000", "2027.1.0"]
)
def test_supported_core_versions(version: str) -> None:
    assert app_discovery.core_supports_mcp_discovery(version) is True


@pytest.mark.asyncio
async def test_missing_hostname_skips(app, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _announce(monkeypatch, _FakeSupervisor(hostname="")) == []


@pytest.mark.asyncio
async def test_missing_secret_path_skips(app, monkeypatch: pytest.MonkeyPatch) -> None:
    app.unlink()
    assert await _announce(monkeypatch, _FakeSupervisor()) == []


@pytest.mark.asyncio
async def test_outside_the_app_nothing_is_announced(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_discovery, "is_running_in_addon", lambda: False)
    assert await _announce(monkeypatch, _FakeSupervisor()) == []


@pytest.mark.asyncio
async def test_supervisor_failure_never_raises(
    app, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _serve(monkeypatch, refuse)
    await app_discovery.announce_mcp_discovery()
    assert "Could not announce" in caplog.text


@pytest.mark.parametrize("flavor", ["homeassistant-addon", "homeassistant-addon-dev"])
def test_both_flavors_declare_the_service(flavor: str) -> None:
    config = yaml.safe_load((_REPO_ROOT / flavor / "config.yaml").read_text())
    assert "mcp" in config["discovery"]
