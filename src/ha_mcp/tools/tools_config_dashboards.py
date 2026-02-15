"""
Configuration management tools for Home Assistant Lovelace dashboards.

This module provides tools for managing dashboard metadata and content.
"""

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
from pydantic import Field

from ..config import get_global_settings
from ..utils.python_sandbox import (
    PythonSandboxError,
    safe_execute,
)
from .helpers import log_tool_usage
from .util_helpers import parse_json_param, validate_guide_response

logger = logging.getLogger(__name__)

# Try to import jq - it's not available on Windows ARM64
try:
    import jq  # noqa: F401 - Used to check availability, re-imported in function

    JQ_AVAILABLE = True
except ImportError:
    JQ_AVAILABLE = False
    logger.warning(
        "jq library not available - jq_transform features will be disabled. "
        "This is expected on Windows ARM64 where jq cannot be compiled."
    )

# Error message when jq_transform is used without jq available
_JQ_UNAVAILABLE_ERROR = (
    "jq_transform is not available - jq library could not be imported. "
    "This is a known limitation on Windows ARM64 where jq cannot be compiled. "
    "Please use the 'config' parameter for full config replacement instead, "
    "or use ha-mcp on Windows x64, Linux, or macOS where jq is supported."
)

# Card documentation base URL
CARD_DOCS_BASE_URL = (
    "https://raw.githubusercontent.com/home-assistant/home-assistant.io/"
    "refs/heads/current/source/_dashboards"
)


def _get_resources_dir() -> Path:
    """Get resources directory path, works for both dev and installed package."""
    # Try to find resources directory relative to this file
    resources_dir = Path(__file__).parent.parent / "resources"
    if resources_dir.exists():
        return resources_dir

    # Fallback: try to find in package data (for installed packages)
    try:
        import importlib.resources as pkg_resources

        # For Python 3.9+
        if hasattr(pkg_resources, "files"):
            resources_dir = pkg_resources.files("ha_mcp") / "resources"
            if hasattr(resources_dir, "__fspath__"):
                return Path(str(resources_dir))
    except (ImportError, AttributeError):
        # If importlib.resources or its attributes are unavailable, fall back to relative path
        pass

    # Last resort: return the relative path and let it fail with clear error
    return resources_dir


def _compute_config_hash(config: dict[str, Any]) -> str:
    """Compute a stable hash of dashboard config for optimistic locking."""
    # Use sorted keys for deterministic serialization
    config_str = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(config_str.encode()).hexdigest()[:16]


async def _verify_config_unchanged(
    client: Any,
    url_path: str,
    original_hash: str,
) -> dict[str, Any]:
    """
    Verify dashboard config hasn't changed since original read.

    Returns dict with:
    - success: bool (True if config unchanged)
    - error: str (if config changed)
    - suggestions: list[str] (if config changed)
    """
    # Re-fetch current config
    get_data: dict[str, Any] = {"type": "lovelace/config"}
    if url_path:
        get_data["url_path"] = url_path

    result = await client.send_websocket_message(get_data)
    current_config = (
        result.get("result", result) if isinstance(result, dict) else result
    )

    if not isinstance(current_config, dict):
        return {"success": True}  # Can't verify, proceed anyway

    current_hash = _compute_config_hash(current_config)

    if current_hash != original_hash:
        return {
            "success": False,
            "error": "Dashboard modified since last read (conflict)",
            "suggestions": [
                "Re-read dashboard with ha_config_get_dashboard",
                "Then retry the operation with fresh data",
            ],
        }

    return {"success": True}


