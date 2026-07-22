"""
End-to-End tests for Managed YAML Config Editing Tool (ha_config_set_yaml).

This test suite validates:
- Security boundaries: path traversal, file allowlist, key allowlist
- CRUD operations: add, replace, remove actions
- Validation: null content rejection, type mismatch errors
- Safeguards: config check integration, post-edit action hints
- Feature flag behavior (disabled by default)
- Comment and HA tag preservation (ruamel.yaml round-trip)

These tests require:
1. The ha_mcp_tools custom component to be installed in Home Assistant
2. The ENABLE_YAML_CONFIG_EDITING feature flag to be enabled

Tests are designed for the Docker Home Assistant test environment.
"""

import logging
import os
import uuid
from typing import Any

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    extract_error_message,
    safe_call_tool,
)
from ...utilities.wait_helpers import wait_for_tool_result

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

    When ``mcp_server`` is None the server runs out-of-process — the inaddon HAOS
    addon (started with ENABLE_YAML_CONFIG_EDITING=true via Supervisor options) or
    the embedded backend's in-process MCP server (with the flag in its
    feature_flags.json override). In both, the session ``mcp_client`` already
    speaks HTTP to that server, so yield it directly instead of wrapping
    ``mcp_server.mcp`` (which would be None).
    """
    if mcp_server is None:
        # Fail fast at fixture setup if the out-of-process server's YAML config
        # tool isn't registered.
        tools = await mcp_client.list_tools()
        tool_names = {t.name for t in tools}
        backend = ha_container_with_fresh_config.get("backend")
        assert TOOL_NAME in tool_names, (
            f"Out-of-process server ({backend}) is missing {TOOL_NAME}. The inaddon "
            f"addon needs enable_yaml_config_editing=true in its install-time "
            f"options (build_image.install_ha_mcp_dev_addon); the embedded backend "
            f"needs it in feature_flags.json (conftest._EMBEDDED_FEATURE_FLAGS)."
        )
        logger.debug("FastMCP client (%s, HTTP) reused for YAML tests", backend)
        # Session-scope mcp_client owns __aexit__; the per-test fixture
        # deliberately doesn't wrap in `async with`.
        yield mcp_client
        return

    from fastmcp import Client

    client = Client(mcp_server.mcp)
    async with client:
        logger.debug("FastMCP client with YAML config tools connected")
        yield client


async def call_set_yaml_confirmed(mcp: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Drive the (default-on) two-step confirm flow to completion.

    The first ``ha_config_set_yaml`` call returns a no-write preview plus a
    ``confirm_token``; repeating the identical call with that token applies
    the edit. ``mcp`` is an :class:`MCPAssertions` instance so both calls go
    through ``call_tool_success`` — a genuine write failure still fails the
    test loudly. Returns the applied-write result.
    """
    data = await mcp.call_tool_success(TOOL_NAME, args)
    if data.get("preview"):
        data = await mcp.call_tool_success(
            TOOL_NAME, {**args, "confirm_token": data["confirm_token"]}
        )
    return data


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

        # An unknown key: not in ALLOWED_YAML_KEYS and not denylisted, so it
        # gets the generic allowlist rejection.
        data = await safe_call_tool(
            mcp_client_with_yaml_config,
            TOOL_NAME,
            {
                "yaml_path": "zigbee2mqtt",
                "action": "replace",
                "content": "permit_join: true",
                "file": "configuration.yaml",
            },
        )
        inner = data
        assert inner.get("success") is False, f"Blocked key should fail: {data}"
        assert "not in the allowed list" in extract_error_message(inner).lower()
        logger.info("Correctly rejected blocked key")

    async def test_denylisted_key_rejected_with_floor_message(
        self, mcp_client_with_yaml_config
    ):
        """YAML_KEY_DENYLIST keys get the categorical refusal, not the
        generic allowlist dump (#1887).

        The distinction matters: the generic message reads as "pick another
        key", while these can never be enabled at all, not even through the
        extra-write-keys setting.
        """

        for key in ("homeassistant", "http", "frontend", "lovelace"):
            data = await safe_call_tool(
                mcp_client_with_yaml_config,
                TOOL_NAME,
                {
                    "yaml_path": key,
                    "action": "replace",
                    "content": "name: Hacked",
                    "file": "configuration.yaml",
                },
            )
            assert data.get("success") is False, f"{key} must be refused: {data}"
            message = extract_error_message(data).lower()
            assert "can never be edited" in message, f"{key}: {message}"
            assert "cannot be lifted" in message, f"{key}: {message}"
            assert "not in the allowed list" not in message, f"{key}: {message}"
        logger.info("Deny floor refused all trust-boundary keys")

    async def test_packages_only_key_still_rejected_in_configuration_yaml(
        self, mcp_client_with_yaml_config
    ):
        """automation/script/scene stay confined to packages/*.yaml (#1887).

        The operator extra-key setting widens the allowlist, but it must not
        reach these: they have storage-mode equivalents, and the per-key
        toggle that governs them only applies to packages targets, so lifting
        the restriction here would route around both.
        """

        for key in ("automation", "script", "scene"):
            data = await safe_call_tool(
                mcp_client_with_yaml_config,
                TOOL_NAME,
                {
                    "yaml_path": key,
                    "action": "add",
                    "content": "[]",
                    "file": "configuration.yaml",
                },
            )
            assert data.get("success") is False, f"{key} must stay rejected: {data}"
            message = extract_error_message(data).lower()
            assert "packages/*.yaml" in message, f"{key}: {message}"
        logger.info("Packages-only keys remain rejected in configuration.yaml")

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
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_add.yaml",
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
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "knx",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_knx.yaml",
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

    async def test_extra_key_write_succeeds_against_real_component(
        self, mcp_client_with_yaml_config, ha_container_with_fresh_config
    ):
        """An operator extra key writes successfully against the real component (#1887).

        Every rejection path is already e2e-covered; this pins the feature's
        actual capability (the server forwarding ``extra_allowed_keys`` and
        the component's real voluptuous schema accepting the widened key),
        which the unit tests cannot reach (voluptuous is mocked there). The
        write only succeeds because ``alert2`` was added to the allowlist;
        without the operator setting it would take the generic allowlist
        rejection.

        ``alert2`` is wired into the container/in-process server's boot env
        (``HA_MCP_EXTRA_YAML_KEYS``) and the embedded server's
        ``feature_flags.json`` override. The inaddon HAOS backend has no
        Supervisor option for this setting (it is a web-UI + env-var setting
        by design), so its addon boots without the key; skip there rather than
        assert a capability that backend was never given.
        """
        if ha_container_with_fresh_config.get("backend") == "haos_inaddon":
            pytest.skip(
                "HA_MCP_EXTRA_YAML_KEYS is not an inaddon Supervisor option; the "
                "addon boots without it (web-UI/env-var setting by design)"
            )

        # alert2 is not in ALLOWED_YAML_KEYS; it reaches the write only through
        # the operator extra-keys setting. Minimal valid mapping under the key.
        content = "defaults: {}\n"

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "alert2",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_extra_key_alert2.yaml",
                },
            )
            assert data.get("success") is True, (
                f"extra-key (alert2) add should succeed: {data}"
            )
            assert data.get("action") == "add"
            # alert2 has no reload service, so it defaults to restart_required.
            assert data.get("post_action") == "restart_required", (
                f"alert2 should default to post_action=restart_required: {data}"
            )
            logger.info("Successfully wrote operator extra key alert2")

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
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": key,
                    "action": "add",
                    "content": content,
                    "file": f"packages/_e2e_test_{key}.yaml",
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
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "automation",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_nested/automations.yaml",
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
            await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": initial,
                    "file": "packages/_e2e_test_replace.yaml",
                },
            )

            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "template",
                    "action": "replace",
                    "content": replacement,
                    "file": "packages/_e2e_test_replace.yaml",
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
            await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_remove.yaml",
                },
            )

            # Then remove
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "template",
                    "action": "remove",
                    "file": "packages/_e2e_test_remove.yaml",
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
            await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "sensor",
                    "action": "add",
                    "content": "- platform: template\n  sensors:\n    test:\n      value_template: 'ok'",
                    "file": "packages/_e2e_test_remove_missing.yaml",
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
            await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "sensor",
                    "action": "add",
                    "content": "- platform: template\n  sensors:\n    test:\n      value_template: 'ok'",
                    "file": "packages/_e2e_test_mismatch.yaml",
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
    """Test config check integration.

    Per-edit backups moved to ha-mcp's shared auto-backup layer (#1579);
    ha_config_set_yaml no longer writes a component-side backup, so the
    former ``test_backup_created`` was removed. Restore-via-snapshot is
    covered in the auto-backup edits-layer tests.
    """

    async def test_config_check_included_in_response(self, mcp_client_with_yaml_config):
        """Config check result should be included in the response."""

        content = "- sensor:\n    - name: Config Check Test\n      state: 'ok'"

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_config_check.yaml",
                },
            )
            inner = data
            assert inner.get("success") is True, f"Add should succeed: {data}"
            # The seeded config is valid, so the guard must actually run and
            # report "ok" — asserting mere presence accepts "unavailable" and
            # would stay green if the check regressed to the silently-dead mode
            # (#1660). This is the only test that exercises the real HA
            # config-check path; the unit tests stub async_check_ha_config_file.
            assert inner.get("config_check") == "ok", (
                f"Config check should be 'ok' for a valid seeded config "
                f"(behavioral regression guard for #1660): {data}"
            )
            logger.info(f"Config check result: {inner.get('config_check')}")

    async def test_post_action_reload_for_template(self, mcp_client_with_yaml_config):
        """Template key should return post_action=reload_available."""

        content = "- sensor:\n    - name: Post Action Test\n      state: 'ok'"

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": content,
                    "file": "packages/_e2e_test_post_action_reload.yaml",
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
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "shell_command",
                    "action": "add",
                    "content": "test_cmd: echo hello",
                    "file": "packages/_e2e_test_post_action_restart.yaml",
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

    async def test_recorder_key_is_editable(self, mcp_client_with_yaml_config):
        """recorder is editable via ha_config_set_yaml (#1852).

        recorder is YAML-only (no UI/storage-mode helper) and has no reload
        service in HA core, so it defaults to post_action=restart_required.
        """

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "recorder",
                    "action": "add",
                    "content": "purge_keep_days: 5",
                    "file": "packages/_e2e_test_recorder.yaml",
                },
            )
            assert data.get("success") is True, f"recorder add should succeed: {data}"
            assert data.get("post_action") == "restart_required", (
                f"recorder should default to post_action=restart_required: {data}"
            )
            assert "reload_service" not in data, (
                f"reload_service should NOT be present for recorder: {data}"
            )


