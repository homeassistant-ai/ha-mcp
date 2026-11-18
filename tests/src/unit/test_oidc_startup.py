"""OIDC command validation and logging configuration."""

import logging
import os
from typing import ClassVar
from unittest.mock import patch

import pytest


class TestMainOidcValidation:
    """Tests for main_oidc() environment variable validation."""

    VALID_OIDC_ENV: ClassVar[dict[str, str]] = {
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
            env = dict(self.VALID_OIDC_ENV)
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

        env = dict(self.VALID_OIDC_ENV)
        del env["HOMEASSISTANT_URL"]
        del env["HOMEASSISTANT_TOKEN"]

        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                main_module.main_oidc()

            assert exc_info.value.code == 1

    def test_valid_config_calls_run_entrypoint(self):
        """main_oidc should call run_entrypoint with valid config."""
        import ha_mcp.__main__ as main_module

        entrypoint_called = False

        def mock_run_entrypoint(coro, label):
            nonlocal entrypoint_called
            entrypoint_called = True
            # Close the coroutine to avoid warning
            coro.close()

        with (
            patch.dict(os.environ, self.VALID_OIDC_ENV, clear=True),
            patch.object(
                main_module, "run_entrypoint", side_effect=mock_run_entrypoint
            ),
        ):
            main_module.main_oidc()

        assert entrypoint_called, "run_entrypoint was not called"

    def test_run_oidc_server_args_wired_correctly(self):
        """main_oidc must bind each env var to the matching run_oidc_server param.

        Regression test for the call site using seven positional args (four
        adjacent strings) with nothing asserting they land in the right
        parameter. Uses four distinct sentinel-like values so a transposition
        (e.g. client_id/client_secret swapped) would be caught.
        """
        import ha_mcp.__main__ as main_module

        env = dict(self.VALID_OIDC_ENV)
        env["OIDC_CONFIG_URL"] = "https://config-url.example.com/sentinel"
        env["OIDC_CLIENT_ID"] = "sentinel-client-id"
        env["OIDC_CLIENT_SECRET"] = "sentinel-client-secret"
        env["MCP_BASE_URL"] = "https://base-url.example.com/sentinel"

        captured_bound = {}

        def mock_run_entrypoint(coro, label):
            # Bound parameters are visible on a not-yet-started coroutine's
            # frame locals; read them before closing (close() clears cr_frame).
            captured_bound.update(coro.cr_frame.f_locals)
            coro.close()

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                main_module, "run_entrypoint", side_effect=mock_run_entrypoint
            ),
        ):
            main_module.main_oidc()

        assert captured_bound["config_url"] == "https://config-url.example.com/sentinel"
        assert captured_bound["client_id"] == "sentinel-client-id"
        assert captured_bound["client_secret"] == "sentinel-client-secret"
        assert captured_bound["base_url"] == "https://base-url.example.com/sentinel"


