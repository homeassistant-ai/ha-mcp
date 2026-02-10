"""
Update management tools for Home Assistant MCP server.

This module provides tools for listing available updates, getting release notes,
and retrieving system version information.
"""

import asyncio
import logging
import re
from typing import Annotated, Any

import httpx
from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import coerce_bool_param

logger = logging.getLogger(__name__)

_ALERTS_URL = "https://alerts.home-assistant.io/alerts.json"
_ALERT_DETAIL_URL = "https://alerts.home-assistant.io/alerts/{alert_id}.json"


def _parse_version(version_str: str) -> tuple[int, ...] | None:
    """Parse a version string like '2025.11.3' into a comparable tuple.

    Returns None if the string cannot be parsed.
    """
    if not version_str:
        return None
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return None


def _filter_alerts(
    alerts: list[dict[str, Any]],
    current_version: tuple[int, ...] | None,
    target_version: tuple[int, ...] | None,
    installed_domains: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Filter alerts into relevant and other categories.

    An alert is 'relevant' if it affects integrations the user has installed
    AND is within the version range being updated through.

    Returns dict with 'relevant' and 'other' lists.
    """
    relevant: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []

    for alert in alerts:
        ha_info = alert.get("homeassistant") or {}

        # Parse version range from alert
        affected_from = _parse_version(
            ha_info.get("affected_from_version") or ha_info.get("min") or ""
        )
        resolved_in = _parse_version(
            ha_info.get("resolved_in_version") or ha_info.get("max") or ""
        )

        # Skip alerts that only affect versions newer than target
        if affected_from and target_version and affected_from > target_version:
            continue

        # Skip alerts resolved before the current version
        if resolved_in and current_version and resolved_in <= current_version:
            continue

        # Extract integration domains from alert
        alert_domains = {
            i.get("package", "") for i in alert.get("integrations", [])
        } - {""}

        alert_info: dict[str, Any] = {
            "id": alert.get("id"),
            "title": alert.get("title"),
            "created": alert.get("created"),
            "integrations": sorted(alert_domains),
            "alert_url": alert.get("alert_url"),
        }

        if alert_domains & installed_domains:
            alert_info["matched_integrations"] = sorted(
                alert_domains & installed_domains
            )
            relevant.append(alert_info)
        else:
            other.append(alert_info)

    return {"relevant": relevant, "other": other}


async def _fetch_alerts() -> list[dict[str, Any]]:
    """Fetch active alerts from alerts.home-assistant.io."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            response = await http_client.get(
                _ALERTS_URL,
                headers={"User-Agent": "HomeAssistant-MCP-Server"},
            )
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    return data
            logger.debug(f"Alerts API returned status {response.status_code}")
            return []
    except Exception as e:
        logger.debug(f"Failed to fetch alerts: {e}")
        return []


async def _fetch_alert_content(alert_id: str) -> str | None:
    """Fetch detailed content for a specific alert."""
    try:
        url = _ALERT_DETAIL_URL.format(alert_id=alert_id)
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.get(
                url,
                headers={"User-Agent": "HomeAssistant-MCP-Server"},
            )
            if response.status_code == 200:
                data = response.json()
                content = data.get("content")
                return str(content) if content else None
            return None
    except Exception:
        return None


async def _get_installed_integration_domains(client: Any) -> set[str]:
    """Get the set of installed integration domains from config entries."""
    try:
        entries = await client._request("GET", "/config/config_entries/entry")
        if isinstance(entries, list):
            return {e.get("domain", "") for e in entries} - {""}
        return set()
    except Exception as e:
        logger.debug(f"Failed to get integration domains: {e}")
        return set()


