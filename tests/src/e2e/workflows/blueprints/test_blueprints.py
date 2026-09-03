"""
Blueprint Management E2E Tests

Tests the blueprint management tools:
- ha_manage_blueprints - one tool for the blueprint lifecycle:
  list / get / import / save / delete / substitute

Note: Tests are designed to work with both Docker test environment (localhost:8124)
and production environments. Blueprint availability may vary.
"""

import logging
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    extract_error_message,
    safe_call_tool,
    wait_for_automation,
)

logger = logging.getLogger(__name__)


def _blueprint_yaml(name: str, description: str, min_version: str | None = None) -> str:
    """Render a minimal automation blueprint for import tests."""
    homeassistant_block = (
        f'  homeassistant:\n    min_version: "{min_version}"\n' if min_version else ""
    )
    return f"""blueprint:
  name: {name}
  description: {description}
  domain: automation
{homeassistant_block}  input:
    target_entity:
      name: Target Entity
      description: The entity to control
      selector:
        entity: {{}}

trigger:
  - platform: time
    at: "00:00:00"

action:
  - service: homeassistant.turn_on
    target:
      entity_id: !input target_entity
"""


# The demo platform in the e2e image registers this light on every lane; the
# automation lifecycle suites lean on it the same way.
_STABLE_ENTITY = "light.bed_light"

# Every tier the get action can name in ``yaml_source``.
_YAML_SOURCES = ("file", "component", "tools_entry", "source_url")


class _ServedBlueprint:
    """One uniquely named blueprint served from the test HA's /local path.

    ``import_into`` writes the file and imports it through the tool under test,
    returning the installed path; ``delete`` removes it through the tool;
    ``cleanup`` unlinks the served file and belongs in ``finally``.
    """

    def __init__(self, server: dict[str, Any], tag: str) -> None:
        local_dir = server.get("local_dir")
        assert local_dir, (
            "local_blueprint_server must expose local_dir (writable served directory)"
        )
        self.run_id = uuid.uuid4().hex[:8]
        self.marker = f"{tag}-{self.run_id}"
        self.filename = f"e2e_{tag}_{self.run_id}.yaml"
        self.served_file = Path(local_dir) / self.filename
        self.url = f"{server['base_url']}/{self.filename}"
        self.yaml = _blueprint_yaml(f"E2E {tag} {self.run_id}", self.marker)

    async def import_into(self, mcp: MCPAssertions) -> str:
        self.served_file.write_text(self.yaml, encoding="utf-8")
        imported = await mcp.call_tool_success(
            "ha_manage_blueprints", {"action": "import", "url": self.url}
        )
        return imported["data"]["imported_blueprint"]["path"]

    async def delete(self, mcp: MCPAssertions, path: str) -> None:
        await mcp.call_tool_success(
            "ha_manage_blueprints",
            {"action": "delete", "domain": "automation", "path": path, "confirm": True},
        )

    def cleanup(self) -> None:
        self.served_file.unlink(missing_ok=True)