def _apply_jq_transform(
    config: dict[str, Any], expression: str
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Apply a jq transformation to dashboard config.

    Returns:
        tuple: (transformed_config, error_message)
        - On success: (dict, None)
        - On failure: (None, error_string)
    """
    # Check if jq is available
    if not JQ_AVAILABLE:
        return None, _JQ_UNAVAILABLE_ERROR

    import jq

    try:
        # Compile and validate the jq expression
        program = jq.compile(expression)
    except ValueError as e:
        return None, f"Invalid jq expression: {e}"

    try:
        # Execute the transformation
        result = program.input_value(config).first()
    except StopIteration:
        return None, "jq expression produced no output"
    except Exception as e:
        return None, f"jq transformation error: {e}"

    # Validate result is still a valid dashboard structure
    if not isinstance(result, dict):
        return None, f"jq result must be a dict, got {type(result).__name__}"

    if "views" not in result and "strategy" not in result:
        return None, "jq result missing required 'views' or 'strategy' key"

    return result, None


def _find_cards_in_config(
    config: dict[str, Any],
    entity_id: str | None = None,
    card_type: str | None = None,
    heading: str | None = None,
) -> list[dict[str, Any]]:
    """
    Find cards in a dashboard config matching the search criteria.

    Returns a list of matches with location info and card config.
    """
    matches: list[dict[str, Any]] = []

    if "strategy" in config:
        return []  # Strategy dashboards don't have explicit cards

    views = config.get("views", [])
    for view_idx, view in enumerate(views):
        if not isinstance(view, dict):
            continue

        view_type = view.get("type", "masonry")

        if view_type == "sections":
            # Sections-based view
            sections = view.get("sections", [])
            for section_idx, section in enumerate(sections):
                if not isinstance(section, dict):
                    continue
                cards = section.get("cards", [])
                for card_idx, card in enumerate(cards):
                    if not isinstance(card, dict):
                        continue
                    if _card_matches(card, entity_id, card_type, heading):
                        matches.append(
                            {
                                "view_index": view_idx,
                                "section_index": section_idx,
                                "card_index": card_idx,
                                "jq_path": f".views[{view_idx}].sections[{section_idx}].cards[{card_idx}]",
                                "card_type": card.get("type"),
                                "card_config": card,
                            }
                        )
        else:
            # Flat view (masonry, panel, sidebar)
            cards = view.get("cards", [])
            for card_idx, card in enumerate(cards):
                if not isinstance(card, dict):
                    continue
                if _card_matches(card, entity_id, card_type, heading):
                    matches.append(
                        {
                            "view_index": view_idx,
                            "section_index": None,
                            "card_index": card_idx,
                            "jq_path": f".views[{view_idx}].cards[{card_idx}]",
                            "card_type": card.get("type"),
                            "card_config": card,
                        }
                    )

    return matches


def _card_matches(
    card: dict[str, Any],
    entity_id: str | None,
    card_type: str | None,
    heading: str | None,
) -> bool:
    """Check if a card matches the search criteria."""
    # Type filter
    if card_type is not None:
        if card.get("type") != card_type:
            return False

    # Entity filter (supports partial matching with *)
    if entity_id is not None:
        card_entity = card.get("entity", "")
        # Also check entities list for cards that have multiple entities
        card_entities = card.get("entities", [])
        if isinstance(card_entities, list):
            all_entities = [card_entity] + [
                e.get("entity", e) if isinstance(e, dict) else e for e in card_entities
            ]
        else:
            all_entities = [card_entity]

        # Support wildcard matching
        if "*" in entity_id:
            pattern = entity_id.replace(".", r"\.").replace("*", ".*")
            if not any(re.match(pattern, e) for e in all_entities if e):
                return False
        else:
            if entity_id not in all_entities:
                return False

    # Heading filter (for heading cards or section titles)
    if heading is not None:
        card_heading = card.get("heading", card.get("title", ""))
        # Case-insensitive partial match
        if heading.lower() not in card_heading.lower():
            return False

    return True


def register_config_dashboard_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant dashboard configuration tools."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard"],
            "title": "Get Dashboard",
        }
    )
    @log_tool_usage
    async def ha_config_get_dashboard(
        url_path: Annotated[
            str | None,
            Field(
                description="Dashboard URL path (e.g., 'lovelace-home'). "
                "Use 'default' for default dashboard. "
                "If omitted with list_only=True, lists all dashboards."
            ),
        ] = None,
        list_only: Annotated[
            bool,
            Field(
                description="If True, list all dashboards instead of getting config. "
                "When True, url_path is ignored.",
            ),
        ] = False,
        force_reload: Annotated[
            bool, Field(description="Force reload from storage (bypass cache)")
        ] = False,
    ) -> dict[str, Any]:
        """
        Get dashboard info - list all dashboards or get config for a specific one.

        Without url_path (or with list_only=True): Lists all storage-mode dashboards
        with metadata including url_path, title, icon, admin requirements.

        With url_path: Returns the full Lovelace dashboard configuration
        including all views and cards.

        EXAMPLES:
        - List all dashboards: ha_config_get_dashboard(list_only=True)
        - Get default dashboard: ha_config_get_dashboard(url_path="default")
        - Get custom dashboard: ha_config_get_dashboard(url_path="lovelace-mobile")
        - Force reload: ha_config_get_dashboard(url_path="lovelace-home", force_reload=True)

        Note: YAML-mode dashboards (defined in configuration.yaml) are not included in list.
        """
        try:
            # List mode
            if list_only:
                result = await client.send_websocket_message(
                    {"type": "lovelace/dashboards/list"}
                )
                if isinstance(result, dict) and "result" in result:
                    dashboards = result["result"]
                elif isinstance(result, list):
                    dashboards = result
                else:
                    dashboards = []

                return {
                    "success": True,
                    "action": "list",
                    "dashboards": dashboards,
                    "count": len(dashboards),
                }

            # Get mode - build WebSocket message
            data: dict[str, Any] = {"type": "lovelace/config", "force": force_reload}
            # Handle "default" as special value for default dashboard
            if url_path and url_path != "default":
                data["url_path"] = url_path

            response = await client.send_websocket_message(data)

            # Check if request failed
            if isinstance(response, dict) and not response.get("success", True):
                error_msg = response.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                return {
                    "success": False,
                    "action": "get",
                    "url_path": url_path,
                    "error": str(error_msg),
                    "suggestions": [
                        "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                        "Check if you have permission to access this dashboard",
                        "Use url_path='default' for default dashboard",
                    ],
                }

            # Extract config from WebSocket response
            config = response.get("result") if isinstance(response, dict) else response

            # Compute hash for optimistic locking in subsequent operations
            config_hash = (
                _compute_config_hash(config) if isinstance(config, dict) else None
            )

            # Calculate config size for progressive disclosure hint
            config_size = len(json.dumps(config)) if isinstance(config, dict) else 0

            result: dict[str, Any] = {
                "success": True,
                "action": "get",
                "url_path": url_path,
                "config": config,
                "config_hash": config_hash,
                "config_size_bytes": config_size,
            }

            # Add hint for large configs (progressive disclosure) - 10KB ≈ 2-3k tokens
            if config_size >= 10000:
                result["hint"] = (
                    f"Large config ({config_size:,} bytes). For edits, use "
                    "ha_dashboard_find_card() + ha_config_set_dashboard(jq_transform=...) "
                    "instead of full config replacement."
                )

            return result
        except Exception as e:
            logger.error(f"Error getting dashboard: {e}")
            return {
                "success": False,
                "action": "get" if not list_only else "list",
                "url_path": url_path,
                "error": str(e),
                "suggestions": [
                    "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                    "Check if you have permission to access this dashboard",
                    "Use url_path='default' for default dashboard",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["dashboard"],
            "title": "Create or Update Dashboard",
        }
    )
    @log_tool_usage
    async def ha_config_set_dashboard(
        url_path: str,
        guide_response: Annotated[
            str | dict[str, Any],
            Field(
                description="REQUIRED: Output from ha_get_tool_guide('dashboard')"
            ),
        ],
        config: str | dict[str, Any] | None = None,
        jq_transform: str | None = None,
        python_transform: str | None = None,
        config_hash: str | None = None,
        title: str | None = None,
        icon: str | None = None,
        require_admin: bool = False,
        show_in_sidebar: bool = True,
    ) -> dict[str, Any]:
        """Create or update a dashboard. url_path must contain a hyphen.

        The guide_response parameter enforces that ha_get_tool_guide('dashboard')
        was called first. The guide contains mode selection guidance, transform examples,
        index shifting warnings, and modern best practices.
        Modes: config (full replacement/new), python_transform (recommended for edits),
        jq_transform (legacy, needs jq binary). Only one mode at a time.
        Use config_hash from ha_config_get_dashboard for transforms (optimistic locking).
        Also: ha_get_dashboard_guide() for structure, ha_get_card_types() for card reference."""
        try:
            # Validate guide_response - enforces ha_get_tool_guide() was called first
            try:
                validate_guide_response(guide_response, "dashboard")
            except ValueError as e:
                return {
                    "success": False,
                    "error": str(e),
                    "suggestion": "Call ha_get_tool_guide('dashboard') first and pass its output as guide_response.",
                }

            # Handle "default" as alias for the default dashboard
            # (matches ha_config_get_dashboard behavior)
            if url_path == "default":
                url_path = "lovelace"

            # Validate url_path contains hyphen for new dashboards
            # The built-in "lovelace" dashboard is exempt since it already exists
            if "-" not in url_path and url_path != "lovelace":
                return {
                    "success": False,
                    "action": "set",
                    "error": "url_path must contain a hyphen (-)",
                    "suggestions": [
                        f"Try '{url_path.replace('_', '-')}' instead",
                        "Use format like 'my-dashboard' or 'mobile-view'",
                        "Use 'lovelace' or 'default' to edit the default dashboard",
                    ],
                }

            # Validate mutual exclusivity of config, jq_transform, and python_transform
            transforms_provided = sum(
                [
                    config is not None,
                    jq_transform is not None,
                    python_transform is not None,
                ]
            )

            if transforms_provided > 1:
                return {
                    "success": False,
                    "action": "set",
                    "error": "Cannot use multiple transform methods simultaneously",
                    "suggestions": [
                        "Use only ONE of: config, jq_transform, or python_transform",
                        "config: Full replacement",
                        "jq_transform: jq-based edits (requires jq installation)",
                        "python_transform: Python-based edits (recommended, works everywhere)",
                    ],
                }

            # Handle python_transform mode
            if python_transform is not None:
                # config_hash is REQUIRED
                if config_hash is None:
                    return {
                        "success": False,
                        "action": "python_transform",
                        "url_path": url_path,
                        "error": "config_hash is required for python_transform",
                        "suggestions": [
                            "Call ha_config_get_dashboard() first",
                            "Use the config_hash from that response",
                        ],
                    }

                # Fetch current dashboard config
                get_data: dict[str, Any] = {"type": "lovelace/config", "force": True}
                if url_path:
                    get_data["url_path"] = url_path

                response = await client.send_websocket_message(get_data)

                if isinstance(response, dict) and not response.get("success", True):
                    error_msg = response.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    return {
                        "success": False,
                        "action": "python_transform",
                        "url_path": url_path,
                        "error": f"Dashboard not found or inaccessible: {error_msg}",
                        "suggestions": [
                            "python_transform requires an existing dashboard",
                            "Use 'config' parameter to create a new dashboard",
                            "Verify dashboard exists with ha_config_get_dashboard(list_only=True)",
                        ],
                    }

                current_config = (
                    response.get("result") if isinstance(response, dict) else response
                )
                if not isinstance(current_config, dict):
                    return {
                        "success": False,
                        "action": "python_transform",
                        "url_path": url_path,
                        "error": "Current dashboard config is invalid",
                        "suggestions": [
                            "Initialize dashboard with 'config' parameter first"
                        ],
                    }

                # Validate config_hash for optimistic locking
                current_hash = _compute_config_hash(current_config)
                if current_hash != config_hash:
                    return {
                        "success": False,
                        "action": "python_transform",
                        "url_path": url_path,
                        "error": "Dashboard modified since last read (conflict)",
                        "suggestions": [
                            "Call ha_config_get_dashboard() again",
                            "Use the fresh config_hash from that response",
                        ],
                    }

                # Apply Python transformation with validation
                try:
                    transformed_config = safe_execute(python_transform, current_config)
                except PythonSandboxError as e:
                    return {
                        "success": False,
                        "action": "python_transform",
                        "url_path": url_path,
                        "error": str(e),
                        "suggestions": [
                            "Check expression syntax",
                            "Ensure only allowed operations are used",
                            "See tool description for allowed operations",
                            f"Expression: {python_transform[:100]}...",
                        ],
                    }

                # Save transformed config
                save_data: dict[str, Any] = {
                    "type": "lovelace/config/save",
                    "config": transformed_config,
                }
                if url_path:
                    save_data["url_path"] = url_path

                save_result = await client.send_websocket_message(save_data)

                if isinstance(save_result, dict) and not save_result.get(
                    "success", True
                ):
                    error_msg = save_result.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    return {
                        "success": False,
                        "action": "python_transform",
                        "url_path": url_path,
                        "error": f"Failed to save transformed config: {error_msg}",
                        "suggestions": [
                            "Expression may have produced invalid dashboard structure",
                            "Verify config format is valid Lovelace JSON",
                        ],
                    }

                # Compute new hash for potential chaining
                new_config_hash = _compute_config_hash(transformed_config)

                return {
                    "success": True,
                    "action": "python_transform",
                    "url_path": url_path,
                    "config_hash": new_config_hash,
                    "python_expression": python_transform,
                    "message": f"Dashboard {url_path} updated via Python transform",
                }

            # Handle jq_transform mode
            if jq_transform is not None:
                # config_hash is REQUIRED for jq_transform
                if config_hash is None:
                    return {
                        "success": False,
                        "action": "jq_transform",
                        "url_path": url_path,
                        "error": "config_hash is required for jq_transform",
                        "suggestions": [
                            "Call ha_config_get_dashboard() or ha_dashboard_find_card() first",
                            "Use the config_hash from that response",
                        ],
                    }

                # Fetch current dashboard config
                get_data: dict[str, Any] = {"type": "lovelace/config", "force": True}
                if url_path:
                    get_data["url_path"] = url_path

                response = await client.send_websocket_message(get_data)

                if isinstance(response, dict) and not response.get("success", True):
                    error_msg = response.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    return {
                        "success": False,
                        "action": "jq_transform",
                        "url_path": url_path,
                        "error": f"Dashboard not found or inaccessible: {error_msg}",
                        "suggestions": [
                            "jq_transform requires an existing dashboard",
                            "Use 'config' parameter to create a new dashboard",
                            "Verify dashboard exists with ha_config_get_dashboard(list_only=True)",
                        ],
                    }

                current_config = (
                    response.get("result") if isinstance(response, dict) else response
                )
                if not isinstance(current_config, dict):
                    return {
                        "success": False,
                        "action": "jq_transform",
                        "url_path": url_path,
                        "error": "Current dashboard config is invalid",
                        "suggestions": [
                            "Initialize dashboard with 'config' parameter first"
                        ],
                    }

                # Validate config_hash for optimistic locking
                current_hash = _compute_config_hash(current_config)
                if current_hash != config_hash:
                    return {
                        "success": False,
                        "action": "jq_transform",
                        "url_path": url_path,
                        "error": "Dashboard modified since last read (conflict)",
                        "suggestions": [
                            "Call ha_config_get_dashboard() or ha_dashboard_find_card() again",
                            "Use the fresh config_hash from that response",
                            "Indices may have changed - re-locate cards with ha_dashboard_find_card()",
                        ],
                    }

                # Apply jq transformation
                transformed_config, error = _apply_jq_transform(
                    current_config, jq_transform
                )
                if error:
                    return {
                        "success": False,
                        "action": "jq_transform",
                        "url_path": url_path,
                        "error": error,
                        "suggestions": [
                            "Verify jq syntax: https://jqlang.github.io/jq/manual/",
                            "Use ha_dashboard_find_card() to get correct jq_path",
                            "Test expression locally: echo '<config>' | jq '<expression>'",
                        ],
                    }

                # Save transformed config
                save_data: dict[str, Any] = {
                    "type": "lovelace/config/save",
                    "config": transformed_config,
                }
                if url_path:
                    save_data["url_path"] = url_path

                save_result = await client.send_websocket_message(save_data)

                if isinstance(save_result, dict) and not save_result.get(
                    "success", True
                ):
                    error_msg = save_result.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    return {
                        "success": False,
                        "action": "jq_transform",
                        "url_path": url_path,
                        "error": f"Failed to save transformed config: {error_msg}",
                        "suggestions": [
                            "jq expression may have produced invalid dashboard structure",
                            "Verify config format is valid Lovelace JSON",
                        ],
                    }

                # Compute new hash for potential chaining
                # transformed_config is guaranteed to be a dict here (validated above)
                new_config_hash = _compute_config_hash(
                    cast(dict[str, Any], transformed_config)
                )

                return {
                    "success": True,
                    "action": "jq_transform",
                    "url_path": url_path,
                    "config_hash": new_config_hash,
                    "jq_expression": jq_transform,
                    "message": f"Dashboard {url_path} updated via jq transform",
                }

            # Check if dashboard exists
            result = await client.send_websocket_message(
                {"type": "lovelace/dashboards/list"}
            )
            if isinstance(result, dict) and "result" in result:
                existing_dashboards = result["result"]
            elif isinstance(result, list):
                existing_dashboards = result
            else:
                existing_dashboards = []
            dashboard_exists = any(
                d.get("url_path") == url_path for d in existing_dashboards
            )

            # The built-in default dashboard ("lovelace") is always present
            # but isn't listed by lovelace/dashboards/list on fresh installs
            if url_path == "lovelace":
                dashboard_exists = True

            # If dashboard doesn't exist, create it
            dashboard_id = None
            if not dashboard_exists:
                # Use provided title or generate from url_path
                dashboard_title = title or url_path.replace("-", " ").title()

                # Build create message
                create_data: dict[str, Any] = {
                    "type": "lovelace/dashboards/create",
                    "url_path": url_path,
                    "title": dashboard_title,
                    "require_admin": require_admin,
                    "show_in_sidebar": show_in_sidebar,
                }
                if icon:
                    create_data["icon"] = icon
                create_result = await client.send_websocket_message(create_data)

                # Check if dashboard creation was successful
                if isinstance(create_result, dict) and not create_result.get(
                    "success", True
                ):
                    error_msg = create_result.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    return {
                        "success": False,
                        "action": "create",
                        "url_path": url_path,
                        "error": str(error_msg),
                    }

                # Extract dashboard ID from create response
                if isinstance(create_result, dict) and "result" in create_result:
                    dashboard_info = create_result["result"]
                    dashboard_id = dashboard_info.get("id")
                elif isinstance(create_result, dict):
                    dashboard_id = create_result.get("id")
            else:
                # If dashboard already exists, get its ID from the list
                for dashboard in existing_dashboards:
                    if dashboard.get("url_path") == url_path:
                        dashboard_id = dashboard.get("id")
                        break

            # Set config if provided
            config_updated = False
            existing_config_size = 0
            hint = None

            if config is not None:
                parsed_config = parse_json_param(config, "config")
                if parsed_config is None or not isinstance(parsed_config, dict):
                    return {
                        "success": False,
                        "action": "set",
                        "error": "Config parameter must be a dict/object",
                        "provided_type": type(parsed_config).__name__,
                    }

                config_dict = cast(dict[str, Any], parsed_config)

                # For existing dashboards, optionally validate config_hash and warn on large replacement
                if dashboard_exists:
                    # Fetch current config for validation/comparison
                    get_data: dict[str, Any] = {
                        "type": "lovelace/config",
                        "force": True,
                    }
                    if url_path:
                        get_data["url_path"] = url_path
                    current_response = await client.send_websocket_message(get_data)
                    current_config = (
                        current_response.get("result")
                        if isinstance(current_response, dict)
                        else current_response
                    )

                    if isinstance(current_config, dict):
                        existing_config_size = len(json.dumps(current_config))

                        # Optional config_hash validation for full replacement
                        if config_hash is not None:
                            current_hash = _compute_config_hash(current_config)
                            if current_hash != config_hash:
                                return {
                                    "success": False,
                                    "action": "set",
                                    "url_path": url_path,
                                    "error": "Dashboard modified since last read (conflict)",
                                    "suggestions": [
                                        "Call ha_config_get_dashboard() again",
                                        "Use the fresh config_hash, or omit config_hash to force replace",
                                    ],
                                }

                        # Soft warning for large config full replacement (10KB ≈ 2-3k tokens)
                        if existing_config_size >= 10000:
                            hint = (
                                f"Replaced large config ({existing_config_size:,} bytes). "
                                "Consider jq_transform for targeted edits."
                            )

                # Build save config message
                config_save_data: dict[str, Any] = {
                    "type": "lovelace/config/save",
                    "config": config_dict,
                }
                if url_path:
                    config_save_data["url_path"] = url_path
                save_result = await client.send_websocket_message(config_save_data)

                # Check if save failed
                if isinstance(save_result, dict) and not save_result.get(
                    "success", True
                ):
                    error_msg = save_result.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    return {
                        "success": False,
                        "action": "set",
                        "url_path": url_path,
                        "error": f"Failed to save dashboard config: {error_msg}",
                        "suggestions": [
                            "Verify config format is valid Lovelace JSON",
                            "Check that you have admin permissions",
                            "Ensure all entity IDs in config exist",
                        ],
                    }

                config_updated = True

            result_dict: dict[str, Any] = {
                "success": True,
                "action": "create" if not dashboard_exists else "update",
                "url_path": url_path,
                "dashboard_id": dashboard_id,
                "dashboard_created": not dashboard_exists,
                "config_updated": config_updated,
                "message": f"Dashboard {url_path} {'created' if not dashboard_exists else 'updated'} successfully",
            }

            if hint:
                result_dict["hint"] = hint

            return result_dict

        except Exception as e:
            logger.error(f"Error setting dashboard: {e}")
            return {
                "success": False,
                "action": "set",
                "url_path": url_path,
                "error": str(e),
                "suggestions": [
                    "Ensure url_path is unique (not already in use for different dashboard type)",
                    "New dashboards require a hyphenated url_path",
                    "Check that you have admin permissions",
                    "Verify config format is valid Lovelace JSON",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["dashboard"],
            "title": "Update Dashboard Metadata",
        }
    )
    @log_tool_usage
    async def ha_config_update_dashboard_metadata(
        dashboard_id: Annotated[
            str, Field(description="Dashboard ID (typically same as url_path)")
        ],
        title: Annotated[str | None, Field(description="New dashboard title")] = None,
        icon: Annotated[str | None, Field(description="New MDI icon name")] = None,
        require_admin: Annotated[
            bool | None, Field(description="Update admin requirement")
        ] = None,
        show_in_sidebar: Annotated[
            bool | None, Field(description="Update sidebar visibility")
        ] = None,
    ) -> dict[str, Any]:
        """
        Update dashboard metadata (title, icon, permissions) without changing content.

        Updates dashboard properties without modifying the actual configuration
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

        Hide from sidebar:
        ha_config_update_dashboard_metadata(
            dashboard_id="hidden-dashboard",
            show_in_sidebar=False
        )
        """
        if all(x is None for x in [title, icon, require_admin, show_in_sidebar]):
            return {
                "success": False,
                "action": "update_metadata",
                "error": "At least one field must be provided to update",
            }

        try:
            # Build update message
            update_data: dict[str, Any] = {
                "type": "lovelace/dashboards/update",
                "dashboard_id": dashboard_id,
            }
            if title is not None:
                update_data["title"] = title
            if icon is not None:
                update_data["icon"] = icon
            if require_admin is not None:
                update_data["require_admin"] = require_admin
            if show_in_sidebar is not None:
                update_data["show_in_sidebar"] = show_in_sidebar

            result = await client.send_websocket_message(update_data)

            # Check if update failed
            if isinstance(result, dict) and not result.get("success", True):
                error_msg = result.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                return {
                    "success": False,
                    "action": "update_metadata",
                    "dashboard_id": dashboard_id,
                    "error": str(error_msg),
                    "suggestions": [
                        "Verify dashboard ID exists using ha_config_get_dashboard(list_only=True)",
                        "Check that you have admin permissions",
                    ],
                }

            return {
                "success": True,
                "action": "update_metadata",
                "dashboard_id": dashboard_id,
                "updated_fields": {
                    k: v
                    for k, v in {
                        "title": title,
                        "icon": icon,
                        "require_admin": require_admin,
                        "show_in_sidebar": show_in_sidebar,
                    }.items()
                    if v is not None
                },
                "dashboard": result,
            }
        except Exception as e:
            logger.error(f"Error updating dashboard metadata: {e}")
            return {
                "success": False,
                "action": "update_metadata",
                "dashboard_id": dashboard_id,
                "error": str(e),
                "suggestions": [
                    "Verify dashboard ID exists using ha_config_get_dashboard(list_only=True)",
                    "Check that you have admin permissions",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["dashboard"],
            "title": "Delete Dashboard",
        }
    )
    @log_tool_usage
    async def ha_config_delete_dashboard(
        dashboard_id: Annotated[
            str,
            Field(description="Dashboard ID to delete (typically same as url_path)"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a storage-mode dashboard completely.

        WARNING: This permanently deletes the dashboard and all its configuration.
        Cannot be undone. Does not work on YAML-mode dashboards.

        EXAMPLES:
        - Delete dashboard: ha_config_delete_dashboard("mobile-dashboard")

        Note: The default dashboard cannot be deleted via this method.
        """
        try:
            response = await client.send_websocket_message(
                {"type": "lovelace/dashboards/delete", "dashboard_id": dashboard_id}
            )

            # Check response for error indication
            if isinstance(response, dict) and not response.get("success", True):
                error_msg = response.get("error", {})
                if isinstance(error_msg, dict):
                    error_str = error_msg.get("message", str(error_msg))
                else:
                    error_str = str(error_msg)

                logger.error(f"Error deleting dashboard: {error_str}")

                # If the error is "not found" / "doesn't exist", treat as success (idempotent)
                if (
                    "unable to find" in error_str.lower()
                    or "not found" in error_str.lower()
                ):
                    return {
                        "success": True,
                        "action": "delete",
                        "dashboard_id": dashboard_id,
                        "message": "Dashboard already deleted or does not exist",
                    }

                # For other errors, return failure
                return {
                    "success": False,
                    "action": "delete",
                    "dashboard_id": dashboard_id,
                    "error": error_str,
                    "suggestions": [
                        "Verify dashboard exists and is storage-mode",
                        "Check that you have admin permissions",
                        "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                        "Cannot delete YAML-mode or default dashboard",
                    ],
                }

            # Delete successful
            return {
                "success": True,
                "action": "delete",
                "dashboard_id": dashboard_id,
                "message": "Dashboard deleted successfully",
            }
        except Exception as e:
            error_str = str(e)
            logger.error(f"Error deleting dashboard: {error_str}")

            # If the error is "not found" / "doesn't exist", treat as success (idempotent)
            if (
                "unable to find" in error_str.lower()
                or "not found" in error_str.lower()
            ):
                return {
                    "success": True,
                    "action": "delete",
                    "dashboard_id": dashboard_id,
                    "message": "Dashboard already deleted or does not exist",
                }

            # For other errors, return failure
            return {
                "success": False,
                "action": "delete",
                "dashboard_id": dashboard_id,
                "error": error_str,
                "suggestions": [
                    "Verify dashboard exists and is storage-mode",
                    "Check that you have admin permissions",
                    "Use ha_config_get_dashboard(list_only=True) to see available dashboards",
                    "Cannot delete YAML-mode or default dashboard",
                ],
            }

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard", "docs", "guide"],
            "title": "Get Dashboard Guide",
        }
    )
    @log_tool_usage
    async def ha_get_dashboard_guide() -> dict[str, Any]:
        """
        Get comprehensive guide for designing Home Assistant dashboards.

        Covers:
        - Part 1: Dashboard structure, views, sections, navigation
        - Part 2: Built-in cards (tile, grid, button), features, actions
        - Part 3: Custom cards (JavaScript modules, registration)
        - Part 4: CSS styling (themes, card-mod patterns)
        - Part 5: HACS integration (finding/installing community cards)
        - Part 6: Complete examples and workflows
        - Part 7: Visual iteration with Playwright/browser MCP for screenshots

        Use this guide before designing dashboards to understand
        all available options, from built-in cards to custom resources.

        Related tools:
        - ha_create_dashboard_resource: Host inline JS/CSS
        - ha_hacs_search / ha_hacs_download: Community cards

        EXAMPLES:
        - Get full guide: ha_get_dashboard_guide()
        """
        try:
            resources_dir = _get_resources_dir()
            guide_path = resources_dir / "dashboard_guide.md"
            guide_content = guide_path.read_text()
            return {
                "success": True,
                "action": "get_guide",
                "guide": guide_content,
                "format": "markdown",
            }
        except Exception as e:
            logger.error(f"Error reading dashboard guide: {e}")
            return {
                "success": False,
                "action": "get_guide",
                "error": str(e),
                "suggestions": [
                    "Ensure dashboard_guide.md exists in resources directory",
                    f"Attempted path: {resources_dir / 'dashboard_guide.md' if 'resources_dir' in locals() else 'unknown'}",
                ],
            }

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard", "docs"],
            "title": "Get Card Types",
        }
    )
    @log_tool_usage
    async def ha_get_card_types() -> dict[str, Any]:
        """
        Get list of all available Home Assistant dashboard card types.

        Returns all 41 card types that can be used in dashboard configurations.

        EXAMPLES:
        - Get card types: ha_get_card_types()

        Use ha_get_card_documentation(card_type) to get detailed docs for a specific card.
        """
        try:
            resources_dir = _get_resources_dir()
            types_path = resources_dir / "card_types.json"
            card_types_data = json.loads(types_path.read_text())
            return {
                "success": True,
                "action": "get_card_types",
                "card_types": card_types_data["card_types"],
                "total_count": card_types_data["total_count"],
                "documentation_base_url": card_types_data["documentation_base_url"],
            }
        except Exception as e:
            logger.error(f"Error reading card types: {e}")
            return {
                "success": False,
                "action": "get_card_types",
                "error": str(e),
                "suggestions": [
                    "Ensure card_types.json exists in resources directory",
                    f"Attempted path: {resources_dir / 'card_types.json' if 'resources_dir' in locals() else 'unknown'}",
                ],
            }

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard", "docs"],
            "title": "Get Card Documentation",
        }
    )
    @log_tool_usage
    async def ha_get_card_documentation(
        card_type: Annotated[
            str,
            Field(
                description="Card type name (e.g., 'light', 'thermostat', 'entity'). "
                "Use ha_get_card_types() to see all available types."
            ),
        ],
    ) -> dict[str, Any]:
        """
        Fetch detailed documentation for a specific dashboard card type.

        Returns the official Home Assistant documentation for the specified card type
        in markdown format, fetched directly from the Home Assistant documentation repository.

        EXAMPLES:
        - Get light card docs: ha_get_card_documentation("light")
        - Get thermostat card docs: ha_get_card_documentation("thermostat")
        - Get entity card docs: ha_get_card_documentation("entity")

        First use ha_get_card_types() to see all 41 available card types.
        """
        try:
            # Validate card type exists
            resources_dir = _get_resources_dir()
            types_path = resources_dir / "card_types.json"
            card_types_data = json.loads(types_path.read_text())

            if card_type not in card_types_data["card_types"]:
                available = ", ".join(card_types_data["card_types"][:10])
                return {
                    "success": False,
                    "action": "get_card_documentation",
                    "card_type": card_type,
                    "error": f"Unknown card type '{card_type}'",
                    "suggestions": [
                        f"Available types include: {available}...",
                        "Use ha_get_card_types() to see full list of 41 card types",
                    ],
                }

            # Fetch documentation from GitHub
            doc_url = f"{CARD_DOCS_BASE_URL}/{card_type}.markdown"

            async with httpx.AsyncClient(timeout=10.0) as http_client:
                response = await http_client.get(doc_url)
                response.raise_for_status()
                return {
                    "success": True,
                    "action": "get_card_documentation",
                    "card_type": card_type,
                    "documentation": response.text,
                    "format": "markdown",
                    "source_url": doc_url,
                }
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to fetch card docs for {card_type}: {e}")
            return {
                "success": False,
                "action": "get_card_documentation",
                "card_type": card_type,
                "error": f"Failed to fetch documentation (HTTP {e.response.status_code})",
                "source_url": doc_url,
            }
        except Exception as e:
            logger.error(f"Error fetching card docs for {card_type}: {e}")
            return {
                "success": False,
                "action": "get_card_documentation",
                "card_type": card_type,
                "error": str(e),
            }

    # =========================================================================
    # Dashboard Resource Management Tools
    # =========================================================================
    # Resource tools have been moved to tools_resources.py for better organization.
    # Available tools:
    # - ha_config_list_dashboard_resources: List all resources
    # - ha_config_set_inline_dashboard_resource: Create/update inline code resources
    # - ha_config_set_dashboard_resource: Create/update URL-based resources
    # - ha_config_delete_dashboard_resource: Delete resources
    # =========================================================================

    # =========================================================================
    # Card Search Tool (partial update tools - controlled by feature flag)
    # Card add/update/remove replaced by jq_transform in ha_config_set_dashboard
    # =========================================================================

    # Check feature flag for partial update tools (lazy check, default enabled)
    try:
        settings = get_global_settings()
        if not settings.enable_dashboard_partial_tools:
            return  # Skip registering find_card if partial tools disabled
    except Exception:
        pass  # Default: register the tool if settings unavailable

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard", "card"],
            "title": "Find Dashboard Card",
        }
    )
    @log_tool_usage
    async def ha_dashboard_find_card(
        url_path: Annotated[
            str | None,
            Field(
                description="Dashboard URL path, e.g. 'lovelace-home'. Omit for default."
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description="Find cards by entity ID. Supports wildcards, e.g. 'sensor.temperature_*'. "
                "Matches cards with this entity in 'entity' or 'entities' field."
            ),
        ] = None,
        card_type: Annotated[
            str | None,
            Field(description="Find cards by type, e.g. 'tile', 'button', 'heading'."),
        ] = None,
        heading: Annotated[
            str | None,
            Field(
                description="Find cards by heading/title text (case-insensitive partial match). "
                "Useful for finding section headings (type: 'heading')."
            ),
        ] = None,
        include_config: Annotated[
            bool,
            Field(
                description="Include full card configuration in results (increases output size)."
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        Find cards in a dashboard by entity_id, type, or heading text.

        Returns card locations (view_index, section_index, card_index) and jq_path
        for use with ha_config_set_dashboard(jq_transform=...).

        Use this tool BEFORE targeted updates to find exact card positions without
        manually parsing the full dashboard config.

        SEARCH CRITERIA (at least one required):
        - entity_id: Match cards containing this entity (supports wildcards with *)
        - card_type: Match cards of this type (e.g., 'tile', 'button', 'heading')
        - heading: Match cards with this text in heading/title (partial, case-insensitive)

        Multiple criteria are AND-ed together.

        EXAMPLES:

        Find all tile cards:
        ha_dashboard_find_card(url_path="my-dashboard", card_type="tile")

        Find cards for a specific entity:
        ha_dashboard_find_card(url_path="my-dashboard", entity_id="light.living_room")

        Find all temperature sensors (wildcard):
        ha_dashboard_find_card(url_path="my-dashboard", entity_id="sensor.temperature_*")

        Find the "Climate" section heading:
        ha_dashboard_find_card(url_path="my-dashboard", heading="Climate", card_type="heading")

        WORKFLOW EXAMPLE:
        1. find = ha_dashboard_find_card(url_path="my-dash", entity_id="light.bedroom")
        2. # Use jq_path and config_hash from result to update:
        3. ha_config_set_dashboard(
               url_path="my-dash",
               config_hash=find["config_hash"],
               jq_transform=f'{find["matches"][0]["jq_path"]}.icon = "mdi:lamp"'
           )
        """
        try:
            # Validate at least one search criteria
            if entity_id is None and card_type is None and heading is None:
                return {
                    "success": False,
                    "action": "find_card",
                    "error": "At least one search criteria required",
                    "suggestions": [
                        "Provide entity_id, card_type, or heading parameter",
                        "Use entity_id='sensor.*' to find all sensor cards",
                        "Use card_type='heading' to find section headings",
                    ],
                }

            # Fetch dashboard config
            get_data: dict[str, Any] = {"type": "lovelace/config", "force": True}
            if url_path:
                get_data["url_path"] = url_path

            response = await client.send_websocket_message(get_data)

            if isinstance(response, dict) and not response.get("success", True):
                error_msg = response.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                return {
                    "success": False,
                    "action": "find_card",
                    "url_path": url_path,
                    "error": f"Failed to get dashboard: {error_msg}",
                    "suggestions": [
                        "Verify dashboard exists with ha_config_get_dashboard(list_only=True)",
                        "Check HA connection",
                    ],
                }

            config = response.get("result") if isinstance(response, dict) else response
            if not isinstance(config, dict):
                return {
                    "success": False,
                    "action": "find_card",
                    "url_path": url_path,
                    "error": "Dashboard config is empty or invalid",
                    "suggestions": [
                        "Initialize dashboard with ha_config_set_dashboard"
                    ],
                }

            # Check for strategy dashboard
            if "strategy" in config:
                return {
                    "success": False,
                    "action": "find_card",
                    "url_path": url_path,
                    "error": "Strategy dashboards have no explicit cards to search",
                    "suggestions": [
                        "Use 'Take Control' in HA UI to convert to editable",
                        "Or create a non-strategy dashboard",
                    ],
                }

            # Find matching cards
            matches = _find_cards_in_config(config, entity_id, card_type, heading)

            # Optionally strip config to reduce output size
            if not include_config:
                for match in matches:
                    del match["card_config"]

            # Compute config hash for potential follow-up operations
            config_hash = _compute_config_hash(config)

            return {
                "success": True,
                "action": "find_card",
                "url_path": url_path,
                "config_hash": config_hash,
                "search_criteria": {
                    "entity_id": entity_id,
                    "card_type": card_type,
                    "heading": heading,
                },
                "matches": matches,
                "match_count": len(matches),
                "hint": "Use jq_path with ha_config_set_dashboard(jq_transform=...) for targeted updates"
                if matches
                else "No matches found. Try broader search criteria.",
            }

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                f"Error finding card: url_path={url_path}, "
                f"entity_id={entity_id}, card_type={card_type}, heading={heading}, "
                f"error={e}",
                exc_info=True,
            )
            return {
                "success": False,
                "action": "find_card",
                "url_path": url_path,
                "error": str(e) if str(e) else f"{type(e).__name__} (no details)",
                "error_type": type(e).__name__,
                "suggestions": [
                    "Check HA connection",
                    "Verify dashboard with ha_config_get_dashboard(list_only=True)",
                ],
            }