def register_update_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant update management tools."""

    async def _list_updates(include_skipped: bool) -> dict[str, Any]:
        """Internal helper to list all update entities."""
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
        if include_skipped:
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
            "include_skipped": include_skipped,
        }

    async def _get_update_details(entity_id: str) -> dict[str, Any]:
        """Internal helper to get details for a specific update entity."""
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
        state = entity_state.get("state", "")

        # Build basic update info
        result: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
            "title": attributes.get("title", entity_id),
            "state": state,
            "update_available": state == "on",
            "installed_version": attributes.get("installed_version"),
            "latest_version": latest_version,
            "release_summary": attributes.get("release_summary"),
            "release_url": attributes.get("release_url"),
            "can_install": not attributes.get("in_progress", False),
            "in_progress": attributes.get("in_progress", False),
            "skipped_version": attributes.get("skipped_version"),
            "auto_update": attributes.get("auto_update", False),
            "category": _categorize_update(entity_id, attributes),
        }

        # Try to fetch release notes
        release_notes = None
        release_notes_source = None

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
                release_notes_source = "websocket"

        except Exception as ws_error:
            logger.debug(
                f"WebSocket release_notes failed for {entity_id}: {ws_error}"
            )

        # Fallback: Try to fetch from GitHub if release_url is available
        if not release_notes:
            release_url = attributes.get("release_url")
            if release_url:
                github_result = await _fetch_github_release_notes(release_url)
                if github_result:
                    release_notes = github_result["notes"]
                    release_notes_source = github_result["source"]

        # Special handling for Home Assistant Core updates
        if not release_notes and "core" in entity_id.lower():
            core_result = await _fetch_core_release_notes(latest_version)
            if core_result:
                release_notes = core_result["notes"]
                release_notes_source = core_result["source"]

        if release_notes:
            result["release_notes"] = release_notes
            result["release_notes_source"] = release_notes_source
        else:
            release_url = attributes.get("release_url")
            if release_url:
                result["release_notes_hint"] = (
                    f"Release notes could not be fetched automatically. "
                    f"View them at: {release_url}"
                )

        return result

    @mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True, "readOnlyHint": True, "tags": ["update"], "title": "Get Updates"})
    @log_tool_usage
    async def ha_get_updates(
        entity_id: Annotated[
            str | None,
            Field(
                description="Update entity ID to get details for (e.g., 'update.home_assistant_core_update'). "
                "If omitted, lists all available updates.",
                default=None,
            ),
        ] = None,
        include_skipped: Annotated[
            bool | str,
            Field(
                description="When listing all updates, include updates that have been skipped (default: False)",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        Get update information - list all updates or get details for a specific one.

        Without an entity_id: Lists all available updates across the system including
        Home Assistant Core, add-ons, device firmware, HACS, and OS updates.

        With an entity_id: Returns detailed information about a specific update including
        version info, category, and release notes (if available).

        EXAMPLES:
        - List all updates: ha_get_updates()
        - List including skipped: ha_get_updates(include_skipped=True)
        - Get specific update: ha_get_updates(entity_id="update.home_assistant_core_update")

        RETURNS (when listing):
        - updates_available: Count of available updates
        - updates: List of update entities with version info
        - categories: Updates grouped by category (core, addons, devices, hacs, os)

        RETURNS (when getting specific update):
        - Update details including installed/latest versions
        - Release notes (fetched from WebSocket API or GitHub)
        - Category and installation status

        TIP: Before updating, use ha_check_breaking_changes() to assess impact.
        It cross-references alerts and release notes with your installed integrations.
        """
        try:
            if entity_id is None:
                # List mode: return all updates
                include_skipped_bool = coerce_bool_param(
                    include_skipped, "include_skipped", default=False
                ) or False
                return await _list_updates(include_skipped_bool)
            else:
                # Get mode: return details for specific update
                return await _get_update_details(entity_id)

        except Exception as e:
            error_msg = str(e)
            if entity_id and ("404" in error_msg or "not found" in error_msg.lower()):
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": f"Update entity not found: {entity_id}",
                    "suggestion": "Use ha_get_updates() without entity_id to see all available updates",
                }
            logger.error(f"Failed to get updates: {e}")
            return {
                "success": False,
                "error": f"Failed to get updates: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify API access permissions",
                ],
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["update"], "title": "Check Breaking Changes"})
    @log_tool_usage
    async def ha_check_breaking_changes(
        version: Annotated[
            str | None,
            Field(
                description="Target HA Core version to check (e.g., '2025.11.0'). "
                "If omitted, uses the latest available Core update version.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Check for breaking changes and alerts before updating Home Assistant Core.

        Gathers release notes, HA alerts, and installed integrations in one call
        to assess update impact. Alerts from alerts.home-assistant.io are
        cross-referenced with your installed integrations to surface only the
        ones that affect your setup.

        WORKFLOW: Call this before update.install to understand what may break.

        RETURNS:
        - current_version / latest_version: Version range being checked
        - release_notes: Full Core release notes (contains breaking changes section)
        - alerts.relevant: Alerts matching integrations you have installed
        - alerts.other: Remaining alerts (not matching your installation)
        - installed_integrations: Your integration domains (for cross-referencing)
        """
        try:
            # Step 1: Find core update entity and determine versions
            current_version = None
            target_version = version
            core_entity_id = None
            core_release_url = None

            states = await client.get_states()
            for state in states:
                eid = state.get("entity_id", "")
                if eid.startswith("update.") and _categorize_update(
                    eid, state.get("attributes", {})
                ) == "core":
                    core_entity_id = eid
                    attrs = state.get("attributes", {})
                    current_version = attrs.get("installed_version")
                    core_release_url = attrs.get("release_url")
                    if not target_version:
                        target_version = attrs.get("latest_version")
                    break

            if not current_version:
                return {
                    "success": False,
                    "error": "Could not determine current Home Assistant Core version",
                    "suggestions": [
                        "Ensure Home Assistant Core update entity exists",
                        "Check that the update integration is loaded",
                    ],
                }

            if not target_version:
                return {
                    "success": True,
                    "current_version": current_version,
                    "message": "No Core update available - already on latest version",
                    "suggestions": [
                        "Specify a version parameter to check a specific version",
                    ],
                }

            # Step 2: Fetch release notes, alerts, and integrations in parallel
            async def _fetch_release_notes() -> tuple[str | None, str | None]:
                """Fetch release notes for the core update."""
                # Try WebSocket first
                if core_entity_id:
                    try:
                        ws_result = await client.send_websocket_message(
                            {
                                "type": "update/release_notes",
                                "entity_id": core_entity_id,
                            }
                        )
                        if ws_result.get("success") and ws_result.get("result"):
                            return ws_result["result"], "websocket"
                    except Exception:
                        pass

                # Fallback to GitHub
                core_result = await _fetch_core_release_notes(target_version)
                if core_result:
                    return core_result["notes"], core_result["source"]

                return None, None

            results = await asyncio.gather(
                _fetch_release_notes(),
                _fetch_alerts(),
                _get_installed_integration_domains(client),
                return_exceptions=True,
            )

            release_notes_result = results[0] if not isinstance(results[0], Exception) else (None, None)
            alerts_data = results[1] if not isinstance(results[1], Exception) else []
            installed_domains = results[2] if not isinstance(results[2], Exception) else set()

            release_notes, release_notes_source = release_notes_result

            # Step 3: Filter alerts by version range and installed integrations
            current_v = _parse_version(current_version)
            target_v = _parse_version(target_version)
            filtered = _filter_alerts(
                alerts_data if isinstance(alerts_data, list) else [],
                current_v,
                target_v,
                installed_domains if isinstance(installed_domains, set) else set(),
            )

            # Step 4: Fetch detailed content for relevant alerts
            if filtered["relevant"]:
                async def _enrich_alert(alert: dict[str, Any]) -> None:
                    if alert.get("id"):
                        content = await _fetch_alert_content(alert["id"])
                        if content:
                            alert["content"] = content

                await asyncio.gather(
                    *[_enrich_alert(a) for a in filtered["relevant"]],
                    return_exceptions=True,
                )

            # Step 5: Build response
            sorted_domains = sorted(installed_domains) if isinstance(installed_domains, set) else []

            response: dict[str, Any] = {
                "success": True,
                "current_version": current_version,
                "latest_version": target_version,
                "installed_integrations": sorted_domains,
                "alerts": {
                    "relevant": filtered["relevant"],
                    "relevant_count": len(filtered["relevant"]),
                    "other_count": len(filtered["other"]),
                    "total_checked": len(alerts_data) if isinstance(alerts_data, list) else 0,
                },
            }

            if release_notes:
                response["release_notes"] = release_notes
                response["release_notes_source"] = release_notes_source
            elif core_release_url:
                response["release_notes_url"] = core_release_url
                response["release_notes_hint"] = (
                    "Release notes could not be fetched automatically. "
                    f"View them at: {core_release_url}"
                )

            if filtered["relevant"]:
                response["warning"] = (
                    f"{len(filtered['relevant'])} alert(s) affect integrations "
                    "you have installed. Review before updating."
                )

            return response

        except Exception as e:
            logger.error(f"Failed to check breaking changes: {e}")
            return {
                "success": False,
                "error": f"Failed to check breaking changes: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Try ha_get_updates() to verify update information is available",
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


async def _fetch_core_release_notes(version: str) -> dict[str, str] | None:
    """
    Fetch release notes for Home Assistant Core from GitHub releases API.

    Home Assistant Core uses blog URLs for release_url which don't contain
    the actual release notes. This function fetches directly from GitHub
    releases using the version tag.

    Args:
        version: The version string (e.g., "2025.11.3")

    Returns:
        Dictionary with 'notes' and 'source' keys, or None if fetch fails
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            # GitHub API URL for Home Assistant Core releases
            api_url = f"https://api.github.com/repos/home-assistant/core/releases/tags/{version}"

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
                    logger.debug(
                        f"Successfully fetched Core release notes from GitHub for version {version}"
                    )
                    return {"notes": str(body), "source": "github_api"}
            elif response.status_code == 403:
                # Check if rate limited
                remaining = response.headers.get("X-RateLimit-Remaining", "0")
                if remaining == "0":
                    logger.warning(
                        f"GitHub API rate limit exceeded for {api_url}"
                    )
            else:
                logger.debug(
                    f"GitHub API returned status {response.status_code} for Core release {version}"
                )

            return None

    except Exception as e:
        logger.debug(f"Failed to fetch Core release notes from GitHub: {e}")
        return None
