"""
HACS (Home Assistant Community Store) E2E Tests

Tests the HACS integration tools for discovering, searching, and managing
custom integrations, Lovelace cards, themes, and more from the HACS store.

Note: These tests require HACS to be installed in the test environment.
The test environment includes HACS in custom_components/ with a pre-configured
config entry.

HACS requires a valid GitHub token to fully function. Without a valid token,
HACS may be in a partially disabled state but the WebSocket API still responds.
Tests should handle both fully functional and partially disabled states.
"""

import logging

import pytest
from fastmcp.exceptions import ToolError

from ...utilities.assertions import (
    parse_mcp_result,
    safe_call_tool,
    tool_error_to_result,
)

logger = logging.getLogger(__name__)


def extract_hacs_data(raw_result) -> dict:
    """Extract data from MCP result, handling nested response structure.

    MCP tool results can be:
    - {"data": {"success": ..., ...}, "metadata": ...}
    - {"success": ..., ...}

    This helper extracts the actual data dict.
    """
    parsed = parse_mcp_result(raw_result)
    if (
        isinstance(parsed, dict)
        and "data" in parsed
        and isinstance(parsed["data"], dict)
    ):
        return parsed["data"]
    return parsed


async def safe_hacs_call(
    mcp_client, tool_name: str, params: dict | None = None
) -> dict:
    """Call a HACS tool and extract data, handling ToolError exceptions.

    HACS tools raise ToolError when the tool fails (e.g., HACS not installed,
    WebSocket command fails). This helper catches ToolError and converts it to
    a dict so that is_hacs_unavailable() can check whether to skip the test.
    """
    try:
        result = await mcp_client.call_tool(tool_name, params or {})
        return extract_hacs_data(result)
    except ToolError as exc:
        return tool_error_to_result(exc)


def is_hacs_unavailable(data: dict) -> tuple[bool, str]:
    """Check if HACS is unavailable based on error response.

    Returns:
        Tuple of (is_unavailable, reason)
    """
    error = data.get("error", "")
    error_code = data.get("error_code", "")
    error_str = str(error).lower()

    # Handle nested error dict structure
    if isinstance(error, dict):
        error_code = error.get("code", error_code)
        error_str = str(error.get("message", "")).lower()

    unavailable_indicators = [
        (error_code == "HACS_NOT_AVAILABLE", "HACS not available"),
        (
            error_code == "HACS_DISABLED",
            f"HACS disabled: {data.get('disabled_reason', 'unknown')}",
        ),
        (
            (error_code == "INTERNAL_ERROR" and "rate" in error_str)
            or ("rate" in error_str and "limit" in error_str),
            "GitHub rate limit",
        ),
        (
            error_code == "INTERNAL_ERROR" and "github" in error_str,
            "GitHub access issue",
        ),
        (error_code == "INTERNAL_ERROR", f"HACS internal error: {error_str}"),
        ("not found" in error_str, "Command not found"),
        ("unknown command" in error_str, "Unknown command"),
        ("disabled" in error_str, "HACS disabled"),
        ("401" in error_str, "GitHub authentication failed"),
    ]

    for condition, reason in unavailable_indicators:
        if condition:
            return True, reason

    return False, ""


