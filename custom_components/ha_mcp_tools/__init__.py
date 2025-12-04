"""HA MCP Tools - Custom component for ha-mcp server.

Provides services that are not available through standard Home Assistant APIs,
enabling AI assistants to perform advanced operations like file management.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

_LOGGER = logging.getLogger(__name__)

DOMAIN = "ha_mcp_tools"

# Allowed directories for file operations (relative to config dir)
ALLOWED_READ_DIRS = ["www", "themes", "custom_templates"]
ALLOWED_WRITE_DIRS = ["www", "themes", "custom_templates"]

# Service schemas
SERVICE_LIST_FILES = "list_files"
SERVICE_LIST_FILES_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("pattern"): cv.string,
    }
)


def _is_path_allowed(config_dir: Path, rel_path: str, allowed_dirs: list[str]) -> bool:
    """Check if a path is within allowed directories."""
    # Normalize the path
    normalized = os.path.normpath(rel_path)

    # Check for path traversal attempts
    if normalized.startswith("..") or normalized.startswith("/"):
        return False

    # Check if path starts with an allowed directory
    parts = normalized.split(os.sep)
    if not parts or parts[0] not in allowed_dirs:
        return False

    # Resolve full path and verify it's still under config_dir
    full_path = config_dir / normalized
    try:
        resolved = full_path.resolve()
        config_resolved = config_dir.resolve()
        return str(resolved).startswith(str(config_resolved))
    except (OSError, ValueError):
        return False


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the HA MCP Tools component."""
    config_dir = Path(hass.config.config_dir)

    async def handle_list_files(call: ServiceCall) -> dict[str, Any]:
        """Handle the list_files service call."""
        rel_path = call.data["path"]
        pattern = call.data.get("pattern")

        # Security check
        if not _is_path_allowed(config_dir, rel_path, ALLOWED_READ_DIRS):
            _LOGGER.warning(
                "Attempted to list files in disallowed path: %s", rel_path
            )
            return {
                "success": False,
                "error": f"Path not allowed. Must be in: {', '.join(ALLOWED_READ_DIRS)}",
                "files": [],
            }

        target_dir = config_dir / rel_path

        if not target_dir.exists():
            return {
                "success": False,
                "error": f"Directory does not exist: {rel_path}",
                "files": [],
            }

        if not target_dir.is_dir():
            return {
                "success": False,
                "error": f"Path is not a directory: {rel_path}",
                "files": [],
            }

        try:
            files = []
            for entry in target_dir.iterdir():
                # Apply pattern filter if provided
                if pattern and not fnmatch.fnmatch(entry.name, pattern):
                    continue

                stat = entry.stat()
                files.append(
                    {
                        "name": entry.name,
                        "path": str(entry.relative_to(config_dir)),
                        "is_dir": entry.is_dir(),
                        "size": stat.st_size if entry.is_file() else 0,
                        "modified": stat.st_mtime,
                    }
                )

            # Sort by name
            files.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

            return {
                "success": True,
                "path": rel_path,
                "pattern": pattern,
                "files": files,
                "count": len(files),
            }

        except PermissionError:
            _LOGGER.error("Permission denied accessing: %s", rel_path)
            return {
                "success": False,
                "error": f"Permission denied: {rel_path}",
                "files": [],
            }
        except OSError as err:
            _LOGGER.error("Error listing files in %s: %s", rel_path, err)
            return {
                "success": False,
                "error": str(err),
                "files": [],
            }

    # Register the service
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_FILES,
        handle_list_files,
        schema=SERVICE_LIST_FILES_SCHEMA,
    )

    _LOGGER.info("HA MCP Tools initialized successfully")
    return True
