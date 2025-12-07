"""
Dashboard resource hosting tools for Home Assistant MCP server.

Provides tools for converting inline JavaScript and CSS code into hosted URLs,
enabling AI assistants to inject custom resources into Home Assistant dashboards
without requiring filesystem access.

See: https://github.com/homeassistant-ai/ha-mcp/issues/266
"""

import base64
import logging
from typing import Any, Literal

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)

# Cloudflare Worker URL for resource hosting
WORKER_BASE_URL = "https://ha-mcp-resources.rapid-math-bbad.workers.dev"

# Maximum base64-encoded URL path length (tested limit: 32KB)
MAX_ENCODED_LENGTH = 32000

# Maximum content size (~24KB before base64 encoding)
# Base64 encoding increases size by ~33%, so 24KB * 1.33 ≈ 32KB
MAX_CONTENT_SIZE = 24000


def register_resources_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register dashboard resource hosting tools."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["resources", "dashboard"],
            "title": "Create Dashboard Resource",
        }
    )
    @log_tool_usage
    async def ha_create_dashboard_resource(
        content: str,
        resource_type: Literal["module", "css"] = "module",
    ) -> dict[str, Any]:
        """
        Convert inline JavaScript or CSS to a hosted URL for dashboard use.

        Creates a URL that serves your code with the correct MIME type and CORS
        headers, allowing it to be loaded by Home Assistant dashboards.

        Args:
            content: JavaScript or CSS code to host (max ~24KB)
            resource_type: Type of resource:
                - "module": ES6 JavaScript module (default) - for custom cards
                - "css": Stylesheet - for custom themes/styling

        Returns:
            Dictionary with:
            - url: Hosted URL to use in dashboard configuration
            - size: Original content size in bytes
            - encoded_size: Base64-encoded size

        Example - Custom card styling:
            ha_create_dashboard_resource(
                content=".my-card { background: #1a1a2e; border-radius: 16px; }",
                resource_type="css"
            )

        Example - Small utility module:
            ha_create_dashboard_resource(
                content="export const formatTemp = (v) => `${v.toFixed(1)}°C`;",
                resource_type="module"
            )

        Notes:
            - URLs are deterministic (same content = same URL)
            - Content is not stored, decoded on-the-fly from URL
            - For files >24KB, use filesystem access instead
            - Use ha_get_dashboard_guide for examples and patterns
        """
        # Validate content
        if not content or not content.strip():
            return {
                "success": False,
                "error": "Content cannot be empty",
            }

        content_bytes = content.encode("utf-8")
        content_size = len(content_bytes)

        # Check size limit
        if content_size > MAX_CONTENT_SIZE:
            return {
                "success": False,
                "error": f"Content too large: {content_size:,} bytes (max {MAX_CONTENT_SIZE:,})",
                "size": content_size,
                "suggestions": [
                    "Minify the code to reduce size",
                    "Split into multiple smaller modules",
                    "For larger files, use filesystem access to /config/www/",
                ],
            }

        # Base64 encode using URL-safe encoding
        encoded = base64.urlsafe_b64encode(content_bytes).decode("ascii")
        encoded_size = len(encoded)

        # Final encoded size check
        if encoded_size > MAX_ENCODED_LENGTH:
            return {
                "success": False,
                "error": f"Encoded content too large: {encoded_size:,} chars (max {MAX_ENCODED_LENGTH:,})",
                "size": content_size,
                "encoded_size": encoded_size,
            }

        url = f"{WORKER_BASE_URL}/{encoded}?type={resource_type}"

        logger.info(
            f"Created dashboard resource: type={resource_type}, "
            f"size={content_size}, encoded_size={encoded_size}"
        )

        return {
            "success": True,
            "url": url,
            "size": content_size,
            "encoded_size": encoded_size,
            "resource_type": resource_type,
        }