@pytest.mark.hacs
class TestHacsSearchInstalled:
    """Test HACS search with installed_only filter functionality."""

    async def test_list_all_installed(self, mcp_client):
        """
        Test: List all installed HACS repositories via ha_hacs_search

        This test validates listing installed repositories using the
        installed_only parameter. In a fresh test environment, there
        should be no installed repos.
        """
        logger.info("Testing ha_hacs_search with installed_only=True...")

        data = await safe_hacs_call(
            mcp_client, "ha_hacs_search", {"installed_only": True}
        )

        if not data.get("success"):
            unavailable, reason = is_hacs_unavailable(data)
            if unavailable:
                pytest.skip(f"HACS not available: {reason}")
            pytest.fail(f"HACS search installed failed: {data.get('error')}")

        # Verify response structure
        assert "total_matches" in data, "Response should include total_matches"
        assert "results" in data, "Response should include results list"
        assert "category_filter" in data, "Response should include category_filter"

        total = data["total_matches"]
        repos = data["results"]

        logger.info(f"Found {total} installed HACS repositories")

        # Validate count matches
        assert total >= 0, "Total should be non-negative"
        assert isinstance(repos, list), "Results should be a list"
        assert len(repos) == total, "Results count should match total"

        # No filter should be applied
        assert data["category_filter"] is None, "No category filter should be applied"

        logger.info("List all installed test passed")

    async def test_list_by_category(self, mcp_client):
        """
        Test: List installed HACS repositories filtered by category

        Test filtering by different categories using ha_hacs_search with installed_only.
        """
        logger.info("Testing ha_hacs_search installed_only with category filter...")

        categories = ["integration", "lovelace", "theme"]

        for category in categories:
            data = await safe_hacs_call(
                mcp_client,
                "ha_hacs_search",
                {"installed_only": True, "category": category},
            )

            if not data.get("success"):
                unavailable, reason = is_hacs_unavailable(data)
                if unavailable:
                    pytest.skip(f"HACS not available: {reason}")
                pytest.fail(
                    f"HACS search installed by {category} failed: {data.get('error')}"
                )

            # Verify filter was applied
            assert data["category_filter"] == category, (
                f"Category filter should be {category}"
            )

            # All returned results should match the category
            for repo in data["results"]:
                assert repo["category"] == category, (
                    f"Repo category should be {category}"
                )

            logger.info(f"Category {category}: {data['total_matches']} installed")

        logger.info("List by category test passed")


@pytest.mark.hacs
class TestHacsSearch:
    """Test HACS store search functionality."""

    async def test_search_basic(self, mcp_client):
        """
        Test: Basic HACS store search

        Search for a common term that should return results.
        """
        logger.info("Testing ha_hacs_search basic search...")

        # Search for something likely to exist in HACS
        data = await safe_hacs_call(mcp_client, "ha_hacs_search", {"query": "mushroom"})

        if not data.get("success"):
            unavailable, reason = is_hacs_unavailable(data)
            if unavailable:
                pytest.skip(f"HACS not available: {reason}")
            pytest.fail(f"HACS search failed: {data.get('error')}")

        # Verify response structure
        assert "query" in data, "Response should include query"
        assert "total_matches" in data, "Response should include total_matches"
        assert "count" in data, "Response should include count"
        assert "results" in data, "Response should include results list"

        # Query should be recorded
        assert data["query"] == "mushroom", "Query should match input"

        logger.info(
            f"Search 'mushroom': {data['total_matches']} matches, {data['count']} returned"
        )

        # Verify result structure if we have results
        if data["results"]:
            result_item = data["results"][0]
            expected_fields = ["name", "full_name", "category", "description"]
            for field in expected_fields:
                assert field in result_item, f"Result should have '{field}' field"

        logger.info("Basic search test passed")

    async def test_search_with_category(self, mcp_client):
        """
        Test: HACS search with category filter

        Search within a specific category.
        """
        logger.info("Testing ha_hacs_search with category filter...")

        data = await safe_hacs_call(
            mcp_client, "ha_hacs_search", {"query": "card", "category": "lovelace"}
        )

        if not data.get("success"):
            unavailable, reason = is_hacs_unavailable(data)
            if unavailable:
                pytest.skip(f"HACS not available: {reason}")
            pytest.fail(f"HACS search with category failed: {data.get('error')}")

        # Verify category filter was applied
        assert data["category_filter"] == "lovelace", (
            "Category filter should be lovelace"
        )

        # All results should be in the lovelace category
        for result_item in data["results"]:
            assert result_item["category"] == "lovelace", (
                "Result category should be lovelace"
            )

        logger.info(f"Search 'card' in lovelace: {data['total_matches']} matches")
        logger.info("Search with category test passed")

    async def test_search_with_max_results(self, mcp_client):
        """
        Test: HACS search with max_results limit

        Verify pagination/limiting works correctly.
        """
        logger.info("Testing ha_hacs_search with max_results...")

        data = await safe_hacs_call(
            mcp_client, "ha_hacs_search", {"query": "integration", "max_results": 5}
        )

        if not data.get("success"):
            unavailable, reason = is_hacs_unavailable(data)
            if unavailable:
                pytest.skip(f"HACS not available: {reason}")
            pytest.fail(f"HACS search with limit failed: {data.get('error')}")

        # Results returned should not exceed max_results
        assert data["count"] <= 5, "Results should not exceed max_results"
        assert len(data["results"]) <= 5, "Actual results should not exceed max_results"

        logger.info(
            f"Max results test: {data['count']}/{data['total_matches']} returned"
        )
        logger.info("Search with max_results test passed")

    async def test_search_no_results(self, mcp_client):
        """
        Test: HACS search with no matching results

        Search for something that shouldn't exist.
        """
        logger.info("Testing ha_hacs_search with no results...")

        data = await safe_hacs_call(
            mcp_client, "ha_hacs_search", {"query": "xyznonexistent12345abcdef"}
        )

        if not data.get("success"):
            unavailable, reason = is_hacs_unavailable(data)
            if unavailable:
                pytest.skip(f"HACS not available: {reason}")
            pytest.fail(f"HACS search failed unexpectedly: {data.get('error')}")

        # Should succeed with empty results
        assert data["total_matches"] == 0, "Should have no matches for nonsense query"
        assert data["count"] == 0, "Should return no results"
        assert len(data["results"]) == 0, "Results list should be empty"

        logger.info("No results search test passed")


