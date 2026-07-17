"""Unit tests for the admin-only settings-UI panel + proxy (issue #1527).

The panel gives every install type the add-on's "Open Web UI" experience without
exposing the loopback secret path: an admin-gated session endpoint mints a
short-lived HttpOnly cookie, and a cookie-validated proxy forwards to the
in-process server's settings routes. Home Assistant / aiohttp are stubbed via
``_embedded_stubs``.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import FakeSession, FakeUpstream, install

install()

import custom_components.ha_mcp_tools.ui_panel as ui_panel  # noqa: E402
from custom_components.ha_mcp_tools.const import DATA_WEBHOOK, DOMAIN  # noqa: E402

_TARGET = "http://127.0.0.1:9584/private_abc"


def _make_user(
    *,
    user_id: str = "u1",
    is_admin: bool = True,
    is_active: bool = True,
    system_generated: bool = False,
) -> MagicMock:
    user = MagicMock(name="user")
    user.id = user_id
    user.is_admin = is_admin
    user.is_active = is_active
    user.system_generated = system_generated
    return user


def _make_hass(*, user: MagicMock | None = None) -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}
    hass.auth.async_get_user = AsyncMock(return_value=user)
    return hass


def _make_request(
    *,
    hass: MagicMock,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    query_string: str = "",
    scheme: str = "https",
    hass_user: MagicMock | None = None,
    body: bytes = b"",
) -> MagicMock:
    req = MagicMock(name="request")
    req.method = method
    req.headers = dict(headers or {})
    req.cookies = dict(cookies or {})
    req.query_string = query_string
    req.scheme = scheme
    req.app = {"hass": hass}
    req.read = AsyncMock(return_value=body)
    req.get = MagicMock(
        side_effect=lambda key, default=None: {"hass_user": hass_user}.get(key, default)
    )
    return req


def _running_hass(session: FakeSession) -> MagicMock:
    hass = _make_hass(user=_make_user())
    hass.data[DOMAIN] = {DATA_WEBHOOK: {"target_url": _TARGET, "session": session}}
    return hass


# ---------------------------------------------------------------------------
# Session endpoint
# ---------------------------------------------------------------------------


class TestSessionMint:
    async def test_admin_gets_httponly_strict_scoped_cookie(self):
        hass = _make_hass()
        user = _make_user(user_id="admin-1", is_admin=True)
        request = _make_request(hass=hass, method="POST", hass_user=user)

        resp = await ui_panel._SessionView().post(request)

        assert resp.status == 200
        cookie = resp.cookies[ui_panel._COOKIE_NAME]
        assert cookie["httponly"] is True
        assert cookie["samesite"] == "Strict"
        assert cookie["path"] == ui_panel._COOKIE_PATH
        assert cookie["max_age"] == ui_panel._SESSION_TTL_SECONDS
        # The token is recorded server-side, mapped to the admin user.
        store = hass.data[ui_panel._SESSIONS_KEY]
        assert store[cookie["value"]]["user_id"] == "admin-1"

    async def test_non_admin_is_refused_without_minting(self):
        hass = _make_hass()
        user = _make_user(is_admin=False)
        request = _make_request(hass=hass, method="POST", hass_user=user)

        resp = await ui_panel._SessionView().post(request)

        assert resp.status == 403
        assert (
            ui_panel._SESSIONS_KEY not in hass.data
            or not hass.data[ui_panel._SESSIONS_KEY]
        )

    async def test_missing_user_is_refused(self):
        hass = _make_hass()
        request = _make_request(hass=hass, method="POST", hass_user=None)
        resp = await ui_panel._SessionView().post(request)
        assert resp.status == 403

    async def test_cookie_secure_only_on_https(self):
        hass = _make_hass()
        user = _make_user()
        https = _make_request(hass=hass, method="POST", hass_user=user, scheme="https")
        http = _make_request(hass=hass, method="POST", hass_user=user, scheme="http")

        https_resp = await ui_panel._SessionView().post(https)
        http_resp = await ui_panel._SessionView().post(http)

        assert https_resp.cookies[ui_panel._COOKIE_NAME]["secure"] is True
        assert http_resp.cookies[ui_panel._COOKIE_NAME]["secure"] is False

    async def test_forwarded_proto_marks_cookie_secure(self):
        hass = _make_hass()
        user = _make_user()
        request = _make_request(
            hass=hass,
            method="POST",
            hass_user=user,
            scheme="http",
            headers={"X-Forwarded-Proto": "https"},
        )
        resp = await ui_panel._SessionView().post(request)
        assert resp.cookies[ui_panel._COOKIE_NAME]["secure"] is True


class TestSessionValidation:
    async def test_valid_admin_session_passes(self):
        hass = _make_hass(user=_make_user(is_admin=True))
        token = ui_panel._mint_session(hass, "u1")
        assert await ui_panel._session_user_is_admin(hass, token) is True

    async def test_unknown_token_fails(self):
        hass = _make_hass(user=_make_user())
        assert await ui_panel._session_user_is_admin(hass, "nope") is False

    async def test_missing_token_fails(self):
        hass = _make_hass(user=_make_user())
        assert await ui_panel._session_user_is_admin(hass, None) is False

    async def test_expired_session_fails_and_is_dropped(self):
        hass = _make_hass(user=_make_user())
        token = ui_panel._mint_session(hass, "u1")
        hass.data[ui_panel._SESSIONS_KEY][token]["expires"] = time.monotonic() - 1

        assert await ui_panel._session_user_is_admin(hass, token) is False
        assert token not in hass.data[ui_panel._SESSIONS_KEY]

    async def test_demoted_user_fails_and_is_dropped(self):
        hass = _make_hass(user=_make_user(is_admin=False))
        token = ui_panel._mint_session(hass, "u1")

        assert await ui_panel._session_user_is_admin(hass, token) is False
        assert token not in hass.data[ui_panel._SESSIONS_KEY]

    async def test_deleted_user_fails(self):
        hass = _make_hass(user=None)
        token = ui_panel._mint_session(hass, "u1")
        assert await ui_panel._session_user_is_admin(hass, token) is False

    async def test_deleted_user_session_entry_is_dropped(self):
        # Review gap: rejection must also evict the session entry (like the
        # demoted-admin sibling) so a later re-add of the user id cannot ride
        # the stale token.
        hass = _make_hass(user=None)
        token = ui_panel._mint_session(hass, "u1")
        await ui_panel._session_user_is_admin(hass, token)
        assert token not in ui_panel._sessions(hass)

    async def test_inactive_user_fails_and_is_dropped(self):
        # Same acceptance bar as the ha_auth webhook gate (review finding).
        hass = _make_hass(user=_make_user(is_admin=True, is_active=False))
        token = ui_panel._mint_session(hass, "u1")
        assert await ui_panel._session_user_is_admin(hass, token) is False
        assert token not in ui_panel._sessions(hass)

    async def test_system_generated_user_fails_and_is_dropped(self):
        hass = _make_hass(user=_make_user(is_admin=True, system_generated=True))
        token = ui_panel._mint_session(hass, "u1")
        assert await ui_panel._session_user_is_admin(hass, token) is False
        assert token not in ui_panel._sessions(hass)


# ---------------------------------------------------------------------------
# Proxy view
# ---------------------------------------------------------------------------


def _valid_cookie(hass: MagicMock) -> dict[str, str]:
    token = ui_panel._mint_session(hass, "u1")
    return {ui_panel._COOKIE_NAME: token}


class TestProxyForwarding:
    async def test_missing_cookie_returns_401(self):
        hass = _running_hass(FakeSession(upstream=FakeUpstream()))
        request = _make_request(hass=hass)  # no cookie
        resp = await ui_panel._ProxyView().get(request, "settings")
        assert resp.status == 401

    async def test_server_down_returns_503(self):
        hass = _make_hass(user=_make_user())  # no DATA_WEBHOOK
        request = _make_request(hass=hass, cookies=_valid_cookie(hass))
        resp = await ui_panel._ProxyView().get(request, "settings")
        assert resp.status == 503

    async def test_path_traversal_rejected(self):
        hass = _running_hass(FakeSession(upstream=FakeUpstream()))
        request = _make_request(hass=hass, cookies=_valid_cookie(hass))
        resp = await ui_panel._ProxyView().get(request, "../secrets")
        assert resp.status == 400

    async def test_forwards_with_cfg_from_local_only_setup(self, monkeypatch):
        # #1803 end-to-end at unit level: the forwarding config stored by
        # async_register_webhook(register_endpoint=False) must be directly
        # consumable by the panel proxy — a cfg key rename on either side of
        # the seam would 503 the sidebar panel again with both halves' own
        # tests still green.
        from custom_components.ha_mcp_tools import mcp_webhook as mw
        from custom_components.ha_mcp_tools.const import DATA_WEBHOOK_ID

        upstream = FakeUpstream(
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=b"<html>settings</html>",
        )
        session = FakeSession(upstream=upstream)
        monkeypatch.setattr(mw.aiohttp, "ClientSession", lambda **kw: session)
        hass = _make_hass(user=_make_user())
        entry = MagicMock()
        entry.data = {DATA_WEBHOOK_ID: "wh-seam"}

        await mw.async_register_webhook(
            hass,
            entry,
            port=9584,
            secret_path="/private_x",
            auth_mode=mw.WEBHOOK_AUTH_NONE,
            register_endpoint=False,
        )
        request = _make_request(hass=hass, cookies=_valid_cookie(hass))

        resp = await ui_panel._ProxyView().get(request, "settings")

        assert resp.status == 200
        assert session.calls[0]["url"] == "http://127.0.0.1:9584/private_x/settings"

    async def test_forwards_page_and_passes_through_html(self):
        upstream = FakeUpstream(
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=b"<html>settings</html>",
        )
        session = FakeSession(upstream=upstream)
        hass = _running_hass(session)
        request = _make_request(hass=hass, cookies=_valid_cookie(hass))

        resp = await ui_panel._ProxyView().get(request, "settings")

        assert resp.status == 200
        assert resp.body == b"<html>settings</html>"
        # Content-Type passes through unchanged (NOT coerced to JSON like the
        # webhook proxy does for untrusted MCP clients).
        assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
        # Forwarded to the loopback settings route under the secret path.
        assert session.calls[0]["url"] == f"{_TARGET}/settings"

    async def test_relay_strips_encoding_and_length_response_headers(self):
        # aiohttp's read() transparently DECOMPRESSES the upstream body, so
        # relaying the upstream's Content-Encoding / Content-Length /
        # Transfer-Encoding would make the browser re-inflate an already
        # inflated body (every gzipped settings response renders as garbage).
        # Pin the response-side stripping (the request side is pinned above).
        upstream = FakeUpstream(
            status=200,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Content-Encoding": "gzip",
                "Content-Length": "999",
                "Transfer-Encoding": "chunked",
                "Cache-Control": "no-store",
            },
            body=b"<html>inflated</html>",
        )
        session = FakeSession(upstream=upstream)
        hass = _running_hass(session)
        request = _make_request(hass=hass, cookies=_valid_cookie(hass))

        resp = await ui_panel._ProxyView().get(request, "settings")

        assert resp.status == 200
        assert "Content-Encoding" not in resp.headers
        assert "Transfer-Encoding" not in resp.headers
        # aiohttp recomputes Content-Length from the actual body if at all;
        # the stale upstream value must not survive the relay.
        assert resp.headers.get("Content-Length") != "999"
        # Benign headers still pass through.
        assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
        assert resp.headers["Cache-Control"] == "no-store"

    async def test_forwards_subpath_with_query_and_strips_sensitive_headers(self):
        session = FakeSession(upstream=FakeUpstream(body=b"{}"))
        hass = _running_hass(session)
        request = _make_request(
            hass=hass,
            method="GET",
            cookies=_valid_cookie(hass),
            query_string="name=x",
            headers={
                "Authorization": "Bearer leak",
                "Cookie": "ha_mcp_tools_ui_session=leak",
                "Host": "example",
                "Accept": "application/json",
            },
        )

        await ui_panel._ProxyView().get(request, "api/settings/backups")

        call = session.calls[0]
        assert call["url"] == f"{_TARGET}/api/settings/backups?name=x"
        fwd = {k.lower() for k in call["headers"]}
        assert "authorization" not in fwd
        assert "cookie" not in fwd
        assert "host" not in fwd
        assert "accept" in fwd  # innocuous headers pass through

    async def test_forwards_only_allowlisted_locale_cookie(self):
        session = FakeSession(upstream=FakeUpstream(body=b"{}"))
        hass = _running_hass(session)
        cookies = {
            **_valid_cookie(hass),
            ui_panel._LOCALE_COOKIE_NAME: "ru-RU",
            "other_browser_cookie": "must-not-leak",
        }
        request = _make_request(
            hass=hass,
            cookies=cookies,
            headers={
                "Cookie": (
                    "ha_mcp_tools_ui_session=secret; ha_mcp_locale=ru-RU; "
                    "other_browser_cookie=must-not-leak"
                )
            },
        )

        await ui_panel._ProxyView().get(request, "settings")

        forwarded = session.calls[0]["headers"]
        assert forwarded["Cookie"] == "ha_mcp_locale=ru-RU"
        assert "ha_mcp_tools_ui_session" not in forwarded["Cookie"]
        assert "other_browser_cookie" not in forwarded["Cookie"]

    @pytest.mark.parametrize("locale", ["ru; admin=true", "x" * 65])
    async def test_rejects_unsafe_locale_cookie(self, locale: str):
        session = FakeSession(upstream=FakeUpstream(body=b"{}"))
        hass = _running_hass(session)
        cookies = {**_valid_cookie(hass), ui_panel._LOCALE_COOKIE_NAME: locale}
        request = _make_request(hass=hass, cookies=cookies)

        await ui_panel._ProxyView().get(request, "settings")

        forwarded = {key.lower() for key in session.calls[0]["headers"]}
        assert "cookie" not in forwarded

    async def test_post_body_is_forwarded(self):
        session = FakeSession(upstream=FakeUpstream(body=b"{}"))
        hass = _running_hass(session)
        request = _make_request(
            hass=hass,
            method="POST",
            cookies=_valid_cookie(hass),
            body=b'{"tools":[]}',
        )
        await ui_panel._ProxyView().post(request, "api/settings/tools")
        assert session.calls[0]["data"] == b'{"tools":[]}'
        assert session.calls[0]["method"] == "POST"

    async def test_upstream_client_error_maps_to_502(self):
        from ._embedded_stubs import ClientError

        session = FakeSession(exc=ClientError("boom"))
        hass = _running_hass(session)
        request = _make_request(hass=hass, cookies=_valid_cookie(hass))
        resp = await ui_panel._ProxyView().get(request, "settings")
        assert resp.status == 502

    async def test_unexpected_error_maps_to_500(self):
        session = FakeSession(exc=RuntimeError("weird"))
        hass = _running_hass(session)
        request = _make_request(hass=hass, cookies=_valid_cookie(hass))
        resp = await ui_panel._ProxyView().get(request, "settings")
        assert resp.status == 500

    async def test_event_stream_is_streamed_with_anti_buffering(self):
        upstream = FakeUpstream(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            chunks=[b"data: a\n\n", b"data: b\n\n"],
        )
        session = FakeSession(upstream=upstream)
        hass = _running_hass(session)
        request = _make_request(hass=hass, cookies=_valid_cookie(hass))

        resp = await ui_panel._ProxyView().get(request, "api/settings/stream")

        assert resp.prepared is True
        assert resp.headers["X-Accel-Buffering"] == "no"
        assert b"".join(resp.written) == b"data: a\n\ndata: b\n\n"
        assert resp.eof is True


# ---------------------------------------------------------------------------
# Boot view + panel registration
# ---------------------------------------------------------------------------


class TestBootView:
    async def test_serves_boot_page(self):
        resp = await ui_panel._BootView().get(_make_request(hass=_make_hass()))
        assert resp.content_type == "text/html"
        assert ui_panel._SESSION_URL.encode() in resp.body
        # The script builds APP_BASE_URL as _APP_PREFIX + "settings", so only the
        # prefix appears literally in the served body.
        assert ui_panel._APP_PREFIX.encode() in resp.body
        assert b"root.hass.language" in resp.body
        assert b"?ha_lang=" in resp.body
        assert b"encodeURIComponent(language)" in resp.body
        assert b"<iframe" in resp.body

    def test_view_auth_model_is_pinned(self):
        # A bare iframe GET cannot carry a bearer: the boot page and the proxy
        # must stay public (the proxy's credential is the session cookie); the
        # session minter must stay behind HA auth. Unit tests call the views
        # directly, so only these assertions catch a requires_auth flip.
        assert ui_panel._BootView.requires_auth is False
        assert ui_panel._SessionView.requires_auth is True
        assert ui_panel._ProxyView.requires_auth is False


class TestPanelRegistration:
    async def test_registers_views_once_and_panel(self):
        hass = _make_hass()
        hass.http = MagicMock()

        await ui_panel.async_register_ui_panel(hass)

        assert hass.http.register_view.call_count == 3
        assert hass.data[ui_panel._VIEWS_REGISTERED_KEY] is True
        panels = hass.data["_fake_frontend_panels"]
        assert ui_panel.PANEL_URL_PATH in panels
        panel = panels[ui_panel.PANEL_URL_PATH]
        assert panel["component_name"] == "iframe"
        assert panel["config"] == {"url": ui_panel._BOOT_URL}
        assert panel["require_admin"] is True

    async def test_second_register_does_not_rebind_views(self):
        hass = _make_hass()
        hass.http = MagicMock()

        await ui_panel.async_register_ui_panel(hass)
        await ui_panel.async_register_ui_panel(hass)

        # Views bound once per session; panel guarded by async_panel_exists.
        assert hass.http.register_view.call_count == 3

    async def test_unregister_removes_panel(self):
        hass = _make_hass()
        hass.http = MagicMock()
        await ui_panel.async_register_ui_panel(hass)

        ui_panel.async_unregister_ui_panel(hass)

        assert ui_panel.PANEL_URL_PATH not in hass.data["_fake_frontend_panels"]


class TestBootPage:
    def test_boot_page_embeds_script_and_session_url(self):
        page = ui_panel.render_boot_page()
        assert ui_panel._SESSION_URL in page
        assert ui_panel.render_boot_script() in page

    def test_boot_script_handles_stale_tokens_without_retry_storm(self):
        # #1802: POSTing a stale bearer in a retry loop trips http.ban and
        # IP-banned users from their own instance. The script must refresh an
        # expired token before use and treat a 401 as terminal (no auto-retry).
        js = ui_panel.render_boot_script()
        assert "refreshAccessToken" in js
        assert "resp.status === 401" in js

    def test_app_url_sits_under_cookie_path(self):
        # The embedded app URL must live under the session cookie's path scope
        # or the browser never attaches the cookie and the settings app 401s.
        # (The script concatenates _APP_PREFIX + "settings", so assert on the
        # prefix and check the scope alignment in Python.)
        assert ui_panel._APP_PREFIX in ui_panel.render_boot_script()
        app_url = ui_panel._APP_PREFIX + "settings"
        assert app_url.startswith(ui_panel._COOKIE_PATH + "/")

    def test_panel_config_shape(self):
        cfg = ui_panel.panel_config()
        assert cfg["component_name"] == "iframe"
        assert cfg["frontend_url_path"] == ui_panel.PANEL_URL_PATH
        assert cfg["require_admin"] is True
        assert cfg["config"] == {"url": ui_panel._BOOT_URL}

    def test_boot_script_is_valid_javascript(self):
        # Parse coverage for the served boot script. It cannot join the
        # _js_harness _PY_RENDERERS set (that discovery runs without Home
        # Assistant installed, and this module imports aiohttp), so it gets its
        # own node syntax check here — skipped cleanly when node is absent.
        import os
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not available")
        js = ui_panel.render_boot_script()
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(js)
            path = handle.name
        try:
            result = subprocess.run(
                [node, "--check", path], capture_output=True, text=True, check=False
            )
        finally:
            os.unlink(path)
        assert result.returncode == 0, result.stderr
