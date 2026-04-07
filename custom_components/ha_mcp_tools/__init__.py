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
from io import StringIO
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
from ruamel.yaml import YAMLError

from .const import (
    ALLOWED_READ_DIRS,
    ALLOWED_WRITE_DIRS,
    ALLOWED_YAML_CONFIG_FILES,
    ALLOWED_YAML_KEYS,
    DOMAIN,
    YAML_KEY_DEFAULT_POST_ACTION,
    YAML_KEY_POST_ACTIONS,
    YAML_WRITE_BLOCKED_FILES,
)
from .yaml_rt import make_yaml, yaml_dumps

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HA-aware YAML loader/dumper
#
# Standard yaml.safe_load chokes on HA custom tags like !include and !secret.
# These classes preserve them as opaque _HATag objects so that a load→edit→dump
# cycle leaves all existing directives intact.
# ---------------------------------------------------------------------------


class _HATag:
    """Opaque wrapper that round-trips HA custom YAML tags unchanged."""

    __slots__ = ("tag", "value")

    def __init__(self, tag: str, value: str) -> None:
        self.tag = tag
        self.value = value


class _HALoader(yaml.SafeLoader):
    """SafeLoader extended to tolerate HA custom tags (!include, !secret, …)."""


def _ha_multi_constructor(
    loader: yaml.Loader, tag_suffix: str, node: yaml.ScalarNode
) -> _HATag:
    return _HATag(tag_suffix, loader.construct_scalar(node))


# "!" prefix catches all local tags while leaving tag:yaml.org,2002:* intact.
_HALoader.add_multi_constructor("!", _ha_multi_constructor)


class _HADumper(yaml.Dumper):
    """Dumper that emits _HATag objects back as their original YAML tags."""


def _ha_tag_representer(dumper: yaml.Dumper, data: _HATag) -> yaml.ScalarNode:
    return dumper.represent_scalar(data.tag, data.value)


_HADumper.add_representer(_HATag, _ha_tag_representer)


# Service names
SERVICE_LIST_FILES = "list_files"
SERVICE_READ_FILE = "read_file"
SERVICE_WRITE_FILE = "write_file"
SERVICE_DELETE_FILE = "delete_file"
SERVICE_EDIT_YAML_CONFIG = "edit_yaml_config"
SERVICE_WRITE_YAML_FILE = "write_yaml_file"

# Service schemas
SERVICE_EDIT_YAML_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("file"): cv.string,
        vol.Required("action"): vol.In(["add", "replace", "remove"]),
        vol.Required("yaml_path"): cv.string,
        vol.Optional("content"): cv.string,
        vol.Optional("backup", default=True): cv.boolean,
    }
)

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

SERVICE_WRITE_YAML_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Required("content"): cv.string,
        vol.Optional("backup", default=True): cv.boolean,
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


