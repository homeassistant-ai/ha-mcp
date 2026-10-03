"""Unit tests for announcing the server to Core's MCP integration (#2307)."""

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import supervisor_api


class _FakeSupervisor:
    """Answers the Supervisor endpoints MCP discovery reads and records POSTs."""

    def __init__(
        self, core_version: str = "2026.10.0", hostname: str = "local-ha-mcp-dev"
    ) -> None:
        self.data = {
            "http://supervisor/core/info": {"version": core_version},
            "http://supervisor/addons/self/info": {"hostname": hostname},
        }
        self.posts: list[tuple[str, dict]] = []

    def urlopen(self, req: urllib.request.Request, timeout: float | None = None) -> Any:
        if req.get_method() == "POST":
            self.posts.append((req.full_url, json.loads(req.data)))
            body = {"result": "ok", "data": {"uuid": "abc"}}
        else:
            body = {"result": "ok", "data": self.data[req.full_url]}
        payload = json.dumps(body).encode()

        class Resp:
            def __enter__(self) -> "Resp":
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return payload

        return Resp()


class TestAnnounceMcpDiscovery:
    """Unit tests for announcing the server to Core's MCP integration (#2307)."""

    def _announce(
        self,
        monkeypatch: pytest.MonkeyPatch,
        supervisor: _FakeSupervisor,
        secret_path: str = "/private_abc12345",
    ) -> list[tuple[str, dict]]:
        monkeypatch.setattr(
            supervisor_api.urllib.request, "urlopen", supervisor.urlopen
        )
        supervisor_api.announce_mcp_discovery(
            secret_path, 9583, "test-token", lambda _: None, lambda _: None
        )
        return supervisor.posts

    def test_posts_the_reachable_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        posts = self._announce(monkeypatch, _FakeSupervisor())
        assert posts == [
            (
                "http://supervisor/discovery",
                {
                    "service": "mcp",
                    "config": {"url": "http://local-ha-mcp-dev:9583/private_abc12345"},
                },
            )
        ]

    def test_trailing_slash_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        posts = self._announce(
            monkeypatch, _FakeSupervisor(), secret_path="/my-custom-path/"
        )
        assert (
            posts[0][1]["config"]["url"]
            == "http://local-ha-mcp-dev:9583/my-custom-path"
        )

    @pytest.mark.parametrize("version", ["2026.9.4", "landingpage", "None"])
    def test_older_or_unknown_core_is_not_announced_to(
        self, monkeypatch: pytest.MonkeyPatch, version: str
    ) -> None:
        assert self._announce(monkeypatch, _FakeSupervisor(core_version=version)) == []

    @pytest.mark.parametrize(
        "version", ["2026.10.0", "2026.10.0b0", "2026.11.0.dev202610010000", "2027.1.0"]
    )
    def test_supported_core_versions(self, version: str) -> None:
        assert supervisor_api.core_supports_mcp_discovery(version) is True

    def test_missing_hostname_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._announce(monkeypatch, _FakeSupervisor(hostname="")) == []

    def test_supervisor_failure_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(req: urllib.request.Request, timeout: float | None = None) -> Any:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(supervisor_api.urllib.request, "urlopen", refuse)
        warnings: list[str] = []
        supervisor_api.announce_mcp_discovery(
            "/private_abc12345", 9583, "test-token", lambda _: None, warnings.append
        )
        assert len(warnings) == 1

    @pytest.mark.parametrize(
        "flavor", ["homeassistant-addon", "homeassistant-addon-dev"]
    )
    def test_both_flavors_declare_the_service(self, flavor: str) -> None:
        import yaml

        config = yaml.safe_load(
            (Path(__file__).parents[2] / flavor / "config.yaml").read_text()
        )
        # The Supervisor rejects the POST for an undeclared service.
        assert config["discovery"] == ["mcp"]
