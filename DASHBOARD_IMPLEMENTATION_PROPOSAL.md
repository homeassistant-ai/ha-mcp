# Dashboard Management Implementation Proposal

**Issue:** #67 - Allow creating and modifying dashboards using ha-mcp
**Branch:** feature/dashboard-management
**Date:** 2025-11-16

## Research Summary

### Home Assistant Lovelace Dashboard API

After researching the Home Assistant core codebase via GitHub code search, I've identified the complete WebSocket API for dashboard management:

#### Available WebSocket Commands

**Dashboard Management (Collection Pattern):**
- `lovelace/dashboards/list` - List all storage-mode dashboards
- `lovelace/dashboards/create` - Create new dashboard
- `lovelace/dashboards/update` - Update dashboard metadata
- `lovelace/dashboards/delete` - Delete dashboard

**Dashboard Configuration (Content Management):**
- `lovelace/config` - Get dashboard content/config
- `lovelace/config/save` - Save dashboard content/config
- `lovelace/config/delete` - Delete dashboard config

#### Key Implementation Details

**Dashboard Metadata Schema (from const.py):**
```python
STORAGE_DASHBOARD_CREATE_FIELDS = {
    "url_path": str,              # Required - URL slug (must contain hyphen)
    "title": str,                 # Required - Dashboard display name
    "icon": str,                  # Optional - MDI icon (default: "mdi:view-dashboard")
    "require_admin": bool,        # Optional - Admin-only access (default: False)
    "show_in_sidebar": bool,      # Optional - Show in sidebar (default: True)
    "mode": "storage"             # Always "storage" for created dashboards
}

STORAGE_DASHBOARD_UPDATE_FIELDS = {
    "title": str,                 # Optional
    "icon": str | None,           # Optional
    "require_admin": bool,        # Optional
    "show_in_sidebar": bool       # Optional
}
```

**Dashboard Configuration Format:**
- Can be dict or string (YAML)
- Contains views, cards, and other Lovelace UI config
- Validated by Home Assistant when saved

**Important Constraints:**
- `url_path` must contain a hyphen (-) unless `allow_single_word: true`
- `url_path` must be unique (not conflict with existing panels)
- Only storage-mode dashboards can be created via API
- YAML-mode dashboards are read-only (defined in configuration.yaml)

## Proposed Implementation

### 1. WebSocket Client Methods (`src/ha_mcp/client/websocket_client.py`)

Add dashboard-specific WebSocket methods:

```python
async def list_dashboards(self) -> list[dict[str, Any]]:
    """List all storage-mode dashboards."""
    return await self.send_command({
        "type": "lovelace/dashboards/list"
    })

async def create_dashboard(
    self,
    url_path: str,
    title: str,
    icon: str | None = None,
    require_admin: bool = False,
    show_in_sidebar: bool = True
) -> dict[str, Any]:
    """Create a new storage-mode dashboard."""
    data = {
        "type": "lovelace/dashboards/create",
        "url_path": url_path,
        "title": title,
        "require_admin": require_admin,
        "show_in_sidebar": show_in_sidebar
    }
    if icon:
        data["icon"] = icon
    return await self.send_command(data)

async def update_dashboard(
    self,
    dashboard_id: str,
    title: str | None = None,
    icon: str | None = None,
    require_admin: bool | None = None,
    show_in_sidebar: bool | None = None
) -> dict[str, Any]:
    """Update dashboard metadata."""
    data = {
        "type": "lovelace/dashboards/update",
        "dashboard_id": dashboard_id
    }
    if title is not None:
        data["title"] = title
    if icon is not None:
        data["icon"] = icon
    if require_admin is not None:
        data["require_admin"] = require_admin
    if show_in_sidebar is not None:
        data["show_in_sidebar"] = show_in_sidebar
    return await self.send_command(data)

async def delete_dashboard(self, dashboard_id: str) -> dict[str, Any]:
    """Delete a storage-mode dashboard."""
    return await self.send_command({
        "type": "lovelace/dashboards/delete",
        "dashboard_id": dashboard_id
    })

async def get_dashboard_config(
    self,
    url_path: str | None = None,
    force: bool = False
) -> dict[str, Any]:
    """Get dashboard configuration/content."""
    data = {"type": "lovelace/config", "force": force}
    if url_path:
        data["url_path"] = url_path
    return await self.send_command(data)

async def save_dashboard_config(
    self,
    config: dict[str, Any] | str,
    url_path: str | None = None
) -> dict[str, Any]:
    """Save dashboard configuration/content."""
    data = {
        "type": "lovelace/config/save",
        "config": config
    }
    if url_path:
        data["url_path"] = url_path
    return await self.send_command(data)

async def delete_dashboard_config(
    self, url_path: str | None = None
) -> dict[str, Any]:
    """Delete dashboard configuration (not the dashboard itself)."""
    data = {"type": "lovelace/config/delete"}
    if url_path:
        data["url_path"] = url_path
    return await self.send_command(data)
```

