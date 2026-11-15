"""Unit tests for the admin-only settings-UI panel + proxy (issue #1527).

The panel gives every install type the add-on's "Open Web UI" experience without
exposing the loopback secret path: an admin-gated session endpoint mints a
short-lived HttpOnly cookie, and a cookie-validated proxy forwards to the
in-process server's settings routes.

Requests go over HTTP to the views bound in a real ``hass``; the settings server
behind the proxy is a real aiohttp server on loopback.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from freezegun.api import FrozenDateTimeFactory
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_READ_ONLY
from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, MockUser
from pytest_homeassistant.typing import ClientSessionGenerator
from yarl import URL

from custom_components.ha_mcp_tools import ui_panel
from custom_components.ha_mcp_tools.const import DATA_WEBHOOK, DATA_WEBHOOK_ID, DOMAIN
from custom_components.ha_mcp_tools.mcp_webhook import (
    WEBHOOK_AUTH_NONE,
    async_register_webhook,
    async_unregister_webhook,
)

# The HTTP surface the boot page, the session endpoint and the proxy share.
_BASE = "/api/ha_mcp_tools/ui"
_BOOT = f"{_BASE}/boot"
_SESSION = f"{_BASE}/session"
_APP = f"{_BASE}/app/"
_SECRET = "/private_abc"
_LOCALE_COOKIE = "ha_mcp_locale"

_Responder = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass
class _Seen:
    """What the settings server received from the proxy."""

    method: str
    path_qs: str
    headers: dict[str, str]
    body: bytes


@dataclass
class _SettingsServer:
    """The in-process server's settings routes, on a real loopback listener."""

    server: TestServer
    seen: list[_Seen] = field(default_factory=list)
    respond: _Responder | None = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.seen.append(
            _Seen(
                request.method,
                request.path_qs,
                {key.lower(): value for key, value in request.headers.items()},
                await request.read(),
            )
        )
        if self.respond is not None:
            return await self.respond(request)
        return web.json_response({"ok": True})


@pytest.fixture
async def settings_server(
    aiohttp_server: Callable[[web.Application], Awaitable[TestServer]],
    socket_enabled: None,
) -> _SettingsServer:
    holder: list[_SettingsServer] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        return await holder[0].handle(request)

    app = web.Application()
    app.router.add_route("*", _SECRET + "/{tail:.*}", handler)
    server = _SettingsServer(await aiohttp_server(app))
    holder.append(server)
    return server


@pytest.fixture
async def http_ready(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})


@pytest.fixture
async def panel_views(hass: HomeAssistant, http_ready: None) -> None:
    await ui_panel.async_register_ui_panel(hass)


@pytest.fixture
async def running_server(
    hass: HomeAssistant, panel_views: None, settings_server: _SettingsServer
) -> AsyncIterator[_SettingsServer]:
    """The server is up: its forwarding config is stored the way setup stores it."""
    entry = MockConfigEntry(domain=DOMAIN, data={DATA_WEBHOOK_ID: "wh-ui-panel"})
    port = settings_server.server.port
    assert port is not None
    await async_register_webhook(
        hass,
        entry,
        port=port,
        secret_path=_SECRET,
        auth_mode=WEBHOOK_AUTH_NONE,
        register_endpoint=False,
    )
    yield settings_server
    await async_unregister_webhook(hass)


async def _mint(
    hass_client: ClientSessionGenerator, access_token: str | None = None
) -> dict[str, str]:
    """Sign in as the user and return the session cookie the browser would hold."""
    client = await hass_client(access_token) if access_token else await hass_client()
    resp = await client.post(_SESSION)
    assert resp.status == 200
    return {name: morsel.value for name, morsel in resp.cookies.items()}


async def _proxy_get(
    hass_client_no_auth: ClientSessionGenerator,
    cookies: dict[str, str] | None,
    headers: dict[str, str] | None = None,
) -> tuple[TestClient, int]:
    client = await hass_client_no_auth()
    resp = await client.get(_APP + "settings", cookies=cookies, headers=headers)
    return client, resp.status


# ---------------------------------------------------------------------------
# Session endpoint
# ---------------------------------------------------------------------------


