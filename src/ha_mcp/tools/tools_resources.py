"""
Dashboard resource hosting tools for Home Assistant MCP server.

Provides tools for managing dashboard resources (custom cards, CSS, JS):
- Inline resources: Code embedded in URL via Cloudflare Worker
- External resources: URLs to /local/, /hacsfiles/, or external CDNs

See: https://github.com/homeassistant-ai/ha-mcp/issues/266
"""

import base64
import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)

# Cloudflare Worker URL for resource hosting
WORKER_BASE_URL = "https://ha-mcp-resources.rapid-math-bbad.workers.dev"

# Maximum base64-encoded URL path length (tested limit: 32KB)
MAX_ENCODED_LENGTH = 32000

# Maximum content size (~24KB before base64 encoding)
# Base64 encoding increases size by ~33%, so 24KB * 1.33 ≈ 32KB
MAX_CONTENT_SIZE = 24000


def _encode_content(content: str) -> tuple[str, int, int]:
    """Encode content to URL-safe base64. Returns (encoded, content_size, encoded_size)."""
    content_bytes = content.encode("utf-8")
    encoded = base64.urlsafe_b64encode(content_bytes).decode("ascii")
    return encoded, len(content_bytes), len(encoded)


def _decode_inline_url(url: str) -> str | None:
    """Decode an inline resource URL back to content. Returns None if not an inline URL."""
    if WORKER_BASE_URL not in url:
        return None
    try:
        # Extract base64 part: https://worker.dev/{base64}?type=module
        encoded = url.replace(f"{WORKER_BASE_URL}/", "").split("?")[0]
        return base64.urlsafe_b64decode(encoded).decode("utf-8")
    except Exception:
        return None


def _is_inline_url(url: str) -> bool:
    """Check if a URL is an inline resource URL."""
    return WORKER_BASE_URL in url


