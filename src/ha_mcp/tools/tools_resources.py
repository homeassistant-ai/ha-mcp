"""
Resource hosting tools for Home Assistant MCP server.

This module provides tools for converting inline JavaScript and CSS code into
hosted URLs via a Cloudflare Worker, enabling AI assistants to inject custom
resources into Home Assistant dashboards.

See: https://github.com/homeassistant-ai/ha-mcp/issues/266
"""

import base64
import logging
from typing import Any, Literal

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)

# Cloudflare Worker URL for resource hosting
WORKER_BASE_URL = "https://ha-mcp-resources.workers.dev"

# Maximum URL length (conservative estimate based on Cloudflare limits)
# URL path limit is ~16KB, leaving room for query params
MAX_ENCODED_LENGTH = 16000

# Approximate size limit for the original content (~12KB before base64 encoding)
# Base64 encoding increases size by ~33%
APPROX_MAX_CONTENT_SIZE = 12000


def register_resources_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register resource hosting tools."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["resources", "dashboard"],
            "title": "Get Resource URL",
        }
    )
    @log_tool_usage
    async def ha_get_resource_url(
        content: str,
        resource_type: Literal["module", "js", "css"] = "module",
    ) -> dict[str, Any]:
        """
        Convert inline JavaScript or CSS code to a hosted URL via Cloudflare Worker.

        This tool enables AI assistants to inject custom JavaScript modules, scripts,
        or CSS stylesheets into Home Assistant dashboards without requiring filesystem
        access. The code is base64-encoded and served from a Cloudflare Worker with
        appropriate MIME types and CORS headers.

        **Parameters:**
        - content: The JavaScript or CSS code to host (max ~12KB)
        - resource_type: Type of resource being hosted:
          - "module": ES6 JavaScript module (application/javascript, for import statements)
          - "js": Regular JavaScript (application/javascript)
          - "css": CSS stylesheet (text/css)

        **Returns:**
        - url: The hosted URL that can be used in dashboard configurations
        - size: Size of the original content in bytes
        - encoded_size: Size of the base64-encoded content
        - resource_type: The type of resource

        **Use Cases:**

        **Simple Custom Card Styling:**
        ```python
        result = ha_get_resource_url(
            content='''
                .my-custom-card {
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    border-radius: 16px;
                    padding: 20px;
                }
            ''',
            resource_type="css"
        )
        # Use result["url"] in dashboard resources
        ```

        **Small Utility Script:**
        ```python
        result = ha_get_resource_url(
            content='''
                export function formatTemperature(value, unit = "C") {
                    const temp = parseFloat(value);
                    if (unit === "F") {
                        return `${((temp * 9/5) + 32).toFixed(1)}\\u00B0F`;
                    }
                    return `${temp.toFixed(1)}\\u00B0C`;
                }
            ''',
            resource_type="module"
        )
        # Import in custom card: import { formatTemperature } from 'result["url"]'
        ```

        **Theme Customization:**
        ```python
        result = ha_get_resource_url(
            content='''
                :root {
                    --primary-color: #03a9f4;
                    --accent-color: #ff5722;
                    --primary-background-color: #1a1a2e;
                }
            ''',
            resource_type="css"
        )
        ```

        **Limitations:**
        - Maximum content size: ~12KB (due to URL length limits)
        - Not suitable for complex custom cards (use filesystem approach instead)
        - Content is public (no authentication on the worker)
        - URLs may be long due to base64 encoding

        **Important Notes:**
        - This tool does NOT require Home Assistant connection
        - URLs are deterministic (same content = same URL)
        - The Cloudflare Worker decodes the base64 and serves with correct MIME type
        - CORS headers are set to allow cross-origin loading

        **Related:**
        - For larger files or complex cards, use filesystem access (Issue #194)
        - For dashboard configuration, see ha_update_dashboard_view
        """
        # Validate content is provided
        if not content or not content.strip():
            return {
                "success": False,
                "error": "Content cannot be empty",
                "suggestions": [
                    "Provide JavaScript or CSS code as the content parameter",
                    "Ensure the content is not just whitespace",
                ],
            }

        # Check content size before encoding
        content_bytes = content.encode("utf-8")
        content_size = len(content_bytes)

        if content_size > APPROX_MAX_CONTENT_SIZE:
            return {
                "success": False,
                "error": f"Content too large: {content_size} bytes (max ~{APPROX_MAX_CONTENT_SIZE} bytes)",
                "size": content_size,
                "suggestions": [
                    "Reduce the size of your JavaScript or CSS code",
                    "For larger files, use filesystem access instead (Issue #194)",
                    "Consider minifying the code to reduce size",
                    "Split large code into multiple smaller modules",
                ],
            }

        # Base64 encode the content using URL-safe encoding
        try:
            encoded = base64.urlsafe_b64encode(content_bytes).decode("ascii")
        except Exception as e:
            logger.error(f"Failed to encode content: {e}")
            return {
                "success": False,
                "error": f"Failed to encode content: {str(e)}",
                "suggestions": [
                    "Ensure the content is valid UTF-8 text",
                    "Check for any binary or non-text content",
                ],
            }

        encoded_size = len(encoded)

        # Final check on encoded length
        if encoded_size > MAX_ENCODED_LENGTH:
            return {
                "success": False,
                "error": f"Encoded content too large: {encoded_size} characters (max {MAX_ENCODED_LENGTH})",
                "size": content_size,
                "encoded_size": encoded_size,
                "suggestions": [
                    "Reduce the size of your JavaScript or CSS code",
                    "For larger files, use filesystem access instead (Issue #194)",
                    "Consider minifying the code to reduce size",
                ],
            }

        # Construct the URL
        url = f"{WORKER_BASE_URL}/{encoded}?type={resource_type}"

        logger.info(
            f"Created resource URL: type={resource_type}, size={content_size}, encoded_size={encoded_size}"
        )

        return {
            "success": True,
            "url": url,
            "size": content_size,
            "encoded_size": encoded_size,
            "resource_type": resource_type,
            "worker_base_url": WORKER_BASE_URL,
            "notes": [
                "URL is deterministic - same content produces same URL",
                "Content is served with appropriate MIME type and CORS headers",
                "For ES6 modules, use resource_type='module'",
            ],
        }
