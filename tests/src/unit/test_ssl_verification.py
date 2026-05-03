"""Unit tests for the HA_VERIFY_SSL toggle.

Verifies that the verify_ssl setting flows from configuration into the
REST httpx client and the WebSocket SSL context.
"""

import ssl
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_global_settings():
    """Force ``get_global_settings`` to re-read environment in each test."""
    import ha_mcp.config as cfg

    cfg._settings = None
    yield
    cfg._settings = None


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
            HomeAssistantClient(base_url="https://ha.local:8123", token="t")
            kwargs = mock_async.call_args.kwargs
            assert kwargs["verify"] is True

    def test_explicit_false_disables_verification(self, monkeypatch):
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.rest_client import HomeAssistantClient

        with patch("ha_mcp.client.rest_client.httpx.AsyncClient") as mock_async:
            HomeAssistantClient(
                base_url="https://ha.local:8123", token="t", verify_ssl=False
            )
            kwargs = mock_async.call_args.kwargs
            assert kwargs["verify"] is False

    def test_env_false_propagates_to_httpx(self, monkeypatch):
        monkeypatch.setenv("HA_VERIFY_SSL", "false")
        monkeypatch.setenv("HOMEASSISTANT_URL", "https://ha.local:8123")
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", "t")
        from ha_mcp.client.rest_client import HomeAssistantClient

        with patch("ha_mcp.client.rest_client.httpx.AsyncClient") as mock_async:
            HomeAssistantClient()
            kwargs = mock_async.call_args.kwargs
            assert kwargs["verify"] is False

    def test_warning_logged_when_disabled(self, monkeypatch, caplog):
        monkeypatch.delenv("HA_VERIFY_SSL", raising=False)
        from ha_mcp.client.rest_client import HomeAssistantClient

        with (
            patch("ha_mcp.client.rest_client.httpx.AsyncClient"),
            caplog.at_level("WARNING", logger="ha_mcp.client.rest_client"),
        ):
            HomeAssistantClient(
                base_url="https://ha.local:8123",
                token="t",
                verify_ssl=False,
            )
        assert any("TLS verification disabled" in r.message for r in caplog.records)


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

    def test_constructor_falls_back_to_settings(self, monkeypatch):
        monkeypatch.setenv("HA_VERIFY_SSL", "false")
        monkeypatch.setenv("HOMEASSISTANT_URL", "https://ha.local:8123")
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", "t")
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="https://ha.local:8123", token="t"
        )
        assert client.verify_ssl is False
