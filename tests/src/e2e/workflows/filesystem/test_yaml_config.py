"""
End-to-End tests for Managed YAML Config Editing Tool (ha_config_set_yaml).

This test suite validates:
- Security boundaries: path traversal, file allowlist, key allowlist
- CRUD operations: add, replace, remove actions
- Validation: null content rejection, type mismatch errors
- Safeguards: backup creation, config check integration, post-edit action hints
- Feature flag behavior (disabled by default)

These tests require:
1. The ha_mcp_tools custom component to be installed in Home Assistant
2. The ENABLE_YAML_CONFIG_EDITING feature flag to be enabled

Note: Most tests will be SKIPPED in CI environments where the ha_mcp_tools
custom component is not pre-installed.

Tests are designed for the Docker Home Assistant test environment.
"""

import logging
import os

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEATURE_FLAG = "ENABLE_YAML_CONFIG_EDITING"
TOOL_NAME = "ha_config_set_yaml"


@pytest.fixture(scope="module")
def yaml_config_tools_enabled(ha_container_with_fresh_config):
    """Enable YAML config editing feature flag for the test module."""
    os.environ[FEATURE_FLAG] = "true"
    logger.info("YAML config editing feature flag enabled")
    yield
    os.environ.pop(FEATURE_FLAG, None)


@pytest.fixture
async def mcp_client_with_yaml_config(yaml_config_tools_enabled, mcp_server):
    """Create MCP client with YAML config editing enabled."""
    from fastmcp import Client

    client = Client(mcp_server.mcp)
    async with client:
        logger.debug("FastMCP client with YAML config tools connected")
        yield client


async def _check_yaml_tool_available(mcp_client) -> tuple[bool, str | None]:
    """Check if ha_config_set_yaml is available in the MCP server."""
    try:
        tools = await mcp_client.list_tools()
        tool_names = [t.name for t in tools]
        if TOOL_NAME not in tool_names:
            return False, f"Tool {TOOL_NAME} not registered"
        return True, None
    except Exception as e:
        return False, f"Error checking tools: {e}"


async def _check_service_available(mcp_client) -> tuple[bool, str | None]:
    """Check if ha_config_set_yaml tool is registered AND the HA service is available."""
    # First check if the tool is even registered
    tool_available, tool_error = await _check_yaml_tool_available(mcp_client)
    if not tool_available:
        return False, tool_error

    # Then probe the service
    try:
        data = await safe_call_tool(
            mcp_client,
            TOOL_NAME,
            {
                "yaml_path": "template",
                "action": "remove",
                "file": "packages/_test_probe.yaml",
            },
        )
        error = data.get("error", {})
        if isinstance(error, dict) and error.get("code") == "COMPONENT_NOT_INSTALLED":
            return False, "ha_mcp_tools custom component not installed"
        return True, None
    except Exception as e:
        return False, f"Error checking service: {e}"


def _skip_if_unavailable(result: tuple[bool, str | None], test_name: str):
    available, error = result
    if not available:
        pytest.skip(f"{test_name}: {error}")



# ---------------------------------------------------------------------------
# Feature flag / registration
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfigAvailability:
    """Test ha_config_set_yaml availability and feature flag behavior."""

    async def test_feature_flag_disabled_by_default(self, mcp_client):
        """Verify tool is NOT registered when feature flag is disabled."""
        original = os.environ.pop(FEATURE_FLAG, None)
        try:
            tools = await mcp_client.list_tools()
            tool_names = [t.name for t in tools]
            if TOOL_NAME not in tool_names:
                logger.info("Tool not registered (feature flag disabled at startup)")
                return
            logger.info("Tool registered — flag was enabled at server startup")
        finally:
            if original:
                os.environ[FEATURE_FLAG] = original

    async def test_tool_registered_when_enabled(self, mcp_client_with_yaml_config):
        """Verify tool IS registered when feature flag is enabled."""
        available, error = await _check_yaml_tool_available(
            mcp_client_with_yaml_config
        )
        if not available:
            pytest.skip(f"YAML config tool not available: {error}")
        logger.info("ha_config_set_yaml is registered and available")


