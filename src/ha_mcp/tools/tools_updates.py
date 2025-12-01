"""
Update management tools for Home Assistant MCP server.

This module provides tools for listing available updates, getting release notes,
and retrieving system version information.
"""

import logging
import re
from typing import Any

import httpx

from .helpers import log_tool_usage
from .util_helpers import coerce_bool_param

logger = logging.getLogger(__name__)


def register_update_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant update management tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["update"], "title": "List Available Updates"})
    @log_tool_usage
    async def ha_list_updates(
        include_skipped: bool | str = False,
    ) -> dict[str, Any]:
        """
        List all available updates across the system.

        Returns update entities for:
        - Home Assistant Core
        - Add-ons (Supervisor)
        - Device firmware (ESPHome, Z-Wave, ZHA, Matter, etc.)
        - HACS integrations/plugins/themes
        - Home Assistant Operating System

        IMPORTANT: Before updating, always check release notes with
        ha_get_release_notes() to identify breaking changes that may
        affect your installation.

        Args:
            include_skipped: Include updates that have been skipped (default: False)

        Returns:
            Dictionary containing:
            - updates_available: Total count of available updates
            - updates: List of update entities with version info
            - categories: Updates grouped by category (core, addons, devices, hacs, os)
        """
        # Coerce boolean parameter that may come as string from XML-style calls
        include_skipped_bool = coerce_bool_param(include_skipped, "include_skipped", default=False) or False

        try:
            # Get all entity states
            states = await client.get_states()

            # Filter for update domain entities
            update_entities = [
                s for s in states if s.get("entity_id", "").startswith("update.")
            ]

            available_updates = []
            skipped_updates = []

            for entity in update_entities:
                entity_id = entity.get("entity_id", "")
                state = entity.get("state", "")
                attributes = entity.get("attributes", {})

                # State "on" means update available
                is_available = state == "on"
                is_skipped = attributes.get("skipped_version") is not None

                update_info = {
                    "entity_id": entity_id,
                    "title": attributes.get("title", entity_id),
                    "installed_version": attributes.get("installed_version"),
                    "latest_version": attributes.get("latest_version"),
                    "release_summary": attributes.get("release_summary"),
                    "release_url": attributes.get("release_url"),
                    "can_install": not attributes.get("in_progress", False),
                    "in_progress": attributes.get("in_progress", False),
                    "supports_release_notes": _supports_release_notes(
                        entity_id, attributes
                    ),
                    "skipped_version": attributes.get("skipped_version"),
                    "auto_update": attributes.get("auto_update", False),
                }

                # Categorize the update
                update_info["category"] = _categorize_update(entity_id, attributes)

                if is_skipped:
                    skipped_updates.append(update_info)
                elif is_available:
                    available_updates.append(update_info)

            # Include skipped updates if requested
            all_updates = available_updates.copy()
            if include_skipped_bool:
                all_updates.extend(skipped_updates)

            # Group by category
            categories: dict[str, list[dict[str, Any]]] = {
                "core": [],
                "os": [],
                "supervisor": [],
                "addons": [],
                "hacs": [],
                "devices": [],
                "other": [],
            }

            for update in all_updates:
                category = update.get("category", "other")
                if category in categories:
                    categories[category].append(update)
                else:
                    categories["other"].append(update)

            # Remove empty categories
            categories = {k: v for k, v in categories.items() if v}

            return {
                "success": True,
                "updates_available": len(available_updates),
                "skipped_count": len(skipped_updates),
                "updates": all_updates,
                "categories": categories,
                "include_skipped": include_skipped_bool,
            }

        except Exception as e:
            logger.error(f"Failed to list updates: {e}")
            return {
                "success": False,
                "error": f"Failed to list updates: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify API access permissions",
                ],
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["system", "docs"], "title": "Get Release Notes"})
    @log_tool_usage
    async def ha_get_release_notes(
        entity_id: str,
    ) -> dict[str, Any]:
        """
        Get full release notes for an available update.

        Returns markdown-formatted changelog with breaking changes,
        new features, and bug fixes.

        IMPORTANT: Always review release notes before updating to identify:
        - Breaking changes affecting your integrations
        - Deprecated features you may be using
        - New features relevant to your setup

        For entities without native release notes (like HA OS), this tool
        fetches from GitHub releases automatically with fallback to raw CDN.

        Args:
            entity_id: Update entity ID (e.g., "update.home_assistant_core_update")

        Returns:
            Dictionary containing:
            - entity_id: The requested entity
            - version: Version the release notes are for
            - release_notes: Markdown-formatted changelog
            - source: Where the notes came from ("websocket", "github_api", or "github_raw")
        """
        try:
            # Validate entity_id format
            if not entity_id.startswith("update."):
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": "Invalid entity_id format. Must start with 'update.'",
                }

            # Get entity state to check if it exists and get attributes
            entity_state = await client.get_entity_state(entity_id)
            attributes = entity_state.get("attributes", {})
            latest_version = attributes.get("latest_version", "unknown")

            # Try WebSocket update/release_notes first
            try:
                ws_result = await client.send_websocket_message(
                    {
                        "type": "update/release_notes",
                        "entity_id": entity_id,
                    }
                )

                if ws_result.get("success") and ws_result.get("result"):
                    release_notes = ws_result.get("result")
                    if release_notes:
                        return {
                            "success": True,
                            "entity_id": entity_id,
                            "version": latest_version,
                            "release_notes": release_notes,
                            "source": "websocket",
                            "title": attributes.get("title", entity_id),
                        }

            except Exception as ws_error:
                logger.debug(
                    f"WebSocket release_notes failed for {entity_id}: {ws_error}"
                )

            # Fallback: Try to fetch from GitHub if release_url is available
            release_url = attributes.get("release_url")
            if release_url:
                github_result = await _fetch_github_release_notes(release_url)
                if github_result:
                    return {
                        "success": True,
                        "entity_id": entity_id,
                        "version": latest_version,
                        "release_notes": github_result["notes"],
                        "source": github_result["source"],
                        "release_url": release_url,
                        "title": attributes.get("title", entity_id),
                    }

            # No release notes available
            return {
                "success": True,
                "entity_id": entity_id,
                "version": latest_version,
                "release_notes": None,
                "message": "No release notes available for this update",
                "release_url": release_url,
                "title": attributes.get("title", entity_id),
            }

        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg or "not found" in error_msg.lower():
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": f"Update entity not found: {entity_id}",
                }
            logger.error(f"Failed to get release notes for {entity_id}: {e}")
            return {
                "success": False,
                "entity_id": entity_id,
                "error": f"Failed to get release notes: {str(e)}",
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["system"], "title": "Get System Version"})
    @log_tool_usage
    async def ha_get_system_version() -> dict[str, Any]:
        """
        Get Home Assistant version and basic system information.

        Returns current version, installation type, and system details.
        Useful for:
        - Checking current version before updates
        - Verifying update success
        - Compatibility checks for integrations
        - Debugging system configuration

        Returns:
            Dictionary containing:
            - version: Current Home Assistant Core version
            - location_name: Configured location name
            - config_dir: Configuration directory path
            - timezone: System timezone
            - components_loaded: Number of loaded components
            - unit_system: Metric or imperial
            - internal_url: Internal access URL (if configured)
            - external_url: External access URL (if configured)
        """
        try:
            # Get configuration via REST API
            config = await client.get_config()

            # Extract relevant information
            version_info = {
                "success": True,
                "version": config.get("version"),
                "location_name": config.get("location_name"),
                "config_dir": config.get("config_dir"),
                "timezone": config.get("time_zone"),
                "elevation": config.get("elevation"),
                "latitude": config.get("latitude"),
                "longitude": config.get("longitude"),
                "unit_system": config.get("unit_system", {}).get(
                    "temperature", "unknown"
                ),
                "components_loaded": len(config.get("components", [])),
                "allowlist_external_dirs": config.get("allowlist_external_dirs", []),
                "allowlist_external_urls": config.get("allowlist_external_urls", []),
                "internal_url": config.get("internal_url"),
                "external_url": config.get("external_url"),
                "currency": config.get("currency"),
                "country": config.get("country"),
                "language": config.get("language"),
                "safe_mode": config.get("safe_mode", False),
                "state": config.get("state"),
            }

            # Clean up None values
            version_info = {k: v for k, v in version_info.items() if v is not None}

            return version_info

        except Exception as e:
            logger.error(f"Failed to get system version: {e}")
            return {
                "success": False,
                "error": f"Failed to get system version: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify API access permissions",
                ],
            }


