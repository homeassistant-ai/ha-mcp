"""
End-to-End tests for Home Assistant Dashboard Resource Management.

This test suite validates the complete lifecycle of dashboard resources including:
- Resource listing
- Resource creation (module, js, css types)
- Resource updates (URL and type changes)
- Resource deletion
- Error handling and validation
- Type validation

Each test uses real Home Assistant API calls via the MCP server to ensure
production-level functionality and compatibility.
"""

import base64
import logging

from ha_mcp.client import HomeAssistantClient

# Import test utilities
from ...utilities.assertions import (
    MCPAssertions,
    extract_error_message,
    safe_call_tool,
)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TestDashboardResourceLifecycle:
    """Test complete dashboard resource CRUD lifecycle."""

    async def test_basic_resource_lifecycle(self, mcp_client):
        """Test create, read, update, delete resource workflow."""
        logger.info("Starting basic resource lifecycle test")
        mcp = MCPAssertions(mcp_client)

        # 1. List initial resources to establish baseline
        logger.info("Listing initial resources...")
        initial_list = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {}
        )
        assert initial_list["success"] is True
        assert "resources" in initial_list
        assert "total_count" in initial_list
        # total_count, not count: count is the page size, so it stops tracking
        # the collection once it holds more than the default limit.
        initial_count = initial_list["total_count"]
        logger.info(f"Initial resource count: {initial_count}")

        # 2. Add a new resource (module type) using set (upsert without resource_id = create)
        logger.info("Adding test resource...")
        add_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {
                "url": "/local/test-e2e-card.js",
                "resource_type": "module",
            },
        )
        assert add_data["success"] is True
        assert add_data["action"] == "created"
        assert add_data["url"] == "/local/test-e2e-card.js"
        assert add_data["resource_type"] == "module"
        resource_id = add_data.get("resource_id")
        assert resource_id is not None, "Resource creation should return resource_id"
        logger.info(f"Created resource with ID: {resource_id}")

        # Small delay for HA to process

        # 3. List resources - verify new resource exists
        logger.info("Verifying resource was added...")
        list_data = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {}
        )
        assert list_data["success"] is True
        assert list_data["total_count"] == initial_count + 1
        assert any(
            r.get("url") == "/local/test-e2e-card.js"
            for r in list_data.get("resources", [])
        )

        # 4. Update the resource URL using set with resource_id
        logger.info("Updating resource URL...")
        update_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {
                "url": "/local/test-e2e-card-v2.js",
                "resource_type": "module",
                "resource_id": resource_id,
            },
        )
        assert update_data["success"] is True
        assert update_data["action"] == "updated"

        # 5. Verify update was applied
        logger.info("Verifying resource update...")
        list_after_update = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {}
        )
        updated_resource = next(
            (
                r
                for r in list_after_update.get("resources", [])
                if r.get("id") == resource_id
            ),
            None,
        )
        assert updated_resource is not None, "Updated resource should still exist"
        assert updated_resource.get("url") == "/local/test-e2e-card-v2.js"

        # 6. Delete the resource
        logger.info("Deleting test resource...")
        delete_data = await mcp.call_tool_success(
            "ha_config_delete_dashboard_resource",
            {"resource_id": resource_id},
        )
        assert delete_data["success"] is True
        assert delete_data["action"] == "delete"

        # 7. Verify deletion
        logger.info("Verifying resource deletion...")
        list_after_delete = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {}
        )
        assert list_after_delete["total_count"] == initial_count
        assert not any(
            r.get("id") == resource_id for r in list_after_delete.get("resources", [])
        )

        logger.info("Basic resource lifecycle test completed successfully")

    async def test_resource_types(self, mcp_client):
        """Test creating resources of different types (module, js, css)."""
        logger.info("Starting resource types test")
        mcp = MCPAssertions(mcp_client)

        created_ids = []

        try:
            # Test module type
            logger.info("Testing module type resource...")
            module_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {"url": "/local/test-module.js", "resource_type": "module"},
            )
            assert module_data["success"] is True
            assert module_data["resource_type"] == "module"
            created_ids.append(module_data.get("resource_id"))

            # Test js type
            logger.info("Testing js type resource...")
            js_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {"url": "/local/test-legacy.js", "resource_type": "js"},
            )
            assert js_data["success"] is True
            assert js_data["resource_type"] == "js"
            created_ids.append(js_data.get("resource_id"))

            # Test css type
            logger.info("Testing css type resource...")
            css_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {"url": "/local/test-theme.css", "resource_type": "css"},
            )
            assert css_data["success"] is True
            assert css_data["resource_type"] == "css"
            created_ids.append(css_data.get("resource_id"))

            # Verify by_type categorization
            list_data = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {}
            )
            assert "by_type" in list_data
            logger.info(f"Resources by type: {list_data['by_type']}")

        finally:
            # Cleanup created resources
            for resource_id in created_ids:
                if resource_id:
                    await mcp_client.call_tool(
                        "ha_config_delete_dashboard_resource",
                        {"resource_id": resource_id},
                    )

        logger.info("Resource types test completed successfully")

    async def test_update_resource_type(self, mcp_client):
        """Test updating resource type."""
        logger.info("Starting update resource type test")
        mcp = MCPAssertions(mcp_client)

        resource_id = None
        try:
            # Create resource with js type
            add_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {"url": "/local/test-changetype.js", "resource_type": "js"},
            )
            resource_id = add_data.get("resource_id")
            assert resource_id is not None

            # Update to module type
            update_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {
                    "url": "/local/test-changetype.js",
                    "resource_type": "module",
                    "resource_id": resource_id,
                },
            )
            assert update_data["success"] is True
            assert update_data["action"] == "updated"

            # Verify type was changed
            list_data = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {}
            )
            updated_resource = next(
                (
                    r
                    for r in list_data.get("resources", [])
                    if r.get("id") == resource_id
                ),
                None,
            )
            assert updated_resource is not None
            assert updated_resource.get("type") == "module"

        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Update resource type test completed successfully")


