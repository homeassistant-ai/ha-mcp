"""OIDC startup forwards configured provider and transport options."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ._oidc_test_helpers import OIDC_PROXY_PATCH_TARGET, make_mock_oidc_proxy
from ._oidc_test_helpers import isolated_oidc_runtime as isolated_oidc_runtime


class TestRunOidcServer:
    """Tests for run_oidc_server async function."""

    @pytest.mark.asyncio
    async def test_creates_oidc_proxy_with_required_openid_scope(self):
        """OIDC mode should require openid and enable scope compatibility."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
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

        assert (
            proxy_init_args["config_url"]
            == "https://auth.example.com/.well-known/openid-configuration"
        )
        assert proxy_init_args["client_id"] == "test-id"
        assert proxy_init_args["client_secret"] == "test-secret"
        assert proxy_init_args["base_url"] == "https://mcp.example.com"
        assert proxy_init_args["require_authorization_consent"] == "external"
        assert proxy_init_args["required_scopes"] == ["openid"]
        assert proxy_init_args.get("setup_scope_compatibility_calls", 0) == 1

    @pytest.mark.asyncio
    async def test_jwt_signing_key_passed_from_env(self):
        """run_oidc_server should pass OIDC_JWT_SIGNING_KEY env var to OIDCProxy."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(
                os.environ, {"OIDC_JWT_SIGNING_KEY": "test-jwt-key"}, clear=False
            ),
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

        assert proxy_init_args["jwt_signing_key"] == "test-jwt-key"

    @pytest.mark.asyncio
    async def test_jwt_signing_key_none_when_unset(self):
        """run_oidc_server should pass None for jwt_signing_key when env var is not set."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        env_without_key = {
            k: v for k, v in os.environ.items() if k != "OIDC_JWT_SIGNING_KEY"
        }
        with (
            patch.dict(os.environ, env_without_key, clear=True),
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

        assert proxy_init_args["jwt_signing_key"] is None

    @pytest.mark.asyncio
    async def test_sets_auth_on_mcp_instance(self):
        """run_oidc_server should set auth on the MCP instance."""
        import ha_mcp.__main__ as main_module

        mock_auth = MagicMock()
        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        # Use a regular function that returns a coroutine-like object
        # to avoid unawaited coroutine warnings from AsyncMock
        async def fake_coro(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_coro)
        mock_server.mcp = mock_mcp

        async def capturerun_with_shutdown(coro):
            # Close the coroutine to avoid warnings
            coro.close()

        with (
            patch(OIDC_PROXY_PATCH_TARGET, return_value=mock_auth),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(
                main_module, "run_with_shutdown", side_effect=capturerun_with_shutdown
            ),
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

        assert mock_mcp.auth == mock_auth

    @pytest.mark.asyncio
    async def test_uses_http_transport(self):
        """run_oidc_server should use the "http" transport, matching sibling HTTP modes."""
        import ha_mcp.__main__ as main_module

        run_kwargs = {}

        async def capturerun_with_shutdown(coro):
            coro.close()

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_coro(**kwargs):
            pass

        def capture_run_async(**kwargs):
            run_kwargs.update(kwargs)
            return fake_coro(**kwargs)

        mock_mcp.run_async = capture_run_async
        mock_server.mcp = mock_mcp

        with (
            patch(OIDC_PROXY_PATCH_TARGET, return_value=MagicMock()),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(
                main_module, "run_with_shutdown", side_effect=capturerun_with_shutdown
            ),
        ):
            await main_module.run_oidc_server(
                config_url="https://auth.example.com/.well-known/openid-configuration",
                client_id="test-id",
                client_secret="test-secret",
                base_url="https://mcp.example.com",
                host="0.0.0.0",
                port=9000,
                path="/custom",
            )

        assert run_kwargs["transport"] == "http"
        assert run_kwargs["port"] == 9000
        assert run_kwargs["path"] == "/custom"

    @pytest.mark.asyncio
    async def test_calls_ensure_host_origin_guard_default_off(self):
        """run_oidc_server must default the Host/Origin guard off.

        Regression test for the real bug fixed in commit 79bdc6d8: OIDC mode is
        deployed behind a public reverse proxy or LAN hostname, so FastMCP's
        DNS-rebinding guard must be defaulted off before the OIDCProxy runs, or
        proxied requests get rejected with 403/421 before OIDC auth even runs.
        """
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch(
                "ha_mcp.transport_security.ensure_host_origin_guard_default_off"
            ) as mock_guard,
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

        mock_guard.assert_called_once()

    @pytest.mark.asyncio
    async def test_allowed_client_redirect_uris_passed_from_env(self):
        """OIDC_ALLOWED_CLIENT_REDIRECT_URIS should be parsed into a list and passed."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(
                os.environ,
                {
                    "OIDC_ALLOWED_CLIENT_REDIRECT_URIS": " https://a.example.com/cb , ,https://b.example.com/cb "
                },
                clear=False,
            ),
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

        assert proxy_init_args["allowed_client_redirect_uris"] == [
            "https://a.example.com/cb",
            "https://b.example.com/cb",
        ]

    @pytest.mark.asyncio
    async def test_allowed_client_redirect_uris_absent_when_unset(self):
        """OIDC_ALLOWED_CLIENT_REDIRECT_URIS unset should not pass the kwarg at all."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        env_without_var = {
            k: v
            for k, v in os.environ.items()
            if k != "OIDC_ALLOWED_CLIENT_REDIRECT_URIS"
        }
        with (
            patch.dict(os.environ, env_without_var, clear=True),
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

        assert "allowed_client_redirect_uris" not in proxy_init_args

    @pytest.mark.asyncio
    async def test_verify_id_token_true_when_env_set(self):
        """OIDC_VERIFY_ID_TOKEN=true should pass verify_id_token=True to OIDCProxy."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(os.environ, {"OIDC_VERIFY_ID_TOKEN": "true"}, clear=False),
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

        assert proxy_init_args["verify_id_token"] is True

    @pytest.mark.asyncio
    async def test_verify_id_token_absent_when_unset(self):
        """OIDC_VERIFY_ID_TOKEN unset should not pass the verify_id_token kwarg."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        env_without_var = {
            k: v for k, v in os.environ.items() if k != "OIDC_VERIFY_ID_TOKEN"
        }
        with (
            patch.dict(os.environ, env_without_var, clear=True),
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

        assert "verify_id_token" not in proxy_init_args

    @pytest.mark.asyncio
    async def test_empty_jwt_signing_key_becomes_none(self):
        """An empty (but set) OIDC_JWT_SIGNING_KEY must reach OIDCProxy as None.

        A set-but-empty string must not bypass FastMCP's derive-from-client-secret
        default the way passing "" would.
        """
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(os.environ, {"OIDC_JWT_SIGNING_KEY": ""}, clear=False),
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

        assert proxy_init_args["jwt_signing_key"] is None

    @pytest.mark.asyncio
    async def test_verify_id_token_absent_when_falsey(self):
        """OIDC_VERIFY_ID_TOKEN=false (set but falsey) must not pass the kwarg."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(os.environ, {"OIDC_VERIFY_ID_TOKEN": "false"}, clear=False),
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

        assert "verify_id_token" not in proxy_init_args

    @pytest.mark.asyncio
    async def test_allowed_client_redirect_uris_whitespace_only_treated_as_unset(self):
        """Separators/whitespace-only value must behave like unset (None / no kwarg)."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(
                os.environ,
                {"OIDC_ALLOWED_CLIENT_REDIRECT_URIS": " , , "},
                clear=False,
            ),
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

        assert "allowed_client_redirect_uris" not in proxy_init_args

    @pytest.mark.asyncio
    async def test_allowed_redirect_uris_unset_logs_warning(self, caplog):
        """OIDC_ALLOWED_CLIENT_REDIRECT_URIS unset must log a warning at startup."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        env_without_var = {
            k: v
            for k, v in os.environ.items()
            if k != "OIDC_ALLOWED_CLIENT_REDIRECT_URIS"
        }
        with (
            patch.dict(os.environ, env_without_var, clear=True),
            patch(
                OIDC_PROXY_PATCH_TARGET,
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "run_with_shutdown", side_effect=noop_shutdown),
            caplog.at_level("WARNING"),
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

        assert any(
            "OIDC_ALLOWED_CLIENT_REDIRECT_URIS" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_allowed_redirect_uris_whitespace_only_also_logs_warning(
        self, caplog
    ):
        """Separators/whitespace-only value must also trigger the unset-warning."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(
                os.environ,
                {"OIDC_ALLOWED_CLIENT_REDIRECT_URIS": " , , "},
                clear=False,
            ),
            patch(
                OIDC_PROXY_PATCH_TARGET,
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "run_with_shutdown", side_effect=noop_shutdown),
            caplog.at_level("WARNING"),
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

        assert any(
            "OIDC_ALLOWED_CLIENT_REDIRECT_URIS" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_allowed_redirect_uris_set_does_not_log_warning(self, caplog):
        """When OIDC_ALLOWED_CLIENT_REDIRECT_URIS is set, no unset-warning fires."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(
                os.environ,
                {"OIDC_ALLOWED_CLIENT_REDIRECT_URIS": "https://a.example.com/cb"},
                clear=False,
            ),
            patch(
                OIDC_PROXY_PATCH_TARGET,
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "run_with_shutdown", side_effect=noop_shutdown),
            caplog.at_level("WARNING"),
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

        assert not any(
            "OIDC_ALLOWED_CLIENT_REDIRECT_URIS" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_audience_passed_from_env(self):
        """OIDC_AUDIENCE should be passed through to OIDCProxy when set."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(
                os.environ,
                {"OIDC_AUDIENCE": "https://mcp.example.com/mcp"},
                clear=False,
            ),
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

        assert proxy_init_args["audience"] == "https://mcp.example.com/mcp"

    @pytest.mark.asyncio
    async def test_audience_absent_when_unset(self):
        """OIDC_AUDIENCE unset should not pass the audience kwarg at all."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=fake_run_async)
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        env_without_var = {k: v for k, v in os.environ.items() if k != "OIDC_AUDIENCE"}
        with (
            patch.dict(os.environ, env_without_var, clear=True),
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

        assert "audience" not in proxy_init_args
