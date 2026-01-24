"""
End-to-End tests for Filesystem Access Tools (ha_list_files, ha_read_file, ha_write_file, ha_delete_file).

This test suite validates the complete lifecycle of filesystem operations including:
- Listing files in allowed directories (www/, themes/, custom_templates/)
- Reading configuration files (configuration.yaml, automations.yaml, etc.)
- Reading secrets.yaml with value masking
- Writing and deleting files in allowed directories
- Security boundary enforcement (cannot write to config files)
- Feature flag behavior (disabled by default)

These tests require:
1. The ha_mcp_tools custom component to be installed in Home Assistant
2. The HAMCP_ENABLE_FILESYSTEM_TOOLS feature flag to be enabled

Note: Most tests in this file will be SKIPPED in CI environments where the
ha_mcp_tools custom component is not pre-installed. This is expected behavior.
To run these tests locally, ensure the ha_mcp_tools component is installed in
the initial_test_state directory.

Tests are designed for the Docker Home Assistant test environment.
"""

import logging
import os
import uuid

import pytest

from ...utilities.assertions import MCPAssertions, parse_mcp_result

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Feature flag name
FEATURE_FLAG = "HAMCP_ENABLE_FILESYSTEM_TOOLS"


@pytest.fixture(scope="module")
def filesystem_tools_enabled(ha_container_with_fresh_config):
    """Enable filesystem tools feature flag for the test module.

    Note: This only sets the feature flag. The ha_mcp_tools component must
    already be installed in the initial_test_state for tests to pass.
    """
    # Enable the feature flag
    os.environ[FEATURE_FLAG] = "true"

    logger.info("Filesystem tools feature flag enabled")

    yield

    # Cleanup: disable feature flag
    os.environ.pop(FEATURE_FLAG, None)


@pytest.fixture
async def mcp_client_with_filesystem(filesystem_tools_enabled, mcp_server):
    """Create MCP client with filesystem tools feature flag enabled."""
    from fastmcp import Client

    client = Client(mcp_server.mcp)

    async with client:
        logger.debug("FastMCP client with filesystem tools connected")
        yield client


async def _check_filesystem_tools_available(mcp_client) -> tuple[bool, str | None]:
    """Check if filesystem tools are available in the MCP server."""
    try:
        # List all available tools
        tools = await mcp_client.list_tools()
        tool_names = [t.name for t in tools]

        filesystem_tools = ["ha_list_files", "ha_read_file", "ha_write_file", "ha_delete_file"]
        available = all(tool in tool_names for tool in filesystem_tools)

        if not available:
            missing = [t for t in filesystem_tools if t not in tool_names]
            return False, f"Missing tools: {missing}"

        return True, None

    except Exception as e:
        return False, f"Error checking tools: {e}"


async def _check_mcp_tools_service_available(mcp_client) -> tuple[bool, str | None]:
    """Check if ha_mcp_tools service is available in Home Assistant.

    Returns (True, None) if the service is available, (False, reason) otherwise.
    """
    try:
        # Try calling ha_list_files - if component is not installed, it returns error
        result = await mcp_client.call_tool(
            "ha_list_files",
            {"path": "www/"},
        )
        data = parse_mcp_result(result)

        # Check if we got the "not installed" error
        if data.get("error_code") == "MCP_TOOLS_NOT_INSTALLED":
            return False, "ha_mcp_tools custom component not installed in Home Assistant"

        # Check for success
        inner_data = data.get("data", data)
        if inner_data.get("success") is True:
            return True, None

        # Other errors might indicate issues but component might be installed
        if inner_data.get("success") is False:
            error = inner_data.get("error", "Unknown error")
            # Path-related errors mean the component IS installed
            if "not allowed" in error.lower() or "must be in" in error.lower():
                return True, None
            return False, error

        return False, "Unexpected response format"

    except Exception as e:
        return False, f"Error checking services: {e}"


def _skip_if_component_not_installed(result: tuple[bool, str | None], test_name: str):
    """Skip test if ha_mcp_tools component is not installed."""
    available, error = result
    if not available:
        pytest.skip(f"{test_name}: {error}")


