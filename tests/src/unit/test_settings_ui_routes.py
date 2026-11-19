"""Where the settings page is mounted, and who may reach it there.

Inside the app the page is mounted at the root for Home Assistant ingress,
which only the Supervisor's address may reach, and under the secret path for
direct access. Elsewhere it is mounted only under the secret path, and the
path is advertised to ``ha_get_overview`` where nothing else points at it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.settings_ui import (
    SUPERVISOR_INGRESS_IP,
    get_http_settings_prefix,
    is_http_settings_mounted,
    register_settings_routes,
)

from ._settings_ui_app import app_server, mounted_http, settings_http
from ._settings_ui_app import settings_world as settings_world

TOOLS = "/api/settings/tools"
POLICY = "/api/policy/config"


Mount = Callable[..., FastMCP]


@pytest.fixture
def mount(monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient) -> Mount:
    """``mount(secret_path, **kwargs)``: the settings routes of the real server,
    built from the environment as it is when called, on a FastMCP server."""

    def _mount(secret_path: str, **kwargs: Any) -> FastMCP:
        mcp = FastMCP("settings-test")
        register_settings_routes(
            mcp, app_server(monkeypatch, core_client), secret_path=secret_path, **kwargs
        )
        return mcp

    return _mount


async def _status(mcp: FastMCP, path: str, *, peer: str = "127.0.0.1") -> int:
    async with mounted_http(mcp, peer=peer) as http:
        return (await http.get(path)).status_code


async def _without_peer(mcp: FastMCP, path: str) -> int:
    """GET ``path`` on a connection that reports no peer (a Unix socket)."""
    statuses: list[int] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"settings")],
        "client": None,
        "server": ("settings", 80),
    }
    await mcp.http_app(path="/mcp", stateless_http=True)(scope, receive, send)
    return statuses[0]


def _advertise_a_stale_prefix(mount: Mount) -> None:
    mount("/stale")
    assert get_http_settings_prefix() == "/stale"


@pytest.fixture
def in_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "app-token")


class TestMounts:
    async def test_the_app_serves_the_page_to_ingress_and_under_the_secret(
        self, monkeypatch: pytest.MonkeyPatch, mount: Mount
    ) -> None:
        """#2236: the app's users open the page through ingress, so the secret
        path is not handed to MCP clients just to advertise it again."""
        _advertise_a_stale_prefix(mount)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "app-token")

        mcp = mount("/private_x")

        for path in ("/", "/settings", TOOLS):
            assert await _status(mcp, path, peer=SUPERVISOR_INGRESS_IP) == 200
        for path in ("/private_x/settings", f"/private_x{TOOLS}"):
            assert await _status(mcp, path) == 200
        assert get_http_settings_prefix() is None

    async def test_outside_the_app_only_the_secret_path_is_served_and_advertised(
        self, mount: Mount
    ) -> None:
        mcp = mount("/mcp")

        assert await _status(mcp, "/settings") == 404
        assert await _status(mcp, "/mcp/settings") == 200
        assert await _status(mcp, f"/mcp{TOOLS}") == 200
        assert get_http_settings_prefix() == "/mcp"

    async def test_the_in_process_server_serves_its_settings_path_unadvertised(
        self, monkeypatch: pytest.MonkeyPatch, mount: Mount
    ) -> None:
        """Home Assistant has its own entry point to the in-process server's page;
        HAOS gives that process a Supervisor token too."""
        _advertise_a_stale_prefix(mount)
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "core-token")

        mcp = mount("/private_x")

        assert await _status(mcp, "/settings", peer=SUPERVISOR_INGRESS_IP) == 404
        assert await _status(mcp, "/private_x/settings") == 200
        assert get_http_settings_prefix() is None

    async def test_without_the_app_or_a_secret_nothing_is_mounted(
        self, mount: Mount
    ) -> None:
        """The routes have no auth of their own, so they are never public."""
        mcp = mount("")

        assert await _status(mcp, "/settings") == 404
        assert get_http_settings_prefix() is None
        assert is_http_settings_mounted() is False

    async def test_a_secret_path_with_route_braces_is_not_mounted(
        self, mount: Mount
    ) -> None:
        """GHSA-mx64-982r-65vg: an unrendered /private_{x} would match any path."""
        mcp = mount("/private_{x}")

        assert await _status(mcp, "/private_anything/settings") == 404
        assert get_http_settings_prefix() is None

    async def test_an_unadvertised_secret_is_served_but_not_handed_out(
        self, mount: Mount
    ) -> None:
        """OAuth/OIDC mount the page under a secret of its own (GHSA-mx64-982r-65vg),
        which ha_get_overview must not give every MCP client; the page is still
        served over HTTP, not by the stdio sidecar."""
        _advertise_a_stale_prefix(mount)

        mcp = mount("/private_s", advertise_prefix=False)

        assert await _status(mcp, "/private_s/settings") == 200
        assert get_http_settings_prefix() is None
        assert is_http_settings_mounted() is True


class TestStdioSection:
    """The sidecar-port settings show only where the page is the stdio sidecar."""

    async def _is_stdio(self) -> bool:
        async with settings_http() as http:
            response = await http.get("/api/settings/advanced")
        result: bool = response.json()["is_stdio"]
        return result

    async def test_the_sidecar_is_stdio(self) -> None:
        assert await self._is_stdio() is True

    async def test_an_http_mount_is_not_stdio(self, mount: Mount) -> None:
        mount("/private_x")

        assert await self._is_stdio() is False

    async def test_an_unadvertised_http_mount_is_not_stdio(self, mount: Mount) -> None:
        """GHSA-mx64-982r-65vg left the prefix unset on a real HTTP mount."""
        mount("/private_x", advertise_prefix=False)

        assert await self._is_stdio() is False


@pytest.mark.usefixtures("in_the_app")
class TestIngressOnlyRoot:
    """The root mount carries no secret: only the Supervisor (ingress) may use it,
    judged by the connection's peer and never by a header the caller sets."""

    async def test_a_direct_caller_cannot_save_through_the_root(
        self, tmp_path: Path, mount: Mount
    ) -> None:
        mcp = mount("/private_x")

        async with mounted_http(mcp) as http:
            response = await http.post(TOOLS, json={"states": {"ha_x": "disabled"}})

        assert response.status_code == 403
        assert not (tmp_path / "tool_config.json").exists()

    async def test_ingress_saves_through_the_root(
        self, tmp_path: Path, mount: Mount
    ) -> None:
        mcp = mount("/private_x")

        async with mounted_http(mcp, peer=SUPERVISOR_INGRESS_IP) as http:
            response = await http.post(TOOLS, json={"states": {"ha_x": "disabled"}})

        assert response.status_code == 200
        assert (tmp_path / "tool_config.json").exists()

    async def test_a_connection_without_a_peer_is_refused(self, mount: Mount) -> None:
        assert await _without_peer(mount("/private_x"), TOOLS) == 403

    async def test_a_forged_forwarded_for_header_does_not_pass(
        self, tmp_path: Path, mount: Mount
    ) -> None:
        mcp = mount("/private_x")

        async with mounted_http(mcp) as http:
            response = await http.post(
                TOOLS,
                json={"states": {"ha_x": "disabled"}},
                headers={"X-Forwarded-For": SUPERVISOR_INGRESS_IP},
            )

        assert response.status_code == 403
        assert not (tmp_path / "tool_config.json").exists()

    async def test_the_root_answers_ingress_what_the_secret_path_answers(
        self, mount: Mount
    ) -> None:
        mcp = mount("/private_x")

        async with mounted_http(mcp, peer=SUPERVISOR_INGRESS_IP) as http:
            root = (await http.get(TOOLS)).json()
        async with mounted_http(mcp) as http:
            secret = (await http.get(f"/private_x{TOOLS}")).json()

        assert root == secret

    async def test_a_direct_caller_cannot_rewrite_the_policy_through_the_root(
        self, mount: Mount
    ) -> None:
        mcp = mount("/private_x")

        async with mounted_http(mcp) as http:
            response = await http.put(POLICY, json={"rules": []})

        assert response.status_code == 403

    async def test_the_page_itself_is_refused_to_a_direct_caller(
        self, mount: Mount
    ) -> None:
        """The "/" front door is mounted apart from the route table."""
        assert await _status(mount("/private_x"), "/") == 403

    async def test_the_secret_path_needs_no_ingress(self, mount: Mount) -> None:
        mcp = mount("/private_x")

        async with mounted_http(mcp) as http:
            response = await http.put(f"/private_x{POLICY}", json={"rules": []})

        assert response.status_code == 200


async def test_outside_the_app_the_secret_path_needs_no_ingress(mount: Mount) -> None:
    """The secret is the only auth there: a guard would refuse every client."""
    mcp = mount("/private_x")

    async with mounted_http(mcp) as http:
        response = await http.put(f"/private_x{POLICY}", json={"rules": []})

    assert await _status(mcp, "/", peer=SUPERVISOR_INGRESS_IP) == 404
    assert response.status_code == 200