class TestDashboardResourceValidation:
    """Test validation and error handling for dashboard resources."""

    async def test_invalid_resource_type(self, mcp_client):
        """Test that invalid resource type is rejected at schema level."""
        logger.info("Starting invalid resource type test")
        import pytest
        from fastmcp.exceptions import ToolError

        # FastMCP validates Literal types at schema level, raising ToolError
        with pytest.raises(ToolError) as exc_info:
            await mcp_client.call_tool(
                "ha_config_set_dashboard_resource",
                {"url": "/local/test.js", "resource_type": "invalid"},
            )

        # Verify the error message mentions the valid options
        error_msg = str(exc_info.value).lower()
        assert "module" in error_msg or "js" in error_msg or "css" in error_msg

        logger.info("Invalid resource type test completed successfully")

    async def test_delete_nonexistent_resource(self, mcp_client):
        """Test that deleting nonexistent resource returns RESOURCE_NOT_FOUND."""
        logger.info("Starting delete nonexistent resource test")
        mcp = MCPAssertions(mcp_client)

        # Deleting a resource that doesn't exist should return RESOURCE_NOT_FOUND
        delete_data = await mcp.call_tool_failure(
            "ha_config_delete_dashboard_resource",
            {"resource_id": "nonexistent-resource-id-12345"},
            expected_error="not found",
        )
        assert delete_data["success"] is False

        logger.info("Delete nonexistent resource test completed successfully")


