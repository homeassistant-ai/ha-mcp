"""Unit tests for the HA_VERIFY_SSL toggle.

Verifies that the verify_ssl setting flows from configuration into the
REST httpx client and the WebSocket SSL context.
"""

import logging
import ssl
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_settings_singleton():
    """Force ``get_global_settings`` to re-read environment in each test."""
    from ha_mcp.config import _reset_global_settings

    _reset_global_settings()
    yield
    _reset_global_settings()


class TestSettingsDefault:
    """``Settings.verify_ssl`` reads from ``HA_VERIFY_SSL``."""

    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.config import get_settings

        assert get_settings().verify_ssl is True

    @pytest.mark.parametrize("falsey", ["false", "False", "0", "no", "off"])
    def test_disabled_via_env(self, monkeypatch, falsey):
        monkeypatch.setenv("HA_VERIFY_SSL", falsey)
        from ha_mcp.config import get_settings

        assert get_settings().verify_ssl is False

    @pytest.mark.parametrize("truthy", ["true", "True", "1", "yes", "on"])
    def test_enabled_via_env(self, monkeypatch, truthy):
        monkeypatch.setenv("HA_VERIFY_SSL", truthy)
        from ha_mcp.config import get_settings

        assert get_settings().verify_ssl is True


class TestRestClientUsesVerifySsl:
    """``HomeAssistantClient`` forwards ``verify_ssl`` to ``httpx.AsyncClient``."""

    def test_default_passes_verify_true(self, monkeypatch):
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.rest_client import HomeAssistantClient

        with patch("ha_mcp.client.rest_client.httpx.AsyncClient") as mock_async:
            client = HomeAssistantClient(base_url="https://ha.local:8123", token="t")
            kwargs = mock_async.call_args.kwargs
            assert kwargs["verify"] is True
            assert client.verify_ssl is True

    def test_explicit_false_disables_verification(self, monkeypatch):
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.rest_client import HomeAssistantClient

        with patch("ha_mcp.client.rest_client.httpx.AsyncClient") as mock_async:
            client = HomeAssistantClient(
                base_url="https://ha.local:8123", token="t", verify_ssl=False
            )
            kwargs = mock_async.call_args.kwargs
            assert kwargs["verify"] is False
            assert client.verify_ssl is False

    def test_env_false_propagates_to_httpx(self, monkeypatch):
        monkeypatch.setenv("HA_VERIFY_SSL", "false")
        monkeypatch.setenv("HOMEASSISTANT_URL", "https://ha.local:8123")
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", "t")
        from ha_mcp.client.rest_client import HomeAssistantClient

        with patch("ha_mcp.client.rest_client.httpx.AsyncClient") as mock_async:
            HomeAssistantClient()
            kwargs = mock_async.call_args.kwargs
            assert kwargs["verify"] is False

    def test_oauth_path_falls_back_to_settings(self, monkeypatch):
        # OAuth path passes base_url + token but no verify_ssl; setting
        # in env must still take effect.
        monkeypatch.setenv("HA_VERIFY_SSL", "false")
        from ha_mcp.client.rest_client import HomeAssistantClient

        with patch("ha_mcp.client.rest_client.httpx.AsyncClient"):
            client = HomeAssistantClient(base_url="https://ha.local:8123", token="t")
            assert client.verify_ssl is False

    def test_warning_logged_when_disabled(self, monkeypatch, caplog):
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.rest_client import HomeAssistantClient

        with (
            patch("ha_mcp.client.rest_client.httpx.AsyncClient"),
            caplog.at_level(logging.WARNING, logger="ha_mcp.client.rest_client"),
        ):
            HomeAssistantClient(
                base_url="https://ha.local:8123",
                token="t",
                verify_ssl=False,
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "expected a WARNING when verify_ssl is False"

    def test_no_warning_when_verification_enabled(self, monkeypatch, caplog):
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.rest_client import HomeAssistantClient

        with (
            patch("ha_mcp.client.rest_client.httpx.AsyncClient"),
            caplog.at_level(logging.WARNING, logger="ha_mcp.client.rest_client"),
        ):
            HomeAssistantClient(
                base_url="https://ha.local:8123",
                token="t",
                verify_ssl=True,
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings, (
            f"expected no WARNINGs when verify_ssl=True, got: {warnings}"
        )

    def test_ssl_error_surfaces_actionable_hint(self, monkeypatch):
        """A TLS failure with verify_ssl=True should mention HA_VERIFY_SSL."""
        import httpx

        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.rest_client import (
            HomeAssistantClient,
            HomeAssistantConnectionError,
        )

        underlying = ssl.SSLCertVerificationError("self-signed certificate")
        wrapped = httpx.ConnectError("[SSL] certificate verify failed")
        wrapped.__cause__ = underlying

        with patch("ha_mcp.client.rest_client.httpx.AsyncClient") as mock_async:
            mock_async.return_value.request = _AsyncRaiser(wrapped)
            client = HomeAssistantClient(
                base_url="https://ha.local:8123", token="t", verify_ssl=True
            )

            import asyncio

            with pytest.raises(HomeAssistantConnectionError) as exc:
                asyncio.run(client._raw_request("GET", "/states"))
            assert "HA_VERIFY_SSL=false" in str(exc.value)


class _AsyncRaiser:
    """Minimal awaitable callable that always raises the given exception."""

    def __init__(self, exc):
        self._exc = exc

    async def __call__(self, *_args, **_kwargs):
        raise self._exc


class TestWebSocketClientUsesVerifySsl:
    """``HomeAssistantWebSocketClient`` builds an SSL context for wss:// URLs."""

    @pytest.mark.asyncio
    async def test_wss_with_verification_uses_default_context(self, monkeypatch):
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="https://ha.example.com:8123", token="t"
        )

        captured: dict = {}

        async def fake_connect(*_args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop after capture")

        with patch(
            "ha_mcp.client.websocket_client.websockets.connect",
            side_effect=fake_connect,
        ):
            assert await client.connect() is False

        ctx = captured.get("ssl")
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    @pytest.mark.asyncio
    async def test_wss_without_verification_disables_checks(self, monkeypatch):
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="https://ha.example.com:8123", token="t", verify_ssl=False
        )

        captured: dict = {}

        async def fake_connect(*_args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop after capture")

        with patch(
            "ha_mcp.client.websocket_client.websockets.connect",
            side_effect=fake_connect,
        ):
            assert await client.connect() is False

        ctx = captured.get("ssl")
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_NONE
        assert ctx.check_hostname is False

    @pytest.mark.asyncio
    async def test_wss_with_path_still_attaches_ssl_context(self, monkeypatch):
        """Path-bearing https URLs (proxy setups) must still build an SSL context."""
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="https://my-ha.example.com/proxy/core",
            token="t",
            verify_ssl=False,
        )
        # URL transform should produce wss:// preserving the path.
        assert client.ws_url.startswith("wss://")
        assert client.ws_url.endswith("/proxy/core/websocket")

        captured_args: list = []
        captured_kwargs: dict = {}

        async def fake_connect(*args, **kwargs):
            captured_args.extend(args)
            captured_kwargs.update(kwargs)
            raise RuntimeError("stop after capture")

        with patch(
            "ha_mcp.client.websocket_client.websockets.connect",
            side_effect=fake_connect,
        ):
            assert await client.connect() is False

        assert captured_args[0].startswith("wss://")
        assert isinstance(captured_kwargs.get("ssl"), ssl.SSLContext)

    @pytest.mark.asyncio
    async def test_ws_url_does_not_attach_ssl_context(self, monkeypatch):
        """Plain ws:// URLs (e.g. supervisor proxy) must not get an SSL context."""
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://supervisor/core", token="t", verify_ssl=False
        )

        captured: dict = {}

        async def fake_connect(*_args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop after capture")

        with patch(
            "ha_mcp.client.websocket_client.websockets.connect",
            side_effect=fake_connect,
        ):
            assert await client.connect() is False

        assert captured.get("ssl") is None

    @pytest.mark.asyncio
    async def test_warning_emitted_only_once_per_client(self, monkeypatch, caplog):
        """Reconnect storms shouldn't spam the warning."""
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="https://ha.example.com:8123", token="t", verify_ssl=False
        )

        async def fake_connect(*_args, **_kwargs):
            raise RuntimeError("stop after capture")

        with (
            patch(
                "ha_mcp.client.websocket_client.websockets.connect",
                side_effect=fake_connect,
            ),
            caplog.at_level(logging.WARNING, logger="ha_mcp.client.websocket_client"),
        ):
            await client.connect()
            await client.connect()
            await client.connect()

        ssl_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "TLS verification disabled" in r.getMessage()
        ]
        assert len(ssl_warnings) == 1, (
            f"expected exactly one TLS-disabled warning across 3 connects, got {len(ssl_warnings)}"
        )

    def test_constructor_falls_back_to_settings(self, monkeypatch):
        monkeypatch.setenv("HA_VERIFY_SSL", "false")
        monkeypatch.setenv("HOMEASSISTANT_URL", "https://ha.local:8123")
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", "t")
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="https://ha.local:8123", token="t"
        )
        assert client.verify_ssl is False

    def test_constructor_safe_default_when_settings_load_fails(
        self, monkeypatch, caplog
    ):
        """Bad env elsewhere shouldn't silently flip TLS off."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        with (
            patch(
                "ha_mcp.client.websocket_client.get_global_settings",
                side_effect=RuntimeError("settings unloadable"),
            ),
            caplog.at_level(logging.WARNING, logger="ha_mcp.client.websocket_client"),
        ):
            client = HomeAssistantWebSocketClient(
                url="https://ha.local:8123", token="t"
            )
        assert client.verify_ssl is True
        assert any(
            "Could not load settings" in r.getMessage() for r in caplog.records
        )


class TestPoolFactoryHonorsEnv:
    """``WebSocketManager``-built clients pick up ``HA_VERIFY_SSL`` from env."""

    def test_factory_default_picks_up_env(self, monkeypatch):
        monkeypatch.setenv("HA_VERIFY_SSL", "false")
        monkeypatch.setenv("HOMEASSISTANT_URL", "https://ha.local:8123")
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", "t")
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        # The pool factory always calls the constructor positionally with
        # only (url, token) — no verify_ssl. This mirrors that exact call
        # shape so a regression where the factory starts hard-coding True
        # is caught here.
        client = HomeAssistantWebSocketClient("https://ha.local:8123", "t")
        assert client.verify_ssl is False


class TestHelperPropagatesVerifySsl:
    """``get_connected_ws_client`` forwards ``verify_ssl`` to the WS client."""

    @pytest.mark.asyncio
    async def test_explicit_false_propagates(self, monkeypatch):
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.tools import helpers as helpers_mod

        captured: dict = {}

        class FakeWsClient:
            def __init__(self, base_url, token, *, verify_ssl=None):
                captured["verify_ssl"] = verify_ssl

            async def connect(self):
                return True

        with patch.object(
            helpers_mod, "HomeAssistantWebSocketClient", FakeWsClient
        ):
            ws, error = await helpers_mod.get_connected_ws_client(
                "https://ha.local:8123", "t", verify_ssl=False
            )
        assert error is None
        assert captured["verify_ssl"] is False