@pytest.mark.blueprint
class TestBlueprintManagement:
    """Test blueprint management workflows."""

    async def test_list_automation_blueprints(self, mcp_client):
        """
        Test: List automation blueprints

        Validates that we can list automation blueprints from Home Assistant.
        """
        logger.info("Testing ha_manage_blueprints (list) for automation domain...")

        async with MCPAssertions(mcp_client) as mcp:
            # List automation blueprints (path=None lists all)
            result = await mcp.call_tool_success(
                "ha_manage_blueprints",
                {"action": "list", "domain": "automation"},
            )

            # Verify response structure
            data = result["data"]
            assert "blueprints" in data, "Response should contain 'blueprints' key"
            assert "count" in data, "Response should contain 'count' key"
            assert "domain" in data, "Response should contain 'domain' key"
            assert data["domain"] == "automation", "Domain should be 'automation'"

            blueprints = data.get("blueprints", [])
            logger.info(f"Found {len(blueprints)} automation blueprints")

            # If blueprints exist, verify their structure
            if blueprints:
                first_blueprint = blueprints[0]
                assert "path" in first_blueprint, "Blueprint should have 'path'"
                assert "domain" in first_blueprint, "Blueprint should have 'domain'"
                assert "name" in first_blueprint, "Blueprint should have 'name'"
                logger.info(
                    f"First blueprint: {first_blueprint.get('name')} ({first_blueprint.get('path')})"
                )

            logger.info("ha_manage_blueprints (list) for automation domain succeeded")

    async def test_list_script_blueprints(self, mcp_client):
        """
        Test: List script blueprints

        Validates that we can list script blueprints from Home Assistant.
        """
        logger.info("Testing ha_manage_blueprints (list) for script domain...")

        async with MCPAssertions(mcp_client) as mcp:
            # List script blueprints (path=None lists all)
            result = await mcp.call_tool_success(
                "ha_manage_blueprints",
                {"action": "list", "domain": "script"},
            )

            # Verify response structure
            data = result["data"]
            assert "blueprints" in data, "Response should contain 'blueprints' key"
            assert "count" in data, "Response should contain 'count' key"
            assert data["domain"] == "script", "Domain should be 'script'"

            blueprints = data.get("blueprints", [])
            logger.info(f"Found {len(blueprints)} script blueprints")

            logger.info("ha_manage_blueprints (list) for script domain succeeded")

    async def test_list_blueprints_invalid_domain(self, mcp_client):
        """
        Test: List blueprints with invalid domain

        Validates proper error handling for invalid domain parameter.
        """
        logger.info("Testing ha_manage_blueprints with invalid domain...")

        async with MCPAssertions(mcp_client) as mcp:
            # Try to list blueprints with invalid domain
            result = await mcp.call_tool_failure(
                "ha_manage_blueprints",
                {"action": "list", "domain": "invalid_domain"},
                expected_error="Invalid domain",
            )

            # Verify error response includes valid domains
            assert "valid_domains" in result, (
                "Error response should include valid domains"
            )
            logger.info("ha_manage_blueprints properly rejects invalid domain")

    async def test_get_blueprint_details(self, mcp_client):
        """
        Test: Get blueprint details

        Validates that we can get detailed information about a specific blueprint.
        First lists blueprints, then retrieves details of an existing one.
        """
        logger.info("Testing ha_manage_blueprints...")

        async with MCPAssertions(mcp_client) as mcp:
            # First, list available blueprints
            list_result = await mcp.call_tool_success(
                "ha_manage_blueprints",
                {"action": "list", "domain": "automation"},
            )

            blueprints = list_result["data"].get("blueprints", [])

            if not blueprints:
                logger.info("No automation blueprints available, skipping detail test")
                pytest.skip("No automation blueprints available for testing")

            # Get details of the first blueprint
            first_blueprint_path = blueprints[0]["path"]
            logger.info(f"Getting details for blueprint: {first_blueprint_path}")

            detail_result = await mcp.call_tool_success(
                "ha_manage_blueprints",
                {"action": "get", "path": first_blueprint_path, "domain": "automation"},
            )

            # Verify response structure
            detail = detail_result["data"]
            assert "path" in detail, "Response should contain 'path'"
            assert "domain" in detail, "Response should contain 'domain'"
            assert "name" in detail, "Response should contain 'name'"
            assert detail["path"] == first_blueprint_path, (
                "Path should match requested path"
            )

            logger.info(f"Blueprint details retrieved: {detail.get('name')}")

            # Check for metadata if available
            if "metadata" in detail:
                meta = detail["metadata"]
                logger.info(
                    f"  Description: {(meta.get('description') or 'N/A')[:100]}..."
                )
                logger.info(f"  Author: {meta.get('author') or 'N/A'}")

            # Check for inputs if available
            if "inputs" in detail:
                inputs = detail["inputs"]
                logger.info(f"  Inputs: {len(inputs)} defined")

            logger.info("ha_manage_blueprints succeeded")

    async def test_get_blueprint_not_found(self, mcp_client):
        """
        Test: ha_manage_blueprints with a nonexistent path returns a structured
        error with code RESOURCE_NOT_FOUND, not success=True.

        Source path: tools_blueprints.py — when the requested path is absent
        from the blueprints registry, raise_tool_error is invoked with
        ErrorCode.RESOURCE_NOT_FOUND and the message "Blueprint not found: ...".

        Hardened from a single suggestions-presence check to explicit
        error-code and structured suggestion-presence assertions.
        """
        logger.info("Testing ha_manage_blueprints with non-existent path...")

        async with MCPAssertions(mcp_client) as mcp:
            # Try to get a non-existent blueprint
            result = await mcp.call_tool_failure(
                "ha_manage_blueprints",
                {
                    "action": "get",
                    "path": "nonexistent/blueprint_a2_e2e_xyz_404.yaml",
                    "domain": "automation",
                },
                expected_error="not found",
            )

            assert result["error"]["code"] == "RESOURCE_NOT_FOUND", (
                f"Expected error code RESOURCE_NOT_FOUND, got: {result['error']}"
            )
            assert "suggestion" in result["error"], (
                "Error response should include a suggestion"
            )
            logger.info("ha_manage_blueprints properly handles non-existent blueprint")

    async def test_get_blueprint_invalid_domain(self, mcp_client):
        """
        Test: Get blueprint with invalid domain

        Validates proper error handling for invalid domain parameter.
        """
        logger.info("Testing ha_manage_blueprints with invalid domain...")

        async with MCPAssertions(mcp_client) as mcp:
            # Try with invalid domain
            result = await mcp.call_tool_failure(
                "ha_manage_blueprints",
                {"action": "get", "path": "some/path.yaml", "domain": "invalid_domain"},
                expected_error="Invalid domain",
            )

            assert "valid_domains" in result, (
                "Error response should include valid domains"
            )
            logger.info("ha_manage_blueprints properly rejects invalid domain")

    async def test_import_blueprint_invalid_url(self, mcp_client):
        """
        Test: Import blueprint with invalid URL format

        Validates proper error handling for invalid URL format.
        """
        logger.info("Testing ha_manage_blueprints with invalid URL...")

        async with MCPAssertions(mcp_client) as mcp:
            # Try with invalid URL format
            await mcp.call_tool_failure(
                "ha_manage_blueprints",
                {"action": "import", "url": "not-a-valid-url"},
                expected_error="Invalid URL",
            )

            logger.info("ha_manage_blueprints properly rejects invalid URL format")

    @pytest.mark.slow
    async def test_import_blueprint_nonexistent_url(self, mcp_client):
        """
        Test: Import blueprint from non-existent URL

        Validates proper error handling when URL doesn't exist or isn't accessible.
        Note: This test makes an actual network request, hence marked as slow.
        """
        logger.info("Testing ha_manage_blueprints with non-existent URL...")

        async with MCPAssertions(mcp_client) as mcp:
            # Try with URL that doesn't exist
            result = await mcp.call_tool_failure(
                "ha_manage_blueprints",
                {
                    "action": "import",
                    "url": "https://example.com/nonexistent/blueprint.yaml",
                },
            )

            # Should fail with appropriate error (suggestions nested under "error")
            assert "suggestions" in result.get("error", {}), (
                "Error response should include suggestions"
            )
            logger.info("ha_manage_blueprints properly handles non-existent URL")

    @pytest.mark.slow
    async def test_import_blueprint_saves_to_disk(
        self, mcp_client, local_blueprint_server
    ):
        """
        Test: Import blueprint actually saves to disk (issue #685)

        Validates that ha_manage_blueprints calls both blueprint/import (validate)
        AND blueprint/save (persist), so the blueprint appears in the list.
        Uses a blueprint file served by the test Home Assistant instance to
        avoid external network dependencies.
        """
        logger.info("Testing ha_manage_blueprints saves blueprint to disk...")

        # Serve the blueprint through Home Assistant's own /local static path.
        # This avoids external network dependencies and host-to-container
        # routing differences across Docker environments.
        test_url = f"{local_blueprint_server['base_url']}/e2e_test_blueprint.yaml"
        logger.info(f"Using local blueprint URL: {test_url}")

        async with MCPAssertions(mcp_client) as mcp:
            # List blueprints before import
            before = await mcp.call_tool_success(
                "ha_manage_blueprints",
                {"action": "list", "domain": "automation"},
            )
            before_paths = [bp["path"] for bp in before["data"].get("blueprints", [])]

            # Try to import
            result = await safe_call_tool(
                mcp_client,
                "ha_manage_blueprints",
                {"action": "import", "url": test_url},
            )

            if result.get("success"):
                # Import succeeded - verify metadata is populated
                imported = result.get("data", {}).get("imported_blueprint", {})
                assert imported.get("path", "").endswith(".yaml"), (
                    f"Blueprint path should end with .yaml, got: {imported.get('path')}"
                )
                assert imported.get("domain") in ("automation", "script"), (
                    f"Blueprint domain should be automation or script, got: {imported.get('domain')}"
                )
                assert imported.get("name"), "Blueprint name should not be empty"
                assert imported["path"] not in before_paths, (
                    f"Blueprint {imported['path']} should not have existed before import"
                )
                logger.info(
                    f"Blueprint imported: {imported.get('name')} at {imported.get('path')}"
                )

                # Verify it appears in the blueprint list
                after = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "list", "domain": imported.get("domain", "automation")},
                )
                after_paths = [bp["path"] for bp in after["data"].get("blueprints", [])]
                assert imported["path"] in after_paths, (
                    f"Imported blueprint {imported['path']} should appear in blueprint list"
                )
                logger.info("Blueprint appears in list after import")
            else:
                # Only acceptable failure is "already exists"
                error_msg = extract_error_message(result)
                assert "already exists" in error_msg.lower(), (
                    f"Expected 'already exists' error, got: {result}"
                )
                logger.info("Blueprint already existed (prior test run), still valid")

            logger.info("ha_manage_blueprints save-to-disk test completed")

    @pytest.mark.slow
    async def test_reimport_blueprint_with_overwrite(
        self, mcp_client, local_blueprint_server
    ):
        """
        Test: Re-import an installed blueprint with overwrite=true (issue #1894)

        Validates the "Re-import Blueprint" equivalent: importing an already
        installed blueprint fails with RESOURCE_ALREADY_EXISTS unless
        overwrite=true is passed, in which case the save succeeds and reports
        overrides_existing=True.
        """
        test_url = f"{local_blueprint_server['base_url']}/e2e_test_blueprint.yaml"
        logger.info(f"Testing blueprint re-import with URL: {test_url}")

        async with MCPAssertions(mcp_client) as mcp:
            # Ensure the blueprint is installed (tolerate a prior import)
            first = await safe_call_tool(
                mcp_client,
                "ha_manage_blueprints",
                {"action": "import", "url": test_url},
            )
            if not first.get("success"):
                error_msg = extract_error_message(first)
                assert "already exists" in error_msg.lower(), (
                    f"Expected 'already exists' error, got: {first}"
                )
                logger.info("Blueprint already installed from a prior run")

            # Re-import without overwrite must fail with a structured error
            failure = await mcp.call_tool_failure(
                "ha_manage_blueprints",
                {"action": "import", "url": test_url},
                expected_error="already exists",
            )
            assert failure["error"]["code"] == "RESOURCE_ALREADY_EXISTS", (
                f"Expected RESOURCE_ALREADY_EXISTS, got: {failure['error']}"
            )
            assert "overwrite" in str(failure["error"]).lower(), (
                "Error should point the caller at overwrite=true"
            )
            logger.info("Re-import without overwrite properly rejected")

            # Re-import with overwrite succeeds and reports the override
            result = await mcp.call_tool_success(
                "ha_manage_blueprints",
                {"action": "import", "url": test_url, "overwrite": True},
            )
            assert result.get("data", {}).get("overrides_existing") is True, (
                f"Expected overrides_existing=True, got: {result}"
            )
            imported = result.get("data", {}).get("imported_blueprint", {})
            assert imported.get("path", "").endswith(".yaml"), (
                f"Blueprint path should end with .yaml, got: {imported.get('path')}"
            )
            logger.info("Blueprint re-imported with overwrite=true")

    @pytest.mark.slow
    async def test_reimport_updates_blueprint_content(
        self, mcp_client, local_blueprint_server
    ):
        """
        Test: Re-import actually replaces the installed blueprint content (issue #1894)

        The issue-#1894 user story: a blueprint's source changed upstream and the
        user re-imports to pick up the new version. Serves v1 of a uniquely-named
        blueprint, imports it (overwrite=true on a fresh import must succeed with
        overrides_existing=False), then serves changed content and re-imports,
        verifying via ha_manage_blueprints that the NEW content landed.
        """
        local_dir = local_blueprint_server.get("local_dir")
        assert local_dir, (
            "local_blueprint_server must expose local_dir (writable served directory)"
        )

        run_id = uuid.uuid4().hex[:8]
        filename = f"e2e_reimport_{run_id}.yaml"
        served_file = Path(local_dir) / filename
        test_url = f"{local_blueprint_server['base_url']}/{filename}"
        marker_v1 = f"reimport-v1-{run_id}"
        marker_v2 = f"reimport-v2-{run_id}"

        try:
            served_file.write_text(
                _blueprint_yaml(f"Reimport E2E {run_id}", marker_v1), encoding="utf-8"
            )

            async with MCPAssertions(mcp_client) as mcp:
                # overwrite=true on a not-yet-installed blueprint: plain install
                first = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "import", "url": test_url, "overwrite": True},
                )
                assert first["data"].get("overrides_existing") is False, (
                    f"Fresh import must not report an override, got: {first}"
                )
                blueprint_path = first["data"]["imported_blueprint"]["path"]

                # Serve changed content and re-import
                served_file.write_text(
                    _blueprint_yaml(f"Reimport E2E {run_id}", marker_v2),
                    encoding="utf-8",
                )
                second = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "import", "url": test_url, "overwrite": True},
                )
                assert second["data"].get("overrides_existing") is True, (
                    f"Re-import must report the override, got: {second}"
                )
                assert "reload" in second["data"].get("message", "").lower(), (
                    f"Override response should mention the consumer reload, got: {second}"
                )

                # The installed blueprint must now carry the v2 content
                detail = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "get", "path": blueprint_path, "domain": "automation"},
                )
                description = (detail["data"].get("metadata") or {}).get(
                    "description"
                ) or ""
                assert marker_v2 in description, (
                    f"Re-imported blueprint should carry '{marker_v2}', got: {description}"
                )
                assert marker_v1 not in description, (
                    f"Old content '{marker_v1}' should be gone, got: {description}"
                )
                logger.info("Re-import replaced blueprint content on disk")
        finally:
            served_file.unlink(missing_ok=True)

    @pytest.mark.slow
    async def test_import_blueprint_min_version_rejected(
        self, mcp_client, local_blueprint_server
    ):
        """
        Test: import surfaces blueprint/import validation_errors

        blueprint/save does not re-run the min-version check, so the tool must
        fail the import itself instead of silently saving an unsupported
        blueprint (found while reviewing #1894's overwrite path, where it would
        clobber a working installed blueprint).
        """
        local_dir = local_blueprint_server.get("local_dir")
        assert local_dir, (
            "local_blueprint_server must expose local_dir (writable served directory)"
        )

        run_id = uuid.uuid4().hex[:8]
        filename = f"e2e_minversion_{run_id}.yaml"
        served_file = Path(local_dir) / filename
        test_url = f"{local_blueprint_server['base_url']}/{filename}"

        try:
            served_file.write_text(
                _blueprint_yaml(
                    f"MinVersion E2E {run_id}",
                    "requires an impossible HA version",
                    min_version="9999.1.0",
                ),
                encoding="utf-8",
            )

            async with MCPAssertions(mcp_client) as mcp:
                result = await mcp.call_tool_failure(
                    "ha_manage_blueprints",
                    {"action": "import", "url": test_url, "overwrite": True},
                    expected_error="Requires at least Home Assistant",
                )
                assert result["error"]["code"] == "VALIDATION_FAILED", (
                    f"Expected VALIDATION_FAILED, got: {result['error']}"
                )
                logger.info("Unsupported blueprint properly rejected at import")
        finally:
            served_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #2329
    # The tests below each serve a uniquely named blueprint from the test HA's
    # own /local path (no external network), import it, exercise the new
    # action, and unlink the served file in ``finally``. Installed blueprints
    # are deleted through the tool under test where the test succeeds; a
    # failed test leaves its uniquely named blueprint behind, which no other
    # test depends on.

    @pytest.mark.slow
    async def test_get_blueprint_returns_yaml_text(
        self, mcp_client, local_blueprint_server
    ):
        """``get`` carries the blueprint's YAML text and names the tier that
        produced it. Which tier answers depends on the lane (embedded → file,
        tools entry → component / tools_entry, bare install → source_url), so
        this asserts the contract every lane shares; the lane-specific tests
        below pin the tier."""
        served = _ServedBlueprint(local_blueprint_server, "get_yaml")
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                detail = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "get", "domain": "automation", "path": path},
                )
                assert served.marker in detail["data"].get("yaml", ""), (
                    f"get should return the served YAML text, got: {detail}"
                )
                assert detail["data"].get("yaml_source") in _YAML_SOURCES, detail
                # The parsed body arrives too, from whichever tier read the text.
                assert detail["data"]["config"]["blueprint"]["domain"] == "automation"
                if detail["data"]["yaml_source"] == "source_url":
                    assert any("re-fetched" in w for w in detail.get("warnings", [])), (
                        f"a source_url re-fetch must be flagged: {detail}"
                    )
                else:
                    assert not any(
                        "re-fetched" in w for w in detail.get("warnings", [])
                    ), detail
                await served.delete(mcp, path)
        finally:
            served.cleanup()

    @pytest.mark.slow
    @pytest.mark.embedded_only
    async def test_get_blueprint_reads_the_installed_file_when_embedded(
        self, mcp_client, local_blueprint_server
    ):
        """The in-process server reads the blueprint file itself (#2329 tier 1):
        no component command, no tools entry, no re-download."""
        served = _ServedBlueprint(local_blueprint_server, "embedded_file")
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                detail = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "get", "domain": "automation", "path": path},
                )
                assert detail["data"].get("yaml_source") == "file", detail
                assert served.marker in detail["data"]["yaml"]
                await served.delete(mcp, path)
        finally:
            served.cleanup()

    @pytest.mark.slow
    @pytest.mark.no_tools_only
    async def test_get_blueprint_yaml_arrives_without_the_tools_entry(
        self, mcp_client, local_blueprint_server
    ):
        """Without the File & YAML Tools entry the text still arrives — through
        the embedded read on the server-entry-only lanes, or the source_url
        re-fetch on a bare install — never through the filesystem tools."""
        served = _ServedBlueprint(local_blueprint_server, "no_tools")
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                detail = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "get", "domain": "automation", "path": path},
                )
                assert served.marker in detail["data"].get("yaml", ""), detail
                # Never "tools_entry": that tier is the File & YAML Tools
                # service, which these lanes deliberately do not have.
                assert detail["data"].get("yaml_source") in (
                    "file",
                    "component",
                    "source_url",
                ), detail
                await served.delete(mcp, path)
        finally:
            served.cleanup()

    @pytest.mark.slow
    async def test_duplicate_blueprint_via_save(
        self, mcp_client, local_blueprint_server
    ):
        """get → save under a new path duplicates a blueprint without the
        filesystem tools; both are listed afterwards."""
        served = _ServedBlueprint(local_blueprint_server, "dup")
        copy_path = f"e2e_copy_{served.run_id}.yaml"
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                detail = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "get", "domain": "automation", "path": path},
                )
                saved = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {
                        "action": "save",
                        "domain": "automation",
                        "path": copy_path,
                        "yaml": detail["data"]["yaml"],
                    },
                )
                assert saved["data"]["overrides_existing"] is False, saved
                listing = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "list", "domain": "automation"},
                )
                paths = {bp["path"] for bp in listing["data"]["blueprints"]}
                assert {path, copy_path} <= paths, (
                    f"expected both installed, got {paths}"
                )
                await served.delete(mcp, copy_path)
                await served.delete(mcp, path)
        finally:
            served.cleanup()

    @pytest.mark.slow
    async def test_edit_blueprint_in_place_via_save(
        self, mcp_client, local_blueprint_server
    ):
        """get → edit the text → save back with overwrite=True replaces the
        installed file and reports the consumer reload."""
        served = _ServedBlueprint(local_blueprint_server, "edit")
        edited_marker = f"edited-{served.run_id}"
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                detail = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "get", "domain": "automation", "path": path},
                )
                edited = detail["data"]["yaml"].replace(served.marker, edited_marker)
                assert edited != detail["data"]["yaml"]
                saved = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {
                        "action": "save",
                        "domain": "automation",
                        "path": path,
                        "yaml": edited,
                        "overwrite": True,
                    },
                )
                assert saved["data"]["overrides_existing"] is True, saved
                assert "reloaded" in saved["data"]["message"].lower()
                after = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "get", "domain": "automation", "path": path},
                )
                description = (after["data"].get("metadata") or {}).get(
                    "description"
                ) or ""
                assert edited_marker in description, after
                assert served.marker not in description, after
                await served.delete(mcp, path)
        finally:
            served.cleanup()

    @pytest.mark.slow
    async def test_save_without_overwrite_rejects_existing(
        self, mcp_client, local_blueprint_server
    ):
        served = _ServedBlueprint(local_blueprint_server, "save_exists")
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                result = await mcp.call_tool_failure(
                    "ha_manage_blueprints",
                    {
                        "action": "save",
                        "domain": "automation",
                        "path": path,
                        "yaml": served.yaml,
                    },
                    expected_error="already exists",
                )
                assert result["error"]["code"] == "RESOURCE_ALREADY_EXISTS", result
                await served.delete(mcp, path)
        finally:
            served.cleanup()

    async def test_save_rejects_invalid_yaml(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_failure(
                "ha_manage_blueprints",
                {
                    "action": "save",
                    "domain": "automation",
                    "path": f"e2e_invalid_{uuid.uuid4().hex[:8]}.yaml",
                    "yaml": "not: [a blueprint",
                },
            )
            assert result["error"]["code"] == "VALIDATION_FAILED", result
            listing = await mcp.call_tool_success(
                "ha_manage_blueprints", {"action": "list", "domain": "automation"}
            )
            assert not any(
                bp["path"].startswith("e2e_invalid_")
                for bp in listing["data"]["blueprints"]
            ), "an invalid blueprint must never be written"

    @pytest.mark.slow
    async def test_delete_blueprint_round_trip(
        self, mcp_client, local_blueprint_server
    ):
        """import → delete(confirm=True) → gone from list, get reports not found."""
        served = _ServedBlueprint(local_blueprint_server, "delete")
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                deleted = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {
                        "action": "delete",
                        "domain": "automation",
                        "path": path,
                        "confirm": True,
                    },
                )
                assert deleted["data"]["path"] == path, deleted
                listing = await mcp.call_tool_success(
                    "ha_manage_blueprints", {"action": "list", "domain": "automation"}
                )
                assert path not in {bp["path"] for bp in listing["data"]["blueprints"]}
                missing = await mcp.call_tool_failure(
                    "ha_manage_blueprints",
                    {"action": "get", "domain": "automation", "path": path},
                    expected_error="not found",
                )
                assert missing["error"]["code"] == "RESOURCE_NOT_FOUND", missing
        finally:
            served.cleanup()

    @pytest.mark.slow
    async def test_delete_blueprint_requires_confirm(
        self, mcp_client, local_blueprint_server
    ):
        served = _ServedBlueprint(local_blueprint_server, "confirm")
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                result = await mcp.call_tool_failure(
                    "ha_manage_blueprints",
                    {"action": "delete", "domain": "automation", "path": path},
                    expected_error="confirm",
                )
                assert result["error"]["code"] == "VALIDATION_INVALID_PARAMETER", result
                listing = await mcp.call_tool_success(
                    "ha_manage_blueprints", {"action": "list", "domain": "automation"}
                )
                assert path in {bp["path"] for bp in listing["data"]["blueprints"]}, (
                    "an unconfirmed delete must change nothing"
                )
                await served.delete(mcp, path)
        finally:
            served.cleanup()

    async def test_delete_blueprint_not_found(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_failure(
                "ha_manage_blueprints",
                {
                    "action": "delete",
                    "domain": "automation",
                    "path": f"e2e_missing_{uuid.uuid4().hex[:8]}.yaml",
                    "confirm": True,
                },
                expected_error="not found",
            )
            assert result["error"]["code"] == "RESOURCE_NOT_FOUND", result

    @pytest.mark.slow
    async def test_delete_blueprint_in_use_rejected(
        self, mcp_client, local_blueprint_server, cleanup_tracker
    ):
        """Home Assistant refuses to delete a blueprint an automation uses; the
        tool names that automation, and the delete succeeds once it is gone."""
        served = _ServedBlueprint(local_blueprint_server, "in_use")
        automation_id = None
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                created = await mcp.call_tool_success(
                    "ha_config_set_automation",
                    {
                        "config": {
                            "alias": f"E2E blueprint consumer {served.run_id}",
                            "use_blueprint": {
                                "path": path,
                                "input": {"target_entity": _STABLE_ENTITY},
                            },
                        }
                    },
                )
                automation_id = created["automation_id"]
                cleanup_tracker.track("automation", automation_id)
                assert await wait_for_automation(mcp_client, automation_id), (
                    f"{automation_id} never became retrievable"
                )

                refused = await mcp.call_tool_failure(
                    "ha_manage_blueprints",
                    {
                        "action": "delete",
                        "domain": "automation",
                        "path": path,
                        "confirm": True,
                    },
                    expected_error="in use",
                )
                assert refused["error"]["code"] == "RESOURCE_LOCKED", refused
                assert automation_id in refused.get("in_use_by", []), (
                    f"the consumer must be named: {refused}"
                )
                assert path in {
                    bp["path"]
                    for bp in (
                        await mcp.call_tool_success(
                            "ha_manage_blueprints",
                            {"action": "list", "domain": "automation"},
                        )
                    )["data"]["blueprints"]
                }, "a refused delete must leave the blueprint installed"

                await mcp.call_tool_success(
                    "ha_config_remove_automation", {"identifier": automation_id}
                )
                automation_id = None
                await served.delete(mcp, path)
        finally:
            if automation_id:
                await safe_call_tool(
                    mcp_client,
                    "ha_config_remove_automation",
                    {"identifier": automation_id},
                )
            served.cleanup()

    @pytest.mark.slow
    async def test_substitute_blueprint_renders_standalone_config(
        self, mcp_client, local_blueprint_server
    ):
        """``substitute`` renders the blueprint plus inputs into a config that
        no longer references the blueprint — the UI's "Take control"."""
        served = _ServedBlueprint(local_blueprint_server, "substitute")
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                rendered = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {
                        "action": "substitute",
                        "domain": "automation",
                        "path": path,
                        "input": {"target_entity": _STABLE_ENTITY},
                    },
                )
                config = rendered["data"]["config"]
                assert "blueprint" not in config and "use_blueprint" not in config, (
                    config
                )
                actions = config.get("action") or config.get("actions")
                assert actions, config
                assert actions[0]["target"]["entity_id"] == _STABLE_ENTITY, config
                # Nothing was written: the blueprint is still the only artifact.
                listing = await mcp.call_tool_success(
                    "ha_manage_blueprints", {"action": "list", "domain": "automation"}
                )
                assert path in {bp["path"] for bp in listing["data"]["blueprints"]}
                await served.delete(mcp, path)
        finally:
            served.cleanup()

    @pytest.mark.slow
    async def test_delete_blueprint_writes_auto_backup(
        self, mcp_client, local_blueprint_server
    ):
        """A confirmed delete is snapshotted first so ha_manage_backup can
        restore the file (#2329) — but only from an installed-file tier.
        The source_url re-download ``get`` may fall back to is never a
        snapshot source (a restore from it could write what the author
        publishes now, not what was deleted), so on a lane where ``get`` had
        to re-fetch, the contract is that NO snapshot was written."""
        served = _ServedBlueprint(local_blueprint_server, "backup")
        try:
            async with MCPAssertions(mcp_client) as mcp:
                path = await served.import_into(mcp)
                detail = await mcp.call_tool_success(
                    "ha_manage_blueprints",
                    {"action": "get", "domain": "automation", "path": path},
                )
                faithful_copy_available = detail["data"].get("yaml_source") in (
                    "file",
                    "component",
                    "tools_entry",
                )
                await served.delete(mcp, path)
                backups = await mcp.call_tool_success(
                    "ha_manage_backup",
                    {
                        "scope": "edits",
                        "action": "list",
                        "domain": "blueprint_automation",
                        "entity_id": path,
                    },
                )
                data = backups["data"]
                assert data["enabled"] is True, (
                    "auto-backup is off in this lane; the pre-delete snapshot "
                    f"cannot be verified: {data}"
                )
                # Snapshot names sanitise the id the way the backup manager
                # does (``_safe_entity_id``): every character outside
                # ``[A-Za-z0-9._-]`` — the ``/`` in a blueprint path — is ``_``.
                safe_path = re.sub(r"[^A-Za-z0-9._-]", "_", path)
                matching = [
                    b
                    for b in data["backups"]
                    if b.get("entity_id") == safe_path
                    and b.get("domain") == "blueprint_automation"
                ]
                if faithful_copy_available:
                    assert matching, f"no pre-delete snapshot for {path}: {data}"
                else:
                    assert not matching, (
                        "a source_url re-fetch must never be stored as the "
                        f"installed file: {data}"
                    )
        finally:
            served.cleanup()