class TestDashboardResourceList:
    """Test resource listing functionality."""

    async def test_list_resources_structure(self, mcp_client):
        """Test that list resources returns proper structure."""
        logger.info("Starting list resources structure test")
        mcp = MCPAssertions(mcp_client)

        list_data = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {}
        )

        assert list_data["success"] is True
        assert list_data["action"] == "list"
        assert "resources" in list_data
        assert "count" in list_data
        assert "by_type" in list_data

        # Verify by_type structure
        by_type = list_data["by_type"]
        assert "module" in by_type
        assert "js" in by_type
        assert "css" in by_type

        # All by_type values should be integers
        assert all(isinstance(v, int) for v in by_type.values())

        logger.info("List resources structure test completed successfully")

    async def test_list_resources_pagination(self, mcp_client):
        """Test that limit/offset paginate the resource listing (issue #1869).

        Also pins the aggregate contract: `by_type` and `inline_count`
        summarise the whole collection, so they must not shrink to the page.
        That is asserted within a single response — three module resources are
        created, and the aggregate has to see all three even though the page
        holds two — so a resource created concurrently cannot skew it.
        """
        logger.info("Starting resource pagination test")
        mcp = MCPAssertions(mcp_client)

        created: list[str] = []
        try:
            for i in range(3):
                add_data = await mcp.call_tool_success(
                    "ha_config_set_dashboard_resource",
                    {
                        "url": f"/local/test-e2e-page-{i}.js",
                        "resource_type": "module",
                    },
                )
                resource_id = add_data.get("resource_id")
                assert resource_id is not None, f"No resource_id: {add_data}"
                created.append(resource_id)

            first = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {"limit": 2, "offset": 0}
            )
            assert len(first["resources"]) == 2, f"limit=2 should cut the page: {first}"
            assert first["count"] == 2, f"count is the page size: {first}"
            assert first["total_count"] >= 3
            assert first["has_more"] is True
            assert first["next_offset"] == 2
            # The page holds 2 records, but the summary still counts all three.
            assert first["by_type"]["module"] >= 3, (
                f"by_type must summarise the collection, not the page: {first}"
            )

            second = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {"limit": 2, "offset": 2}
            )
            assert second["offset"] == 2
            # Records without an id are dropped rather than collected as None,
            # which would collide across the pages and fail a correct listing.
            first_ids = {r["id"] for r in first["resources"] if r.get("id")}
            second_ids = {r["id"] for r in second["resources"] if r.get("id")}
            assert not (first_ids & second_ids), (
                f"pages must not overlap: {first_ids & second_ids}"
            )

            logger.info(f"Pagination verified across {first['total_count']} resources")
        finally:
            for resource_id in created:
                await mcp.call_tool_success(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

    async def test_list_resources_returns_resource_ids(self, mcp_client):
        """Test that listed resources have IDs for CRUD operations."""
        logger.info("Starting list resources returns IDs test")
        mcp = MCPAssertions(mcp_client)

        # Create a resource first
        add_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"url": "/local/test-id-check.js", "resource_type": "module"},
        )
        resource_id = add_data.get("resource_id")

        try:
            list_data = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {}
            )

            # Find our resource
            our_resource = next(
                (
                    r
                    for r in list_data.get("resources", [])
                    if r.get("url") == "/local/test-id-check.js"
                ),
                None,
            )
            assert our_resource is not None, "Created resource should appear in list"
            assert "id" in our_resource, "Resource should have an ID"
            assert "url" in our_resource, "Resource should have a URL"
            assert "type" in our_resource, "Resource should have a type"

        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("List resources returns IDs test completed successfully")

    async def test_list_resources_include_content(self, mcp_client):
        """Test that include_content flag works."""
        logger.info("Starting list resources include_content test")
        mcp = MCPAssertions(mcp_client)

        # Create a known inline resource so the flag has something to act on;
        # asserting only success would pass with include_content ignored.
        content = "export const INCLUDE_CONTENT_PROBE = 1;" + ("// pad" * 40)
        created = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": content, "resource_type": "module"},
        )
        resource_id = created.get("resource_id")
        try:

            def _find(payload):
                return next(
                    (
                        r
                        for r in payload.get("resources", [])
                        if r.get("id") == resource_id
                    ),
                    None,
                )

            without = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {"include_content": False}
            )
            row = _find(without)
            assert row is not None
            assert "_content" not in row, "content leaked without the flag"
            assert row["_preview"].endswith("...")

            with_content = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {"include_content": True}
            )
            row = _find(with_content)
            assert row is not None
            assert row["_content"] == content
            assert "_preview" not in row
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("List resources include_content test completed successfully")