### 2. MCP Tools Module (`src/ha_mcp/tools/tools_config_dashboards.py`)

New module following the existing config tools pattern:

```python
"""
Configuration management tools for Home Assistant Lovelace dashboards.

This module provides tools for managing dashboard metadata and content.
"""

import logging
from typing import Annotated, Any, cast

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)


def register_config_dashboard_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant dashboard configuration tools."""

    @mcp.tool
    @log_tool_usage
    async def ha_config_list_dashboards() -> dict[str, Any]:
        """
        List all Home Assistant storage-mode dashboards.

        Returns metadata for all custom dashboards including url_path, title,
        icon, admin requirements, and sidebar visibility.

        Note: Only shows storage-mode dashboards. YAML-mode dashboards
        (defined in configuration.yaml) are not included.

        EXAMPLES:
        - List dashboards: ha_config_list_dashboards()
        """
        try:
            dashboards = await client.websocket_client.list_dashboards()
            return {
                "success": True,
                "action": "list",
                "dashboards": dashboards,
                "count": len(dashboards)
            }
        except Exception as e:
            logger.error(f"Error listing dashboards: {e}")
            return {
                "success": False,
                "action": "list",
                "error": str(e)
            }

    @mcp.tool
    @log_tool_usage
    async def ha_config_get_dashboard(
        url_path: Annotated[
            str | None,
            Field(
                description="Dashboard URL path (e.g., 'lovelace-home'). "
                "Use None or empty string for default dashboard."
            )
        ] = None,
        force_reload: Annotated[
            bool,
            Field(description="Force reload from storage (bypass cache)")
        ] = False
    ) -> dict[str, Any]:
        """
        Get complete dashboard configuration including all views and cards.

        Returns the full Lovelace dashboard configuration in JSON format.

        EXAMPLES:
        - Get default dashboard: ha_config_get_dashboard()
        - Get custom dashboard: ha_config_get_dashboard("lovelace-mobile")
        - Force reload: ha_config_get_dashboard("lovelace-home", force_reload=True)
        """
        try:
            config = await client.websocket_client.get_dashboard_config(
                url_path=url_path or None,
                force=force_reload
            )
            return {
                "success": True,
                "action": "get",
                "url_path": url_path,
                "config": config
            }
        except Exception as e:
            logger.error(f"Error getting dashboard config: {e}")
            return {
                "success": False,
                "action": "get",
                "url_path": url_path,
                "error": str(e),
                "suggestions": [
                    "Verify dashboard exists using ha_config_list_dashboards()",
                    "Check if you have permission to access this dashboard",
                    "Use None/empty string for default dashboard"
                ]
            }

    @mcp.tool
    @log_tool_usage
    async def ha_config_create_dashboard(
        url_path: Annotated[
            str,
            Field(
                description="Unique URL path for dashboard (must contain hyphen, "
                "e.g., 'my-dashboard', 'mobile-view')"
            )
        ],
        title: Annotated[
            str,
            Field(description="Dashboard display name shown in sidebar")
        ],
        icon: Annotated[
            str | None,
            Field(
                description="MDI icon name (e.g., 'mdi:home', 'mdi:cellphone'). "
                "Defaults to 'mdi:view-dashboard'"
            )
        ] = None,
        require_admin: Annotated[
            bool,
            Field(description="Restrict dashboard to admin users only")
        ] = False,
        show_in_sidebar: Annotated[
            bool,
            Field(description="Show dashboard in sidebar navigation")
        ] = True,
        initial_config: Annotated[
            str | dict[str, Any] | None,
            Field(
                description="Optional initial dashboard configuration. "
                "Can be dict or YAML string with views and cards."
            )
        ] = None
    ) -> dict[str, Any]:
        """
        Create a new Home Assistant dashboard.

        Creates a new storage-mode dashboard with optional initial configuration.

        IMPORTANT: url_path must contain a hyphen (-) to be valid.

        EXAMPLES:

        Create empty dashboard:
        ha_config_create_dashboard(
            url_path="mobile-dashboard",
            title="Mobile View",
            icon="mdi:cellphone"
        )

        Create dashboard with initial config:
        ha_config_create_dashboard(
            url_path="home-dashboard",
            title="Home Overview",
            initial_config={
                "views": [{
                    "title": "Home",
                    "cards": [{
                        "type": "entities",
                        "entities": ["light.living_room"]
                    }]
                }]
            }
        )
        """
        try:
            # Validate url_path contains hyphen
            if "-" not in url_path:
                return {
                    "success": False,
                    "action": "create",
                    "error": "url_path must contain a hyphen (-)",
                    "suggestions": [
                        f"Try '{url_path.replace('_', '-')}' instead",
                        "Use format like 'my-dashboard' or 'mobile-view'"
                    ]
                }

            # Create dashboard metadata
            result = await client.websocket_client.create_dashboard(
                url_path=url_path,
                title=title,
                icon=icon,
                require_admin=require_admin,
                show_in_sidebar=show_in_sidebar
            )

            # Set initial config if provided
            if initial_config:
                config_to_save = parse_json_param(initial_config)
                await client.websocket_client.save_dashboard_config(
                    config=config_to_save,
                    url_path=url_path
                )

            return {
                "success": True,
                "action": "create",
                "url_path": url_path,
                "dashboard": result,
                "has_initial_config": initial_config is not None
            }
        except Exception as e:
            logger.error(f"Error creating dashboard: {e}")
            return {
                "success": False,
                "action": "create",
                "url_path": url_path,
                "error": str(e),
                "suggestions": [
                    "Ensure url_path is unique (not already in use)",
                    "Verify url_path contains a hyphen",
                    "Check that you have admin permissions"
                ]
            }

    @mcp.tool
    @log_tool_usage
    async def ha_config_update_dashboard(
        url_path: Annotated[
            str,
            Field(description="Dashboard URL path to update")
        ],
        config: Annotated[
            str | dict[str, Any],
            Field(
                description="Complete dashboard configuration with views, cards, etc. "
                "Can be dict or YAML string. This REPLACES the entire config."
            )
        ]
    ) -> dict[str, Any]:
        """
        Update dashboard configuration (views, cards, layout).

        IMPORTANT: This replaces the ENTIRE dashboard configuration.
        Get current config first with ha_config_get_dashboard() if you want
        to make partial updates.

        EXAMPLES:

        Update dashboard with new config:
        ha_config_update_dashboard(
            url_path="mobile-dashboard",
            config={
                "views": [{
                    "title": "Home",
                    "cards": [
                        {"type": "weather-forecast", "entity": "weather.home"},
                        {"type": "entities", "entities": ["light.bedroom"]}
                    ]
                }]
            }
        )

        Workflow for partial updates:
        1. current = ha_config_get_dashboard("my-dashboard")
        2. Modify current["config"] as needed
        3. ha_config_update_dashboard("my-dashboard", current["config"])
        """
        try:
            config_to_save = parse_json_param(config)
            await client.websocket_client.save_dashboard_config(
                config=config_to_save,
                url_path=url_path
            )
            return {
                "success": True,
                "action": "update",
                "url_path": url_path,
                "message": "Dashboard configuration updated successfully"
            }
        except Exception as e:
            logger.error(f"Error updating dashboard config: {e}")
            return {
                "success": False,
                "action": "update",
                "url_path": url_path,
                "error": str(e),
                "suggestions": [
                    "Verify dashboard exists using ha_config_list_dashboards()",
                    "Check configuration format is valid Lovelace YAML/JSON",
                    "Get current config first with ha_config_get_dashboard()"
                ]
            }

    @mcp.tool
    @log_tool_usage
    async def ha_config_update_dashboard_metadata(
        dashboard_id: Annotated[
            str,
            Field(description="Dashboard ID (typically same as url_path)")
        ],
        title: Annotated[
            str | None,
            Field(description="New dashboard title")
        ] = None,
        icon: Annotated[
            str | None,
            Field(description="New MDI icon name")
        ] = None,
        require_admin: Annotated[
            bool | None,
            Field(description="Update admin requirement")
        ] = None,
        show_in_sidebar: Annotated[
            bool | None,
            Field(description="Update sidebar visibility")
        ] = None
    ) -> dict[str, Any]:
        """
        Update dashboard metadata (title, icon, permissions).

        Updates dashboard properties without changing the actual configuration
        (views/cards). At least one field must be provided.

        EXAMPLES:

        Change dashboard title:
        ha_config_update_dashboard_metadata(
            dashboard_id="mobile-dashboard",
            title="Mobile View v2"
        )

        Update multiple properties:
        ha_config_update_dashboard_metadata(
            dashboard_id="admin-panel",
            title="Admin Dashboard",
            icon="mdi:shield-account",
            require_admin=True
        )
        """
        if all(x is None for x in [title, icon, require_admin, show_in_sidebar]):
            return {
                "success": False,
                "action": "update_metadata",
                "error": "At least one field must be provided to update"
            }

        try:
            result = await client.websocket_client.update_dashboard(
                dashboard_id=dashboard_id,
                title=title,
                icon=icon,
                require_admin=require_admin,
                show_in_sidebar=show_in_sidebar
            )
            return {
                "success": True,
                "action": "update_metadata",
                "dashboard_id": dashboard_id,
                "dashboard": result
            }
        except Exception as e:
            logger.error(f"Error updating dashboard metadata: {e}")
            return {
                "success": False,
                "action": "update_metadata",
                "dashboard_id": dashboard_id,
                "error": str(e),
                "suggestions": [
                    "Verify dashboard ID exists using ha_config_list_dashboards()",
                    "Check that you have admin permissions"
                ]
            }

    @mcp.tool
    @log_tool_usage
    async def ha_config_delete_dashboard(
        dashboard_id: Annotated[
            str,
            Field(description="Dashboard ID to delete (typically same as url_path)")
        ]
    ) -> dict[str, Any]:
        """
        Delete a storage-mode dashboard completely.

        WARNING: This permanently deletes the dashboard and all its configuration.
        Cannot be undone. Does not work on YAML-mode dashboards.

        EXAMPLES:
        - Delete dashboard: ha_config_delete_dashboard("mobile-dashboard")
        """
        try:
            await client.websocket_client.delete_dashboard(dashboard_id)
            return {
                "success": True,
                "action": "delete",
                "dashboard_id": dashboard_id,
                "message": "Dashboard deleted successfully"
            }
        except Exception as e:
            logger.error(f"Error deleting dashboard: {e}")
            return {
                "success": False,
                "action": "delete",
                "dashboard_id": dashboard_id,
                "error": str(e),
                "suggestions": [
                    "Verify dashboard exists and is storage-mode",
                    "Check that you have admin permissions",
                    "Use ha_config_list_dashboards() to see available dashboards"
                ]
            }
```