@pytest.mark.blueprint
async def test_blueprint_discovery_workflow(mcp_client):
    """
    Test: Complete blueprint discovery workflow

    Validates the typical user journey for discovering and exploring blueprints:
    1. List all blueprints
    2. Get details of interesting blueprints
    3. Review inputs and configuration
    """
    logger.info("Testing complete blueprint discovery workflow...")

    async with MCPAssertions(mcp_client) as mcp:
        # Step 1: List automation blueprints
        logger.info("Step 1: List automation blueprints...")
        list_result = await mcp.call_tool_success(
            "ha_manage_blueprints",
            {"action": "list", "domain": "automation"},
        )

        automation_count = list_result["data"].get("count", 0)
        logger.info(f"Found {automation_count} automation blueprints")

        # Step 2: List script blueprints
        logger.info("Step 2: List script blueprints...")
        script_result = await mcp.call_tool_success(
            "ha_manage_blueprints",
            {"action": "list", "domain": "script"},
        )

        script_count = script_result["data"].get("count", 0)
        logger.info(f"Found {script_count} script blueprints")

        # Step 3: If blueprints exist, explore one
        blueprints = list_result["data"].get("blueprints", [])
        if blueprints:
            logger.info("Step 3: Exploring first blueprint...")
            first_blueprint = blueprints[0]

            detail_result = await mcp.call_tool_success(
                "ha_manage_blueprints",
                {
                    "action": "get",
                    "path": first_blueprint["path"],
                    "domain": "automation",
                },
            )

            explored = detail_result["data"]
            logger.info(f"Explored blueprint: {explored.get('name')}")

            # Log input requirements if available
            if "inputs" in explored:
                inputs = explored["inputs"]
                logger.info(f"Blueprint requires {len(inputs)} inputs:")
                for input_name, input_config in list(inputs.items())[:3]:
                    logger.info(
                        f"  - {input_name}: {(input_config.get('description') or 'No description')[:50]}"
                    )
        else:
            logger.info("Step 3: Skipped (no blueprints available)")

        logger.info("Blueprint discovery workflow completed successfully")


