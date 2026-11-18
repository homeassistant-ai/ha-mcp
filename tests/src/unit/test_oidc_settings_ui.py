"""OIDC provider compatibility and settings route isolation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ._oidc_test_helpers import OIDC_PROXY_PATCH_TARGET, make_mock_oidc_proxy
from ._oidc_test_helpers import isolated_oidc_runtime as isolated_oidc_runtime


class TestOIDCProxySignatureSubset:
    """Offline check that our hand-written kwarg set is accepted by OIDCProxy.

    This can't catch drift in *our* call (that's ``make_mock_oidc_proxy``'s
    job, via its pinned signature) — it catches the other direction: a kwarg
    we pass that FastMCP's real ``OIDCProxy.__init__`` no longer accepts. No
    instantiation, no network — pure ``inspect.signature`` comparison.
    """

    def test_run_oidc_server_kwargs_are_subset_of_oidc_proxy_params(self):
        import inspect

        from ha_mcp._vendor.fastmcp.server.auth.oidc_proxy import OIDCProxy

        oidc_proxy_params = set(inspect.signature(OIDCProxy.__init__).parameters)

        # Every kwarg name run_oidc_server can pass to OIDCProxy(**proxy_kwargs).
        run_oidc_server_kwargs = {
            "config_url",
            "client_id",
            "client_secret",
            "base_url",
            "require_authorization_consent",
            "required_scopes",
            "jwt_signing_key",
            "allowed_client_redirect_uris",
            "verify_id_token",
            "audience",
        }

        assert run_oidc_server_kwargs.issubset(oidc_proxy_params), (
            f"run_oidc_server passes kwargs not accepted by OIDCProxy.__init__: "
            f"{run_oidc_server_kwargs - oidc_proxy_params}"
        )


class TestRunOidcServerSettingsUI:
    """OIDC mode serves the settings UI behind a dedicated secret path — a
    deliberate feature addition (it registered none before) — never the
    OIDC-known MCP path where custom routes bypass auth (GHSA-mx64-982r-65vg)."""

    async def run_server(self):
        import ha_mcp.__main__ as main_module

        MockOIDCProxy = make_mock_oidc_proxy({})
        mock_mcp = MagicMock()
        mock_mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server = MagicMock()
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch(
                OIDC_PROXY_PATCH_TARGET,
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "run_with_shutdown", side_effect=noop_shutdown),
        ):
            await main_module.run_oidc_server(
                config_url="https://auth.example.com/.well-known/openid-configuration",
                client_id="test-id",
                client_secret="test-secret",
                base_url="https://mcp.example.com",
                host="0.0.0.0",
                port=8086,
                path="/mcp",
            )
        return mock_mcp

    @pytest.mark.asyncio
    async def test_settings_mounted_off_secret_path(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)

        mock_mcp = await self.run_server()
        paths = [c.args[0] for c in mock_mcp.custom_route.call_args_list]

        assert any(p.startswith("/private_") and p.endswith("/settings") for p in paths)
        assert not any(p.startswith("/mcp/") and "settings" in p for p in paths)

        from ha_mcp.settings_ui import (
            get_http_settings_prefix,
            is_http_settings_mounted,
        )

        assert get_http_settings_prefix() is None
        assert is_http_settings_mounted() is True

    @pytest.mark.asyncio
    async def test_disable_env_mounts_no_settings(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)

        mock_mcp = await self.run_server()
        paths = [c.args[0] for c in mock_mcp.custom_route.call_args_list]

        assert not any("settings" in p for p in paths)