### 3. Registry Integration (`src/ha_mcp/tools/registry.py`)

Add to imports:
```python
from .tools_config_dashboards import register_config_dashboard_tools
```

Add to `register_all_tools()`:
```python
# Register config management tools (helpers, scripts, automations, dashboards)
register_config_helper_tools(self.mcp, self.client)
register_config_script_tools(self.mcp, self.client)
register_config_automation_tools(self.mcp, self.client)
register_config_dashboard_tools(self.mcp, self.client)  # NEW
```

### 4. REST Client Fallback (Optional)

For systems without WebSocket access, consider adding REST endpoints if available.
Initial research shows dashboards are primarily WebSocket-based.

## Tool Summary

The implementation adds **6 new MCP tools**:

1. **ha_config_list_dashboards()** - List all storage-mode dashboards
2. **ha_config_get_dashboard(url_path?)** - Get complete dashboard config
3. **ha_config_create_dashboard(...)** - Create new dashboard with metadata and optional initial config
4. **ha_config_update_dashboard(url_path, config)** - Update dashboard configuration (content)
5. **ha_config_update_dashboard_metadata(...)** - Update dashboard metadata only
6. **ha_config_delete_dashboard(dashboard_id)** - Delete dashboard permanently

## Testing Strategy

### Unit Tests (Optional)
Add WebSocket client method tests in `tests/unit/client/test_websocket_client.py`