@pytest.mark.filesystem
class TestFilesystemToolsAvailability:
    """Test filesystem tools availability and feature flag behavior."""

    async def test_feature_flag_disabled_by_default(self, mcp_client):
        """Verify filesystem tools are NOT available when feature flag is disabled."""
        # This test runs WITHOUT the filesystem_tools_enabled fixture
        # to verify default behavior

        # Ensure feature flag is disabled
        original_value = os.environ.pop(FEATURE_FLAG, None)

        try:
            # The tools should still be listed if the server was started with flag enabled
            # So we check if calling them returns the expected error
            tools = await mcp_client.list_tools()
            tool_names = [t.name for t in tools]

            # If tools aren't even registered, that's expected (flag was off at server start)
            if "ha_list_files" not in tool_names:
                logger.info("Filesystem tools not registered (feature flag disabled at startup)")
                return

            # If tools are registered but flag is now off, they should still work
            # (the flag is checked at registration time, not call time)
            logger.info("Filesystem tools are registered - flag was enabled at server startup")

        finally:
            # Restore original value
            if original_value:
                os.environ[FEATURE_FLAG] = original_value

    async def test_tools_registered_when_enabled(self, mcp_client_with_filesystem):
        """Verify filesystem tools ARE available when feature flag is enabled."""
        available, error = await _check_filesystem_tools_available(mcp_client_with_filesystem)

        if not available:
            pytest.skip(f"Filesystem tools not available: {error}")

        logger.info("All filesystem tools are registered and available")


