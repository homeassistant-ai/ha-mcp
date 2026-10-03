"""Public package exports stay compatible without eager startup imports."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _run_python(program: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(program)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_importing_package_and_main_does_not_import_server_or_auth() -> None:
    result = _run_python(
        """
        import sys
        import ha_mcp
        assert 'ha_mcp.server' not in sys.modules
        assert 'ha_mcp.auth' not in sys.modules
        assert 'ha_mcp.client.rest_client' not in sys.modules
        import ha_mcp.__main__
        assert 'ha_mcp.server' not in sys.modules
        assert 'ha_mcp.auth' not in sys.modules
        assert 'ha_mcp.client.rest_client' not in sys.modules
        """
    )
    assert result.returncode == 0, result.stderr


def test_public_exports_and_package_introspection_remain_compatible() -> None:
    result = _run_python(
        """
        import ha_mcp
        from ha_mcp import (
            Settings, HomeAssistantClient, HomeAssistantSmartMCPServer,
            HomeAssistantOAuthProvider, ErrorCode, create_error_response,
            create_connection_error, create_auth_error,
            create_entity_not_found_error,
            create_entity_unavailable_after_dispatch_error,
            create_service_error, create_validation_error,
            create_config_error, create_timeout_error, is_error_response,
            get_error_code, get_error_message,
        )
        expected = {
            'Settings': ('ha_mcp.config', 'Settings'),
            'HomeAssistantClient': ('ha_mcp.client.rest_client', 'HomeAssistantClient'),
            'HomeAssistantSmartMCPServer': ('ha_mcp.server', 'HomeAssistantSmartMCPServer'),
            'HomeAssistantOAuthProvider': ('ha_mcp.auth', 'HomeAssistantOAuthProvider'),
            'ErrorCode': ('ha_mcp.errors', 'ErrorCode'),
            'create_error_response': ('ha_mcp.errors', 'create_error_response'),
            'create_connection_error': ('ha_mcp.errors', 'create_connection_error'),
            'create_auth_error': ('ha_mcp.errors', 'create_auth_error'),
            'create_entity_not_found_error': ('ha_mcp.errors', 'create_entity_not_found_error'),
            'create_entity_unavailable_after_dispatch_error': ('ha_mcp.errors', 'create_entity_unavailable_after_dispatch_error'),
            'create_service_error': ('ha_mcp.errors', 'create_service_error'),
            'create_validation_error': ('ha_mcp.errors', 'create_validation_error'),
            'create_config_error': ('ha_mcp.errors', 'create_config_error'),
            'create_timeout_error': ('ha_mcp.errors', 'create_timeout_error'),
            'is_error_response': ('ha_mcp.errors', 'is_error_response'),
            'get_error_code': ('ha_mcp.errors', 'get_error_code'),
            'get_error_message': ('ha_mcp.errors', 'get_error_message'),
        }
        from importlib import import_module
        for name, (module_name, attribute) in expected.items():
            assert globals()[name] is getattr(import_module(module_name), attribute)
        assert set(ha_mcp.__all__) == set(expected)
        assert set(expected) <= set(dir(ha_mcp))
        """
    )
    assert result.returncode == 0, result.stderr


def test_embedded_connection_can_be_set_before_server_construction() -> None:
    result = _run_python(
        """
        import ha_mcp
        from ha_mcp import config
        assert config._settings is None
        config.set_embedded_connection('http://127.0.0.1:8123', 'embedded-token')
        assert config._settings is None
        settings = config.get_global_settings()
        assert settings.homeassistant_url == 'http://127.0.0.1:8123'
        assert settings.homeassistant_token == 'embedded-token'
        """
    )
    assert result.returncode == 0, result.stderr