### E2E Tests
Create `tests/src/e2e/workflows/dashboards/test_lifecycle.py`:

```python
"""E2E tests for dashboard management workflow."""

import pytest
from tests.mcp_testbed import MCPTestBed


class TestDashboardLifecycle:
    """Test dashboard CRUD operations."""

    async def test_basic_dashboard_lifecycle(self, mcp: MCPTestBed):
        """Test create, read, update, delete dashboard."""
        # Create dashboard
        create_result = await mcp.call_tool_success(
            "ha_config_create_dashboard",
            {
                "url_path": "test-dashboard",
                "title": "Test Dashboard",
                "icon": "mdi:test-tube",
                "initial_config": {
                    "views": [{
                        "title": "Test View",
                        "cards": []
                    }]
                }
            }
        )
        assert create_result["success"] is True

        # List dashboards
        list_result = await mcp.call_tool_success(
            "ha_config_list_dashboards", {}
        )
        assert any(d["url_path"] == "test-dashboard"
                  for d in list_result["dashboards"])

        # Get dashboard
        get_result = await mcp.call_tool_success(
            "ha_config_get_dashboard",
            {"url_path": "test-dashboard"}
        )
        assert get_result["success"] is True
        assert "views" in get_result["config"]

        # Update config
        update_result = await mcp.call_tool_success(
            "ha_config_update_dashboard",
            {
                "url_path": "test-dashboard",
                "config": {
                    "views": [{
                        "title": "Updated View",
                        "cards": [{"type": "markdown", "content": "Test"}]
                    }]
                }
            }
        )
        assert update_result["success"] is True

        # Update metadata
        meta_result = await mcp.call_tool_success(
            "ha_config_update_dashboard_metadata",
            {
                "dashboard_id": "test-dashboard",
                "title": "Updated Test Dashboard"
            }
        )
        assert meta_result["success"] is True

        # Delete dashboard
        delete_result = await mcp.call_tool_success(
            "ha_config_delete_dashboard",
            {"dashboard_id": "test-dashboard"}
        )
        assert delete_result["success"] is True

    async def test_url_path_validation(self, mcp: MCPTestBed):
        """Test that url_path must contain hyphen."""
        result = await mcp.call_tool(
            "ha_config_create_dashboard",
            {
                "url_path": "nodash",  # Invalid - no hyphen
                "title": "Test"
            }
        )
        assert result["success"] is False
        assert "hyphen" in result["error"].lower()
```