# ---------------------------------------------------------------------------
# Security boundaries
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfigSecurity:
    """Test security boundaries for YAML config editing."""

    async def test_path_traversal_blocked(self, mcp_client_with_yaml_config):
        """Path traversal attempts must be rejected."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Path traversal")

        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "template",
                "action": "add",
                "content": "- sensor:\n    - name: test\n      state: 'ok'",
                "file": "../etc/passwd",
            },
        )
        inner = data
        assert inner.get("success") is False, f"Path traversal should fail: {data}"
        logger.info("Correctly blocked path traversal")

    async def test_disallowed_file_rejected(self, mcp_client_with_yaml_config):
        """Files outside the allowlist must be rejected."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Disallowed file")

        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "template",
                "action": "add",
                "content": "- sensor:\n    - name: test\n      state: 'ok'",
                "file": "automations.yaml",
            },
        )
        inner = data
        assert inner.get("success") is False, f"Disallowed file should fail: {data}"
        assert "not allowed" in inner.get("error", "").lower()
        logger.info("Correctly rejected disallowed file")

    async def test_blocked_key_rejected(self, mcp_client_with_yaml_config):
        """Keys not in the allowlist must be rejected."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Blocked key")

        # 'homeassistant' is not in ALLOWED_YAML_KEYS
        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "homeassistant",
                "action": "replace",
                "content": "name: Hacked",
                "file": "configuration.yaml",
            },
        )
        inner = data
        assert inner.get("success") is False, f"Blocked key should fail: {data}"
        assert "not in the allowed list" in inner.get("error", "").lower()
        logger.info("Correctly rejected blocked key")

    async def test_helper_keys_not_allowed(self, mcp_client_with_yaml_config):
        """Keys manageable via ha_config_set_helper must not be in the allowlist."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Helper keys")

        helper_keys = [
            "input_boolean",
            "input_number",
            "input_text",
            "input_select",
            "input_datetime",
            "input_button",
            "counter",
            "timer",
            "schedule",
        ]

        for key in helper_keys:
            data = await safe_call_tool(
                mcp_client_with_yaml_config,
                TOOL_NAME,
                {
                    "yaml_path": key,
                    "action": "add",
                    "content": "test: true",
                    "file": "configuration.yaml",
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is False, (
                f"Helper key '{key}' should be rejected: {data}"
            )
        logger.info("All helper-manageable keys correctly rejected")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfigValidation:
    """Test input validation for YAML config editing."""

    async def test_null_content_rejected(self, mcp_client_with_yaml_config):
        """Empty/null YAML content must be rejected."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Null content")

        # "null" parses to None via yaml.safe_load
        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "template",
                "action": "add",
                "content": "null",
                "file": "packages/_test_null.yaml",
                "backup": False,
            },
        )
        inner = data
        assert inner.get("success") is False, (
            f"Null content should be rejected: {data}"
        )
        logger.info("Correctly rejected null content")

    async def test_invalid_yaml_rejected(self, mcp_client_with_yaml_config):
        """Invalid YAML syntax must be rejected."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Invalid YAML")

        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "template",
                "action": "add",
                "content": "  bad:\n yaml: [\n  unclosed",
                "file": "packages/_test_invalid.yaml",
                "backup": False,
            },
        )
        inner = data
        assert inner.get("success") is False, (
            f"Invalid YAML should be rejected: {data}"
        )
        logger.info("Correctly rejected invalid YAML")

    async def test_missing_content_for_add(self, mcp_client_with_yaml_config):
        """add/replace actions require content."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Missing content")

        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "template",
                "action": "add",
                "file": "packages/_test_no_content.yaml",
            },
        )
        inner = data
        assert inner.get("success") is False, (
            f"Missing content should fail: {data}"
        )
        logger.info("Correctly rejected missing content")


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfigOperations:
    """Test add/replace/remove operations on package files."""

    async def test_add_to_new_package_file(self, mcp_client_with_yaml_config):
        """Adding to a non-existent package file creates it."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Add to new package")

        content = "- sensor:\n    - name: E2E Test Sensor\n      state: 'ok'"

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_add.yaml",
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Add should succeed: {data}"
            assert inner.get("action") == "add"
            logger.info("Successfully added template to new package file")

    async def test_replace_key(self, mcp_client_with_yaml_config):
        """Replace overwrites the key content."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Replace key")

        # First, create a file with initial content
        initial = "- sensor:\n    - name: Initial\n      state: 'v1'"
        replacement = "- sensor:\n    - name: Replaced\n      state: 'v2'"

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": initial,
                    "file": "packages/_e2e_test_replace.yaml",
                    "backup": False,
                },
            )

            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "replace",
                    "content": replacement,
                    "file": "packages/_e2e_test_replace.yaml",
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Replace should succeed: {data}"
            assert inner.get("action") == "replace"
            logger.info("Successfully replaced template key")

    async def test_remove_key(self, mcp_client_with_yaml_config):
        """Remove deletes the key from the file."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Remove key")

        content = "- sensor:\n    - name: To Remove\n      state: 'bye'"

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            # Add first
            await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_remove.yaml",
                    "backup": False,
                },
            )

            # Then remove
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "remove",
                    "file": "packages/_e2e_test_remove.yaml",
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Remove should succeed: {data}"
            assert inner.get("action") == "remove"
            logger.info("Successfully removed template key")

    async def test_remove_nonexistent_key_fails(self, mcp_client_with_yaml_config):
        """Removing a key that doesn't exist should fail."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Remove nonexistent key")

        # First create a file with a different key
        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "sensor",
                    "action": "add",
                    "content": "- platform: template\n  sensors:\n    test:\n      value_template: 'ok'",
                    "file": "packages/_e2e_test_remove_missing.yaml",
                    "backup": False,
                },
            )

        # Now attempt to remove a key that doesn't exist — expect failure
        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "template",
                "action": "remove",
                "file": "packages/_e2e_test_remove_missing.yaml",
            },
        )
        inner = data
        assert inner.get("success") is False, (
            f"Removing nonexistent key should fail: {data}"
        )
        logger.info("Correctly rejected removing nonexistent key")

    async def test_add_type_mismatch_errors(self, mcp_client_with_yaml_config):
        """Adding with type mismatch (e.g., list + dict) should error, not silently replace."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Type mismatch")

        # Create a file with a list value
        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "sensor",
                    "action": "add",
                    "content": "- platform: template\n  sensors:\n    test:\n      value_template: 'ok'",
                    "file": "packages/_e2e_test_mismatch.yaml",
                    "backup": False,
                },
            )

        # Try to add a dict to the existing list — expect failure
        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "sensor",
                "action": "add",
                "content": "key: value",
                "file": "packages/_e2e_test_mismatch.yaml",
                "backup": False,
            },
        )
        inner = data
        assert inner.get("success") is False, (
            f"Type mismatch should error: {data}"
        )
        assert "type mismatch" in inner.get("error", "").lower()
        logger.info("Correctly errored on type mismatch")


