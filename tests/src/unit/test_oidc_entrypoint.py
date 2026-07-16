"""Unit tests for OIDC entry point (main_oidc / _run_oidc_server).

These tests verify environment variable validation, logging setup,
and the OIDC server startup path without requiring a real OIDC provider.
"""

import os
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Sentinel used by MockOIDCProxy below to distinguish "kwarg not passed" from
# "kwarg passed with a falsy/None value" for the optional constructor args.
_UNSET = object()


def _make_mock_oidc_proxy(capture: dict) -> type:
    """Build a MockOIDCProxy class pinned to production's exact kwarg set.

    Unlike a ``**kwargs``-catch-all mock, this has an explicit signature with
    exactly the kwargs ``_run_oidc_server`` passes. An unexpected kwarg (e.g.
    constructor-kwarg drift from a future FastMCP release) raises TypeError
    instead of silently passing. ``allowed_client_redirect_uris`` and
    ``verify_id_token`` are optional in production, so they default to the
    ``_UNSET`` sentinel here and are only recorded in ``capture`` when
    actually passed.
    """

    class MockOIDCProxy:
        def __init__(
            self,
            *,
            config_url,
            client_id,
            client_secret,
            base_url,
            require_authorization_consent,
            jwt_signing_key,
            allowed_client_redirect_uris=_UNSET,
            verify_id_token=_UNSET,
        ):
            capture["config_url"] = config_url
            capture["client_id"] = client_id
            capture["client_secret"] = client_secret
            capture["base_url"] = base_url
            capture["require_authorization_consent"] = require_authorization_consent
            capture["jwt_signing_key"] = jwt_signing_key
            if allowed_client_redirect_uris is not _UNSET:
                capture["allowed_client_redirect_uris"] = allowed_client_redirect_uris
            if verify_id_token is not _UNSET:
                capture["verify_id_token"] = verify_id_token

    return MockOIDCProxy


class TestMainOidcValidation:
    """Tests for main_oidc() environment variable validation."""

    _VALID_OIDC_ENV: ClassVar[dict[str, str]] = {
        "HOMEASSISTANT_URL": "http://test.local:8123",
        "HOMEASSISTANT_TOKEN": "test_token",
        "OIDC_CONFIG_URL": "https://auth.example.com/.well-known/openid-configuration",
        "OIDC_CLIENT_ID": "test-client-id",
        "OIDC_CLIENT_SECRET": "test-client-secret",
        "MCP_BASE_URL": "https://mcp.example.com",
        "LOG_LEVEL": "DEBUG",
    }

    def test_missing_all_oidc_vars_exits(self):
        """main_oidc should exit when all OIDC env vars are missing."""
        import ha_mcp.__main__ as main_module

        env = {
            "HOMEASSISTANT_URL": "http://test.local:8123",
            "HOMEASSISTANT_TOKEN": "test_token",
        }
        clean_env = dict(env)

        with (
            patch.dict(os.environ, clean_env, clear=True),
            pytest.raises(SystemExit) as exc_info,
        ):
            main_module.main_oidc()

        assert exc_info.value.code == 1

    def test_missing_single_oidc_var_exits(self):
        """main_oidc should exit when any single OIDC env var is missing."""
        import ha_mcp.__main__ as main_module

        for missing_key in [
            "OIDC_CONFIG_URL",
            "OIDC_CLIENT_ID",
            "OIDC_CLIENT_SECRET",
            "MCP_BASE_URL",
        ]:
            env = dict(self._VALID_OIDC_ENV)
            del env[missing_key]

            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(SystemExit) as exc_info:
                    main_module.main_oidc()

                assert exc_info.value.code == 1, (
                    f"Expected exit 1 when {missing_key} is missing"
                )

    def test_missing_ha_credentials_exits(self):
        """main_oidc should exit when HA credentials are missing."""
        import ha_mcp.__main__ as main_module

        env = dict(self._VALID_OIDC_ENV)
        del env["HOMEASSISTANT_URL"]
        del env["HOMEASSISTANT_TOKEN"]

        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                main_module.main_oidc()

            assert exc_info.value.code == 1

    def test_valid_config_calls_run_entrypoint(self):
        """main_oidc should call _run_entrypoint with valid config."""
        import ha_mcp.__main__ as main_module

        entrypoint_called = False

        def mock_run_entrypoint(coro, label):
            nonlocal entrypoint_called
            entrypoint_called = True
            # Close the coroutine to avoid warning
            coro.close()

        with (
            patch.dict(os.environ, self._VALID_OIDC_ENV, clear=True),
            patch.object(
                main_module, "_run_entrypoint", side_effect=mock_run_entrypoint
            ),
        ):
            main_module.main_oidc()

        assert entrypoint_called, "_run_entrypoint was not called"


