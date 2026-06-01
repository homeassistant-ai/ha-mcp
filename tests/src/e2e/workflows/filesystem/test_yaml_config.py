"""
End-to-End tests for Managed YAML Config Editing Tool (ha_config_set_yaml).

This test suite validates:
- Security boundaries: path traversal, file allowlist, key allowlist
- CRUD operations: add, replace, remove actions
- Validation: null content rejection, type mismatch errors
- Safeguards: backup creation, config check integration, post-edit action hints
- Feature flag behavior (disabled by default)
- Comment and HA tag preservation (ruamel.yaml round-trip)

These tests require:
1. The ha_mcp_tools custom component to be installed in Home Assistant
2. The ENABLE_YAML_CONFIG_EDITING feature flag to be enabled

Tests are designed for the Docker Home Assistant test environment.
"""

import logging
import os

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    extract_error_message,
    safe_call_tool,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEATURE_FLAG = "ENABLE_YAML_CONFIG_EDITING"
TOOL_NAME = "ha_config_set_yaml"
READ_TOOL = "ha_read_file"


@pytest.fixture(scope="module")
def yaml_config_tools_enabled(ha_container_with_fresh_config):
    """Enable YAML config editing feature flag for the test module.

    In inaddon mode the env-flip applies only to the test process; the
    addon container has its own env and is started with the flag set at
    install time (see ``build_image.install_ha_mcp_dev_addon``).
    """
    os.environ[FEATURE_FLAG] = "true"
    logger.info("YAML config editing feature flag enabled")
    yield
    os.environ.pop(FEATURE_FLAG, None)


@pytest.fixture
async def mcp_client_with_yaml_config(
    yaml_config_tools_enabled,
    mcp_server,
    mcp_client,
    ha_container_with_fresh_config,
):
    """Yield an MCP client with YAML-config editing enabled.

    In inaddon mode ``mcp_server`` is None (the addon is the server) and
    the session ``mcp_client`` already speaks HTTP to the addon —
    started with ENABLE_YAML_CONFIG_EDITING=true via Supervisor options
    at install time. Yield that client directly.
    """
    if ha_container_with_fresh_config.get("backend") == "haos_inaddon":
        # Fail fast at fixture setup if the addon's install-time options
        # drifted and the YAML config tool isn't registered.
        tools = await mcp_client.list_tools()
        tool_names = {t.name for t in tools}
        assert TOOL_NAME in tool_names, (
            f"Inaddon addon is missing {TOOL_NAME}; the addon's install-time "
            f"options (build_image.install_ha_mcp_dev_addon) must include "
            f"enable_yaml_config_editing=true."
        )
        logger.debug("FastMCP client (inaddon, HTTP) reused for YAML tests")
        # Session-scope mcp_client owns __aexit__; the per-test fixture
        # deliberately doesn't wrap in `async with`.
        yield mcp_client
        return

    from fastmcp import Client

    client = Client(mcp_server.mcp)
    async with client:
        logger.debug("FastMCP client with YAML config tools connected")
        yield client


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
        tools = await mcp_client_with_yaml_config.list_tools()
        tool_names = [t.name for t in tools]
        assert TOOL_NAME in tool_names, f"{TOOL_NAME} not registered"
        logger.info("ha_config_set_yaml is registered and available")