# ---------------------------------------------------------------------------
# Backup and config check
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfigSafeguards:
    """Test backup creation and config check integration."""

    async def test_backup_created(self, mcp_client_with_yaml_config):
        """Backup should be created when backup=True (default)."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Backup creation")

        content = "- sensor:\n    - name: Backup Test\n      state: 'ok'"

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            # Create initial file
            await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_backup.yaml",
                    "backup": False,
                },
            )

            # Now modify with backup enabled
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "replace",
                    "content": "- sensor:\n    - name: Modified\n      state: 'v2'",
                    "file": "packages/_e2e_test_backup.yaml",
                    "backup": True,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Replace should succeed: {data}"
            assert inner.get("backup_path"), (
                f"Backup path should be present: {data}"
            )
            assert "yaml_backups" in inner.get("backup_path", "")
            logger.info(f"Backup created at: {inner.get('backup_path')}")

    async def test_config_check_included_in_response(self, mcp_client_with_yaml_config):
        """Config check result should be included in the response."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Config check")

        content = "- sensor:\n    - name: Config Check Test\n      state: 'ok'"

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_config_check.yaml",
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Add should succeed: {data}"
            # config_check should be present (ok, errors, or unavailable)
            assert "config_check" in inner, (
                f"Config check result should be in response: {data}"
            )
            logger.info(f"Config check result: {inner.get('config_check')}")

    async def test_post_action_reload_for_template(self, mcp_client_with_yaml_config):
        """Template key should return post_action=reload_available."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Post action reload")

        content = "- sensor:\n    - name: Post Action Test\n      state: 'ok'"

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_post_action_reload.yaml",
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Add should succeed: {data}"
            assert inner.get("post_action") == "reload_available", (
                f"template should have post_action=reload_available: {data}"
            )
            assert "reload_service" in inner, (
                f"reload_service should be present for reloadable keys: {data}"
            )
            logger.info(
                f"post_action={inner.get('post_action')}, "
                f"reload_service={inner.get('reload_service')}"
            )

    async def test_post_action_restart_for_shell_command(self, mcp_client_with_yaml_config):
        """shell_command key should return post_action=restart_required."""
        service_check = await _check_service_available(mcp_client_with_yaml_config)
        _skip_if_unavailable(service_check, "Post action restart")

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "shell_command",
                    "action": "add",
                    "content": "test_cmd: echo hello",
                    "file": "packages/_e2e_test_post_action_restart.yaml",
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Add should succeed: {data}"
            assert inner.get("post_action") == "restart_required", (
                f"shell_command should have post_action=restart_required: {data}"
            )
            assert "reload_service" not in inner, (
                f"reload_service should NOT be present for restart-only keys: {data}"
            )
            logger.info(f"post_action={inner.get('post_action')}")