class TestMainOidcLogging:
    """Tests for OIDC mode logging configuration."""

    _VALID_OIDC_ENV: ClassVar[dict[str, str]] = {
        "HOMEASSISTANT_URL": "http://test.local:8123",
        "HOMEASSISTANT_TOKEN": "test_token",
        "OIDC_CONFIG_URL": "https://auth.example.com/.well-known/openid-configuration",
        "OIDC_CLIENT_ID": "test-client-id",
        "OIDC_CLIENT_SECRET": "test-client-secret",
        "MCP_BASE_URL": "https://mcp.example.com",
    }

    def test_logging_uses_force_true(self):
        """main_oidc should call _setup_logging with force=True."""
        import ha_mcp.__main__ as main_module

        setup_logging_calls = []

        def mock_setup_logging(level, force=False):
            setup_logging_calls.append({"level": level, "force": force})

        env = dict(self._VALID_OIDC_ENV)
        env["LOG_LEVEL"] = "DEBUG"

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(main_module, "_setup_logging", side_effect=mock_setup_logging),
            patch.object(
                main_module, "_run_entrypoint", side_effect=lambda c, _l: c.close()
            ),
        ):
            main_module.main_oidc()

        assert len(setup_logging_calls) >= 1
        assert setup_logging_calls[0]["force"] is True

    def test_logging_respects_log_level_env(self):
        """main_oidc should use LOG_LEVEL env var for logging."""
        import ha_mcp.__main__ as main_module

        setup_logging_calls = []

        def mock_setup_logging(level, force=False):
            setup_logging_calls.append({"level": level, "force": force})

        env = dict(self._VALID_OIDC_ENV)
        env["LOG_LEVEL"] = "WARNING"

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(main_module, "_setup_logging", side_effect=mock_setup_logging),
            patch.object(
                main_module, "_run_entrypoint", side_effect=lambda c, _l: c.close()
            ),
        ):
            main_module.main_oidc()

        assert setup_logging_calls[0]["level"] == "WARNING"

    def test_logging_defaults_to_info(self):
        """main_oidc should default to INFO log level."""
        import ha_mcp.__main__ as main_module

        setup_logging_calls = []

        def mock_setup_logging(level, force=False):
            setup_logging_calls.append({"level": level, "force": force})

        env = dict(self._VALID_OIDC_ENV)
        # Don't set LOG_LEVEL

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(main_module, "_setup_logging", side_effect=mock_setup_logging),
            patch.object(
                main_module, "_run_entrypoint", side_effect=lambda c, _l: c.close()
            ),
        ):
            main_module.main_oidc()

        assert setup_logging_calls[0]["level"] == "INFO"