def _is_path_allowed_for_dir(
    config_dir: Path, rel_path: str, allowed_dirs: list[str]
) -> bool:
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
            _LOGGER.warning("Attempted to list files in disallowed path: %s", rel_path)
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
            _LOGGER.warning("Attempted to read disallowed path: %s", rel_path)
            allowed_patterns = (
                ALLOWED_READ_FILES
                + [f"{d}/**" for d in ALLOWED_READ_DIRS]
                + ["packages/*.yaml", "custom_components/**/*.py"]
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
            _LOGGER.warning("Attempted to write to disallowed path: %s", rel_path)
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
                    lambda: target_file.parent.mkdir(parents=True, exist_ok=True)
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
            _LOGGER.warning("Attempted to delete from disallowed path: %s", rel_path)
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

    async def handle_edit_yaml_config(call: ServiceCall) -> ServiceResponse:
        """Handle the edit_yaml_config service call."""
        ry = make_yaml()
        rel_path = call.data["file"]
        action = call.data["action"]
        yaml_path = call.data["yaml_path"]
        content = call.data.get("content")
        do_backup = call.data.get("backup", True)

        # Validate file path — only configuration.yaml and packages/*.yaml
        normalized = os.path.normpath(rel_path)  # noqa: ASYNC240
        if normalized.startswith("..") or normalized.startswith("/"):
            return {
                "success": False,
                "error": "Path traversal is not allowed.",
            }

        is_config_yaml = normalized in ALLOWED_YAML_CONFIG_FILES
        is_package = fnmatch.fnmatch(normalized, "packages/*.yaml") or fnmatch.fnmatch(
            normalized, "packages/**/*.yaml"
        )
        if not is_config_yaml and not is_package:
            return {
                "success": False,
                "error": (
                    f"File '{rel_path}' is not allowed. "
                    f"Only {', '.join(ALLOWED_YAML_CONFIG_FILES)} and packages/*.yaml are supported."
                ),
            }

        # Validate yaml_path against allowlist
        if yaml_path not in ALLOWED_YAML_KEYS:
            return {
                "success": False,
                "error": (
                    f"Key '{yaml_path}' is not in the allowed list. "
                    f"Allowed keys: {', '.join(sorted(ALLOWED_YAML_KEYS))}"
                ),
            }

        # Validate content is valid YAML for add/replace
        parsed_content: Any = None
        if action in ("add", "replace"):
            if not content:
                return {
                    "success": False,
                    "error": f"'content' is required for action '{action}'.",
                }
            try:
                parsed_content = ry.load(StringIO(content))
            except YAMLError as err:
                return {
                    "success": False,
                    "error": f"Invalid YAML content: {err}",
                }
            if parsed_content is None:
                return {
                    "success": False,
                    "error": "Content parsed as null/empty. Provide non-empty YAML.",
                }

        target_file = config_dir / normalized
        backup_path_str: str | None = None

        try:
            # Read existing file content (or start with empty dict)
            if target_file.exists():
                raw_content = await hass.async_add_executor_job(target_file.read_text)
                try:
                    data = ry.load(StringIO(raw_content)) or {}
                except YAMLError as err:
                    return {
                        "success": False,
                        "error": f"Cannot parse existing file '{rel_path}': {err}",
                    }
                if not isinstance(data, dict):
                    return {
                        "success": False,
                        "error": f"File '{rel_path}' root is not a YAML mapping.",
                    }
            else:
                if action == "remove":
                    return {
                        "success": False,
                        "error": f"File does not exist: {rel_path}",
                    }
                data = {}
                raw_content = ""

            # Create backup before editing (from already-read content, not disk)
            if do_backup and raw_content:
                backup_dir = config_dir / "www" / "yaml_backups"
                await hass.async_add_executor_job(
                    lambda: backup_dir.mkdir(parents=True, exist_ok=True)
                )
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_name = normalized.replace(os.sep, "_")
                backup_file = backup_dir / f"{safe_name}.{timestamp}.bak"
                await hass.async_add_executor_job(
                    backup_file.write_text, raw_content
                )
                backup_path_str = str(backup_file.relative_to(config_dir))
                _LOGGER.info("Backup created: %s", backup_path_str)

            # Perform the action
            if action == "add":
                if yaml_path in data:
                    existing = data[yaml_path]
                    # Merge: list extends list, dict merges dict
                    if isinstance(existing, list) and isinstance(parsed_content, list):
                        data[yaml_path] = existing + parsed_content
                    elif isinstance(existing, dict) and isinstance(
                        parsed_content, dict
                    ):
                        existing.update(parsed_content)
                    else:
                        return {
                            "success": False,
                            "error": (
                                f"Type mismatch for key '{yaml_path}': "
                                f"existing is {type(existing).__name__}, "
                                f"new content is {type(parsed_content).__name__}. "
                                "Use action='replace' to overwrite."
                            ),
                        }
                else:
                    data[yaml_path] = parsed_content
            elif action == "replace":
                data[yaml_path] = parsed_content
            elif action == "remove":
                if yaml_path not in data:
                    return {
                        "success": False,
                        "error": f"Key '{yaml_path}' not found in '{rel_path}'.",
                    }
                del data[yaml_path]

            # Serialize back to YAML
            try:
                new_content = yaml_dumps(ry, data)
            except YAMLError as err:
                return {
                    "success": False,
                    "error": f"Failed to serialize YAML: {err}",
                }

            # Validate the result parses cleanly
            try:
                ry.load(StringIO(new_content))
            except YAMLError as err:
                return {
                    "success": False,
                    "error": f"Generated YAML failed validation: {err}",
                }

            # Create parent directories if needed (for new package files)
            if not target_file.parent.exists():
                await hass.async_add_executor_job(
                    lambda: target_file.parent.mkdir(parents=True, exist_ok=True)
                )

            # Atomic write: write to temp file, then rename into place
            def _atomic_write() -> None:
                tmp_file = target_file.with_suffix(".tmp")
                tmp_file.write_text(new_content)
                os.replace(str(tmp_file), str(target_file))

            await hass.async_add_executor_job(_atomic_write)

            stat = target_file.stat()
            modified_dt = datetime.fromtimestamp(stat.st_mtime)

            _LOGGER.info(
                "YAML config edited: %s (action=%s, key=%s)",
                rel_path,
                action,
                yaml_path,
            )

            result: dict[str, Any] = {
                "success": True,
                "file": rel_path,
                "action": action,
                "yaml_path": yaml_path,
                "size": stat.st_size,
                "modified": modified_dt.isoformat(),
            }
            if backup_path_str:
                result["backup_path"] = backup_path_str

            # Surface the post-edit action required to activate the change
            post_info = YAML_KEY_POST_ACTIONS.get(
                yaml_path, YAML_KEY_DEFAULT_POST_ACTION
            )
            result.update(post_info)

            # Run HA config check to verify the file is loadable
            try:
                check_result = await hass.services.async_call(
                    "homeassistant",
                    "check_config",
                    {},
                    blocking=True,
                    return_response=True,
                )
                if isinstance(check_result, dict):
                    errors = check_result.get("errors")
                    if errors:
                        result["config_check"] = "errors"
                        result["config_check_errors"] = errors
                        _LOGGER.warning(
                            "Config check found errors after editing %s: %s",
                            rel_path,
                            errors,
                        )
                    else:
                        result["config_check"] = "ok"
            except Exception as check_err:
                result["config_check"] = "unavailable"
                result["config_check_error"] = str(check_err)
                _LOGGER.debug("Config check unavailable: %s", check_err)

            return result

        except PermissionError:
            _LOGGER.error("Permission denied editing: %s", rel_path)
            return {
                "success": False,
                "error": f"Permission denied: {rel_path}",
            }
        except OSError as err:
            _LOGGER.error("Error editing YAML config %s: %s", rel_path, err)
            return {
                "success": False,
                "error": str(err),
            }

    async def handle_write_yaml_file(call: ServiceCall) -> ServiceResponse:
        """Handle the write_yaml_file service call.

        Writes any YAML file within the config directory.
        secrets.yaml is always blocked. Path traversal is blocked.
        Optionally creates a timestamped backup before overwriting.
        Validates the provided content is parseable YAML before writing.
        """
        rel_path = call.data["path"]
        content = call.data["content"]
        do_backup = call.data.get("backup", True)

        # Block path traversal
        normalized = os.path.normpath(rel_path)  # noqa: ASYNC240
        if normalized.startswith("..") or normalized.startswith("/"):
            return {"success": False, "error": "Path traversal is not allowed."}

        # Only allow .yaml files
        if not normalized.endswith(".yaml"):
            return {
                "success": False,
                "error": f"Only .yaml files are allowed. Got: {rel_path}",
            }

        # Block secrets.yaml and any other blocked files
        base_name = os.path.basename(normalized)  # noqa: ASYNC240
        if base_name in YAML_WRITE_BLOCKED_FILES:
            return {
                "success": False,
                "error": f"Writing '{base_name}' is not allowed for security reasons.",
            }

        # Resolve and verify the path stays within config_dir
        target_file = config_dir / normalized
        try:
            resolved = target_file.resolve()
            config_resolved = config_dir.resolve()
            if not str(resolved).startswith(str(config_resolved)):
                return {"success": False, "error": "Path escapes config directory."}
        except (OSError, ValueError) as err:
            return {"success": False, "error": f"Invalid path: {err}"}

        # Validate the new content is parseable YAML (tolerates !include / !secret)
        try:
            yaml.load(content, Loader=_HALoader)  # noqa: S506
        except yaml.YAMLError as err:
            return {"success": False, "error": f"Invalid YAML content: {err}"}

        backup_path_str: str | None = None

        try:
            # Create backup of the existing file if requested
            if do_backup and target_file.exists():
                backup_dir = config_dir / "www" / "yaml_backups"
                await hass.async_add_executor_job(
                    backup_dir.mkdir, parents=True, exist_ok=True
                )
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_name = normalized.replace(os.sep, "_")
                backup_file = backup_dir / f"{safe_name}.{timestamp}.bak"
                raw_existing = await hass.async_add_executor_job(target_file.read_text)
                await hass.async_add_executor_job(backup_file.write_text, raw_existing)
                backup_path_str = str(backup_file.relative_to(config_dir))
                _LOGGER.info("Backup created: %s", backup_path_str)

            # Create parent directories if needed
            if not target_file.parent.exists():
                await hass.async_add_executor_job(
                    target_file.parent.mkdir, parents=True, exist_ok=True
                )

            is_new = not target_file.exists()

            # Atomic write: write to temp file then rename into place
            def _atomic_write() -> None:
                tmp_file = target_file.with_suffix(".tmp")
                tmp_file.write_text(content)
                os.replace(str(tmp_file), str(target_file))

            await hass.async_add_executor_job(_atomic_write)

            stat = target_file.stat()
            modified_dt = datetime.fromtimestamp(stat.st_mtime)

            _LOGGER.info(
                "YAML file written: %s (%d bytes, new=%s)", rel_path, stat.st_size, is_new
            )

            result: dict[str, Any] = {
                "success": True,
                "path": rel_path,
                "size": stat.st_size,
                "modified": modified_dt.isoformat(),
                "created": is_new,
            }
            if backup_path_str:
                result["backup_path"] = backup_path_str

            # Run HA config check to verify the file is loadable
            try:
                check_result = await hass.services.async_call(
                    "homeassistant",
                    "check_config",
                    {},
                    blocking=True,
                    return_response=True,
                )
                if isinstance(check_result, dict):
                    errors = check_result.get("errors")
                    if errors:
                        result["config_check"] = "errors"
                        result["config_check_errors"] = errors
                        _LOGGER.warning(
                            "Config check found errors after writing %s: %s",
                            rel_path,
                            errors,
                        )
                    else:
                        result["config_check"] = "ok"
            except Exception as check_err:
                result["config_check"] = "unavailable"
                result["config_check_error"] = str(check_err)
                _LOGGER.debug("Config check unavailable: %s", check_err)

            return result

        except PermissionError:
            _LOGGER.error("Permission denied writing YAML file: %s", rel_path)
            return {"success": False, "error": f"Permission denied: {rel_path}"}
        except OSError as err:
            _LOGGER.error("Error writing YAML file %s: %s", rel_path, err)
            return {"success": False, "error": str(err)}

    # Register all services with response support
    hass.services.async_register(
        DOMAIN,
        SERVICE_EDIT_YAML_CONFIG,
        handle_edit_yaml_config,
        schema=SERVICE_EDIT_YAML_CONFIG_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

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

    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_YAML_FILE,
        handle_write_yaml_file,
        schema=SERVICE_WRITE_YAML_FILE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    _LOGGER.info("HA MCP Tools initialized with file management services")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Remove all services
    hass.services.async_remove(DOMAIN, SERVICE_EDIT_YAML_CONFIG)
    hass.services.async_remove(DOMAIN, SERVICE_LIST_FILES)
    hass.services.async_remove(DOMAIN, SERVICE_READ_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_WRITE_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_WRITE_YAML_FILE)
    return True
