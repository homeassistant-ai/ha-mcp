"""Unit tests for the admin-only settings-UI panel + proxy (issue #1527).

The panel gives every install type the add-on's "Open Web UI" experience without
exposing the loopback secret path: an admin-gated session endpoint mints a
short-lived HttpOnly cookie, and a cookie-validated proxy forwards to the
in-process server's settings routes.

Requests go over HTTP to the views bound in a real ``hass``; the settings server
behind the proxy is a real aiohttp server on loopback.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import secrets
import shutil
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from aiohttp import web
from freezegun.api import FrozenDateTimeFactory
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_READ_ONLY
from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockUser
from pytest_homeassistant.typing import ClientSessionGenerator
from yarl import URL

from custom_components.ha_mcp_tools import ui_panel
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK,
    DOMAIN,
)
from custom_components.ha_mcp_tools.mcp_webhook import (
    async_unregister_webhook,
)

from ._garbled_upstream import garbled_upstream
from ._js_harness import HarnessResult, run_script
from ._ui_panel_core import (
    _APP,
    _BASE,
    _SECRET,
    _SESSION,
    _forward_to,
    _mint,
    _proxy_get,
    _SettingsServer,
)
from ._ui_panel_core import (
    admin_cookies as admin_cookies,
)
from ._ui_panel_core import (
    http_ready as http_ready,
)
from ._ui_panel_core import (
    panel_views as panel_views,
)
from ._ui_panel_core import (
    running_server as running_server,
)
from ._ui_panel_core import (
    settings_server as settings_server,
)

_BOOT = f"{_BASE}/boot"
_LOCALE_COOKIE = "ha_mcp_locale"

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

    async def test_failed_upstream_maps_to_502_without_the_secret_path(
        self,
        hass: HomeAssistant,
        admin_cookies: dict[str, str],
        hass_client_no_auth: ClientSessionGenerator,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The proxy's error quotes the target URL, whose path is the server's
        secret; neither the reply nor the log may repeat it."""
        await async_unregister_webhook(hass)
        async with garbled_upstream() as port:
            await _forward_to(hass, port)
            client = await hass_client_no_auth()

            resp = await client.get(_APP + "settings", cookies=admin_cookies)

        assert resp.status == 502
        assert "upstream request failed" in caplog.text
        assert _SECRET not in caplog.text
        assert _SECRET not in await resp.text()

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
        release = asyncio.Event()

        async def events(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: a\n\n")
            await release.wait()
            await response.write(b"data: b\n\n")
            await response.write_eof()
            return response

        running_server.respond = events
        client = await hass_client_no_auth()

        try:
            resp = await asyncio.wait_for(
                client.get(_APP + "api/settings/stream", cookies=admin_cookies), 5
            )
            assert resp.headers["X-Accel-Buffering"] == "no"
            assert resp.content_type == "text/event-stream"
            assert await asyncio.wait_for(resp.content.readany(), 5) == b"data: a\n\n"
            release.set()
            assert await asyncio.wait_for(resp.read(), 5) == b"data: b\n\n"
        finally:
            release.set()


# ---------------------------------------------------------------------------
# Boot view + panel registration
# ---------------------------------------------------------------------------


class _Scripts(HTMLParser):
    """The text of every ``<script>`` element of a page, as a browser reads it."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self._inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inside = tag == "script"
        if self._inside:
            self.scripts.append("")

    def handle_endtag(self, tag: str) -> None:
        self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside:
            self.scripts[-1] += data


def _boot_script(page: str) -> str:
    """The script the served boot page runs."""
    parser = _Scripts()
    parser.feed(page)
    [script] = parser.scripts
    return script


# The boot page's own DOM: the status element it writes to and the iframe it
# fills. The script runs as soon as it is parsed, so both must exist.
_BOOT_DOM = """<!DOCTYPE html><html><body>
<div class="msg"></div><iframe></iframe>
</body></html>"""

# What the boot script fetches: the session it mints, then the app URL it
# probes before pointing the iframe at it.
_BOOT_FETCHES = {
    "/api/ha_mcp_tools/ui/session": {"status": 200, "json": {}},
    "/api/ha_mcp_tools/ui/app/settings": {"status": 200, "body": "<!doctype html>"},
}


class _IframeSrc(HTMLParser):
    """The ``src`` the boot page's iframe was pointed at."""

    def __init__(self) -> None:
        super().__init__()
        self.src: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "iframe" and self.src is None:
            self.src = dict(attrs).get("src")


def _parent_prelude(search: str, language: str = "en") -> str:
    """Replace the boot script's parent frame with a fake frontend window."""
    return (
        "window.__parent = {"
        f"  location: {{ search: {json.dumps(search)} }},"
        "  document: { querySelector: (sel) => sel === 'home-assistant'"
        f"    ? {{ hass: {{ language: {json.dumps(language)},"
        "      auth: { accessToken: 'bearer' } } } : null },"
        "  addEventListener: (type, fn) => { window.__parentHandlers[type] = fn; },"
        "};"
        "window.__parentHandlers = {};"
        "Object.defineProperty(window, 'parent', {"
        "  configurable: true, get: () => window.__parent });"
    )


def _boot_iframe(
    script: str, *, search: str, invoke: str = "", language: str = "en"
) -> tuple[str, HarnessResult]:
    """Run the boot script against ``search`` and return the iframe's target."""
    result = run_script(
        script,
        initial_html=_BOOT_DOM,
        prelude=_parent_prelude(search, language),
        fetch_map=_BOOT_FETCHES,
        invoke=invoke,
        settle_ms=2000,
        timeout_s=30,
    )
    parser = _IframeSrc()
    parser.feed(result.dom)
    return parser.src or "", result


@pytest.fixture
async def boot_script(
    panel_views: None, hass_client_no_auth: ClientSessionGenerator
) -> str:
    """The script of the served boot page, as a browser would run it."""
    client = await hass_client_no_auth()
    page = await (await client.get(_BOOT)).text()
    return _boot_script(page)


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
        assert _boot_script(page)
        assert _SESSION in page
        # The script builds the app URL as this prefix + "settings".
        assert _APP in page
        assert "root.hass.language" in page
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

    async def test_boot_script_is_valid_javascript(
        self,
        panel_views: None,
        hass_client_no_auth: ClientSessionGenerator,
        tmp_path: Path,
    ) -> None:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not available")
        client = await hass_client_no_auth()
        page = await (await client.get(_BOOT)).text()
        script = tmp_path / "boot.js"
        script.write_text(_boot_script(page), encoding="utf-8")

        check = await asyncio.create_subprocess_exec(
            node, "--check", str(script), stderr=asyncio.subprocess.PIPE
        )
        _, errors = await check.communicate()

        assert check.returncode == 0, errors.decode()


class TestBootLink:
    """#3104: a notification link opens its tab and approval card.

    On an embedded install the panel route is the only deep-linkable target
    that needs no secret path, so the iframe's ``src`` is the URL the settings
    app actually loads.
    """

    async def test_a_link_opens_its_tab_and_approval_card(
        self, boot_script: str
    ) -> None:
        token = secrets.token_urlsafe(24)

        src, result = _boot_iframe(
            boot_script, search=f"?tab=tool-security-policies&token={token}"
        )

        assert src == (
            "/api/ha_mcp_tools/ui/app/settings"
            f"?tab=tool-security-policies&token={token}&ha_lang=en"
        ), result.errors

    async def test_foreign_parameters_never_reach_the_app(
        self, boot_script: str
    ) -> None:
        token = secrets.token_urlsafe(24)

        src, result = _boot_iframe(
            boot_script,
            search=f"?tab=tools&drop=me&approve=1&action=approve&token={token}",
        )

        assert src == (
            f"/api/ha_mcp_tools/ui/app/settings?tab=tools&token={token}&ha_lang=en"
        ), result.errors

    async def test_a_crafted_token_never_reaches_the_app(
        self, boot_script: str
    ) -> None:
        src, result = _boot_iframe(
            boot_script,
            search='?tab=tool-security-policies&token=x"],[data-pending-token^="As2-',
        )

        assert src == (
            "/api/ha_mcp_tools/ui/app/settings?tab=tool-security-policies&ha_lang=en"
        ), result.errors

    async def test_a_crafted_tab_never_reaches_the_app(self, boot_script: str) -> None:
        src, result = _boot_iframe(
            boot_script, search='?tab=x"],[data-panel=tool-security-policies'
        )

        assert src == "/api/ha_mcp_tools/ui/app/settings?ha_lang=en", result.errors

    async def test_a_link_followed_while_the_panel_is_open_updates_the_app(
        self, boot_script: str
    ) -> None:
        token = secrets.token_urlsafe(24)

        # The initial load settles first (100 ms of virtual time), then the
        # parent navigates, as it does when a notification link is opened with
        # the panel already showing.
        src, result = _boot_iframe(
            boot_script,
            search="?tab=tools",
            invoke=(
                "setTimeout(() => {"
                "  window.__parent.location.search ="
                f" '?tab=tool-security-policies&token={token}';"
                "  window.__parentHandlers['location-changed']();"
                "}, 100);"
            ),
        )

        assert src == (
            "/api/ha_mcp_tools/ui/app/settings"
            f"?tab=tool-security-policies&token={token}&ha_lang=en"
        ), (result.errors, src)


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
