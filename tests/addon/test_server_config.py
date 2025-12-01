"""Test MCP server configuration and metadata."""

from mcp.types import Icon


class TestServerIcons:
    """Verify server icon configuration meets MCP requirements."""

    def test_server_icons_defined(self):
        """Check that SERVER_ICONS is defined and properly structured."""
        from ha_mcp.server import SERVER_ICONS

        assert SERVER_ICONS is not None, "SERVER_ICONS must be defined"
        assert isinstance(SERVER_ICONS, list), "SERVER_ICONS must be a list"
        assert len(SERVER_ICONS) > 0, "SERVER_ICONS must contain at least one icon"

    def test_icons_are_valid_mcp_icon_types(self):
        """Check that all icons are valid MCP Icon objects."""
        from ha_mcp.server import SERVER_ICONS

        for i, icon in enumerate(SERVER_ICONS):
            assert isinstance(icon, Icon), f"Icon at index {i} must be an mcp.types.Icon"

    def test_icons_have_required_fields(self):
        """Check that all icons have required 'src' field."""
        from ha_mcp.server import SERVER_ICONS

        for i, icon in enumerate(SERVER_ICONS):
            assert icon.src, f"Icon at index {i} must have a non-empty 'src' field"
            # src should be a URL pointing to GitHub raw content
            assert icon.src.startswith("https://"), "Icon src must be an HTTPS URL"

    def test_icons_have_valid_mime_types(self):
        """Check that icons have valid MIME types when specified."""
        from ha_mcp.server import SERVER_ICONS

        valid_image_types = {"image/png", "image/svg+xml", "image/jpeg", "image/webp"}
        for i, icon in enumerate(SERVER_ICONS):
            if icon.mimeType:
                assert icon.mimeType in valid_image_types, (
                    f"Icon at index {i} has invalid mimeType: {icon.mimeType}"
                )

    def test_icons_include_svg_format(self):
        """Check that at least one SVG icon is included for scalability."""
        from ha_mcp.server import SERVER_ICONS

        svg_icons = [icon for icon in SERVER_ICONS if icon.mimeType == "image/svg+xml"]
        assert len(svg_icons) > 0, "Should include at least one SVG icon for scalability"

    def test_icons_include_raster_format(self):
        """Check that at least one raster icon (PNG) is included for compatibility."""
        from ha_mcp.server import SERVER_ICONS

        raster_icons = [icon for icon in SERVER_ICONS if icon.mimeType == "image/png"]
        assert len(raster_icons) > 0, "Should include at least one PNG icon for compatibility"

    def test_icon_urls_point_to_correct_repository(self):
        """Check that icon URLs point to the ha-mcp repository."""
        from ha_mcp.server import SERVER_ICONS

        expected_base = "https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/"
        for i, icon in enumerate(SERVER_ICONS):
            assert icon.src.startswith(expected_base), (
                f"Icon at index {i} should point to homeassistant-ai/ha-mcp repository"
            )