class TestDashboardResourceUrlPatterns:
    """Test various URL patterns for resources."""

    async def test_local_url_pattern(self, mcp_client):
        """Test /local/ URL pattern (www directory)."""
        logger.info("Starting local URL pattern test")
        mcp = MCPAssertions(mcp_client)

        add_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"url": "/local/custom-cards/my-card.js", "resource_type": "module"},
        )
        resource_id = add_data.get("resource_id")

        try:
            assert add_data["success"] is True
            assert add_data["url"] == "/local/custom-cards/my-card.js"
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Local URL pattern test completed successfully")

    async def test_external_url_pattern(self, mcp_client):
        """Test external HTTPS URL pattern."""
        logger.info("Starting external URL pattern test")
        mcp = MCPAssertions(mcp_client)

        add_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {
                "url": "https://cdn.jsdelivr.net/npm/test-card@1.0.0/dist/card.js",
                "resource_type": "module",
            },
        )
        resource_id = add_data.get("resource_id")

        try:
            assert add_data["success"] is True
            assert "jsdelivr" in add_data["url"]
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("External URL pattern test completed successfully")

    async def test_hacsfiles_url_pattern(self, mcp_client):
        """Test /hacsfiles/ URL pattern (HACS resources)."""
        logger.info("Starting hacsfiles URL pattern test")
        mcp = MCPAssertions(mcp_client)

        add_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"url": "/hacsfiles/button-card/button-card.js", "resource_type": "module"},
        )
        resource_id = add_data.get("resource_id")

        try:
            assert add_data["success"] is True
            assert add_data["url"] == "/hacsfiles/button-card/button-card.js"
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Hacsfiles URL pattern test completed successfully")


async def _raw_resource_url(
    ha_client: HomeAssistantClient, resource_id: str | None
) -> str | None:
    """Fetch a resource's URL straight from HA's registry over the raw WS API.

    Ground truth for what the tool actually registered — the MCP list tool
    masks inline URLs as "[inline]", so proving the stored URL is a
    self-contained data: URI needs this side channel.
    """
    result = await ha_client.send_websocket_message({"type": "lovelace/resources"})
    resources = result.get("result") if isinstance(result, dict) else result
    for res in resources or []:
        if str(res.get("id")) == str(resource_id):
            return res.get("url")
    return None


async def _assert_stored_as_data_uri(
    ha_client: HomeAssistantClient,
    resource_id: str | None,
    mime_prefix: str,
    expected_content: str,
) -> None:
    """Assert HA stored the resource as a data: URI decoding byte-identically.

    Goes through the raw WS API deliberately: the MCP list tool masks inline
    URLs as "[inline]", so only this side channel can prove what HA actually
    holds.
    """
    raw_url = await _raw_resource_url(ha_client, resource_id)
    assert raw_url is not None
    assert raw_url.startswith(mime_prefix)
    decoded = base64.b64decode(raw_url.partition(",")[2]).decode("utf-8")
    assert decoded == expected_content