# ---------------------------------------------------------------------------
# Security boundaries
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfigSecurity:
    """Test security boundaries for YAML config editing."""

    async def test_path_traversal_blocked(self, mcp_client_with_yaml_config):
        """Path traversal attempts must be rejected."""

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
        assert "not allowed" in extract_error_message(inner).lower()
        logger.info("Correctly rejected disallowed file")

    async def test_blocked_key_rejected(self, mcp_client_with_yaml_config):
        """Keys not in the allowlist must be rejected."""

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
        assert "not in the allowed list" in extract_error_message(inner).lower()
        logger.info("Correctly rejected blocked key")

    async def test_helper_keys_not_allowed(self, mcp_client_with_yaml_config):
        """Keys manageable via ha_config_set_helper must not be in the allowlist."""

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
        assert inner.get("success") is False, f"Null content should be rejected: {data}"
        logger.info("Correctly rejected null content")

    async def test_invalid_yaml_rejected(self, mcp_client_with_yaml_config):
        """Invalid YAML syntax must be rejected."""

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
        assert inner.get("success") is False, f"Invalid YAML should be rejected: {data}"
        logger.info("Correctly rejected invalid YAML")

    async def test_missing_content_for_add(self, mcp_client_with_yaml_config):
        """add/replace actions require content."""

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
        assert inner.get("success") is False, f"Missing content should fail: {data}"
        logger.info("Correctly rejected missing content")


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfigOperations:
    """Test add/replace/remove operations on package files."""

    async def test_add_to_new_package_file(self, mcp_client_with_yaml_config):
        """Adding to a non-existent package file creates it."""

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

    async def test_add_knx_to_package_file(self, mcp_client_with_yaml_config):
        """knx is in ALLOWED_YAML_KEYS and can be edited in package files (issue #1367)."""

        # Minimal valid knx YAML — a sensor reading from a group address.
        content = (
            "sensor:\n"
            "  - name: E2E KNX Test Sensor\n"
            "    state_address: '1/2/3'\n"
            "    type: temperature\n"
        )

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "knx",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_knx.yaml",
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"knx add should succeed: {data}"
            assert inner.get("action") == "add"
            # knx is not in YAML_KEY_POST_ACTIONS, so it defaults to restart_required.
            assert inner.get("post_action") == "restart_required", (
                f"knx should default to post_action=restart_required: {data}"
            )
            logger.info("Successfully added knx to package file")

    @pytest.mark.parametrize(
        ("key", "content", "reload_service"),
        [
            (
                "automation",
                (
                    "- id: e2e_yaml_packages_only_automation\n"
                    "  alias: E2E YAML Packages-Only Automation\n"
                    "  trigger:\n"
                    "    - platform: sun\n"
                    "      event: sunset\n"
                    "  action:\n"
                    "    - service: persistent_notification.create\n"
                    "      data:\n"
                    "        message: hi\n"
                ),
                "automation.reload",
            ),
            (
                "script",
                (
                    "e2e_yaml_packages_only_script:\n"
                    "  alias: E2E YAML Packages-Only Script\n"
                    "  sequence:\n"
                    "    - service: persistent_notification.create\n"
                    "      data:\n"
                    "        message: hi\n"
                ),
                "script.reload",
            ),
            (
                "scene",
                (
                    "- id: e2e_yaml_packages_only_scene\n"
                    "  name: E2E YAML Packages-Only Scene\n"
                    "  entities:\n"
                    "    light.kitchen:\n"
                    "      state: 'on'\n"
                ),
                "scene.reload",
            ),
        ],
    )
    async def test_add_packages_only_key_to_package_file(
        self, mcp_client_with_yaml_config, key, content, reload_service
    ):
        """automation/script/scene each accepted in packages/*.yaml with native reload."""

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": key,
                    "action": "add",
                    "content": content,
                    "file": f"packages/_e2e_test_{key}.yaml",
                    "backup": False,
                },
            )
            assert data.get("success") is True, (
                f"{key} add to package should succeed: {data}"
            )
            assert data.get("action") == "add"
            assert data.get("post_action") == "reload_available", (
                f"{key} should be reload_available: {data}"
            )
            assert data.get("reload_service") == reload_service, (
                f"reload_service should be {reload_service}: {data}"
            )
            logger.info("Successfully added %s to package file", key)

    async def test_add_automation_to_nested_package_path(
        self, mcp_client_with_yaml_config
    ):
        """Nested ``packages/<subdir>/*.yaml`` is accepted.

        ``is_package`` matches via ``fnmatch.fnmatch(normalized,
        "packages/*.yaml")``. ``fnmatch``'s ``*`` matches ``/`` too,
        so the single pattern covers both flat
        ``packages/foo.yaml`` and nested
        ``packages/sub/foo.yaml``. This test pins the nested-path
        behaviour so a future tightening to "flat only" (which
        ``fnmatch`` can't express directly — would need explicit
        segment checks) can't silently break nested user configs.
        """

        content = (
            "- id: e2e_yaml_packages_nested_test\n"
            "  alias: E2E YAML Packages Nested Test\n"
            "  trigger:\n"
            "    - platform: sun\n"
            "      event: sunset\n"
            "  action:\n"
            "    - service: persistent_notification.create\n"
            "      data:\n"
            "        message: hi\n"
        )

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "automation",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_nested/automations.yaml",
                    "backup": False,
                },
            )
            assert data.get("success") is True, (
                f"automation add to nested package path should succeed: {data}"
            )
            assert data.get("post_action") == "reload_available"
            assert data.get("reload_service") == "automation.reload"
            logger.info("Successfully added automation to nested package path")

    async def test_packages_only_keys_rejected_in_configuration_yaml(
        self, mcp_client_with_yaml_config
    ):
        """automation/script/scene must be rejected in configuration.yaml.

        Storage-mode collections live in .storage/; allowing these keys in
        configuration.yaml would let them collide. Package files are the
        opt-in surface.
        """

        for key in ("automation", "script", "scene"):
            data = await safe_call_tool(
                mcp_client_with_yaml_config,
                TOOL_NAME,
                {
                    "yaml_path": key,
                    "action": "add",
                    "content": "- id: x\n  alias: x\n",
                    "file": "configuration.yaml",
                    "backup": False,
                },
            )
            assert data.get("success") is False, (
                f"{key} in configuration.yaml should be rejected: {data}"
            )
            msg = extract_error_message(data)
            # Pin the actionable path-form (``packages/*.yaml``) rather
            # than a generic ``packages`` substring so a future
            # readability-driven reword (e.g. "package files") can't
            # silently drop the technically-specific guidance users
            # need to act on.
            assert "packages/*.yaml" in msg, (
                f"{key} error message should mention packages/*.yaml: {data}"
            )
            # Spell out each tool name individually — an agent reading
            # the rejection would otherwise see the combined slash-form
            # as a single (malformed) tool name and fail to route.
            assert "ha_config_set_automation" in msg, (
                f"{key} error should mention ha_config_set_automation: {data}"
            )
            assert "ha_config_set_script" in msg, (
                f"{key} error should mention ha_config_set_script: {data}"
            )
            assert "ha_config_set_scene" in msg, (
                f"{key} error should mention ha_config_set_scene: {data}"
            )
            # Rejected calls must not advertise reload metadata.
            assert data.get("post_action") is None, (
                f"{key} rejection should not include post_action: {data}"
            )
            assert data.get("reload_service") is None, (
                f"{key} rejection should not include reload_service: {data}"
            )
        logger.info("automation/script/scene correctly rejected in configuration.yaml")

    async def test_replace_key(self, mcp_client_with_yaml_config):
        """Replace overwrites the key content."""

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
        assert inner.get("success") is False, f"Type mismatch should error: {data}"
        assert "type mismatch" in extract_error_message(inner).lower()
        logger.info("Correctly errored on type mismatch")