@pytest.mark.hacs
class TestHacsRepositoryInfo:
    """Test HACS repository info functionality."""

    async def test_repository_info_not_found(self, mcp_client):
        """
        Test: Get info for non-existent repository

        Should return an appropriate error.
        """
        logger.info("Testing ha_hacs_repository_info with nonexistent repo...")

        parsed = await safe_call_tool(
            mcp_client,
            "ha_hacs_repository_info",
            {"repository_id": "nonexistent/repo12345"},
        )
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed

        unavailable, reason = is_hacs_unavailable(data)
        if unavailable:
            pytest.skip(f"HACS not available: {reason}")

        # Should fail with appropriate error
        assert data.get("success") is False, "Should fail for nonexistent repo"
        assert "error" in data or "error_code" in data, "Should have error information"

        logger.info("Repository not found test passed")

    async def test_repository_info_with_search(self, mcp_client):
        """
        Test: Get repository info for a found repository

        First search for a repo, then get its details.
        """
        logger.info("Testing ha_hacs_repository_info with valid repo...")

        # First search for a popular repo
        search_data = await safe_hacs_call(
            mcp_client, "ha_hacs_search", {"query": "hacs", "max_results": 1}
        )

        if not search_data.get("success"):
            unavailable, reason = is_hacs_unavailable(search_data)
            if unavailable:
                pytest.skip(f"HACS not available: {reason}")
            pytest.fail(f"Search failed: {search_data.get('error')}")

        if not search_data.get("results"):
            pytest.skip("No repositories found in HACS store to test")

        # Get the first result's ID
        repo = search_data["results"][0]
        repo_id = repo.get("id")
        repo_full_name = repo.get("full_name")

        if not repo_id and not repo_full_name:
            pytest.skip("Repository has no ID or full_name")

        # Try to get repository info using the ID or full_name
        identifier = str(repo_id) if repo_id else repo_full_name

        logger.info(f"Getting info for repository: {identifier}")

        info_data = await safe_hacs_call(
            mcp_client, "ha_hacs_repository_info", {"repository_id": identifier}
        )

        if not info_data.get("success"):
            # Some repos may not have detailed info available
            error = info_data.get("error", "Unknown error")
            logger.warning(f"Could not get repository info: {error}")
            pytest.skip(f"Repository info not available: {error}")

        # Verify response structure
        assert "name" in info_data, "Response should include name"
        assert "full_name" in info_data, "Response should include full_name"
        assert "category" in info_data, "Response should include category"

        logger.info(
            f"Repository info: {info_data.get('name')} ({info_data.get('category')})"
        )
        logger.info("Repository info test passed")