class TestInlineDashboardResource:
    """Test inline dashboard resource creation (code to data: URI)."""

    async def test_create_inline_module(self, mcp_client, ha_client):
        """Inline module: registered as a data: URI holding the exact content."""
        logger.info("Starting inline module creation test")
        mcp = MCPAssertions(mcp_client)

        # Create inline resource
        content = "class TestCard extends HTMLElement { connectedCallback() { this.innerHTML = 'Test'; } } customElements.define('test-card', TestCard);"
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": content, "resource_type": "module"},
        )

        resource_id = create_data.get("resource_id")
        try:
            assert create_data["success"] is True
            assert create_data["action"] == "created"
            assert create_data["resource_type"] == "module"
            assert create_data["size"] == len(content.encode("utf-8"))
            assert resource_id is not None

            # Ground truth: HA's registry holds a self-contained data: URI
            # that decodes byte-identical to the submitted content.
            await _assert_stored_as_data_uri(
                ha_client, resource_id, "data:text/javascript;base64,", content
            )

            # Verify it appears in list with inline marker
            list_data = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {"include_content": True}
            )

            # Find our inline resource
            our_resource = next(
                (
                    r
                    for r in list_data.get("resources", [])
                    if r.get("id") == resource_id
                ),
                None,
            )
            assert our_resource is not None
            assert our_resource.get("_inline") is True
            assert our_resource.get("url") == "[inline]"
            assert our_resource.get("_content") == content
            assert "_legacy_worker" not in our_resource

        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Inline module creation test completed successfully")

    async def test_create_inline_css(self, mcp_client, ha_client):
        """Inline CSS: registered as a data:text/css URI holding the content."""
        logger.info("Starting inline CSS creation test")
        mcp = MCPAssertions(mcp_client)

        content = ".my-card { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 16px; }"
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": content, "resource_type": "css"},
        )

        resource_id = create_data.get("resource_id")
        try:
            assert create_data["success"] is True
            assert create_data["resource_type"] == "css"

            await _assert_stored_as_data_uri(
                ha_client, resource_id, "data:text/css;charset=utf-8;base64,", content
            )
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Inline CSS creation test completed successfully")

    async def test_large_inline_resource_round_trips(self, mcp_client, ha_client):
        """A near-cap payload survives HA storage and comes back byte-identical.

        The cap raise to 128KB is what this PR exists to enable, and the
        failure modes it could hit (HA WS message limits, the .storage
        write, the MCP response path) only appear against real HA — an
        in-process encode/decode round trip cannot catch them.
        """
        logger.info("Starting large inline resource test")
        mcp = MCPAssertions(mcp_client)

        # Realistic single-file bundle shape, sized just under the cap.
        newline = chr(10)
        filler = ("// padding to emulate a bundled card" + newline) * 3000
        content = (
            "class BigE2ECard extends HTMLElement {"
            "  setConfig(c) { this.config = c; }"
            "}"
            "customElements.define('big-e2e-card', BigE2ECard);" + newline + filler
        )
        content = content[:120_000]
        assert len(content.encode("utf-8")) > 100_000, "payload should be large"

        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": content, "resource_type": "module"},
        )
        resource_id = create_data.get("resource_id")
        try:
            assert create_data["success"] is True
            assert create_data["size"] == len(content.encode("utf-8"))

            # HA really stored the whole thing.
            raw_url = await _raw_resource_url(ha_client, resource_id)
            assert raw_url is not None
            decoded = base64.b64decode(raw_url.partition(",")[2]).decode("utf-8")
            assert decoded == content

            # And the tool can hand it back intact (the migration path
            # depends on full-fidelity read-back). Locate the resource
            # deterministically first: a fixed limit=1/offset=0 window only
            # ever inspects the first registry row, so the assertion below
            # would silently be skipped whenever this resource is not it.
            index_probe = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {"limit": 500}
            )
            ids = [r.get("id") for r in index_probe.get("resources", [])]
            assert resource_id in ids, f"resource {resource_id} missing from listing"
            offset = ids.index(resource_id)

            # Fetch exactly that row, so the whole content budget is
            # available to it regardless of what else is registered.
            listed = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources",
                {"include_content": True, "limit": 1, "offset": offset},
            )
            match = next(
                (r for r in listed.get("resources", []) if r.get("id") == resource_id),
                None,
            )
            assert match is not None, "resource not returned at its own offset"
            assert not match.get("_content_truncated"), (
                "a lone max-size resource must fit the per-response budget"
            )
            assert match["_content"] == content
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Large inline resource test completed successfully")

    async def test_legacy_worker_resource_migrates_on_update(
        self, mcp_client, ha_client
    ):
        """A pre-switch worker-hosted resource converts to a data: URI on update.

        Installations that created inline resources before the worker
        retirement (#2060) still have workers.dev URLs registered; re-saving
        with content= must replace them with self-contained data: URIs.
        """
        logger.info("Starting legacy worker migration test")
        mcp = MCPAssertions(mcp_client)

        # The ÿ forces bytes whose base64 uses the URL-safe alphabet's
        # '-'/'_' characters, so the legacy decoder's distinct alphabet is
        # actually exercised (standard-b64 decode of it would differ).
        legacy_content = "export const LEGACY = 'ÿÿ';"
        encoded = base64.urlsafe_b64encode(legacy_content.encode()).decode()
        assert "-" in encoded or "_" in encoded
        legacy_url = (
            "https://ha-mcp-resources.rapid-math-bbad.workers.dev/"
            f"{encoded}?type=module"
        )

        # Seed the pre-switch state via url= mode (a plain URL registration).
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"url": legacy_url, "resource_type": "module"},
        )
        resource_id = create_data.get("resource_id")
        try:
            # The list tool must recognize it, decode it locally, and flag it.
            list_data = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {"include_content": True}
            )
            legacy_res = next(
                (
                    r
                    for r in list_data.get("resources", [])
                    if r.get("id") == resource_id
                ),
                None,
            )
            assert legacy_res is not None
            assert legacy_res.get("_inline") is True
            assert legacy_res.get("_legacy_worker") is True
            assert legacy_res.get("_content") == legacy_content
            # The remediation text is emitted once per response, not per
            # resource, and must route agents through include_content=True
            # (the default response carries only the truncated _preview).
            assert "include_content=True" in list_data.get("migration_hint", "")

            # Re-save with content= → migrated to a data: URI.
            new_content = "export const MIGRATED = 2;"
            update_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {
                    "content": new_content,
                    "resource_type": "module",
                    "resource_id": resource_id,
                },
            )
            assert update_data["action"] == "updated"

            await _assert_stored_as_data_uri(
                ha_client, resource_id, "data:text/javascript;base64,", new_content
            )

            # The list tool no longer flags it as legacy-hosted.
            post_list = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {"include_content": True}
            )
            migrated = next(
                (
                    r
                    for r in post_list.get("resources", [])
                    if r.get("id") == resource_id
                ),
                None,
            )
            assert migrated is not None
            assert migrated.get("_inline") is True
            assert "_legacy_worker" not in migrated
            assert migrated.get("_content") == new_content
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Legacy worker migration test completed successfully")

    async def test_inline_empty_content_error(self, mcp_client):
        """Test that empty content returns error."""
        logger.info("Starting inline empty content error test")

        data = await safe_call_tool(
            mcp_client,
            "ha_config_set_dashboard_resource",
            {"content": ""},
        )
        assert data["success"] is False
        error_msg = extract_error_message(data)
        assert "empty" in error_msg.lower()

        logger.info("Inline empty content error test completed successfully")

    async def test_inline_update_existing(self, mcp_client):
        """Test updating an existing inline resource."""
        logger.info("Starting inline update test")
        mcp = MCPAssertions(mcp_client)

        # Create initial resource
        content_v1 = "const VERSION = 1;"
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": content_v1, "resource_type": "module"},
        )
        resource_id = create_data.get("resource_id")

        try:
            assert create_data["action"] == "created"

            # Update with new content
            content_v2 = "const VERSION = 2; // Updated"
            update_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {
                    "content": content_v2,
                    "resource_type": "module",
                    "resource_id": resource_id,
                },
            )
            assert update_data["success"] is True
            assert update_data["action"] == "updated"
            assert update_data["size"] == len(content_v2.encode("utf-8"))

        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Inline update test completed successfully")