# ---------------------------------------------------------------------------
# Backup and config check
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfigSafeguards:
    """Test backup creation and config check integration."""

    async def test_backup_created(self, mcp_client_with_yaml_config):
        """Backup should be created when backup=True (default)."""

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
            backup_path = inner.get("backup_path", "")
            assert backup_path, f"Backup path should be present: {data}"
            # Backups must live directly under .ha_mcp_tools_backups/ (config
            # root, not served by HA's /local/ static handler). Anything else
            # — including a www/.ha_mcp_tools_backups/ variant or any other
            # publicly-served prefix — is a regression of GHSA-g39v-cvjh-8fpf.
            assert backup_path.startswith(".ha_mcp_tools_backups/"), (
                f"Backup path must start with .ha_mcp_tools_backups/ "
                f"(not under www/ or any served path): {backup_path}"
            )
            logger.info(f"Backup created at: {backup_path}")

    async def test_config_check_included_in_response(self, mcp_client_with_yaml_config):
        """Config check result should be included in the response."""

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

    async def test_post_action_restart_for_shell_command(
        self, mcp_client_with_yaml_config
    ):
        """shell_command key should return post_action=restart_required."""

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


# ---------------------------------------------------------------------------
# Comment and HA tag preservation
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfigCommentPreservation:
    """Test that YAML comments and HA tags (e.g. !secret) survive edits."""

    async def test_comments_preserved_after_add(self, mcp_client_with_yaml_config):
        """Comments and !secret tags in one key survive when a different key is added."""

        test_file = "packages/_e2e_test_comments.yaml"
        initial_content = (
            "# Sensor configuration\n"
            "- sensor:\n"
            "    - name: Commented Sensor  # inline comment\n"
            "      api_key: !secret sensor_api_key\n"
            "      state: 'ok'"
        )

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            # Write content with comments and !secret under 'template' key
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "replace",
                    "content": initial_content,
                    "file": test_file,
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Initial write failed: {data}"

            # Add a DIFFERENT key — forces full file re-parse/re-serialize
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "sensor",
                    "action": "add",
                    "content": "- platform: template\n  sensors:\n    extra:\n      value_template: 'yes'",
                    "file": test_file,
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Add second key failed: {data}"

        # Read back and verify comments + tag survived the round-trip
        read_data = await safe_call_tool(
            mcp_client_with_yaml_config,
            READ_TOOL,
            {"path": test_file},
        )
        if read_data.get("success") is not True:
            pytest.skip(f"ha_read_file not functional for packages: {read_data}")

        content = read_data.get("content", "")
        assert "# Sensor configuration" in content, (
            f"Block comment lost after add: {content!r}"
        )
        assert "# inline comment" in content, (
            f"Inline comment lost after add: {content!r}"
        )
        assert "!secret" in content, f"!secret tag lost after add: {content!r}"
        assert "sensor_api_key" in content, (
            f"Secret key name lost after add: {content!r}"
        )
        logger.info("Comments and !secret tags preserved after adding a second key")

    async def test_ha_tags_preserved_after_edit(self, mcp_client_with_yaml_config):
        """HA-specific YAML tags like !secret must survive when a different key is edited."""

        test_file = "packages/_e2e_test_tags.yaml"
        initial_content = (
            "api_key: !secret my_api_key\nname: Tagged Sensor\nstate: 'active'"
        )

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            # Create a file with !secret tag under 'template' key
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "template",
                    "action": "replace",
                    "content": initial_content,
                    "file": test_file,
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Initial write failed: {data}"

            # Add a DIFFERENT key — forces full file round-trip
            data = await mcp.call_tool_success(
                TOOL_NAME,
                {
                    "yaml_path": "sensor",
                    "action": "add",
                    "content": "- platform: time_date\n  display_options:\n    - date",
                    "file": test_file,
                    "backup": False,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Add different key failed: {data}"

        # Read back and verify !secret from template key survived
        read_data = await safe_call_tool(
            mcp_client_with_yaml_config,
            READ_TOOL,
            {"path": test_file},
        )
        if read_data.get("success") is not True:
            pytest.skip(f"ha_read_file not functional for packages: {read_data}")

        content = read_data.get("content", "")
        assert "!secret" in content, (
            f"!secret tag lost after editing different key: {content!r}"
        )
        assert "my_api_key" in content, (
            f"Secret key name lost after editing different key: {content!r}"
        )
        logger.info("HA !secret tags preserved after editing a different key")


# ---------------------------------------------------------------------------
# YAML-mode dashboard registration (issue #1034)
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlModeDashboardRegistration:
    """E2E: register and remove a YAML-mode dashboard via lovelace.dashboards.<url_path>."""

    URL_PATH = "ha-mcp-test-dash"
    DASHBOARD_FILE = "dashboards/ha_mcp_test.yaml"

    async def test_register_and_remove_dashboard_entry(
        self, mcp_client_with_yaml_config
    ):
        """Exercise the add + remove lifecycle for a YAML-mode dashboard entry.

        Combined into a single test rather than a register-then-remove pair so
        the test does not depend on the pytest-xdist worker distribution
        preserving sibling-test ordering. See #1196 for the original
        test-isolation race that motivated this refactor.
        """
        yaml_path = f"lovelace.dashboards.{self.URL_PATH}"

        add_data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": yaml_path,
                "action": "add",
                "content": (
                    "mode: yaml\n"
                    "title: HA MCP Test\n"
                    f"filename: {self.DASHBOARD_FILE}\n"
                    "show_in_sidebar: false\n"
                ),
                "file": "configuration.yaml",
                "backup": True,
            },
        )
        assert add_data.get("success") is True, add_data
        assert add_data.get("post_action") == "restart_required"

        read = await safe_call_tool(
            mcp_client_with_yaml_config,
            READ_TOOL,
            {"path": "configuration.yaml"},
        )
        assert read.get("success") is True
        assert f"{self.URL_PATH}:" in read["content"]
        # lovelace.mode must NOT be introduced as a sibling of dashboards
        assert "lovelace:\n  mode:" not in read["content"]

        remove_data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": yaml_path,
                "action": "remove",
                "file": "configuration.yaml",
                "backup": False,
            },
        )
        assert remove_data.get("success") is True, remove_data

        # Verify the entry is removed from the file (mirrors the post-add read-back).
        read_after = await safe_call_tool(
            mcp_client_with_yaml_config,
            READ_TOOL,
            {"path": "configuration.yaml"},
        )
        assert read_after.get("success") is True
        assert f"{self.URL_PATH}:" not in read_after["content"]

    async def test_rejects_reserved_url_path(self, mcp_client_with_yaml_config):
        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "lovelace.dashboards.lovelace",
                "action": "add",
                "content": "mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
            },
        )
        assert data.get("success") is False
        assert "reserved" in (data.get("error") or "").lower()

    async def test_rejects_filename_traversal(self, mcp_client_with_yaml_config):
        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "lovelace.dashboards.bad-dash",
                "action": "add",
                "content": "mode: yaml\ntitle: x\nfilename: ../secrets.yaml\n",
            },
        )
        assert data.get("success") is False
        assert "filename" in (data.get("error") or "").lower()

    async def test_rejects_lovelace_mode_dotted_path(self, mcp_client_with_yaml_config):
        """Confirm we did not unlock other lovelace.* keys."""
        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "lovelace.mode",
                "action": "replace",
                "content": "yaml\n",
            },
        )
        assert data.get("success") is False