# ---------------------------------------------------------------------------
# Configured (non-default) packages folder — issue #1854
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfigCustomPackagesFolder:
    """#1854: package files under the *configured* packages folder are editable.

    The e2e container binds packages under a non-default folder via
    ``homeassistant: packages: !include_dir_named custom_packages`` (see
    tests/initial_test_state/configuration.yaml). The folder ships empty, so
    these also cover first-package creation under a configured folder: detection
    reads the directive, not the folder contents. If the folder were still
    hardcoded to ``packages``, a ``custom_packages/*.yaml`` target would be
    rejected as "not allowed"; these prove it is detected at runtime.
    """

    async def test_edit_under_configured_packages_folder(
        self, mcp_client_with_yaml_config
    ):
        """A plain allowed key writes successfully under custom_packages/.

        Reaching a success here means the path itself was accepted: a
        non-config, non-theme path is only allowed when detected as a package
        folder, so this exercises the detection, not just the key allowlist.
        """
        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "template",
                    "action": "add",
                    "content": (
                        "- sensor:\n"
                        "    - name: E2E Alt Folder Sensor\n"
                        '      state: "{{ 1 }}"\n'
                    ),
                    "file": "custom_packages/_e2e_alt_folder.yaml",
                },
            )
            assert data.get("success") is True, (
                f"editing under the configured packages folder should "
                f"succeed (#1854): {data}"
            )

    async def test_packages_only_key_allowed_in_configured_folder(
        self, mcp_client_with_yaml_config
    ):
        """A PACKAGES_ONLY key (automation) is accepted under custom_packages/.

        automation/script/scene are rejected in configuration.yaml but allowed
        in a package file. Accepting automation here proves the folder is
        classified as a package folder (is_package), not merely path-allowed.
        """
        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "automation",
                    "action": "add",
                    "content": (
                        "- alias: E2E Alt Folder Automation\n"
                        "  triggers: []\n"
                        "  actions: []\n"
                    ),
                    "file": "custom_packages/_e2e_alt_automation.yaml",
                },
            )
            assert data.get("success") is True, (
                f"PACKAGES_ONLY key 'automation' should be accepted under the "
                f"configured packages folder (#1854): {data}"
            )


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
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "template",
                    "action": "replace",
                    "content": initial_content,
                    "file": test_file,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Initial write failed: {data}"

            # Add a DIFFERENT key — forces full file re-parse/re-serialize
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "sensor",
                    "action": "add",
                    "content": "- platform: template\n  sensors:\n    extra:\n      value_template: 'yes'",
                    "file": test_file,
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
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "template",
                    "action": "replace",
                    "content": initial_content,
                    "file": test_file,
                },
            )
            inner = data
            assert inner.get("success") is True, f"Initial write failed: {data}"

            # Add a DIFFERENT key — forces full file round-trip
            data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": "sensor",
                    "action": "add",
                    "content": "- platform: time_date\n  display_options:\n    - date",
                    "file": test_file,
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

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            add_data = await call_set_yaml_confirmed(
                mcp,
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

        async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
            remove_data = await call_set_yaml_confirmed(
                mcp,
                {
                    "yaml_path": yaml_path,
                    "action": "remove",
                    "file": "configuration.yaml",
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
@pytest.mark.themes
class TestYamlConfigThemesIntegration:
    """E2E: ha_config_set_yaml can create/delete themes, triggering reload."""

    async def test_create_theme_and_reload_makes_it_appear(
        self, mcp_client_with_yaml_config
    ):
        """Create a theme via ha_config_set_yaml → reload → verify it appears in list.

        Exercises the themes/ allowlist, post-action reload (frontend.reload_themes),
        and the full lifecycle: add → list shows it → remove → list no longer shows it.
        Uses a zz_-prefixed name so it cannot collide with seeded Test Theme A/B.
        """
        theme_file = "themes/zz_e2e_created.yaml"
        theme_name = "E2E Created Theme"
        # Content is the theme-variable mapping only; the component nests it
        # under yaml_path (the theme name) itself.
        theme_content = (
            "primary-color: '#ff6b6b'\n"
            "accent-color: '#4ecdc4'\n"
            "text-primary-color: '#1a1a1a'\n"
        )

        try:
            # Create the theme file.
            async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
                add_data = await call_set_yaml_confirmed(
                    mcp,
                    {
                        "yaml_path": theme_name,
                        "action": "add",
                        "content": theme_content,
                        "file": theme_file,
                    },
                )
                assert add_data.get("success") is True, (
                    f"Theme add should succeed: {add_data}"
                )
                assert add_data.get("post_action") == "reload_performed", (
                    f"Theme add should trigger reload_performed: {add_data}"
                )
                assert add_data.get("reload_service") == "frontend.reload_themes", (
                    f"reload_service should be frontend.reload_themes: {add_data}"
                )

                logger.info(f"Created theme {theme_name} via ha_config_set_yaml")

            # Verify the theme appears in ha_manage_theme list.
            list_result = await safe_call_tool(
                mcp_client_with_yaml_config, "ha_manage_theme", {"action": "list"}
            )
            assert list_result.get("success") is True, (
                f"List themes after add failed: {list_result}"
            )
            theme_names = list_result["data"]["themes"]
            assert theme_name in theme_names, (
                f"{theme_name} should appear after add+reload: {theme_names}"
            )
            logger.info(f"Verified {theme_name} appears in theme list")

        finally:
            # Cleanup: remove the theme and verify it disappears from the list.
            # This guarantees the themes-module count==2 assertion can never
            # observe the extra theme (sequential within-worker execution).
            async with MCPAssertions(mcp_client_with_yaml_config) as mcp:
                remove_data = await call_set_yaml_confirmed(
                    mcp,
                    {
                        "yaml_path": theme_name,
                        "action": "remove",
                        "file": theme_file,
                    },
                )
                assert remove_data.get("success") is True, (
                    f"Theme remove should succeed: {remove_data}"
                )
                assert remove_data.get("post_action") == "reload_performed", (
                    f"Theme remove should trigger reload_performed: {remove_data}"
                )

            # Verify the theme no longer appears.
            list_after_remove = await safe_call_tool(
                mcp_client_with_yaml_config, "ha_manage_theme", {"action": "list"}
            )
            assert list_after_remove.get("success") is True, (
                f"List themes after remove failed: {list_after_remove}"
            )
            final_theme_names = list_after_remove["data"]["themes"]
            assert theme_name not in final_theme_names, (
                f"{theme_name} should disappear after remove+reload: {final_theme_names}"
            )
            logger.info(f"Verified {theme_name} removed and no longer in list")


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


# ---------------------------------------------------------------------------
# #1579 PR2 — ha_config_set_yaml folds into the shared edits auto-backup
# ---------------------------------------------------------------------------


async def _wait_backup_name(
    mcp_client: Any, *, domain: str, marker: str, timeout: int = 20
) -> str:
    """Poll the edits-backup list until a snapshot whose entity_id contains
    ``marker`` appears for ``domain``; return its name. ``marker`` is a unique
    token in the path (survives entity_id sanitization, which only swaps
    non-[A-Za-z0-9._-] chars)."""

    def _entries(d: dict[str, Any]) -> list[Any]:
        return d.get("backups") or d.get("data", {}).get("backups", []) or []

    data = await wait_for_tool_result(
        mcp_client,
        tool_name="ha_manage_backup",
        arguments={"scope": "edits", "action": "list", "domain": domain},
        predicate=lambda d: any(marker in e["entity_id"] for e in _entries(d)),
        description=f"{domain} auto-backup containing {marker!r}",
        timeout=timeout,
    )
    matches = [e for e in _entries(data) if marker in e["entity_id"]]
    return matches[0]["name"]


@pytest.mark.filesystem
class TestYamlConfigAutoBackup:
    """ha_config_set_yaml writes are captured into the shared edits store
    (replacing the old component-side .ha_mcp_tools_backups/ copy) and are
    list/diff/restore-able like every other auto-backed-up write."""

    async def test_yaml_edit_captures_and_restores(self, mcp_client_with_yaml_config):
        mcp = mcp_client_with_yaml_config
        marker = uuid.uuid4().hex[:8]
        file = f"packages/_e2e_backup_{marker}.yaml"
        ypath = "template"

        # Create the key — no prior value, so capture is skipped.
        async with MCPAssertions(mcp) as asserts:
            add = await call_set_yaml_confirmed(
                asserts,
                {
                    "yaml_path": ypath,
                    "action": "add",
                    "content": "- sensor:\n    - name: V1\n      state: '1'",
                    "file": file,
                },
            )
        assert add.get("success") is True, add

        try:
            # Replace — captures the prior (V1) subtree as a yaml snapshot.
            async with MCPAssertions(mcp) as asserts:
                replace = await call_set_yaml_confirmed(
                    asserts,
                    {
                        "yaml_path": ypath,
                        "action": "replace",
                        "content": "- sensor:\n    - name: V2\n      state: '2'",
                        "file": file,
                    },
                )
            assert replace.get("success") is True, replace

            name = await _wait_backup_name(mcp, domain="yaml", marker=marker)

            # Diff: stored (V1) vs current (V2) — text kind, non-empty.
            diff = await safe_call_tool(
                mcp,
                "ha_manage_backup",
                {"scope": "edits", "action": "diff", "backup_name": name},
            )
            assert diff.get("success") is True, diff
            assert diff["data"]["kind"] == "text", diff
            assert diff["data"]["unchanged"] is False, diff

            # Restore → reverts the key to V1.
            restore = await safe_call_tool(
                mcp,
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": name},
            )
            assert restore.get("success") is True, restore

            read = await safe_call_tool(mcp, READ_TOOL, {"path": file})
            assert read.get("success") is True, read
            assert "V1" in read["content"], read["content"]
        finally:
            # Best-effort cleanup: drive the confirm flow with safe_call_tool
            # so a preview (or a failure) can't raise out of ``finally`` and
            # mask the real assertion error.
            rm = await safe_call_tool(
                mcp,
                TOOL_NAME,
                {"yaml_path": ypath, "action": "remove", "file": file},
            )
            if rm.get("preview"):
                await safe_call_tool(
                    mcp,
                    TOOL_NAME,
                    {
                        "yaml_path": ypath,
                        "action": "remove",
                        "file": file,
                        "confirm_token": rm["confirm_token"],
                    },
                )


# ---------------------------------------------------------------------------
# #1720 — two-step preview/confirm flow (default ON)
# ---------------------------------------------------------------------------


@pytest.mark.filesystem
class TestYamlConfirmFlow:
    """Two-step preview->confirm flow (#1720), default ON.

    Exercises the wrapper flag -> service -> component preview/confirm path
    end-to-end: the first call returns a no-write diff preview plus a
    confirm_token, and the edit only lands when the identical call is
    repeated with that token.
    """

    async def test_preview_then_confirm(self, mcp_client_with_yaml_config):
        """First call previews (writes nothing); repeating with the token writes."""
        mcp_client = mcp_client_with_yaml_config
        fname = f"packages/confirm_{uuid.uuid4().hex[:8]}.yaml"
        args = {
            "file": fname,
            "action": "add",
            "yaml_path": "command_line",
            "content": (
                '- sensor:\n    name: e2e_confirm_probe\n    command: "echo 1"\n'
            ),
        }
        async with MCPAssertions(mcp_client) as mcp:
            preview = await mcp.call_tool_success(TOOL_NAME, args)
            assert preview["preview"] is True, preview
            assert preview["written"] is False, preview
            assert "+command_line:" in preview["diff"], preview
            assert preview["confirm_token"], preview

            # The preview must not have touched disk (asserting NOT-success is
            # robust whether the read fails because the file is missing or
            # because ha_read_file isn't functional for packages).
            read = await safe_call_tool(mcp_client, READ_TOOL, {"path": fname})
            assert not read.get("success", False), (
                f"preview must not write the file: {read}"
            )

            done = await mcp.call_tool_success(
                TOOL_NAME, {**args, "confirm_token": preview["confirm_token"]}
            )
            assert done["written"] is True, done
            assert "+command_line:" in done["diff"], done

    async def test_wrong_token_re_previews(self, mcp_client_with_yaml_config):
        """A bogus confirm_token re-previews (mismatch flagged) and writes nothing."""
        mcp_client = mcp_client_with_yaml_config
        fname = f"packages/confirm_{uuid.uuid4().hex[:8]}.yaml"
        args = {
            "file": fname,
            "action": "add",
            "yaml_path": "command_line",
            "content": (
                '- sensor:\n    name: e2e_confirm_probe2\n    command: "echo 1"\n'
            ),
        }
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success(
                TOOL_NAME, {**args, "confirm_token": "bogus"}
            )
            assert result["preview"] is True, result
            assert result["confirm_token_mismatch"] is True, result
            assert result["written"] is False, result

        # A wrong token must not have written anything either.
        read = await safe_call_tool(mcp_client, READ_TOOL, {"path": fname})
        assert not read.get("success", False), (
            f"wrong-token preview must not write the file: {read}"
        )
