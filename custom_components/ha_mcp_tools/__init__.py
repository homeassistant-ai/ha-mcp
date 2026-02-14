"""HA MCP Tools - Custom component for ha-mcp server.

Provides services that are not available through standard Home Assistant APIs,
enabling AI assistants to perform advanced operations like file management.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import config_validation as cv

from .const import ALLOWED_READ_DIRS, ALLOWED_WRITE_DIRS, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_LIST_FILES = "list_files"
SERVICE_READ_FILE = "read_file"
SERVICE_WRITE_FILE = "write_file"
SERVICE_DELETE_FILE = "delete_file"

# Service schemas
SERVICE_LIST_FILES_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("pattern"): cv.string,
    }
)

SERVICE_READ_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("tail_lines"): vol.Coerce(int),
    }
)

SERVICE_WRITE_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Required("content"): cv.string,
        vol.Optional("overwrite", default=False): cv.boolean,
        vol.Optional("create_dirs", default=True): cv.boolean,
    }
)

SERVICE_DELETE_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
    }
)

# Files that are allowed to be read (even if not in ALLOWED_READ_DIRS)
ALLOWED_READ_FILES = [
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "secrets.yaml",
    "home-assistant.log",
]

# Default tail lines for log files
DEFAULT_LOG_TAIL_LINES = 1000


def _is_path_allowed_for_dir(config_dir: Path, rel_path: str, allowed_dirs: list[str]) -> bool:
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


def _is_path_allowed_for_read(config_dir: Path, rel_path: str) -> bool:
    """Check if a path is allowed for reading.

    Allowed:
    - Files directly in config dir: configuration.yaml, automations.yaml, etc.
    - Files in allowed directories: www/, themes/, custom_templates/
    - Files matching patterns: packages/*.yaml, custom_components/**/*.py
    """
    normalized = os.path.normpath(rel_path)

    # Check for path traversal attempts
    if normalized.startswith("..") or normalized.startswith("/"):
        return False

    # Resolve full path and verify it's still under config_dir
    full_path = config_dir / normalized
    try:
        resolved = full_path.resolve()
        config_resolved = config_dir.resolve()
        if not str(resolved).startswith(str(config_resolved)):
            return False
    except (OSError, ValueError):
        return False

    # Check if it's one of the explicitly allowed files in config root
    if normalized in ALLOWED_READ_FILES:
        return True

    # Check if path starts with an allowed directory
    parts = normalized.split(os.sep)
    if parts and parts[0] in ALLOWED_READ_DIRS:
        return True

    # Check for packages/*.yaml pattern
    if fnmatch.fnmatch(normalized, "packages/*.yaml"):
        return True
    if fnmatch.fnmatch(normalized, "packages/**/*.yaml"):
        return True

    # Check for custom_components/**/*.py pattern
    return fnmatch.fnmatch(normalized, "custom_components/**/*.py")


def _mask_secrets_content(content: str) -> str:
    """Mask secret values in secrets.yaml content.

    Replaces actual values with [MASKED] to prevent leaking sensitive data.
    """
    # Pattern to match YAML key-value pairs
    # Handles: key: value, key: "value", key: 'value'
    lines = content.split("\n")
    masked_lines = []

    for line in lines:
        # Skip comments and empty lines
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            masked_lines.append(line)
            continue

        # Match key: value pattern
        match = re.match(r"^(\s*)([^:\s]+)(\s*:\s*)(.+)$", line)
        if match:
            indent, key, separator, value = match.groups()
            # Mask the value
            masked_lines.append(f"{indent}{key}{separator}[MASKED]")
        else:
            masked_lines.append(line)

    return "\n".join(masked_lines)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA MCP Tools from a config entry."""
    config_dir = Path(hass.config.config_dir)

    async def handle_list_files(call: ServiceCall) -> ServiceResponse:
        """Handle the list_files service call."""
        rel_path = call.data["path"]
        pattern = call.data.get("pattern")

        # Security check
        if not _is_path_allowed_for_dir(config_dir, rel_path, ALLOWED_READ_DIRS):
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
            for item in target_dir.iterdir():
                # Apply pattern filter if provided
                if pattern and not fnmatch.fnmatch(item.name, pattern):
                    continue

                stat = item.stat()
                files.append(
                    {
                        "name": item.name,
                        "path": str(item.relative_to(config_dir)),
                        "is_dir": item.is_dir(),
                        "size": stat.st_size if item.is_file() else 0,
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

    async def handle_read_file(call: ServiceCall) -> ServiceResponse:
        """Handle the read_file service call."""
        rel_path = call.data["path"]
        tail_lines = call.data.get("tail_lines")

        # Security check
        if not _is_path_allowed_for_read(config_dir, rel_path):
            _LOGGER.warning(
                "Attempted to read disallowed path: %s", rel_path
            )
            allowed_patterns = (
                ALLOWED_READ_FILES +
                [f"{d}/**" for d in ALLOWED_READ_DIRS] +
                ["packages/*.yaml", "custom_components/**/*.py"]
            )
            return {
                "success": False,
                "error": f"Path not allowed. Allowed patterns: {', '.join(allowed_patterns)}",
            }

        target_file = config_dir / rel_path

        if not target_file.exists():
            return {
                "success": False,
                "error": f"File does not exist: {rel_path}",
            }

        if not target_file.is_file():
            return {
                "success": False,
                "error": f"Path is not a file: {rel_path}",
            }

        try:
            stat = target_file.stat()
            modified_dt = datetime.fromtimestamp(stat.st_mtime)

            # Read file content
            content = await hass.async_add_executor_job(target_file.read_text)

            # Apply special handling for specific files
            normalized = os.path.normpath(rel_path)  # noqa: ASYNC240

            # Mask secrets.yaml
            if normalized == "secrets.yaml":
                content = _mask_secrets_content(content)

            # Apply tail for log files
            if normalized == "home-assistant.log":
                lines = content.split("\n")
                limit = tail_lines if tail_lines else DEFAULT_LOG_TAIL_LINES
                if len(lines) > limit:
                    content = "\n".join(lines[-limit:])
                    truncated = True
                else:
                    truncated = False

                return {
                    "success": True,
                    "path": rel_path,
                    "content": content,
                    "size": stat.st_size,
                    "modified": modified_dt.isoformat(),
                    "lines_returned": min(len(lines), limit),
                    "total_lines": len(lines),
                    "truncated": truncated,
                }

            # Apply tail for other files if requested
            if tail_lines:
                lines = content.split("\n")
                if len(lines) > tail_lines:
                    content = "\n".join(lines[-tail_lines:])

            return {
                "success": True,
                "path": rel_path,
                "content": content,
                "size": stat.st_size,
                "modified": modified_dt.isoformat(),
            }

        except PermissionError:
            _LOGGER.error("Permission denied reading: %s", rel_path)
            return {
                "success": False,
                "error": f"Permission denied: {rel_path}",
            }
        except UnicodeDecodeError:
            _LOGGER.error("Cannot read binary file: %s", rel_path)
            return {
                "success": False,
                "error": f"Cannot read binary file: {rel_path}. Only text files are supported.",
            }
        except OSError as err:
            _LOGGER.error("Error reading file %s: %s", rel_path, err)
            return {
                "success": False,
                "error": str(err),
            }

    async def handle_write_file(call: ServiceCall) -> ServiceResponse:
        """Handle the write_file service call."""
        rel_path = call.data["path"]
        content = call.data["content"]
        overwrite = call.data.get("overwrite", False)
        create_dirs = call.data.get("create_dirs", True)

        # Security check - only allow writes to specific directories
        if not _is_path_allowed_for_dir(config_dir, rel_path, ALLOWED_WRITE_DIRS):
            _LOGGER.warning(
                "Attempted to write to disallowed path: %s", rel_path
            )
            return {
                "success": False,
                "error": f"Write not allowed. Must be in: {', '.join(ALLOWED_WRITE_DIRS)}",
            }

        target_file = config_dir / rel_path

        # Check if file exists and overwrite is not allowed
        if target_file.exists() and not overwrite:
            return {
                "success": False,
                "error": f"File already exists: {rel_path}. Set overwrite=true to replace.",
            }

        try:
            # Create parent directories if needed
            if create_dirs:
                await hass.async_add_executor_job(
                    target_file.parent.mkdir, parents=True, exist_ok=True
                )

            # Check parent directory exists
            if not target_file.parent.exists():
                return {
                    "success": False,
                    "error": f"Parent directory does not exist: {target_file.parent.relative_to(config_dir)}",
                }

            # Determine if this is a new file
            is_new = not target_file.exists()

            # Write the file
            await hass.async_add_executor_job(target_file.write_text, content)

            stat = target_file.stat()
            modified_dt = datetime.fromtimestamp(stat.st_mtime)

            _LOGGER.info("Wrote file: %s (%d bytes)", rel_path, stat.st_size)

            return {
                "success": True,
                "path": rel_path,
                "size": stat.st_size,
                "modified": modified_dt.isoformat(),
                "created": is_new,
                "message": f"File {'created' if is_new else 'updated'} successfully",
            }

        except PermissionError:
            _LOGGER.error("Permission denied writing: %s", rel_path)
            return {
                "success": False,
                "error": f"Permission denied: {rel_path}",
            }
        except OSError as err:
            _LOGGER.error("Error writing file %s: %s", rel_path, err)
            return {
                "success": False,
                "error": str(err),
            }

    async def handle_delete_file(call: ServiceCall) -> ServiceResponse:
        """Handle the delete_file service call."""
        rel_path = call.data["path"]

        # Security check - only allow deletes from specific directories
        if not _is_path_allowed_for_dir(config_dir, rel_path, ALLOWED_WRITE_DIRS):
            _LOGGER.warning(
                "Attempted to delete from disallowed path: %s", rel_path
            )
            return {
                "success": False,
                "error": f"Delete not allowed. Must be in: {', '.join(ALLOWED_WRITE_DIRS)}",
            }

        target_file = config_dir / rel_path

        if not target_file.exists():
            return {
                "success": False,
                "error": f"File does not exist: {rel_path}",
            }

        if not target_file.is_file():
            return {
                "success": False,
                "error": f"Path is not a file (cannot delete directories): {rel_path}",
            }

        try:
            # Get file info before deletion for the response
            stat = target_file.stat()

            # Delete the file
            await hass.async_add_executor_job(target_file.unlink)

            _LOGGER.info("Deleted file: %s (%d bytes)", rel_path, stat.st_size)

            return {
                "success": True,
                "path": rel_path,
                "deleted_size": stat.st_size,
                "message": f"File deleted successfully: {rel_path}",
            }

        except PermissionError:
            _LOGGER.error("Permission denied deleting: %s", rel_path)
            return {
                "success": False,
                "error": f"Permission denied: {rel_path}",
            }
        except OSError as err:
            _LOGGER.error("Error deleting file %s: %s", rel_path, err)
            return {
                "success": False,
                "error": str(err),
            }

    # Register all services with response support
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_FILES,
        handle_list_files,
        schema=SERVICE_LIST_FILES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_FILE,
        handle_read_file,
        schema=SERVICE_READ_FILE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_FILE,
        handle_write_file,
        schema=SERVICE_WRITE_FILE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_FILE,
        handle_delete_file,
        schema=SERVICE_DELETE_FILE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    _LOGGER.info("HA MCP Tools initialized with file management services")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Remove all services
    hass.services.async_remove(DOMAIN, SERVICE_LIST_FILES)
    hass.services.async_remove(DOMAIN, SERVICE_READ_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_WRITE_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_FILE)
    return True