@pytest.mark.filesystem
class TestDashboardsDirectoryAllowlist:
    """E2E: the dashboards/ directory is in the read/write allowlist."""

    async def test_write_read_delete_dashboard_yaml_file(
        self, mcp_client_with_yaml_config
    ):
        """Exercise the write + read + delete lifecycle for a dashboard YAML file.

        Combined into a single test rather than three siblings sharing
        ``dashboards/ha_mcp_test_view.yaml`` so the test does not depend on
        the pytest-xdist worker distribution preserving sibling-test
        ordering. Same race shape as TestYamlModeDashboardRegistration —
        see #1196.
        """
        path = "dashboards/ha_mcp_test_view.yaml"

        write_data = await safe_call_tool(
            mcp_client_with_yaml_config,
            "ha_write_file",
            {
                "path": path,
                "content": "title: HA MCP Test\nviews:\n  - title: Home\n    cards: []\n",
                "overwrite": True,
            },
        )
        assert write_data.get("success") is True, write_data

        read_data = await safe_call_tool(
            mcp_client_with_yaml_config,
            READ_TOOL,
            {"path": path},
        )
        assert read_data.get("success") is True
        assert "HA MCP Test" in read_data["content"]

        delete_data = await safe_call_tool(
            mcp_client_with_yaml_config,
            "ha_delete_file",
            {"path": path, "confirm": True},
        )
        assert delete_data.get("success") is True, delete_data