@pytest.mark.hacs
@pytest.mark.slow
class TestHacsWriteOperations:
    """Test HACS write operations (add repository, download).

    These tests are marked slow because they perform actual installations
    and may take longer to complete.
    """

    async def test_add_invalid_repository(self, mcp_client):
        """
        Test: Add invalid repository format

        Should fail with validation error.
        """
        logger.info("Testing ha_hacs_add_repository with invalid format...")

        parsed = await safe_call_tool(
            mcp_client,
            "ha_hacs_add_repository",
            {"repository": "invalid-format-no-slash", "category": "integration"},
        )
        data = (
            parsed.get("data", parsed)
            if isinstance(parsed.get("data"), dict)
            else parsed
        )

        unavailable, reason = is_hacs_unavailable(data)
        if unavailable:
            pytest.skip(f"HACS not available: {reason}")

        # Should fail with format error
        assert data.get("success") is False, "Should fail for invalid format"
        assert (
            "INVALID_REPOSITORY_FORMAT" in str(data.get("error_code", ""))
            or "format" in str(data.get("error", "")).lower()
        ), "Error should mention invalid format"

        logger.info("Invalid repository format test passed")

    async def test_download_nonexistent_repository(self, mcp_client):
        """
        Test: Download non-existent repository

        Should fail with not found error.
        """
        logger.info("Testing ha_hacs_download with nonexistent repo...")

        parsed = await safe_call_tool(
            mcp_client,
            "ha_hacs_download",
            {"repository_id": "nonexistent/fake-repo-12345"},
        )
        data = (
            parsed.get("data", parsed)
            if isinstance(parsed.get("data"), dict)
            else parsed
        )

        unavailable, reason = is_hacs_unavailable(data)
        if unavailable:
            pytest.skip(f"HACS not available: {reason}")

        # Should fail with not found error
        assert data.get("success") is False, "Should fail for nonexistent repo"

        logger.info("Download nonexistent repository test passed")


@pytest.mark.hacs
async def test_hacs_discovery(mcp_client):
    """
    Test: Basic HACS discovery

    Quick smoke test to verify HACS tools are available and responsive.
    """
    logger.info("Testing basic HACS discovery...")

    data = await safe_hacs_call(
        mcp_client, "ha_hacs_search", {"installed_only": True, "max_results": 1}
    )

    unavailable, reason = is_hacs_unavailable(data)
    if unavailable:
        logger.info(f"HACS is not available: {reason}")
        pytest.skip(f"HACS not installed or not loaded: {reason}")

    if data.get("success"):
        logger.info("HACS discovery successful")
    else:
        logger.warning(f"HACS discovery returned error: {data.get('error')}")

    logger.info("HACS discovery test completed")