def _supports_release_notes(entity_id: str, attributes: dict[str, Any]) -> bool:
    """
    Determine if an update entity supports fetching release notes.

    Returns True if the entity supports release notes through any method:
    - WebSocket update/release_notes command (native HA support)
    - GitHub API/raw CDN fallback (when release_url is available)

    Most entities will return True as they have either native support or a release_url.
    """
    # Check for supported_features that indicate release notes support
    # Feature flag 1 = install, 2 = specific_version, 4 = progress, 8 = backup
    # 16 = release_notes (0x10)
    supported_features = attributes.get("supported_features", 0)
    has_release_notes_feature = (supported_features & 16) != 0

    # Entity supports release notes if it has either:
    # 1. Native WebSocket support (feature flag)
    # 2. A release_url (can fetch from GitHub)
    return has_release_notes_feature or attributes.get("release_url") is not None


def _categorize_update(entity_id: str, attributes: dict[str, Any]) -> str:
    """Categorize an update entity based on its entity_id and attributes."""
    entity_lower = entity_id.lower()
    # Use 'or ""' to handle both missing keys AND explicit None values
    title_lower = (attributes.get("title") or "").lower()

    # Core update
    if "home_assistant_core" in entity_lower or (
        "core" in entity_lower and "home_assistant" in title_lower
    ):
        return "core"

    # Operating System
    if "operating_system" in entity_lower or "haos" in entity_lower:
        return "os"

    # Supervisor
    if "supervisor" in entity_lower:
        return "supervisor"

    # HACS updates
    if "hacs" in entity_lower:
        return "hacs"

    # Add-ons (typically named update.xxx_update where xxx is addon name)
    # Add-ons usually have "Add-on" in title or specific patterns
    if "add-on" in title_lower or "addon" in title_lower:
        return "addons"

    # Device firmware updates (ESPHome, Z-Wave, Zigbee, etc.)
    device_patterns = ["esphome", "zwave", "zigbee", "zha", "matter", "firmware"]
    if any(
        pattern in entity_lower or pattern in title_lower for pattern in device_patterns
    ):
        return "devices"

    # Default to other
    return "other"