@pytest.mark.blueprint
async def test_blueprint_search_integration(mcp_client):
    """
    Test: Blueprint search integration

    Validates that blueprints can be discovered through search functionality
    and that the blueprint tools work with other MCP tools.
    """
    logger.info("Testing blueprint search integration...")

    async with MCPAssertions(mcp_client) as mcp:
        # List blueprints
        result = await mcp.call_tool_success(
            "ha_manage_blueprints",
            {"action": "list", "domain": "automation"},
        )

        blueprints = result["data"].get("blueprints", [])
        logger.info(f"Blueprint search found {len(blueprints)} results")

        # Verify blueprint metadata is searchable/useful
        for bp in blueprints[:3]:  # Check first 3
            assert "path" in bp, "Blueprint should have path for retrieval"
            assert "name" in bp, "Blueprint should have name for display"

        logger.info("Blueprint search integration test completed")


@pytest.mark.blueprint
async def test_blueprint_automation_lifecycle(mcp_client):
    """
    Test: Create and update blueprint-based automation

    Validates that blueprint automations can be created and updated without
    requiring trigger/action fields, fixing issue #363.
    """
    logger.info("Testing blueprint automation lifecycle...")

    async with MCPAssertions(mcp_client) as mcp:
        # Step 1: List available blueprints
        list_result = await mcp.call_tool_success(
            "ha_manage_blueprints",
            {"action": "list", "domain": "automation"},
        )

        blueprints = list_result["data"].get("blueprints", [])
        if not blueprints:
            logger.info("No automation blueprints available, skipping test")
            pytest.skip("No automation blueprints available for testing")

        # Use the first available blueprint
        blueprint_path = blueprints[0]["path"]
        logger.info(f"Using blueprint: {blueprint_path}")

        # Step 2: Get blueprint details to understand required inputs
        detail_result = await mcp.call_tool_success(
            "ha_manage_blueprints",
            {"action": "get", "path": blueprint_path, "domain": "automation"},
        )

        inputs = detail_result["data"].get("inputs", {})
        logger.info(f"Blueprint has {len(inputs)} inputs")

        # Step 3: Create automation from blueprint (no trigger/action fields)
        # Note: We can't actually test creation with empty inputs since HA validates
        # blueprint inputs. Instead, we test that the tool ACCEPTS the config without
        # trigger/action fields (it will fail later at HA validation, not our validation)
        automation_config = {
            "alias": "Test Blueprint Automation E2E",
            "description": "Testing blueprint automation creation (issue #363)",
            "use_blueprint": {
                "path": blueprint_path,
                "input": {},  # Empty inputs - will fail HA validation but pass our validation
            },
        }

        # This should reach HA (proving our validation passed) even if HA rejects it
        # If our validation failed, we'd get a different error code
        # Use safe_call_tool to handle ToolError exceptions from validation failures
        create_result = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": automation_config},
        )

        # Check if it was our validation or HA's validation that failed
        if not create_result.get("success"):
            error_msg = str(create_result.get("error", {}).get("message", ""))
            # If error is about missing blueprint inputs, our validation passed! HA rejected it.
            if "Missing input" in error_msg or "input" in error_msg.lower():
                logger.info(
                    "✅ Our validation passed (config reached HA), HA rejected due to missing blueprint inputs as expected"
                )
                logger.info(
                    "✅ Blueprint automation lifecycle test completed (validation works)"
                )
                return
            # If error is about missing trigger/action, our fix didn't work
            if "trigger" in error_msg.lower() or "action" in error_msg.lower():
                raise AssertionError(
                    f"Our validation failed - still requiring trigger/action: {error_msg}"
                )
            # Some other error
            raise AssertionError(f"Unexpected error: {create_result}")

        # If it succeeded, great! (unlikely with empty inputs)
        automation_id = create_result.get("entity_id") or create_result.get("id")
        assert automation_id, "Should return automation ID"
        logger.info(f"✅ Created blueprint automation: {automation_id}")

        # If we got here, the automation was created successfully
        # Step 4: Wait for automation to be registered, then verify no trigger/action fields
        config = await wait_for_automation(mcp_client, automation_id)
        assert config is not None, (
            f"Automation {automation_id} not found after creation"
        )
        assert "use_blueprint" in config, "Config should have use_blueprint"
        logger.info("✅ Blueprint automation config verified")

        # Step 5: Clean up
        await mcp.call_tool_success(
            "ha_config_remove_automation",
            {"identifier": automation_id},
        )

        logger.info("✅ Blueprint automation lifecycle test completed")