class TestSessionMint:
    async def test_admin_gets_httponly_strict_scoped_cookie(
        self, panel_views: None, hass_client: ClientSessionGenerator
    ) -> None:
        client = await hass_client()

        resp = await client.post(_SESSION)

        assert resp.status == 200
        (cookie,) = resp.cookies.values()
        assert cookie["httponly"] is True
        assert cookie["samesite"] == "Strict"
        # The browser sends it to the settings app, and only there.
        assert (_APP + "settings").startswith(cookie["path"] + "/")
        assert not _SESSION.startswith(cookie["path"] + "/")
        assert int(cookie["max-age"]) == (await resp.json())["ttl"] > 0

    async def test_non_admin_is_refused_without_a_cookie(
        self,
        panel_views: None,
        hass_client: ClientSessionGenerator,
        hass_read_only_access_token: str,
    ) -> None:
        client = await hass_client(hass_read_only_access_token)

        resp = await client.post(_SESSION)

        assert resp.status == 403
        assert not resp.cookies

    async def test_session_without_home_assistant_sign_in_is_refused(
        self, panel_views: None, hass_client_no_auth: ClientSessionGenerator
    ) -> None:
        client = await hass_client_no_auth()

        resp = await client.post(_SESSION)

        assert resp.status == 401
        assert not resp.cookies

    async def test_cookie_is_secure_only_when_reached_over_https(
        self, panel_views: None, hass_client: ClientSessionGenerator
    ) -> None:
        client = await hass_client()

        plain = await client.post(_SESSION)
        proxied = await client.post(_SESSION, headers={"X-Forwarded-Proto": "https"})

        assert not next(iter(plain.cookies.values()))["secure"]
        assert next(iter(proxied.cookies.values()))["secure"] is True