async def _fetch_github_release_notes(release_url: str) -> dict[str, str] | None:
    """
    Fetch release notes from GitHub releases API with fallback to raw CDN.

    Tries multiple sources in order:
    1. GitHub API (best formatting, but rate limited)
    2. GitHub raw content CDN (no rate limits, but may not have release notes)

    Parses GitHub release URLs and fetches the release body from the API.

    Args:
        release_url: URL to a GitHub release page

    Returns:
        Dictionary with 'notes' and 'source' keys, or None if fetch fails
    """
    try:
        # Parse GitHub URL patterns:
        # https://github.com/owner/repo/releases/tag/v1.2.3
        # https://github.com/owner/repo/releases/v1.2.3

        github_pattern = r"https://github\.com/([^/]+)/([^/]+)/releases(?:/tag)?/([^/?#]+)"
        match = re.match(github_pattern, release_url)

        if not match:
            logger.debug(f"Could not parse GitHub URL: {release_url}")
            return None

        owner, repo, tag = match.groups()

        async with httpx.AsyncClient(timeout=15.0) as http_client:
            # Try 1: GitHub API (has release notes in structured format)
            api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"

            response = await http_client.get(
                api_url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "HomeAssistant-MCP-Server",
                },
            )

            if response.status_code == 200:
                release_data = response.json()
                body = release_data.get("body", "")
                if body:
                    return {"notes": str(body), "source": "github_api"}
            elif response.status_code == 403:
                # Check if rate limited
                remaining = response.headers.get("X-RateLimit-Remaining", "0")
                if remaining == "0":
                    logger.warning(
                        f"GitHub API rate limit exceeded for {api_url}, trying raw CDN fallback"
                    )
            else:
                logger.debug(
                    f"GitHub API returned status {response.status_code} for {api_url}"
                )

            # Try 2: GitHub raw content CDN (for markdown files)
            # Common locations: CHANGELOG.md, RELEASES.md, docs/releases/{tag}.md
            raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{tag}"

            changelog_paths = [
                "CHANGELOG.md",
                "RELEASES.md",
                "RELEASE_NOTES.md",
                f"docs/releases/{tag}.md",
                "docs/CHANGELOG.md",
            ]

            for path in changelog_paths:
                raw_url = f"{raw_base}/{path}"
                try:
                    response = await http_client.get(
                        raw_url,
                        headers={"User-Agent": "HomeAssistant-MCP-Server"},
                    )

                    if response.status_code == 200:
                        content = response.text
                        if content and len(content) > 50:  # Basic content validation
                            logger.debug(
                                f"Successfully fetched release notes from raw CDN: {raw_url}"
                            )
                            return {"notes": content, "source": "github_raw"}
                except Exception as raw_error:
                    logger.debug(f"Failed to fetch from {raw_url}: {raw_error}")
                    continue

            logger.debug(
                f"Could not fetch release notes from API or raw CDN for {release_url}"
            )
            return None

    except Exception as e:
        logger.debug(f"Failed to fetch GitHub release notes: {e}")
        return None