## Benefits

### Addresses Issue #67 Concerns

1. **Direct injection** - Dashboards are created/updated via MCP without copy/paste
2. **Performance** - Large configs sent via WebSocket, not chat messages
3. **Partial updates** - Can update specific sections without regenerating entire config
4. **Format validation** - Home Assistant validates YAML/JSON on save
5. **No manual formatting** - Eliminates indentation/formatting errors

### Follows Existing Patterns

- Uses same tool structure as scripts/automations/helpers
- Consistent error handling and response format
- Follows WebSocket client patterns
- Integrates with existing registry system

### AI-Friendly

- Clear documentation and examples in docstrings
- Helpful error messages with suggestions
- Supports both dict and YAML string formats
- Validates inputs before API calls

## Migration Path

1. Implement WebSocket client methods
2. Create tools module with 6 tools
3. Register in tools registry
4. Add E2E tests
5. Update AGENTS.md with dashboard management patterns
6. Update main README.md tool count (20+ → 26+)

## Open Questions

1. Should we support YAML string format for configs, or dict only?
   - **Recommendation:** Support both (like script/automation tools)

2. Should we add a "clone dashboard" helper tool?
   - **Recommendation:** No, can be done by get + create

3. Should we validate Lovelace card types before saving?
   - **Recommendation:** No, let Home Assistant handle validation

4. Should we support dashboard resources (custom cards)?
   - **Recommendation:** Phase 2 - separate resource management tools

## References

- Home Assistant Core: `homeassistant/components/lovelace/`
- Collection Pattern: `homeassistant/helpers/collection.py`
- Lovelace Docs: https://www.home-assistant.io/lovelace/
- Issue #67: https://github.com/homeassistant-ai/ha-mcp/issues/67