class TestSessionValidation:
    async def test_admin_session_opens_the_proxy(
        self,
        running_server: _SettingsServer,
        hass_client: ClientSessionGenerator,
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        cookies = await _mint(hass_client)

        _, status = await _proxy_get(hass_client_no_auth, cookies)

        assert status == 200

    @pytest.mark.parametrize("cookie_value", [None, "nope"])
    async def test_missing_or_unknown_session_is_refused(
        self,
        running_server: _SettingsServer,
        hass_client: ClientSessionGenerator,
        hass_client_no_auth: ClientSessionGenerator,
        cookie_value: str | None,
    ) -> None:
        cookies = {
            name: cookie_value for name in await _mint(hass_client) if cookie_value
        }

        _, status = await _proxy_get(hass_client_no_auth, cookies)

        assert status == 401
        assert not running_server.seen

    async def test_expired_session_is_refused(
        self,
        running_server: _SettingsServer,
        hass_client: ClientSessionGenerator,
        hass_client_no_auth: ClientSessionGenerator,
        freezer: FrozenDateTimeFactory,
    ) -> None:
        client = await hass_client()
        resp = await client.post(_SESSION)
        ttl = (await resp.json())["ttl"]
        cookies = {name: morsel.value for name, morsel in resp.cookies.items()}

        freezer.tick(timedelta(seconds=ttl - 60))
        _, before = await _proxy_get(hass_client_no_auth, cookies)
        freezer.tick(timedelta(seconds=120))
        _, after = await _proxy_get(hass_client_no_auth, cookies)

        assert (before, after) == (200, 401)

    async def test_demoted_admin_loses_the_session_for_good(
        self,
        running_server: _SettingsServer,
        hass: HomeAssistant,
        hass_admin_user: MockUser,
        hass_client: ClientSessionGenerator,
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        cookies = await _mint(hass_client)
        _, signed_in = await _proxy_get(hass_client_no_auth, cookies)

        await hass.auth.async_update_user(
            hass_admin_user, group_ids=[GROUP_ID_READ_ONLY]
        )
        _, demoted = await _proxy_get(hass_client_no_auth, cookies)
        await hass.auth.async_update_user(hass_admin_user, group_ids=[GROUP_ID_ADMIN])
        _, promoted_again = await _proxy_get(hass_client_no_auth, cookies)

        assert (signed_in, demoted, promoted_again) == (200, 401, 401)

    async def test_deleted_user_loses_the_session(
        self,
        running_server: _SettingsServer,
        hass: HomeAssistant,
        hass_admin_user: MockUser,
        hass_client: ClientSessionGenerator,
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        cookies = await _mint(hass_client)

        await hass.auth.async_remove_user(hass_admin_user)
        _, status = await _proxy_get(hass_client_no_auth, cookies)

        assert status == 401
        assert not running_server.seen

    async def test_deactivated_user_loses_the_session_for_good(
        self,
        running_server: _SettingsServer,
        hass: HomeAssistant,
        hass_admin_user: MockUser,
        hass_client: ClientSessionGenerator,
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        cookies = await _mint(hass_client)

        await hass.auth.async_deactivate_user(hass_admin_user)
        _, deactivated = await _proxy_get(hass_client_no_auth, cookies)
        await hass.auth.async_activate_user(hass_admin_user)
        _, reactivated = await _proxy_get(hass_client_no_auth, cookies)

        assert (deactivated, reactivated) == (401, 401)

    async def test_system_user_session_is_refused(
        self,
        running_server: _SettingsServer,
        hass_client: ClientSessionGenerator,
        hass_client_no_auth: ClientSessionGenerator,
        hass_supervisor_access_token: str,
    ) -> None:
        cookies = await _mint(hass_client, hass_supervisor_access_token)

        _, status = await _proxy_get(hass_client_no_auth, cookies)

        assert status == 401


# ---------------------------------------------------------------------------
# Proxy view
# ---------------------------------------------------------------------------


@pytest.fixture
async def admin_cookies(
    running_server: _SettingsServer, hass_client: ClientSessionGenerator
) -> dict[str, str]:
    return await _mint(hass_client)


class TestProxyForwarding:
    async def test_server_down_returns_503(
        self,
        panel_views: None,
        hass_client: ClientSessionGenerator,
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        cookies = await _mint(hass_client)

        _, status = await _proxy_get(hass_client_no_auth, cookies)

        assert status == 503

    async def test_path_traversal_is_rejected(
        self,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        client = await hass_client_no_auth()

        resp = await client.get(
            URL(_APP + "%2e%2e/secrets", encoded=True), cookies=admin_cookies
        )

        assert resp.status == 400
        assert not running_server.seen

    async def test_forwards_page_and_passes_through_html(
        self,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        async def page(request: web.Request) -> web.StreamResponse:
            return web.Response(body=b"<html>settings</html>", content_type="text/html")

        running_server.respond = page
        client = await hass_client_no_auth()

        resp = await client.get(_APP + "settings", cookies=admin_cookies)

        assert resp.status == 200
        assert await resp.read() == b"<html>settings</html>"
        # Passes through unchanged (NOT coerced to JSON like the webhook proxy
        # does for untrusted MCP clients).
        assert resp.content_type == "text/html"
        # Forwarded to the loopback settings route under the secret path.
        assert running_server.seen[0].path_qs == f"{_SECRET}/settings"

    async def test_relayed_page_is_not_inflated_twice(
        self,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        """A gzipped settings page renders, not garbage: the body is inflated once."""

        async def gzipped(request: web.Request) -> web.StreamResponse:
            return web.Response(
                body=gzip.compress(b"<html>inflated</html>"),
                headers={
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Encoding": "gzip",
                    "Cache-Control": "no-store",
                },
            )

        running_server.respond = gzipped
        client = await hass_client_no_auth()

        resp = await client.get(_APP + "settings", cookies=admin_cookies)

        assert resp.status == 200
        assert await resp.read() == b"<html>inflated</html>"
        assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
        assert resp.headers["Cache-Control"] == "no-store"

    async def test_forwards_subpath_with_query_and_strips_sensitive_headers(
        self,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_access_token: str,
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        client = await hass_client_no_auth()

        await client.get(
            _APP + "api/settings/backups?name=x",
            cookies=admin_cookies,
            headers={
                "Authorization": f"Bearer {hass_access_token}",
                "Host": "example",
                "Accept": "application/json",
            },
        )

        seen = running_server.seen[0]
        assert seen.path_qs == f"{_SECRET}/api/settings/backups?name=x"
        assert "authorization" not in seen.headers
        assert "cookie" not in seen.headers
        assert seen.headers["host"] != "example"
        assert seen.headers["accept"] == "application/json"

    @pytest.mark.parametrize("method", ["post", "put", "delete"])
    async def test_write_requests_forward_method_and_body(
        self,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        method: str,
    ) -> None:
        client = await hass_client_no_auth()

        resp = await getattr(client, method)(
            _APP + "api/settings/tools", cookies=admin_cookies, data=b'{"tools":[]}'
        )

        assert resp.status == 200
        seen = running_server.seen[0]
        assert (seen.method, seen.body) == (method.upper(), b'{"tools":[]}')

    async def test_forwards_only_the_locale_cookie(
        self,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        cookies = {
            **admin_cookies,
            _LOCALE_COOKIE: "ru-RU",
            "other_browser_cookie": "must-not-leak",
        }

        await _proxy_get(hass_client_no_auth, cookies)

        assert running_server.seen[0].headers["cookie"] == "ha_mcp_locale=ru-RU"

    @pytest.mark.parametrize(
        ("locale", "forwarded"),
        [
            ("ru-RU", True),
            ("en_US", True),
            ("a" * 64, True),
            ("-ru", False),
            ("ru-", False),
            ("ru--RU", False),
            ("en__US", False),
            ("", False),
            ("café", False),
            ("٣", False),
            ("a" * 65, False),
            ('"ru; admin=true"', False),
        ],
    )
    async def test_locale_cookie_is_forwarded_only_when_safe(
        self,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        locale: str,
        forwarded: bool,
    ) -> None:
        (session_name,) = admin_cookies
        header = (
            f"{session_name}={admin_cookies[session_name]}; {_LOCALE_COOKIE}={locale}"
        )

        await _proxy_get(hass_client_no_auth, None, headers={"Cookie": header})

        cookie = running_server.seen[0].headers.get("cookie")
        assert cookie == (f"{_LOCALE_COOKIE}={locale}" if forwarded else None)

    async def test_unreachable_server_maps_to_502_without_the_secret_path(
        self,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await running_server.server.close()
        client = await hass_client_no_auth()

        resp = await client.get(_APP + "settings", cookies=admin_cookies)

        assert resp.status == 502
        assert "upstream request failed" in caplog.text
        assert _SECRET not in caplog.text

    async def test_unexpected_error_maps_to_500(
        self,
        hass: HomeAssistant,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        forwarding = hass.data[DOMAIN]
        assert forwarding is not None
        await forwarding[DATA_WEBHOOK]["session"].close()

        _, status = await _proxy_get(hass_client_no_auth, admin_cookies)

        assert status == 500

    async def test_event_stream_is_streamed_with_anti_buffering(
        self,
        running_server: _SettingsServer,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        async def events(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: a\n\n")
            await response.write(b"data: b\n\n")
            await response.write_eof()
            return response

        running_server.respond = events
        client = await hass_client_no_auth()

        resp = await client.get(_APP + "api/settings/stream", cookies=admin_cookies)

        assert resp.headers["X-Accel-Buffering"] == "no"
        assert resp.content_type == "text/event-stream"
        assert await resp.read() == b"data: a\n\ndata: b\n\n"


# ---------------------------------------------------------------------------
# Boot view + panel registration
# ---------------------------------------------------------------------------


class TestBootView:
    async def test_page_is_public_and_embeds_script_and_session_url(
        self, panel_views: None, hass_client_no_auth: ClientSessionGenerator
    ) -> None:
        """A bare iframe GET carries no bearer, so the boot page is not behind auth."""
        client = await hass_client_no_auth()

        resp = await client.get(_BOOT)
        page = await resp.text()

        assert resp.status == 200
        assert resp.content_type == "text/html"
        assert page == ui_panel.render_boot_page()
        assert ui_panel.render_boot_script() in page
        assert _SESSION in page
        # The script builds the app URL as this prefix + "settings".
        assert _APP in page
        assert "root.hass.language" in page
        assert "?ha_lang=" in page
        assert "encodeURIComponent(language)" in page
        assert "<iframe" in page

    async def test_boot_script_handles_stale_tokens_without_retry_storm(
        self, panel_views: None, hass_client_no_auth: ClientSessionGenerator
    ) -> None:
        """#1802: a retried stale bearer got users IP-banned from their instance.

        The script refreshes an expired token before use and treats a 401 as
        terminal (no auto-retry).
        """
        client = await hass_client_no_auth()

        page = await (await client.get(_BOOT)).text()

        assert "refreshAccessToken" in page
        assert "resp.status === 401" in page

    def test_boot_script_is_valid_javascript(self, tmp_path: Path) -> None:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not available")
        script = tmp_path / "boot.js"
        script.write_text(ui_panel.render_boot_script(), encoding="utf-8")

        result = subprocess.run(
            [node, "--check", str(script)], capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stderr


class TestPanelRegistration:
    async def test_registers_the_views_and_an_admin_only_iframe_panel(
        self,
        hass: HomeAssistant,
        http_ready: None,
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        await ui_panel.async_register_ui_panel(hass)

        panel = hass.data[frontend.DATA_PANELS][ui_panel.PANEL_URL_PATH]
        assert panel.component_name == "iframe"
        assert panel.require_admin is True
        client = await hass_client_no_auth()
        # The panel opens the boot page; the other two views are bound as well.
        assert panel.config is not None
        assert (await client.get(panel.config["url"])).status == 200
        assert (await client.post(_SESSION)).status == 401
        assert (await client.get(_APP + "settings")).status == 401

    async def test_second_register_keeps_one_panel_and_does_not_rebind_views(
        self,
        hass: HomeAssistant,
        http_ready: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await ui_panel.async_register_ui_panel(hass)
        await ui_panel.async_register_ui_panel(hass)

        # Binding a view twice fails in aiohttp and is logged by the component.
        assert "failed to register" not in caplog.text
        assert ui_panel.PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]

    async def test_unregister_removes_the_panel_and_keeps_the_views(
        self,
        hass: HomeAssistant,
        panel_views: None,
        hass_client_no_auth: ClientSessionGenerator,
    ) -> None:
        ui_panel.async_unregister_ui_panel(hass)

        assert ui_panel.PANEL_URL_PATH not in hass.data[frontend.DATA_PANELS]
        client = await hass_client_no_auth()
        assert (await client.get(_BOOT)).status == 200