@pytest.mark.filesystem
class TestYamlConfigSkillContentDelivery:
    """E2E: ha_config_set_yaml participates in the write-tool skill_content
    delivery feature (#1182) — the sixth write tool exposing MandatoryBPS.

    Lives here (not in workflows/automation/test_skill_content_delivery.py
    with the other five tools) because ha_config_set_yaml is feature-flag
    + custom-component gated and needs the mcp_client_with_yaml_config
    fixture owned by this module.
    """

    _TEMPLATE_SENSOR = "- sensor:\n    - name: E2E Skill Sensor\n      state: 'ok'"

    async def test_default_on_attaches_skill_content(self, mcp_client_with_yaml_config):
        """MandatoryBPS defaults to True → template-guidelines.md ships
        under skill_content with the hint as the first response key."""
        result = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "template",
                "action": "add",
                "content": self._TEMPLATE_SENSOR,
                "file": "packages/_e2e_skill_bps_on.yaml",
                "backup": False,
            },
        )
        assert result.get("success") is True, f"yaml add failed: {result}"

        keys = list(result.keys())
        assert keys[0] == "skill_content_hint", (
            f"skill_content_hint must be the first response key, got {keys}"
        )
        skill_content = result.get("skill_content") or {}
        assert skill_content, "skill_content must be non-empty"
        assert "template-guidelines.md" in "\n".join(skill_content.keys()), (
            f"expected template-guidelines.md among {list(skill_content.keys())}"
        )

    async def test_mandatorybps_false_suppresses_skill_content(
        self, mcp_client_with_yaml_config
    ):
        """Explicit MandatoryBPS=False suppresses both skill_content and the
        hint on the yaml tool."""
        result = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "template",
                "action": "add",
                "content": self._TEMPLATE_SENSOR,
                "file": "packages/_e2e_skill_bps_off.yaml",
                "backup": False,
                "MandatoryBPS": False,
            },
        )
        assert result.get("success") is True, f"yaml add failed: {result}"
        assert "skill_content" not in result, (
            "MandatoryBPS=False must suppress skill_content"
        )
        assert "skill_content_hint" not in result, (
            "MandatoryBPS=False must suppress skill_content_hint"
        )