@pytest.mark.blueprint
async def test_blueprint_automation_with_empty_arrays(mcp_client):
    """
    Test: Blueprint automation with empty trigger/action arrays gets cleaned

    Validates that if a user mistakenly provides empty trigger/action/condition
    arrays with a blueprint automation, they are stripped before saving (issue #363).
    """
    logger.info("Testing blueprint automation with empty arrays...")

    async with MCPAssertions(mcp_client) as mcp:
        # List available blueprints
        list_result = await mcp.call_tool_success(
            "ha_manage_blueprints",
            {"action": "list", "domain": "automation"},
        )

        blueprints = list_result["data"].get("blueprints", [])
        if not blueprints:
            pytest.skip("No automation blueprints available for testing")

        blueprint_path = blueprints[0]["path"]

        # Create blueprint automation WITH empty arrays (should be stripped)
        automation_config = {
            "alias": "Test Blueprint Empty Arrays E2E",
            "use_blueprint": {
                "path": blueprint_path,
                "input": {},
            },
            "trigger": [],  # These should be stripped
            "action": [],  # These should be stripped
            "condition": [],  # These should be stripped
        }

        # The key test: This should pass our validation (not fail with "missing trigger/action")
        # It will fail HA validation due to missing blueprint inputs, but that's expected
        # Use safe_call_tool to handle ToolError exceptions from validation failures
        create_result = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": automation_config},
        )

        # If our validation works, it should reach HA (which will reject due to missing inputs)
        if not create_result.get("success"):
            error_msg = str(create_result.get("error", {}).get("message", ""))
            # If error is about missing blueprint inputs, our validation passed!
            if "Missing input" in error_msg or "input" in error_msg.lower():
                logger.info(
                    "✅ Empty arrays were stripped (passed our validation, failed HA blueprint validation as expected)"
                )
                logger.info("✅ Empty arrays test completed")
                return
            # If error is about missing trigger/action, our fix didn't work
            if "trigger" in error_msg.lower() or "action" in error_msg.lower():
                raise AssertionError(
                    f"Empty arrays not stripped - validation failed: {error_msg}"
                )
            # Some other error
            raise AssertionError(f"Unexpected error: {create_result}")

        # If somehow it succeeded (unlikely with empty inputs)
        automation_id = create_result.get("entity_id") or create_result.get("id")
        logger.info(
            f"✅ Created blueprint automation with empty arrays: {automation_id}"
        )

        # Clean up
        await mcp.call_tool_success(
            "ha_config_remove_automation",
            {"identifier": automation_id},
        )

        logger.info("✅ Empty arrays test completed")
