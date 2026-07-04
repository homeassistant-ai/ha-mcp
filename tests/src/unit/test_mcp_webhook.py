"""Unit tests for the in-process embedded MCP server webhook ingress (#1527).

Covers the forwarding handler (header stripping, SSE streaming, session-id
propagation, content-type coercion, error mapping), the two auth postures
(``none`` secret-URL vs ``ha_auth`` bearer), the RFC 8414 / RFC 9728 discovery
views, and the register/unregister lifecycle.

Home Assistant and aiohttp are stubbed via ``_embedded_stubs`` (imported first so
the fakes are installed before ``mcp_webhook`` binds them), mirroring
``test_addon_bootstrap.py``'s sys.modules approach.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ._embedded_stubs import FakeSession, FakeUpstream, install, make_request

# Install the HA / aiohttp sys.modules stubs and put homeassistant-integration/
# on sys.path BEFORE importing the integration modules below. This statement is
# also an isort barrier so the component imports are never reordered above it
# (which would import mcp_webhook before the stubs exist).
install()

import ha_mcp_server.mcp_webhook as mw  # noqa: E402
from ha_mcp_server.const import (  # noqa: E402
    DATA_WEBHOOK,
    DATA_WEBHOOK_ID,
    DOMAIN,
    OAUTH_BASE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)

TARGET_URL = "http://127.0.0.1:9584/private_aaaaaaaaaaaaaaaa"
WEBHOOK_ID = "mcp_0123456789abcdef0123456789abcdef"


def _make_hass(*, validate_result=None, validate_side_effect=None) -> MagicMock:
    """Build a fake hass with an auth token validator and empty data."""
    hass = MagicMock(name="hass")
    hass.data = {}
    if validate_side_effect is not None:
        hass.auth.async_validate_access_token = MagicMock(
            side_effect=validate_side_effect
        )
    else:
        hass.auth.async_validate_access_token = MagicMock(return_value=validate_result)
    return hass


def _store_cfg(
    hass: MagicMock,
    *,
    session: FakeSession,
    auth_mode: str = WEBHOOK_AUTH_NONE,
    resource_server: object | None = None,
) -> None:
    hass.data[DOMAIN] = {
        DATA_WEBHOOK: {
            "webhook_id": WEBHOOK_ID,
            "target_url": TARGET_URL,
            "session": session,
            "auth_mode": auth_mode,
            "resource_server": resource_server,
        }
    }


# ---------------------------------------------------------------------------
# Forwarding handler — no-auth (secret URL) posture
# ---------------------------------------------------------------------------


class TestForwardingHandler:
    async def test_missing_config_returns_503(self):
        hass = _make_hass()  # no DATA_WEBHOOK stored
        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request())
        assert resp.status == 503

    async def test_post_forwards_method_body_and_target(self):
        upstream = FakeUpstream(
            status=200, headers={"Content-Type": "application/json"}, body=b'{"ok":1}'
        )
        session = FakeSession(upstream=upstream)
        hass = _make_hass()
        _store_cfg(hass, session=session)

        body = b'{"jsonrpc":"2.0","method":"initialize"}'
        request = make_request(headers={"Content-Type": "application/json"}, body=body)
        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, request)

        assert len(session.calls) == 1
        call = session.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == TARGET_URL
        assert call["data"] == body
        assert resp.status == 200
        assert resp.body == b'{"ok":1}'
        # Anti-buffering / no-transform headers are always set.
        assert resp.headers["Cache-Control"] == "no-cache, no-transform"
        assert resp.headers["Content-Encoding"] == "identity"

    async def test_empty_body_forwards_none_not_empty_bytes(self):
        session = FakeSession(upstream=FakeUpstream(status=200))
        hass = _make_hass()
        _store_cfg(hass, session=session)

        await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request(body=b""))
        assert session.calls[0]["data"] is None

    async def test_hop_by_hop_and_authorization_headers_stripped(self):
        session = FakeSession(upstream=FakeUpstream(status=200))
        hass = _make_hass()
        _store_cfg(hass, session=session)

        request = make_request(
            headers={
                "Host": "example.nabu.casa",
                "Content-Length": "42",
                "Transfer-Encoding": "chunked",
                "Connection": "keep-alive",
                "Cookie": "session=secret",
                "Authorization": "Bearer should-not-forward",
                "Mcp-Session-Id": "sess-42",
                "Content-Type": "application/json",
                "X-Custom": "keep-me",
            }
        )
        await mw._async_handle_webhook(hass, WEBHOOK_ID, request)

        forwarded = {k.lower(): v for k, v in session.calls[0]["headers"].items()}
        for stripped in (
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "cookie",
            "authorization",
        ):
            assert stripped not in forwarded, f"{stripped} should be stripped"
        # Non-hop-by-hop headers pass through untouched.
        assert forwarded["mcp-session-id"] == "sess-42"
        assert forwarded["content-type"] == "application/json"
        assert forwarded["x-custom"] == "keep-me"

    async def test_mcp_session_id_propagated_from_upstream(self):
        upstream = FakeUpstream(
            status=200,
            headers={"Content-Type": "application/json", "Mcp-Session-Id": "srv-77"},
            body=b"{}",
        )
        session = FakeSession(upstream=upstream)
        hass = _make_hass()
        _store_cfg(hass, session=session)

        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request())
        assert resp.headers["Mcp-Session-Id"] == "srv-77"

    async def test_content_type_whitelist_coerces_unknown_to_json(self):
        upstream = FakeUpstream(
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=b"<script>evil()</script>",
        )
        session = FakeSession(upstream=upstream)
        hass = _make_hass()
        _store_cfg(hass, session=session)

        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request())
        assert resp.headers["Content-Type"] == "application/json"

    async def test_json_content_type_preserved(self):
        upstream = FakeUpstream(
            status=200, headers={"Content-Type": "application/json"}, body=b"{}"
        )
        session = FakeSession(upstream=upstream)
        hass = _make_hass()
        _store_cfg(hass, session=session)

        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request())
        assert resp.headers["Content-Type"] == "application/json"

    async def test_sse_branch_streams_chunks_with_anti_buffering_headers(self):
        chunks = [b"event: message\ndata: 1\n\n", b"data: 2\n\n"]
        upstream = FakeUpstream(
            status=200,
            headers={"Content-Type": "text/event-stream"},
            chunks=chunks,
        )
        session = FakeSession(upstream=upstream)
        hass = _make_hass()
        _store_cfg(hass, session=session)

        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request())

        assert isinstance(resp, mw.web.StreamResponse)
        assert resp.prepared is True
        assert resp.written == chunks
        assert resp.eof is True
        assert resp.headers["Content-Type"] == "text/event-stream"
        assert resp.headers["X-Accel-Buffering"] == "no"
        assert resp.headers["Content-Encoding"] == "identity"
        assert resp.headers["Cache-Control"] == "no-cache, no-transform"

    async def test_sse_propagates_session_id(self):
        upstream = FakeUpstream(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Mcp-Session-Id": "stream-sess",
            },
            chunks=[b"data: x\n\n"],
        )
        session = FakeSession(upstream=upstream)
        hass = _make_hass()
        _store_cfg(hass, session=session)

        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request())
        assert resp.headers["Mcp-Session-Id"] == "stream-sess"

    async def test_client_error_maps_to_502(self):
        session = FakeSession(exc=mw.aiohttp.ClientError("connection refused"))
        hass = _make_hass()
        _store_cfg(hass, session=session)

        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request())
        assert resp.status == 502

    async def test_unexpected_error_maps_to_500(self):
        session = FakeSession(exc=RuntimeError("boom"))
        hass = _make_hass()
        _store_cfg(hass, session=session)

        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, make_request())
        assert resp.status == 500


# ---------------------------------------------------------------------------
# Auth gate — ha_auth posture
# ---------------------------------------------------------------------------


class TestHaAuthGate:
    def _provider(self, hass):
        return mw.ResourceServer(hass, WEBHOOK_ID)

    async def test_valid_bearer_passes_through(self):
        hass = _make_hass(validate_result=SimpleNamespace(id="refresh-token"))
        session = FakeSession(upstream=FakeUpstream(status=200))
        _store_cfg(
            hass,
            session=session,
            auth_mode=WEBHOOK_AUTH_HA,
            resource_server=self._provider(hass),
        )

        request = make_request(headers={"Authorization": "Bearer good-token"})
        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, request)

        assert len(session.calls) == 1  # forwarded upstream
        assert resp.status == 200
        request.read.assert_awaited()

    async def test_missing_bearer_returns_401_challenge(self):
        hass = _make_hass(validate_result=None)
        session = FakeSession(upstream=FakeUpstream(status=200))
        _store_cfg(
            hass,
            session=session,
            auth_mode=WEBHOOK_AUTH_HA,
            resource_server=self._provider(hass),
        )

        request = make_request(headers={"Host": "example.nabu.casa"})
        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, request)

        assert resp.status == 401
        assert session.calls == []  # never forwarded upstream
        request.read.assert_not_awaited()  # short-circuited before read
        www = resp.headers["WWW-Authenticate"]
        assert www.startswith("Bearer realm=")
        assert 'realm="Home Assistant MCP Server"' in www
        assert (
            f'resource_metadata="https://example.nabu.casa{OAUTH_BASE}/protected-resource"'
            in www
        )

    async def test_invalid_bearer_returns_401(self):
        hass = _make_hass(validate_result=None)  # validator rejects
        session = FakeSession(upstream=FakeUpstream(status=200))
        _store_cfg(
            hass,
            session=session,
            auth_mode=WEBHOOK_AUTH_HA,
            resource_server=self._provider(hass),
        )

        request = make_request(headers={"Authorization": "Bearer nope"})
        resp = await mw._async_handle_webhook(hass, WEBHOOK_ID, request)
        assert resp.status == 401
        assert session.calls == []


class TestResourceServerValidation:
    def _provider(self, **kw):
        hass = _make_hass(**kw)
        return mw.ResourceServer(hass, WEBHOOK_ID), hass

    async def test_no_authorization_header_is_unauthorized(self):
        provider, _ = self._provider(validate_result=object())
        assert await provider.validate_request(make_request(headers={})) is False

    async def test_non_bearer_scheme_is_unauthorized(self):
        provider, hass = self._provider(validate_result=object())
        request = make_request(headers={"Authorization": "Basic abc"})
        assert await provider.validate_request(request) is False
        hass.auth.async_validate_access_token.assert_not_called()

    async def test_empty_bearer_token_is_unauthorized(self):
        provider, hass = self._provider(validate_result=object())
        request = make_request(headers={"Authorization": "Bearer    "})
        assert await provider.validate_request(request) is False
        hass.auth.async_validate_access_token.assert_not_called()

    async def test_valid_token_authorized(self):
        provider, _ = self._provider(validate_result=SimpleNamespace(id="rt"))
        request = make_request(headers={"Authorization": "Bearer tok"})
        assert await provider.validate_request(request) is True

    async def test_validator_none_is_unauthorized(self):
        provider, _ = self._provider(validate_result=None)
        request = make_request(headers={"Authorization": "Bearer tok"})
        assert await provider.validate_request(request) is False

    async def test_validator_raise_is_unauthorized_not_500(self):
        provider, _ = self._provider(validate_side_effect=ValueError("bad token"))
        request = make_request(headers={"Authorization": "Bearer tok"})
        assert await provider.validate_request(request) is False

    async def test_awaitable_validator_result_is_awaited(self):
        hass = _make_hass()

        async def _async_validate(_token):
            return SimpleNamespace(id="rt")

        hass.auth.async_validate_access_token = MagicMock(side_effect=_async_validate)
        provider = mw.ResourceServer(hass, WEBHOOK_ID)
        request = make_request(headers={"Authorization": "Bearer tok"})
        assert await provider.validate_request(request) is True


# ---------------------------------------------------------------------------
# Discovery documents (RFC 8414 / RFC 9728)
# ---------------------------------------------------------------------------


def _live_hass(
    auth_mode: str = WEBHOOK_AUTH_HA, webhook_id: str = WEBHOOK_ID
) -> MagicMock:
    hass = _make_hass()
    # Mirrors async_register_webhook's runtime cfg: the views resolve the
    # ACTIVE provider from here per request (stale-binding fix).
    hass.data[DOMAIN] = {
        DATA_WEBHOOK: {
            "auth_mode": auth_mode,
            "resource_server": mw.ResourceServer(hass, webhook_id),
        }
    }
    return hass


class TestDiscoveryViews:
    def test_build_base_url_prefers_forwarded_headers(self):
        request = make_request(
            headers={
                "Host": "internal:8123",
                "X-Forwarded-Host": "abc.ui.nabu.casa",
                "X-Forwarded-Proto": "https",
            },
            scheme="http",
        )
        assert mw._build_base_url(request) == "https://abc.ui.nabu.casa"

    def test_build_base_url_falls_back_to_request(self):
        request = make_request(headers={"Host": "ha.local:8123"}, scheme="http")
        assert mw._build_base_url(request) == "http://ha.local:8123"

    def test_authorization_server_document_shape(self):
        doc = mw._authorization_server_document("https://x.nabu.casa")
        assert doc["issuer"] == f"https://x.nabu.casa{OAUTH_BASE}"
        assert doc["authorization_endpoint"] == "https://x.nabu.casa/auth/authorize"
        assert doc["token_endpoint"] == "https://x.nabu.casa/auth/token"
        assert doc["response_types_supported"] == ["code"]
        assert doc["code_challenge_methods_supported"] == ["S256"]
        assert doc["token_endpoint_auth_methods_supported"] == ["none"]
        assert doc["client_id_metadata_document_supported"] is True
        assert "registration_endpoint" not in doc

    async def test_protected_resource_view_payload_when_live(self):
        hass = _live_hass()
        view = mw._ProtectedResourceMetadataView(hass)
        request = make_request(headers={"Host": "abc.ui.nabu.casa"})
        resp = await view.get(request)
        assert resp.status == 200
        body = resp.json_body
        assert body["resource"] == f"https://abc.ui.nabu.casa/api/webhook/{WEBHOOK_ID}"
        assert body["authorization_servers"] == [
            f"https://abc.ui.nabu.casa{OAUTH_BASE}"
        ]
        assert body["bearer_methods_supported"] == ["header"]

    async def test_protected_resource_view_404_when_not_live(self):
        hass = _live_hass(auth_mode=WEBHOOK_AUTH_NONE)
        view = mw._ProtectedResourceMetadataView(hass)
        resp = await view.get(make_request(headers={"Host": "x"}))
        assert resp.status == 404

    async def test_authorization_server_view_payload_when_live(self):
        hass = _live_hass()
        view = mw._AuthorizationServerMetadataView(hass)
        resp = await view.get(make_request(headers={"Host": "abc.ui.nabu.casa"}))
        assert resp.status == 200
        assert resp.json_body["token_endpoint"] == "https://abc.ui.nabu.casa/auth/token"

    async def test_authorization_server_view_404_when_entry_unloaded(self):
        hass = _make_hass()  # no DOMAIN data at all
        view = mw._AuthorizationServerMetadataView(hass)
        resp = await view.get(make_request(headers={"Host": "x"}))
        assert resp.status == 404

    def test_wellknown_protected_resource_url_is_parameterized(self):
        # Stale-binding fix: the webhook id is a route PARAMETER, so the one
        # bound view serves whichever entry is currently live (a remove+re-add
        # mints a new id in the same HA session).
        assert mw._WellKnownProtectedResourceView.url == (
            "/.well-known/oauth-protected-resource/api/webhook/{webhook_id}"
        )

    async def test_wellknown_protected_resource_serves_only_current_id(self):
        hass = _live_hass()
        view = mw._WellKnownProtectedResourceView(hass)
        request = make_request(headers={"Host": "abc.ui.nabu.casa"})
        ok = await view.get(request, webhook_id=WEBHOOK_ID)
        assert ok.status == 200
        stale = await view.get(request, webhook_id="mcp_stale_previous_entry")
        assert stale.status == 404

    async def test_views_serve_new_provider_after_entry_recreate(self):
        # The live-found stale-binding scenario: views registered during the
        # FIRST entry keep working for a SECOND entry with a new webhook id.
        hass = _live_hass()
        view = mw._ProtectedResourceMetadataView(hass)
        hass.data[DOMAIN][DATA_WEBHOOK] = {
            "auth_mode": WEBHOOK_AUTH_HA,
            "resource_server": mw.ResourceServer(hass, "mcp_second_entry_id"),
        }
        resp = await view.get(make_request(headers={"Host": "abc.ui.nabu.casa"}))
        assert resp.status == 200
        assert resp.json_body["resource"] == (
            "https://abc.ui.nabu.casa/api/webhook/mcp_second_entry_id"
        )

    def test_metadata_views_bundle_is_seven_unique_named_views(self):
        views = mw._metadata_views(_make_hass())
        assert len(views) == 7
        names = {v.name for v in views}
        assert len(names) == 7  # all unique route names
        urls = {v.url for v in views}
        assert len(urls) == 7  # all unique route paths


# ---------------------------------------------------------------------------
# Registration / teardown
# ---------------------------------------------------------------------------


def _register_hass() -> MagicMock:
    hass = _make_hass()
    hass.data = {}
    hass.config = SimpleNamespace(components={"webhook"})  # skip async_setup_component
    hass.http = MagicMock()
    return hass


def _entry() -> MagicMock:
    entry = MagicMock()
    entry.data = {DATA_WEBHOOK_ID: WEBHOOK_ID}
    return entry


class TestRegisterWebhook:
    @pytest.fixture(autouse=True)
    def _reset_registration_state(self):
        # async_register / async_unregister are module-global MagicMocks shared
        # across tests; reset call history and any injected side effect so each
        # registration test starts clean (the per-session views guard lives in
        # hass.data, which is fresh per test).
        mw.async_register.reset_mock(side_effect=True)
        mw.async_unregister.reset_mock(side_effect=True)
        yield
        mw.async_register.reset_mock(side_effect=True)
        mw.async_unregister.reset_mock(side_effect=True)

    async def test_none_auth_registers_and_stores_cfg(self, monkeypatch):
        hass = _register_hass()
        fake_session = FakeSession()
        monkeypatch.setattr(mw.aiohttp, "ClientSession", lambda **kw: fake_session)

        await mw.async_register_webhook(
            hass,
            _entry(),
            port=9584,
            secret_path="/private_x",
            auth_mode=WEBHOOK_AUTH_NONE,
        )

        cfg = hass.data[DOMAIN][DATA_WEBHOOK]
        assert cfg["auth_mode"] == WEBHOOK_AUTH_NONE
        assert cfg["target_url"] == "http://127.0.0.1:9584/private_x"
        assert cfg["resource_server"] is None
        mw.async_register.assert_called_once()
        # Reload-safe: clears any stale registration before (re)registering.
        mw.async_unregister.assert_called_once_with(hass, WEBHOOK_ID)

    async def test_ha_auth_registers_resource_server_and_views(self, monkeypatch):
        hass = _register_hass()
        monkeypatch.setattr(mw.aiohttp, "ClientSession", lambda **kw: FakeSession())
        # Fresh hass ⇒ the once-per-session guard flag is absent, so this run
        # binds the discovery views.
        assert mw._OAUTH_VIEWS_REGISTERED_KEY not in hass.data

        await mw.async_register_webhook(
            hass,
            _entry(),
            port=9584,
            secret_path="/private_x",
            auth_mode=WEBHOOK_AUTH_HA,
        )

        cfg = hass.data[DOMAIN][DATA_WEBHOOK]
        assert isinstance(cfg["resource_server"], mw.ResourceServer)
        # Seven discovery views were bound on hass.http.
        assert hass.http.register_view.call_count == 7
        assert hass.data.get(mw._OAUTH_VIEWS_REGISTERED_KEY) is True

    async def test_ha_auth_re_enable_reuses_bound_views(self, monkeypatch):
        # aiohttp cannot unregister a bound view; the once-per-session guard
        # lives at a TOP-LEVEL hass.data key precisely so a none->ha_auth->
        # none->ha_auth cycle re-USES the 7 views instead of re-binding them
        # (which raises and takes the whole bring-up down). Review finding:
        # only the first registration was tested.
        hass = _register_hass()
        monkeypatch.setattr(mw.aiohttp, "ClientSession", lambda **kw: FakeSession())

        await mw.async_register_webhook(
            hass,
            _entry(),
            port=9584,
            secret_path="/private_x",
            auth_mode=WEBHOOK_AUTH_HA,
        )
        assert hass.http.register_view.call_count == 7
        await mw.async_unregister_webhook(hass)

        # Second enable in the same HA session: no new bindings, no raise.
        await mw.async_register_webhook(
            hass,
            _entry(),
            port=9584,
            secret_path="/private_x",
            auth_mode=WEBHOOK_AUTH_HA,
        )
        assert hass.http.register_view.call_count == 7
        assert isinstance(
            hass.data[DOMAIN][DATA_WEBHOOK]["resource_server"], mw.ResourceServer
        )

    async def test_registration_failure_closes_session_and_unregisters(
        self, monkeypatch
    ):
        hass = _register_hass()
        fake_session = FakeSession()
        monkeypatch.setattr(mw.aiohttp, "ClientSession", lambda **kw: fake_session)
        # async_register raises → the except path must unregister + close session.
        mw.async_register.side_effect = RuntimeError("duplicate webhook")

        with pytest.raises(RuntimeError):
            await mw.async_register_webhook(
                hass,
                _entry(),
                port=9584,
                secret_path="/private_x",
                auth_mode=WEBHOOK_AUTH_NONE,
            )

        assert fake_session.closed is True
        assert DATA_WEBHOOK not in hass.data.get(DOMAIN, {})

    async def test_unregister_pops_cfg_and_closes_session(self, monkeypatch):
        hass = _register_hass()
        fake_session = FakeSession()
        monkeypatch.setattr(mw.aiohttp, "ClientSession", lambda **kw: fake_session)
        await mw.async_register_webhook(
            hass,
            _entry(),
            port=9584,
            secret_path="/private_x",
            auth_mode=WEBHOOK_AUTH_NONE,
        )

        await mw.async_unregister_webhook(hass)
        assert DATA_WEBHOOK not in hass.data[DOMAIN]
        assert fake_session.closed is True

    async def test_unregister_is_idempotent(self):
        hass = _register_hass()
        # No cfg present — must be a clean no-op.
        await mw.async_unregister_webhook(hass)
        await mw.async_unregister_webhook(hass)