def register_resources_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register dashboard resource tools."""

    # =========================================================================
    # List Dashboard Resources
    # =========================================================================

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard", "resources"],
            "title": "List Dashboard Resources",
        }
    )
    @log_tool_usage
    async def ha_config_list_dashboard_resources(
        include_content: Annotated[
            bool,
            Field(
                description="Include full decoded content for inline resources. "
                "Default False to save tokens (shows 150-char preview instead)."
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        List all Lovelace dashboard resources (custom cards, themes, CSS/JS).

        Returns all registered resources. For inline resources (created with
        ha_config_set_dashboard_resource(content=...)), shows a preview of the content
        instead of the full encoded URL to save tokens.

        Args:
            include_content: If True, includes full decoded content for inline
                resources in "_content" field. Default False (150-char preview only).

        Resource types:
        - module: ES6 JavaScript modules (modern custom cards)
        - js: Legacy JavaScript files
        - css: CSS stylesheets

        Each resource has a unique ID for update/delete operations.

        EXAMPLES:
        - List all resources: ha_config_list_dashboard_resources()
        - List with full content: ha_config_list_dashboard_resources(include_content=True)

        Note: Requires advanced mode to be enabled in Home Assistant for resource
        management through the UI, but API access works regardless.
        """
        try:
            result = await client.send_websocket_message({"type": "lovelace/resources"})

            # Handle WebSocket response format
            if isinstance(result, dict) and "result" in result:
                resources = result["result"]
            elif isinstance(result, list):
                resources = result
            else:
                resources = []

            # Process resources - decode inline URLs for preview
            processed = []
            for resource in resources:
                res = dict(resource)
                url = res.get("url", "")

                if _is_inline_url(url):
                    # Decode inline content
                    content = _decode_inline_url(url)
                    if content:
                        res["_inline"] = True
                        res["_size"] = len(content)

                        if include_content:
                            # Include full content when requested
                            res["_content"] = content
                        else:
                            # Show preview (first 150 chars) to save tokens
                            preview = content[:150]
                            if len(content) > 150:
                                preview += "..."
                            res["_preview"] = preview

                        # Replace URL with placeholder to save tokens
                        res["url"] = "[inline]"

                processed.append(res)

            # Categorize resources by type
            categorized: dict[str, list[Any]] = {"module": [], "js": [], "css": []}
            inline_count = 0
            for res in processed:
                res_type = res.get("type", "unknown")
                if res_type in categorized:
                    categorized[res_type].append(res)
                if res.get("_inline"):
                    inline_count += 1

            return {
                "success": True,
                "action": "list",
                "resources": processed,
                "count": len(processed),
                "inline_count": inline_count,
                "by_type": {
                    "module": len(categorized["module"]),
                    "js": len(categorized["js"]),
                    "css": len(categorized["css"]),
                },
            }
        except Exception as e:
            logger.error(f"Error listing dashboard resources: {e}")
            return {
                "success": False,
                "action": "list",
                "error": str(e),
                "suggestions": [
                    "Ensure Home Assistant is running and accessible",
                    "Check that you have admin permissions",
                ],
            }

    # =========================================================================
    # Set Dashboard Resource (upsert - supports both inline code and external URLs)
    # =========================================================================

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["dashboard", "resources"],
            "title": "Set Dashboard Resource",
        }
    )
    @log_tool_usage
    async def ha_config_set_dashboard_resource(
        content: Annotated[
            str | None,
            Field(
                description="JavaScript or CSS code to host inline (max ~24KB). "
                "The code is embedded in the URL via Cloudflare Worker - no file storage needed. "
                "Mutually exclusive with url. Supports 'module' and 'css' types only."
            ),
        ] = None,
        url: Annotated[
            str | None,
            Field(
                description="URL of the resource. Can be: "
                "/local/file.js (www/ directory), "
                "/hacsfiles/component/file.js (HACS), "
                "https://cdn.example.com/card.js (external). "
                "Mutually exclusive with content."
            ),
        ] = None,
        resource_type: Annotated[
            Literal["module", "js", "css"],
            Field(
                description="Resource type: 'module' for ES6 modules (modern cards, default), "
                "'js' for legacy JavaScript (url mode only), 'css' for stylesheets"
            ),
        ] = "module",
        resource_id: Annotated[
            str | None,
            Field(
                description="Resource ID to update. If omitted, creates a new resource. "
                "Get IDs from ha_config_list_dashboard_resources()"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a dashboard resource (inline code or external URL).

        Provide exactly one of:
        - content: Inline JavaScript or CSS code (embedded in URL, no file storage needed)
        - url: External resource URL (/local/, /hacsfiles/, or https://...)

        INLINE MODE (content=):
        - Custom card code written inline
        - CSS styling for dashboards
        - Small utility modules (<24KB)
        - URLs are deterministic (same content = same URL)
        - Supports 'module' and 'css' types only (not 'js')

        URL MODE (url=):
        - Files in /config/www/ directory (/local/...)
        - HACS-installed cards (/hacsfiles/...)
        - External CDN resources (https://...)
        - Supports all types: 'module', 'js', 'css'

        RESOURCE TYPES:
        - module: ES6 JavaScript modules (recommended for custom cards)
        - js: Legacy JavaScript files (older custom cards, url mode only)
        - css: CSS stylesheets (themes, global styles)

        EXAMPLES:

        Inline custom card:
        ha_config_set_dashboard_resource(
            content=\"\"\"
            class MyCard extends HTMLElement {
              setConfig(config) { this.config = config; }
              set hass(hass) {
                this.innerHTML = `<ha-card>Hello ${hass.states[this.config.entity]?.state}</ha-card>`;
              }
            }
            customElements.define('my-card', MyCard);
            \"\"\",
            resource_type="module"
        )

        Add custom card from www/ directory:
        ha_config_set_dashboard_resource(
            url="/local/my-custom-card.js",
            resource_type="module"
        )

        Add HACS card (after installing via ha_hacs_download):
        ha_config_set_dashboard_resource(
            url="/hacsfiles/lovelace-mushroom/mushroom.js",
            resource_type="module"
        )

        Update existing resource:
        ha_config_set_dashboard_resource(
            url="/local/my-card-v2.js",
            resource_type="module",
            resource_id="abc123"
        )

        Note: After adding a resource, clear browser cache or hard refresh
        (Ctrl+Shift+R) to load changes.
        """
        # Validate: exactly one of content or url must be provided
        if content is not None and url is not None:
            return {
                "success": False,
                "error": "Provide either 'content' (inline code) or 'url' (external), not both",
                "suggestions": [
                    "Use content= for inline JavaScript/CSS code",
                    "Use url= for /local/, /hacsfiles/, or https:// resources",
                ],
            }

        if content is None and url is None:
            return {
                "success": False,
                "error": "Either 'content' (inline code) or 'url' (external) is required",
                "suggestions": [
                    "Use content= for inline JavaScript/CSS code",
                    "Use url= for /local/, /hacsfiles/, or https:// resources",
                ],
            }

        # ---- Inline content mode ----
        if content is not None:
            if not content.strip():
                return {
                    "success": False,
                    "error": "Content cannot be empty",
                }

            if resource_type == "js":
                return {
                    "success": False,
                    "error": "Inline content does not support resource_type='js'",
                    "suggestions": [
                        "Use resource_type='module' for ES6 JavaScript (recommended)",
                        "Use url= mode with resource_type='js' for legacy files",
                    ],
                }

            content_bytes = content.encode("utf-8")
            content_size = len(content_bytes)

            if content_size > MAX_CONTENT_SIZE:
                return {
                    "success": False,
                    "error": f"Content too large: {content_size:,} bytes (max {MAX_CONTENT_SIZE:,})",
                    "size": content_size,
                    "suggestions": [
                        "Minify the code to reduce size",
                        "Split into multiple smaller modules",
                        "Use url= with a /local/ path for larger files",
                    ],
                }

            encoded, _, encoded_size = _encode_content(content)

            if encoded_size > MAX_ENCODED_LENGTH:
                return {
                    "success": False,
                    "error": f"Encoded content too large: {encoded_size:,} chars (max {MAX_ENCODED_LENGTH:,})",
                    "size": content_size,
                }

            resource_url = f"{WORKER_BASE_URL}/{encoded}?type={resource_type}"

            try:
                if resource_id:
                    result = await client.send_websocket_message(
                        {
                            "type": "lovelace/resources/update",
                            "resource_id": resource_id,
                            "url": resource_url,
                            "res_type": resource_type,
                        }
                    )
                    action = "updated"
                else:
                    result = await client.send_websocket_message(
                        {
                            "type": "lovelace/resources/create",
                            "url": resource_url,
                            "res_type": resource_type,
                        }
                    )
                    action = "created"

                if isinstance(result, dict) and not result.get("success", True):
                    error_msg = result.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    return {
                        "success": False,
                        "action": action,
                        "error": str(error_msg),
                    }

                resource_info = result.get("result") if isinstance(result, dict) else result
                new_resource_id = resource_id
                if isinstance(resource_info, dict):
                    new_resource_id = resource_info.get("id", resource_id)

                logger.info(
                    f"Inline dashboard resource {action}: id={new_resource_id}, "
                    f"type={resource_type}, size={content_size}"
                )

                return {
                    "success": True,
                    "action": action,
                    "resource_id": new_resource_id,
                    "resource_type": resource_type,
                    "size": content_size,
                    "note": "Clear browser cache or hard refresh to load changes",
                }
            except Exception as e:
                logger.error(f"Error setting inline dashboard resource: {e}")
                return {
                    "success": False,
                    "action": "update" if resource_id else "create",
                    "error": str(e),
                    "suggestions": [
                        "Ensure Home Assistant is running and accessible",
                        "Check that you have admin permissions",
                    ],
                }

        # ---- External URL mode ----
        try:
            if resource_id:
                result = await client.send_websocket_message(
                    {
                        "type": "lovelace/resources/update",
                        "resource_id": resource_id,
                        "url": url,
                        "res_type": resource_type,
                    }
                )
                action = "updated"
            else:
                result = await client.send_websocket_message(
                    {
                        "type": "lovelace/resources/create",
                        "url": url,
                        "res_type": resource_type,
                    }
                )
                action = "created"

            if isinstance(result, dict) and not result.get("success", True):
                error_msg = result.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))

                error_str = str(error_msg).lower()
                if "already exists" in error_str or "duplicate" in error_str:
                    return {
                        "success": False,
                        "action": action,
                        "url": url,
                        "error": "Resource with this URL already exists",
                        "suggestions": [
                            "Use ha_config_list_dashboard_resources() to find existing resource",
                            "Provide resource_id to update the existing resource",
                        ],
                    }

                return {
                    "success": False,
                    "action": action,
                    "url": url,
                    "error": str(error_msg),
                }

            resource_info = result.get("result") if isinstance(result, dict) else result
            new_resource_id = resource_id
            if isinstance(resource_info, dict):
                new_resource_id = resource_info.get("id", resource_id)

            logger.info(
                f"Dashboard resource {action}: id={new_resource_id}, "
                f"type={resource_type}, url={url}"
            )

            return {
                "success": True,
                "action": action,
                "resource_id": new_resource_id,
                "resource_type": resource_type,
                "url": url,
                "note": "Clear browser cache or hard refresh to load changes",
            }
        except Exception as e:
            logger.error(f"Error setting dashboard resource: {e}")
            return {
                "success": False,
                "action": "update" if resource_id else "create",
                "url": url,
                "error": str(e),
                "suggestions": [
                    "Ensure Home Assistant is running and accessible",
                    "Check that you have admin permissions",
                    "Verify the URL is correctly formatted",
                ],
            }

    # =========================================================================
    # Delete Dashboard Resource
    # =========================================================================

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["dashboard", "resources"],
            "title": "Delete Dashboard Resource",
        }
    )
    @log_tool_usage
    async def ha_config_delete_dashboard_resource(
        resource_id: Annotated[
            str,
            Field(
                description="Resource ID to delete. Get from ha_config_list_dashboard_resources()"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Delete a dashboard resource.

        Removes a resource from Home Assistant. The resource will no longer
        be loaded on dashboards. This operation is idempotent - deleting
        a non-existent resource will succeed.

        WARNING: Deleting a resource used by custom cards in your dashboards
        will cause those cards to fail to load.

        EXAMPLES:
        ha_config_delete_dashboard_resource(resource_id="abc123")

        Note: Use ha_config_list_dashboard_resources() to find resource IDs
        before deleting. Ensure no dashboards depend on the resource.
        """
        try:
            result = await client.send_websocket_message(
                {
                    "type": "lovelace/resources/delete",
                    "resource_id": resource_id,
                }
            )

            # Check for errors
            if isinstance(result, dict) and not result.get("success", True):
                error_msg = result.get("error", {})
                if isinstance(error_msg, dict):
                    error_str = error_msg.get("message", str(error_msg))
                else:
                    error_str = str(error_msg)

                # If "not found", treat as success (idempotent)
                if "not found" in error_str.lower() or "unable to find" in error_str.lower():
                    return {
                        "success": True,
                        "action": "delete",
                        "resource_id": resource_id,
                        "message": "Resource already deleted or does not exist",
                    }

                return {
                    "success": False,
                    "action": "delete",
                    "resource_id": resource_id,
                    "error": error_str,
                }

            logger.info(f"Dashboard resource deleted: id={resource_id}")

            return {
                "success": True,
                "action": "delete",
                "resource_id": resource_id,
                "message": "Resource deleted successfully",
            }
        except Exception as e:
            error_str = str(e)
            logger.error(f"Error deleting dashboard resource: {error_str}")

            # If "not found", treat as success (idempotent)
            if "not found" in error_str.lower() or "unable to find" in error_str.lower():
                return {
                    "success": True,
                    "action": "delete",
                    "resource_id": resource_id,
                    "message": "Resource already deleted or does not exist",
                }

            return {
                "success": False,
                "action": "delete",
                "resource_id": resource_id,
                "error": error_str,
                "suggestions": [
                    "Verify resource ID using ha_config_list_dashboard_resources()",
                    "Check that you have admin permissions",
                ],
            }