class TestMainOidcLogging:
    """Tests for OIDC mode logging configuration."""

    VALID_OIDC_ENV: ClassVar[dict[str, str]] = {
        "HOMEASSISTANT_URL": "http://test.local:8123",
        "HOMEASSISTANT_TOKEN": "test_token",
        "OIDC_CONFIG_URL": "https://auth.example.com/.well-known/openid-configuration",
        "OIDC_CLIENT_ID": "test-client-id",
        "OIDC_CLIENT_SECRET": "test-client-secret",
        "MCP_BASE_URL": "https://mcp.example.com",
    }

    def test_logging_uses_force_true(self):
        """main_oidc should call setup_logging with force=True."""
        import ha_mcp.__main__ as main_module

        setup_logging_calls = []

        def mock_setup_logging(level, force=False):
            setup_logging_calls.append({"level": level, "force": force})

        env = dict(self.VALID_OIDC_ENV)
        env["LOG_LEVEL"] = "DEBUG"

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(main_module, "setup_logging", side_effect=mock_setup_logging),
            patch.object(
                main_module, "run_entrypoint", side_effect=lambda c, _l: c.close()
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

        env = dict(self.VALID_OIDC_ENV)
        env["LOG_LEVEL"] = "WARNING"

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(main_module, "setup_logging", side_effect=mock_setup_logging),
            patch.object(
                main_module, "run_entrypoint", side_effect=lambda c, _l: c.close()
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

        env = dict(self.VALID_OIDC_ENV)
        # Don't set LOG_LEVEL

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(main_module, "setup_logging", side_effect=mock_setup_logging),
            patch.object(
                main_module, "run_entrypoint", side_effect=lambda c, _l: c.close()
            ),
        ):
            main_module.main_oidc()

        assert setup_logging_calls[0]["level"] == "INFO"

    def test_setup_logging_configures_fastmcp_logger(self, monkeypatch):
        """LOG_LEVEL should apply to FastMCP's non-propagating logger."""
        import ha_mcp.__main__ as main_module
        from ha_mcp._vendor import fastmcp

        fastmcp_logger = logging.getLogger("fastmcp")
        streamable_http_logger = logging.getLogger("mcp.server.streamable_http")
        fastmcp_server_logger = logging.getLogger("fastmcp.server.server")
        original_level = fastmcp_logger.level
        original_handlers = fastmcp_logger.handlers[:]
        original_propagate = fastmcp_logger.propagate
        original_formatters = [handler.formatter for handler in original_handlers]
        original_streamable_http_filters = streamable_http_logger.filters[:]
        original_fastmcp_server_filters = fastmcp_server_logger.filters[:]
        try:
            monkeypatch.setattr(fastmcp.settings, "log_enabled", True)
            fastmcp_logger.setLevel(logging.INFO)
            main_module.setup_logging("DEBUG", force=False)
            assert fastmcp_logger.getEffectiveLevel() == logging.DEBUG
            assert fastmcp_logger.handlers == original_handlers
            assert fastmcp_logger.propagate is original_propagate
            assert all(
                handler.formatter is formatter
                for handler, formatter in zip(
                    fastmcp_logger.handlers, original_formatters, strict=True
                )
            )
        finally:
            fastmcp_logger.setLevel(original_level)
            fastmcp_logger.handlers[:] = original_handlers
            fastmcp_logger.propagate = original_propagate
            for handler, formatter in zip(
                original_handlers, original_formatters, strict=True
            ):
                handler.setFormatter(formatter)
            streamable_http_logger.filters[:] = original_streamable_http_filters
            fastmcp_server_logger.filters[:] = original_fastmcp_server_filters

    def test_setup_logging_preserves_fastmcp_logging_opt_out(self, monkeypatch, caplog):
        """FASTMCP_LOG_ENABLED=false should suppress FastMCP records."""
        import ha_mcp.__main__ as main_module
        from ha_mcp._vendor import fastmcp

        fastmcp_logger = logging.getLogger("fastmcp")
        fastmcp_server_logger = logging.getLogger("fastmcp.server.server")
        streamable_http_logger = logging.getLogger("mcp.server.streamable_http")
        original_fastmcp_state = (
            fastmcp_logger.level,
            fastmcp_logger.handlers[:],
            fastmcp_logger.propagate,
            fastmcp_logger.disabled,
        )
        original_server_state = (
            fastmcp_server_logger.level,
            fastmcp_server_logger.handlers[:],
            fastmcp_server_logger.propagate,
            fastmcp_server_logger.disabled,
            fastmcp_server_logger.filters[:],
        )
        original_streamable_http_filters = streamable_http_logger.filters[:]
        try:
            monkeypatch.setattr(fastmcp.settings, "log_enabled", False)
            # Reproduce FastMCP's fresh disabled-startup state: configure_logging
            # returns before installing handlers or disabling propagation.
            fastmcp_logger.setLevel(logging.NOTSET)
            fastmcp_logger.handlers.clear()
            fastmcp_logger.propagate = True
            fastmcp_logger.disabled = False
            fastmcp_server_logger.setLevel(logging.NOTSET)
            fastmcp_server_logger.handlers.clear()
            fastmcp_server_logger.propagate = True
            fastmcp_server_logger.disabled = False

            with caplog.at_level(logging.DEBUG):
                main_module.setup_logging("DEBUG", force=False)
                fastmcp_server_logger.critical("disabled-fastmcp-sentinel")

            assert "disabled-fastmcp-sentinel" not in caplog.messages
        finally:
            (
                fastmcp_logger.level,
                fastmcp_logger.handlers[:],
                fastmcp_logger.propagate,
                fastmcp_logger.disabled,
            ) = original_fastmcp_state
            (
                fastmcp_server_logger.level,
                fastmcp_server_logger.handlers[:],
                fastmcp_server_logger.propagate,
                fastmcp_server_logger.disabled,
                fastmcp_server_logger.filters[:],
            ) = original_server_state
            streamable_http_logger.filters[:] = original_streamable_http_filters