class TestRunOidcServer:
    """Tests for _run_oidc_server async function."""

    @pytest.mark.asyncio
    async def test_creates_oidc_proxy(self):
        """_run_oidc_server should create an OIDCProxy with correct args."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = _make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(
            side_effect=lambda **kwargs: fake_run_async(**kwargs)
        )
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch(
                "ha_mcp.__main__.OIDCProxy"
                if hasattr(main_module, "OIDCProxy")
                else "fastmcp.server.auth.oidc_proxy.OIDCProxy",
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "_run_with_shutdown", side_effect=noop_shutdown),
        ):
            await main_module._run_oidc_server(
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

    @pytest.mark.asyncio
    async def test_jwt_signing_key_passed_from_env(self):
        """_run_oidc_server should pass OIDC_JWT_SIGNING_KEY env var to OIDCProxy."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = _make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(
            side_effect=lambda **kwargs: fake_run_async(**kwargs)
        )
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(
                os.environ, {"OIDC_JWT_SIGNING_KEY": "test-jwt-key"}, clear=False
            ),
            patch(
                "ha_mcp.__main__.OIDCProxy"
                if hasattr(main_module, "OIDCProxy")
                else "fastmcp.server.auth.oidc_proxy.OIDCProxy",
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "_run_with_shutdown", side_effect=noop_shutdown),
        ):
            await main_module._run_oidc_server(
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
        """_run_oidc_server should pass None for jwt_signing_key when env var is not set."""
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = _make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(
            side_effect=lambda **kwargs: fake_run_async(**kwargs)
        )
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        env_without_key = {
            k: v for k, v in os.environ.items() if k != "OIDC_JWT_SIGNING_KEY"
        }
        with (
            patch.dict(os.environ, env_without_key, clear=True),
            patch(
                "ha_mcp.__main__.OIDCProxy"
                if hasattr(main_module, "OIDCProxy")
                else "fastmcp.server.auth.oidc_proxy.OIDCProxy",
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "_run_with_shutdown", side_effect=noop_shutdown),
        ):
            await main_module._run_oidc_server(
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
        """_run_oidc_server should set auth on the MCP instance."""
        import ha_mcp.__main__ as main_module

        mock_auth = MagicMock()
        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        # Use a regular function that returns a coroutine-like object
        # to avoid unawaited coroutine warnings from AsyncMock
        async def fake_coro(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(side_effect=lambda **kwargs: fake_coro(**kwargs))
        mock_server.mcp = mock_mcp

        async def capture_run_with_shutdown(coro):
            # Close the coroutine to avoid warnings
            coro.close()

        with (
            patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", return_value=mock_auth),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(
                main_module, "_run_with_shutdown", side_effect=capture_run_with_shutdown
            ),
        ):
            await main_module._run_oidc_server(
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
        """_run_oidc_server should use the "http" transport, matching sibling HTTP modes."""
        import ha_mcp.__main__ as main_module

        run_kwargs = {}

        async def capture_run_with_shutdown(coro):
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
            patch("fastmcp.server.auth.oidc_proxy.OIDCProxy", return_value=MagicMock()),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(
                main_module, "_run_with_shutdown", side_effect=capture_run_with_shutdown
            ),
        ):
            await main_module._run_oidc_server(
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
        """_run_oidc_server must default the Host/Origin guard off.

        Regression test for the real bug fixed in commit 79bdc6d8: OIDC mode is
        deployed behind a public reverse proxy or LAN hostname, so FastMCP's
        DNS-rebinding guard must be defaulted off before the OIDCProxy runs, or
        proxied requests get rejected with 403/421 before OIDC auth even runs.
        """
        import ha_mcp.__main__ as main_module

        proxy_init_args: dict = {}
        MockOIDCProxy = _make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(
            side_effect=lambda **kwargs: fake_run_async(**kwargs)
        )
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch(
                "ha_mcp.transport_security.ensure_host_origin_guard_default_off"
            ) as mock_guard,
            patch(
                "ha_mcp.__main__.OIDCProxy"
                if hasattr(main_module, "OIDCProxy")
                else "fastmcp.server.auth.oidc_proxy.OIDCProxy",
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "_run_with_shutdown", side_effect=noop_shutdown),
        ):
            await main_module._run_oidc_server(
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
        MockOIDCProxy = _make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(
            side_effect=lambda **kwargs: fake_run_async(**kwargs)
        )
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
                "ha_mcp.__main__.OIDCProxy"
                if hasattr(main_module, "OIDCProxy")
                else "fastmcp.server.auth.oidc_proxy.OIDCProxy",
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "_run_with_shutdown", side_effect=noop_shutdown),
        ):
            await main_module._run_oidc_server(
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
        MockOIDCProxy = _make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(
            side_effect=lambda **kwargs: fake_run_async(**kwargs)
        )
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
                "ha_mcp.__main__.OIDCProxy"
                if hasattr(main_module, "OIDCProxy")
                else "fastmcp.server.auth.oidc_proxy.OIDCProxy",
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "_run_with_shutdown", side_effect=noop_shutdown),
        ):
            await main_module._run_oidc_server(
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
        MockOIDCProxy = _make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(
            side_effect=lambda **kwargs: fake_run_async(**kwargs)
        )
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(os.environ, {"OIDC_VERIFY_ID_TOKEN": "true"}, clear=False),
            patch(
                "ha_mcp.__main__.OIDCProxy"
                if hasattr(main_module, "OIDCProxy")
                else "fastmcp.server.auth.oidc_proxy.OIDCProxy",
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "_run_with_shutdown", side_effect=noop_shutdown),
        ):
            await main_module._run_oidc_server(
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
        MockOIDCProxy = _make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(
            side_effect=lambda **kwargs: fake_run_async(**kwargs)
        )
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        env_without_var = {
            k: v for k, v in os.environ.items() if k != "OIDC_VERIFY_ID_TOKEN"
        }
        with (
            patch.dict(os.environ, env_without_var, clear=True),
            patch(
                "ha_mcp.__main__.OIDCProxy"
                if hasattr(main_module, "OIDCProxy")
                else "fastmcp.server.auth.oidc_proxy.OIDCProxy",
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "_run_with_shutdown", side_effect=noop_shutdown),
        ):
            await main_module._run_oidc_server(
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
        MockOIDCProxy = _make_mock_oidc_proxy(proxy_init_args)

        mock_server = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.get_tools = AsyncMock(return_value=[])

        async def fake_run_async(**kwargs):
            pass

        mock_mcp.run_async = MagicMock(
            side_effect=lambda **kwargs: fake_run_async(**kwargs)
        )
        mock_server.mcp = mock_mcp

        async def noop_shutdown(coro):
            coro.close()

        with (
            patch.dict(os.environ, {"OIDC_JWT_SIGNING_KEY": ""}, clear=False),
            patch(
                "ha_mcp.__main__.OIDCProxy"
                if hasattr(main_module, "OIDCProxy")
                else "fastmcp.server.auth.oidc_proxy.OIDCProxy",
                MockOIDCProxy,
            ),
            patch(
                "ha_mcp.server.HomeAssistantSmartMCPServer", return_value=mock_server
            ),
            patch.object(main_module, "_run_with_shutdown", side_effect=noop_shutdown),
        ):
            await main_module._run_oidc_server(
                config_url="https://auth.example.com/.well-known/openid-configuration",
                client_id="test-id",
                client_secret="test-secret",
                base_url="https://mcp.example.com",
                host="0.0.0.0",
                port=8086,
                path="/mcp",
            )

        assert proxy_init_args["jwt_signing_key"] is None