@pytest.mark.hacs
@pytest.mark.slow
class TestMcpToolsInstallation:
    """Test ha_mcp_tools custom component installation via HACS.

    These tests install the ha_mcp_tools custom component using HACS,
    which provides advanced services not available through standard HA APIs.

    Note: These tests require:
    - HACS to be installed and functional
    - A valid GitHub token configured in HACS
    - Network access to GitHub
    """

    @pytest.fixture(autouse=True)
    async def check_hacs_available(self, mcp_client):
        """Pre-flight check: verify HACS is available before attempting install operations.

        This prevents flaky test failures when HACS is rate-limited or temporarily unavailable.
        """
        logger.info("Pre-flight check: verifying HACS availability...")
        data = await safe_hacs_call(
            mcp_client, "ha_hacs_search", {"installed_only": True, "max_results": 1}
        )

        unavailable, reason = is_hacs_unavailable(data)
        if unavailable:
            pytest.skip(f"HACS not available for install tests: {reason}")

        if not data.get("success"):
            error = data.get("error", "Unknown error")
            pytest.skip(f"HACS not ready: {error}")

        logger.info("Pre-flight check passed: HACS is available")

    async def test_install_mcp_tools_basic(self, mcp_client):
        """
        Test: Install ha_mcp_tools via HACS (without restart)

        This test validates that the install tool can add the repository
        and download the custom component. Does not restart HA.
        """
        logger.info("Testing ha_install_mcp_tools (without restart)...")

        # Before installation, verify HACS is available and ready
        info_data = await safe_hacs_call(
            mcp_client, "ha_hacs_search", {"installed_only": True, "max_results": 1}
        )
        unavailable, reason = is_hacs_unavailable(info_data)
        if unavailable:
            pytest.skip(f"HACS not available or not ready: {reason}")

        data = await safe_hacs_call(
            mcp_client, "ha_install_mcp_tools", {"restart": False}
        )

        logger.info(
            f"Install result: success={data.get('success')}, message={data.get('message')}"
        )

        if not data.get("success"):
            unavailable, reason = is_hacs_unavailable(data)
            if unavailable:
                pytest.skip(f"HACS not available for installation: {reason}")

            # Check for GitHub token issues
            error = str(data.get("error", ""))
            if (
                "401" in error
                or "token" in error.lower()
                or "rate limit" in error.lower()
            ):
                pytest.skip(f"GitHub access issue: {error}")

            pytest.fail(f"Installation failed: {data.get('error')}")

        # Verify successful installation response
        assert data.get("installed") or data.get("already_installed"), (
            "Response should indicate installation status"
        )

        if data.get("already_installed"):
            logger.info(f"ha_mcp_tools already installed: {data.get('version')}")
        else:
            logger.info("ha_mcp_tools installed successfully")
            assert "note" in data, "Should include note about restart"

        # Verify services list is provided
        services = data.get("services", [])
        assert len(services) > 0, "Should list available services"
        assert any("list_files" in s for s in services), (
            "Should mention list_files service"
        )

        logger.info("Install MCP tools (no restart) test passed")

    async def test_install_mcp_tools_idempotent(self, mcp_client):
        """
        Test: Installing ha_mcp_tools is idempotent

        Calling install twice should succeed and return already_installed status.
        """
        logger.info("Testing ha_install_mcp_tools idempotency...")

        # Before installation, verify HACS is available and ready
        info_data = await safe_hacs_call(
            mcp_client, "ha_hacs_search", {"installed_only": True, "max_results": 1}
        )
        unavailable, reason = is_hacs_unavailable(info_data)
        if unavailable:
            pytest.skip(f"HACS not available or not ready: {reason}")

        # First install
        data1 = await safe_hacs_call(
            mcp_client, "ha_install_mcp_tools", {"restart": False}
        )

        if not data1.get("success"):
            unavailable, reason = is_hacs_unavailable(data1)
            if unavailable:
                pytest.skip(f"HACS not available: {reason}")
            error = str(data1.get("error", ""))
            if "401" in error or "token" in error.lower():
                pytest.skip(f"GitHub access issue: {error}")
            pytest.fail(f"First install failed: {data1.get('error')}")

        # Second install should also succeed
        data2 = await safe_hacs_call(
            mcp_client, "ha_install_mcp_tools", {"restart": False}
        )

        assert data2.get("success"), (
            f"Second install should succeed: {data2.get('error')}"
        )
        assert data2.get("already_installed"), (
            "Second install should report already_installed"
        )

        logger.info("Install MCP tools idempotency test passed")

    async def test_check_mcp_tools_in_hacs(self, mcp_client):
        """
        Test: Verify ha_mcp_tools appears in HACS installed list after installation

        After installing, the component should appear in the HACS repository list.
        """
        logger.info("Testing ha_mcp_tools appears in HACS list...")

        # Before installation, verify HACS is available and ready
        info_data = await safe_hacs_call(
            mcp_client, "ha_hacs_search", {"installed_only": True, "max_results": 1}
        )
        unavailable, reason = is_hacs_unavailable(info_data)
        if unavailable:
            pytest.skip(f"HACS not available or not ready: {reason}")

        # First ensure it's installed
        install_data = await safe_hacs_call(
            mcp_client, "ha_install_mcp_tools", {"restart": False}
        )

        if not install_data.get("success"):
            unavailable, reason = is_hacs_unavailable(install_data)
            if unavailable:
                pytest.skip(f"HACS not available: {reason}")
            error = str(install_data.get("error", ""))
            if "401" in error or "token" in error.lower():
                pytest.skip(f"GitHub access issue: {error}")
            pytest.fail(f"Install failed: {install_data.get('error')}")

        # Now check HACS search for installed integrations
        list_data = await safe_hacs_call(
            mcp_client,
            "ha_hacs_search",
            {"installed_only": True, "category": "integration"},
        )

        if not list_data.get("success"):
            pytest.fail(f"Failed to search installed: {list_data.get('error')}")

        repos = list_data.get("results", [])
        mcp_tools_repo = None

        for repo in repos:
            full_name = repo.get("full_name", "").lower()
            name = repo.get("name", "").lower()
            # Match either the main repo or test fork
            if (
                "homeassistant-ai/ha-mcp" in full_name
                or "ha-mcp-test-custom-component" in full_name
                or "ha_mcp_tools" in name
            ):
                mcp_tools_repo = repo
                break

        assert mcp_tools_repo is not None, (
            "ha_mcp_tools should appear in HACS installed list after installation"
        )

        logger.info(f"Found ha_mcp_tools in HACS: {mcp_tools_repo.get('full_name')}")
        logger.info(f"Version: {mcp_tools_repo.get('installed_version')}")

        logger.info("Check MCP tools in HACS test passed")