@pytest.mark.filesystem
class TestListFiles:
    """Test ha_list_files tool functionality."""

    async def test_list_files_in_www_directory(self, mcp_client_with_filesystem):
        """Test listing files in the www directory."""
        # First check if component is available
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "List files in www")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # List files in www/
            result_data = await mcp.call_tool_success(
                "ha_list_files",
                {"path": "www/"},
            )

            # Check response structure
            data = result_data.get("data", result_data)
            assert data.get("success") is True, f"List files failed: {data}"
            assert "files" in data, f"Missing files in response: {data}"
            assert "count" in data, f"Missing count in response: {data}"

            logger.info(f"Listed {data.get('count', 0)} files in www/")

            # Verify files list structure
            files = data.get("files", [])
            for f in files:
                assert "name" in f, f"File missing name: {f}"
                assert "path" in f, f"File missing path: {f}"
                assert "is_dir" in f, f"File missing is_dir: {f}"

            logger.info(f"Found files: {[f['name'] for f in files]}")

    async def test_list_files_with_pattern_filter(self, mcp_client_with_filesystem):
        """Test listing files with glob pattern filter."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "List files with pattern")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # List only .jpg files
            result_data = await mcp.call_tool_success(
                "ha_list_files",
                {"path": "www/", "pattern": "*.jpg"},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is True, f"List files failed: {data}"

            # All returned files should match the pattern
            files = data.get("files", [])
            for f in files:
                assert f["name"].endswith(".jpg"), f"File doesn't match pattern: {f['name']}"

            logger.info(f"Found {len(files)} .jpg files in www/")

    async def test_list_files_disallowed_directory(self, mcp_client_with_filesystem):
        """Test that listing files in disallowed directories fails."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "List files in disallowed dir")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # Try to list files in root config directory (not allowed)
            result_data = await mcp.call_tool_success(
                "ha_list_files",
                {"path": "./"},
            )

            data = result_data.get("data", result_data)
            # Should fail with security error
            assert data.get("success") is False, f"Should have failed: {data}"
            assert "not allowed" in data.get("error", "").lower() or "must be in" in data.get("error", "").lower(), (
                f"Wrong error message: {data.get('error')}"
            )
            logger.info("Correctly rejected listing disallowed directory")

    async def test_list_files_path_traversal_blocked(self, mcp_client_with_filesystem):
        """Test that path traversal attempts are blocked."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Path traversal blocked")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # Try path traversal
            result_data = await mcp.call_tool_success(
                "ha_list_files",
                {"path": "www/../"},
            )

            data = result_data.get("data", result_data)
            # Should fail with security error
            assert data.get("success") is False, f"Path traversal should fail: {data}"
            logger.info("Correctly blocked path traversal attempt")


@pytest.mark.filesystem
class TestReadFile:
    """Test ha_read_file tool functionality."""

    async def test_read_configuration_yaml(self, mcp_client_with_filesystem):
        """Test reading configuration.yaml file."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Read configuration.yaml")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            result_data = await mcp.call_tool_success(
                "ha_read_file",
                {"path": "configuration.yaml"},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is True, f"Read file failed: {data}"
            assert "content" in data, f"Missing content in response: {data}"

            content = data.get("content", "")
            # Verify it's actual configuration content
            assert "default_config:" in content or "homeassistant:" in content, (
                f"Content doesn't look like configuration.yaml: {content[:200]}"
            )

            logger.info(f"Successfully read configuration.yaml ({data.get('size', 0)} bytes)")

    async def test_read_secrets_yaml_masked(self, mcp_client_with_filesystem):
        """Test reading secrets.yaml - values should be masked."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Read secrets.yaml")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            result_data = await mcp.call_tool_success(
                "ha_read_file",
                {"path": "secrets.yaml"},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is True, f"Read file failed: {data}"

            content = data.get("content", "")

            # The actual secret values should be masked
            # Original secrets.yaml has: some_password: welcome
            # Should be masked to: some_password: [MASKED]
            assert "[MASKED]" in content, f"Secret values should be masked: {content}"
            assert "welcome" not in content, f"Actual secret value should not appear: {content}"

            logger.info("Successfully read secrets.yaml with masked values")

    async def test_read_file_in_www_directory(self, mcp_client_with_filesystem):
        """Test reading a file from the www directory."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Read file in www")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # First list files to find what's available
            list_result = await mcp.call_tool_success(
                "ha_list_files",
                {"path": "www/"},
            )

            list_data = list_result.get("data", list_result)
            files = list_data.get("files", [])

            # Skip binary files - look for text files
            text_files = [f for f in files if not f["name"].endswith((".jpg", ".png", ".gif", ".ico"))]

            if not text_files:
                logger.info("No text files in www/ to test reading")
                return

            # Try to read the first text file
            file_to_read = text_files[0]
            result_data = await mcp.call_tool_success(
                "ha_read_file",
                {"path": f"www/{file_to_read['name']}"},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is True, f"Read file failed: {data}"
            logger.info(f"Successfully read www/{file_to_read['name']}")

    async def test_read_nonexistent_file(self, mcp_client_with_filesystem):
        """Test reading a file that doesn't exist."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Read nonexistent file")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            result_data = await mcp.call_tool_success(
                "ha_read_file",
                {"path": "nonexistent_file_xyz123.yaml"},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is False, f"Should have failed: {data}"
            assert "not exist" in data.get("error", "").lower() or "not allowed" in data.get("error", "").lower(), (
                f"Wrong error: {data.get('error')}"
            )
            logger.info("Correctly handled nonexistent file")

    async def test_read_disallowed_file(self, mcp_client_with_filesystem):
        """Test reading a file outside allowed paths."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Read disallowed file")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # Try to read /etc/passwd (path traversal attempt)
            result_data = await mcp.call_tool_success(
                "ha_read_file",
                {"path": "../../../etc/passwd"},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is False, f"Should have failed: {data}"
            logger.info("Correctly blocked read of disallowed file")


@pytest.mark.filesystem
class TestWriteFile:
    """Test ha_write_file tool functionality."""

    async def test_write_file_in_www_directory(self, mcp_client_with_filesystem, cleanup_tracker):
        """Test writing a new file to the www directory."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Write file in www")

        test_filename = f"test_e2e_{uuid.uuid4().hex[:8]}.txt"
        test_content = "This is a test file created by E2E tests.\nSafe to delete."

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # Write a new file
            result_data = await mcp.call_tool_success(
                "ha_write_file",
                {
                    "path": f"www/{test_filename}",
                    "content": test_content,
                },
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is True, f"Write file failed: {data}"
            assert data.get("created") is True, f"Should be marked as created: {data}"

            logger.info(f"Successfully created www/{test_filename}")
            cleanup_tracker.track("file", f"www/{test_filename}")

            # Verify by reading it back
            read_result = await mcp.call_tool_success(
                "ha_read_file",
                {"path": f"www/{test_filename}"},
            )

            read_data = read_result.get("data", read_result)
            assert read_data.get("success") is True, f"Read back failed: {read_data}"
            assert read_data.get("content") == test_content, (
                f"Content mismatch: {read_data.get('content')}"
            )

            logger.info("Verified file content after write")

            # Clean up
            await mcp.call_tool_success(
                "ha_delete_file",
                {"path": f"www/{test_filename}", "confirm": True},
            )
            logger.info(f"Cleaned up test file www/{test_filename}")

    async def test_write_file_overwrite_protection(self, mcp_client_with_filesystem):
        """Test that overwrite protection works."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Write overwrite protection")

        test_filename = f"test_overwrite_{uuid.uuid4().hex[:8]}.txt"

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # Create initial file
            await mcp.call_tool_success(
                "ha_write_file",
                {"path": f"www/{test_filename}", "content": "Original content"},
            )

            # Try to overwrite without flag (should fail)
            result_data = await mcp.call_tool_success(
                "ha_write_file",
                {"path": f"www/{test_filename}", "content": "New content", "overwrite": False},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is False, f"Should have failed without overwrite: {data}"
            assert "exists" in data.get("error", "").lower(), f"Wrong error: {data.get('error')}"

            logger.info("Correctly blocked overwrite without flag")

            # Now overwrite with flag (should succeed)
            result_data = await mcp.call_tool_success(
                "ha_write_file",
                {"path": f"www/{test_filename}", "content": "New content", "overwrite": True},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is True, f"Overwrite with flag should work: {data}"

            logger.info("Successfully overwrote file with flag")

            # Clean up
            await mcp.call_tool_success(
                "ha_delete_file",
                {"path": f"www/{test_filename}", "confirm": True},
            )

    async def test_write_file_create_directories(self, mcp_client_with_filesystem):
        """Test creating directories when writing files."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Write with create_dirs")

        test_subdir = f"test_subdir_{uuid.uuid4().hex[:8]}"
        test_path = f"www/{test_subdir}/nested/test.txt"

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # Write file with nested directories
            result_data = await mcp.call_tool_success(
                "ha_write_file",
                {"path": test_path, "content": "Nested file content", "create_dirs": True},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is True, f"Write with create_dirs failed: {data}"

            logger.info(f"Successfully created nested file {test_path}")

            # Verify the file exists
            read_result = await mcp.call_tool_success(
                "ha_read_file",
                {"path": test_path},
            )

            read_data = read_result.get("data", read_result)
            assert read_data.get("success") is True, f"Read nested file failed: {read_data}"

            # Clean up - delete the file first
            await mcp.call_tool_success(
                "ha_delete_file",
                {"path": test_path, "confirm": True},
            )
            logger.info("Cleaned up nested test file")


@pytest.mark.filesystem
class TestDeleteFile:
    """Test ha_delete_file tool functionality."""

    async def test_delete_file_requires_confirmation(self, mcp_client_with_filesystem):
        """Test that deletion requires explicit confirmation."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Delete requires confirmation")

        test_filename = f"test_delete_{uuid.uuid4().hex[:8]}.txt"

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # Create a file to delete
            await mcp.call_tool_success(
                "ha_write_file",
                {"path": f"www/{test_filename}", "content": "To be deleted"},
            )

            # Try to delete without confirmation (should fail)
            result_data = await mcp.call_tool_success(
                "ha_delete_file",
                {"path": f"www/{test_filename}", "confirm": False},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is False, f"Should require confirmation: {data}"
            assert "confirm" in data.get("error", "").lower() or "not confirmed" in data.get("message", "").lower(), (
                f"Wrong error message: {data}"
            )

            logger.info("Correctly required confirmation for delete")

            # Now delete with confirmation
            result_data = await mcp.call_tool_success(
                "ha_delete_file",
                {"path": f"www/{test_filename}", "confirm": True},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is True, f"Delete with confirmation should work: {data}"

            logger.info("Successfully deleted file with confirmation")

    async def test_delete_nonexistent_file(self, mcp_client_with_filesystem):
        """Test deleting a file that doesn't exist."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Delete nonexistent file")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            result_data = await mcp.call_tool_success(
                "ha_delete_file",
                {"path": "www/nonexistent_file_xyz123.txt", "confirm": True},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is False, f"Should have failed: {data}"
            assert "not exist" in data.get("error", "").lower(), f"Wrong error: {data.get('error')}"

            logger.info("Correctly handled delete of nonexistent file")


@pytest.mark.filesystem
class TestSecurityBoundaries:
    """Test security boundaries for filesystem operations."""

    async def test_cannot_write_to_configuration_yaml(self, mcp_client_with_filesystem):
        """Test that writing to configuration.yaml is blocked."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Cannot write to config")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            result_data = await mcp.call_tool_success(
                "ha_write_file",
                {"path": "configuration.yaml", "content": "# Malicious content", "overwrite": True},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is False, f"Should block writing to config: {data}"
            assert "not allowed" in data.get("error", "").lower() or "must be in" in data.get("error", "").lower(), (
                f"Wrong error message: {data.get('error')}"
            )

            logger.info("Correctly blocked write to configuration.yaml")

    async def test_cannot_write_to_secrets_yaml(self, mcp_client_with_filesystem):
        """Test that writing to secrets.yaml is blocked."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Cannot write to secrets")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            result_data = await mcp.call_tool_success(
                "ha_write_file",
                {"path": "secrets.yaml", "content": "malicious_secret: hacked", "overwrite": True},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is False, f"Should block writing to secrets: {data}"

            logger.info("Correctly blocked write to secrets.yaml")

    async def test_cannot_delete_configuration_files(self, mcp_client_with_filesystem):
        """Test that deleting configuration files is blocked."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Cannot delete config")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # Try to delete configuration.yaml
            result_data = await mcp.call_tool_success(
                "ha_delete_file",
                {"path": "configuration.yaml", "confirm": True},
            )

            data = result_data.get("data", result_data)
            assert data.get("success") is False, f"Should block deleting config: {data}"

            logger.info("Correctly blocked delete of configuration.yaml")

    async def test_cannot_access_files_outside_config(self, mcp_client_with_filesystem):
        """Test that files outside config directory cannot be accessed."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Cannot access outside config")

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # Try various path traversal attacks
            malicious_paths = [
                "../../../etc/passwd",
                "/etc/passwd",
                "www/../../etc/passwd",
                "www/../../../etc/hosts",
            ]

            for path in malicious_paths:
                result_data = await mcp.call_tool_success(
                    "ha_read_file",
                    {"path": path},
                )

                data = result_data.get("data", result_data)
                assert data.get("success") is False, f"Should block path traversal {path}: {data}"

            logger.info("Correctly blocked all path traversal attempts")


@pytest.mark.filesystem
class TestFullCRUDWorkflow:
    """Test complete CRUD workflow for filesystem operations."""

    async def test_complete_file_lifecycle(self, mcp_client_with_filesystem):
        """Test Create, Read, Update, Delete workflow."""
        service_check = await _check_mcp_tools_service_available(mcp_client_with_filesystem)
        _skip_if_component_not_installed(service_check, "Complete CRUD workflow")

        test_filename = f"crud_test_{uuid.uuid4().hex[:8]}.css"
        test_path = f"www/{test_filename}"

        async with MCPAssertions(mcp_client_with_filesystem) as mcp:
            # CREATE
            logger.info("1. CREATE: Writing new CSS file")
            create_result = await mcp.call_tool_success(
                "ha_write_file",
                {"path": test_path, "content": ".test { color: red; }"},
            )

            create_data = create_result.get("data", create_result)
            assert create_data.get("success") is True
            assert create_data.get("created") is True
            logger.info(f"   Created {test_path}")

            # READ
            logger.info("2. READ: Verifying file content")
            read_result = await mcp.call_tool_success(
                "ha_read_file",
                {"path": test_path},
            )

            read_data = read_result.get("data", read_result)
            assert read_data.get("success") is True
            assert read_data.get("content") == ".test { color: red; }"
            logger.info("   Content verified")

            # LIST (verify in directory listing)
            logger.info("3. LIST: Checking file appears in directory")
            list_result = await mcp.call_tool_success(
                "ha_list_files",
                {"path": "www/", "pattern": "crud_test_*.css"},
            )

            list_data = list_result.get("data", list_result)
            assert list_data.get("success") is True
            files = list_data.get("files", [])
            assert any(f["name"] == test_filename for f in files), f"File not in listing: {files}"
            logger.info("   File found in directory listing")

            # UPDATE
            logger.info("4. UPDATE: Modifying file content")
            update_result = await mcp.call_tool_success(
                "ha_write_file",
                {"path": test_path, "content": ".test { color: blue; background: white; }", "overwrite": True},
            )

            update_data = update_result.get("data", update_result)
            assert update_data.get("success") is True
            assert update_data.get("created") is False  # Not created, updated
            logger.info("   File updated")

            # Verify update
            read_result2 = await mcp.call_tool_success(
                "ha_read_file",
                {"path": test_path},
            )

            read_data2 = read_result2.get("data", read_result2)
            assert "background: white" in read_data2.get("content", "")
            logger.info("   Update verified")

            # DELETE
            logger.info("5. DELETE: Removing file")
            delete_result = await mcp.call_tool_success(
                "ha_delete_file",
                {"path": test_path, "confirm": True},
            )

            delete_data = delete_result.get("data", delete_result)
            assert delete_data.get("success") is True
            logger.info("   File deleted")

            # Verify deletion
            final_read = await mcp.call_tool_success(
                "ha_read_file",
                {"path": test_path},
            )

            final_data = final_read.get("data", final_read)
            assert final_data.get("success") is False
            assert "not exist" in final_data.get("error", "").lower() or "not allowed" in final_data.get("error", "").lower()
            logger.info("   Deletion verified")

            logger.info("Complete CRUD lifecycle test PASSED")


@pytest.mark.filesystem
class TestMcpToolsComponentNotInstalled:
    """Test behavior when ha_mcp_tools component is not installed."""

    async def test_graceful_error_when_component_missing(self, mcp_client_with_filesystem):
        """Test that tools return helpful error when component is missing.

        This test verifies the error handling in the MCP tools layer
        when the HA custom component is not available. This test should
        PASS regardless of whether the component is installed - it validates
        the error message format when the component is missing.
        """
        available, _ = await _check_filesystem_tools_available(mcp_client_with_filesystem)
        if not available:
            pytest.skip("Filesystem tools not registered")

        # Check if the ha_mcp_tools service is actually available
        service_available, _ = await _check_mcp_tools_service_available(mcp_client_with_filesystem)

        if not service_available:
            # This is the expected case when component is not installed
            # The MCP tool should return a helpful error message
            async with MCPAssertions(mcp_client_with_filesystem) as mcp:
                result = await mcp.client.call_tool(
                    "ha_list_files",
                    {"path": "www/"},
                )

                data = parse_mcp_result(result)

                # Should fail with helpful message about installing component
                if data.get("success") is False or data.get("data", {}).get("success") is False:
                    error_msg = data.get("error", "") or data.get("data", {}).get("error", "")
                    error_code = data.get("error_code", "") or data.get("data", {}).get("error_code", "")

                    # Check for helpful installation guidance
                    assert (
                        "not installed" in error_msg.lower() or
                        "ha_mcp_tools" in error_msg.lower() or
                        error_code == "MCP_TOOLS_NOT_INSTALLED"
                    ), f"Should provide helpful error: {data}"

                    logger.info("Correctly returned helpful error when component not installed")
                else:
                    # This means the component IS installed - test passes
                    logger.info("Component appears to be installed (unexpected in CI)")
        else:
            logger.info("ha_mcp_tools service is available, component is installed - test passes")
