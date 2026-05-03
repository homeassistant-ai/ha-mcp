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
    DASHBOARD_URL_PATH_PATTERN,
    DOMAIN,
    RESERVED_DASHBOARD_URL_PATHS,
    YAML_KEY_DEFAULT_POST_ACTION,
    YAML_KEY_POST_ACTIONS,
)
from .yaml_rt import make_yaml, yaml_dumps

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_LIST_FILES = "list_files"
SERVICE_READ_FILE = "read_file"
SERVICE_WRITE_FILE = "write_file"
SERVICE_DELETE_FILE = "delete_file"
SERVICE_EDIT_YAML_CONFIG = "edit_yaml_config"

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


def _validate_dashboard_filename(filename: str) -> str | None:
    """Validate a YAML-mode dashboard `filename:` value.

    Returns None if valid, otherwise a human-readable error string.

    Rules:
    - Must be a non-empty string.
    - Must end in '.yaml'.
    - Must resolve to a path under 'dashboards/' (no traversal escape).
    - No absolute paths, no '..' segments.
    """
    if not filename or not isinstance(filename, str):
        return "filename must be a non-empty string"
    if filename.startswith("/"):
        return "filename must not be an absolute path"
    if not filename.endswith(".yaml"):
        return "filename must end with .yaml"

    normalized = os.path.normpath(filename)
    if normalized.startswith("..") or normalized.startswith("/"):
        return "filename must not escape the config directory"

    parts = normalized.split(os.sep)
    if not parts or parts[0] != "dashboards":
        return "filename must be under dashboards/"
    # Reject any '..' segment (defence-in-depth after normpath collapse)
    if ".." in parts:
        return "filename must not contain path traversal segments"
    return None


def _list_files_sync(
    target_dir: Path, config_dir: Path, pattern: str | None
) -> dict[str, Any]:
    """Bundle list_files blocking I/O for a single executor offload.

    Returns either {"files": [...]} or {"_error": <kind>} so the async caller
    can format the structured error response without re-entering the executor.
    """
    if not target_dir.exists():
        return {"_error": "not_found"}
    if not target_dir.is_dir():
        return {"_error": "not_a_dir"}
    files: list[dict[str, Any]] = []
    for item in target_dir.iterdir():
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
    files.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"files": files}


def _read_file_sync(target_file: Path) -> dict[str, Any]:
    """Bundle read_file blocking I/O for a single executor offload."""
    if not target_file.exists():
        return {"_error": "not_found"}
    if not target_file.is_file():
        return {"_error": "not_a_file"}
    stat = target_file.stat()
    content = target_file.read_text()
    return {"content": content, "size": stat.st_size, "mtime": stat.st_mtime}


def _write_file_sync(
    target_file: Path,
    content: str,
    overwrite: bool,
    create_dirs: bool,
    config_dir: Path,
) -> dict[str, Any]:
    """Bundle write_file blocking I/O for a single executor offload."""
    exists = target_file.exists()
    if exists and not overwrite:
        return {"_error": "exists_no_overwrite"}
    if create_dirs:
        target_file.parent.mkdir(parents=True, exist_ok=True)
    elif not target_file.parent.exists():
        return {
            "_error": "no_parent",
            "parent": str(target_file.parent.relative_to(config_dir)),
        }
    target_file.write_text(content)
    stat = target_file.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime, "is_new": not exists}


def _delete_file_sync(target_file: Path) -> dict[str, Any]:
    """Bundle delete_file blocking I/O for a single executor offload."""
    if not target_file.exists():
        return {"_error": "not_found"}
    if not target_file.is_file():
        return {"_error": "not_a_file"}
    stat = target_file.stat()
    target_file.unlink()
    return {"size": stat.st_size}


def _build_edit_yaml_config_handler(hass):
    """Build and return the async handle_edit_yaml_config handler.

    Extracted to module level so it can be tested without registering
    the full integration.
    """
    config_dir = Path(hass.config.config_dir)

    async def handle_edit_yaml_config(call: ServiceCall) -> dict[str, Any]:
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

        # Parse and validate yaml_path (replaces the old ALLOWED_YAML_KEYS check)
        kind, path_parts, path_err = _parse_and_validate_yaml_path(yaml_path)
        if path_err is not None:
            return {"success": False, "error": path_err}

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
        backup_path_str = None

        try:
            # Read existing file content (or start with empty dict)
            target_exists = await hass.async_add_executor_job(target_file.exists)
            if target_exists:
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

            # Perform the action — branch on kind
            if kind == "lovelace_dashboard":
                url_path = path_parts[2]

                # filename validation for add/replace
                if action in ("add", "replace"):
                    if not isinstance(parsed_content, dict):
                        return {
                            "success": False,
                            "error": "lovelace.dashboards.<url_path> content must be a YAML mapping",
                        }
                    fn_err = _validate_dashboard_filename(
                        parsed_content.get("filename", "")
                    )
                    if fn_err is not None:
                        return {"success": False, "error": fn_err}

                # Walk/create lovelace.dashboards
                lovelace = data.setdefault("lovelace", {})
                if not isinstance(lovelace, dict):
                    return {
                        "success": False,
                        "error": "Existing 'lovelace' key is not a YAML mapping",
                    }
                dashboards = lovelace.setdefault("dashboards", {})
                if not isinstance(dashboards, dict):
                    return {
                        "success": False,
                        "error": "Existing 'lovelace.dashboards' is not a YAML mapping",
                    }

                if action == "add":
                    if url_path in dashboards:
                        existing = dashboards[url_path]
                        if isinstance(existing, dict) and isinstance(parsed_content, dict):
                            existing.update(parsed_content)
                        else:
                            return {
                                "success": False,
                                "error": (
                                    f"Type mismatch for dashboard '{url_path}': use 'replace' "
                                    "to overwrite."
                                ),
                            }
                    else:
                        dashboards[url_path] = parsed_content
                elif action == "replace":
                    dashboards[url_path] = parsed_content
                elif action == "remove":
                    if url_path not in dashboards:
                        return {
                            "success": False,
                            "error": (
                                f"Dashboard '{url_path}' not found under lovelace.dashboards."
                            ),
                        }
                    del dashboards[url_path]
                    # Clean up empty parent containers to keep the file tidy
                    if not dashboards:
                        del lovelace["dashboards"]
                    if not lovelace:
                        del data["lovelace"]

            else:
                # Single-key apply logic
                yaml_key = path_parts[0]
                if action == "add":
                    if yaml_key in data:
                        existing = data[yaml_key]
                        # Merge: list extends list, dict merges dict
                        if isinstance(existing, list) and isinstance(parsed_content, list):
                            data[yaml_key] = existing + parsed_content
                        elif isinstance(existing, dict) and isinstance(parsed_content, dict):
                            existing.update(parsed_content)
                        else:
                            return {
                                "success": False,
                                "error": (
                                    f"Type mismatch for key '{yaml_key}': "
                                    f"existing is {type(existing).__name__}, "
                                    f"new content is {type(parsed_content).__name__}. "
                                    "Use action='replace' to overwrite."
                                ),
                            }
                    else:
                        data[yaml_key] = parsed_content
                elif action == "replace":
                    data[yaml_key] = parsed_content
                elif action == "remove":
                    if yaml_key not in data:
                        return {
                            "success": False,
                            "error": f"Key '{yaml_key}' not found in '{rel_path}'.",
                        }
                    del data[yaml_key]

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

            # Create parent directories if needed (for new package files).
            # mkdir(exist_ok=True) is idempotent so no pre-check is required.
            await hass.async_add_executor_job(
                lambda: target_file.parent.mkdir(parents=True, exist_ok=True)
            )

            # Atomic write: write to temp file, then rename into place
            def _atomic_write() -> None:
                tmp_file = target_file.with_suffix(".tmp")
                tmp_file.write_text(new_content)
                os.replace(str(tmp_file), str(target_file))

            await hass.async_add_executor_job(_atomic_write)

            stat = await hass.async_add_executor_job(target_file.stat)
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
            if kind == "lovelace_dashboard":
                post_info = {"post_action": "restart_required"}
            else:
                post_info = YAML_KEY_POST_ACTIONS.get(
                    path_parts[0], YAML_KEY_DEFAULT_POST_ACTION
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

    return handle_edit_yaml_config


def _parse_and_validate_yaml_path(
    yaml_path: str,
) -> tuple[str, tuple[str, ...], str | None]:
    """Parse and validate a yaml_path argument.

    Two accepted shapes:
    1. Single segment in ALLOWED_YAML_KEYS -> kind='single'
    2. Exactly 'lovelace.dashboards.<url_path>' -> kind='lovelace_dashboard'

    Returns (kind, parts, error). On error, kind is '' and parts is ().
    """
    if not yaml_path or not isinstance(yaml_path, str):
        return "", (), "yaml_path must be a non-empty string"

    parts = tuple(yaml_path.split("."))

    # Shape 1: single key
    if len(parts) == 1:
        key = parts[0]
        if key in ALLOWED_YAML_KEYS:
            return "single", parts, None
        return (
            "",
            (),
            (
                f"Key '{yaml_path}' is not in the allowed list. "
                f"Allowed keys: {', '.join(sorted(ALLOWED_YAML_KEYS))}. "
                "For YAML-mode dashboards use 'lovelace.dashboards.<url_path>'."
            ),
        )

    # Shape 2: lovelace.dashboards.<url_path>
    if parts[:2] != ("lovelace", "dashboards") or len(parts) != 3:
        return (
            "",
            (),
            (
                f"Dotted yaml_path '{yaml_path}' is not supported. "
                "The only accepted dotted form is 'lovelace.dashboards.<url_path>'."
            ),
        )

    url_path = parts[2]
    if url_path in RESERVED_DASHBOARD_URL_PATHS:
        return (
            "",
            (),
            f"url_path '{url_path}' is reserved by Home Assistant and cannot be used.",
        )
    if not DASHBOARD_URL_PATH_PATTERN.fullmatch(url_path):
        return (
            "",
            (),
            (
                f"url_path '{url_path}' is invalid. Must be lowercase letters/digits "
                "separated by hyphens (e.g., 'energy-dashboard')."
            ),
        )
    return "lovelace_dashboard", parts, None


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

        try:
            result = await hass.async_add_executor_job(
                _list_files_sync, target_dir, config_dir, pattern
            )
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

        err_kind = result.get("_error")
        if err_kind == "not_found":
            return {
                "success": False,
                "error": f"Directory does not exist: {rel_path}",
                "files": [],
            }
        if err_kind == "not_a_dir":
            return {
                "success": False,
                "error": f"Path is not a directory: {rel_path}",
                "files": [],
            }

        files = result["files"]
        return {
            "success": True,
            "path": rel_path,
            "pattern": pattern,
            "files": files,
            "count": len(files),
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

        try:
            result = await hass.async_add_executor_job(_read_file_sync, target_file)
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

        err_kind = result.get("_error")
        if err_kind == "not_found":
            return {
                "success": False,
                "error": f"File does not exist: {rel_path}",
            }
        if err_kind == "not_a_file":
            return {
                "success": False,
                "error": f"Path is not a file: {rel_path}",
            }

        modified_dt = datetime.fromtimestamp(result["mtime"])
        content = result["content"]
        stat_size = result["size"]

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
                "size": stat_size,
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
            "size": stat_size,
            "modified": modified_dt.isoformat(),
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

        try:
            result = await hass.async_add_executor_job(
                _write_file_sync,
                target_file,
                content,
                overwrite,
                create_dirs,
                config_dir,
            )
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

        err_kind = result.get("_error")
        if err_kind == "exists_no_overwrite":
            return {
                "success": False,
                "error": f"File already exists: {rel_path}. Set overwrite=true to replace.",
            }
        if err_kind == "no_parent":
            return {
                "success": False,
                "error": f"Parent directory does not exist: {result['parent']}",
            }

        size = result["size"]
        modified_dt = datetime.fromtimestamp(result["mtime"])
        is_new = result["is_new"]

        _LOGGER.info("Wrote file: %s (%d bytes)", rel_path, size)

        return {
            "success": True,
            "path": rel_path,
            "size": size,
            "modified": modified_dt.isoformat(),
            "created": is_new,
            "message": f"File {'created' if is_new else 'updated'} successfully",
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

        try:
            result = await hass.async_add_executor_job(_delete_file_sync, target_file)
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

        err_kind = result.get("_error")
        if err_kind == "not_found":
            return {
                "success": False,
                "error": f"File does not exist: {rel_path}",
            }
        if err_kind == "not_a_file":
            return {
                "success": False,
                "error": f"Path is not a file (cannot delete directories): {rel_path}",
            }

        size = result["size"]
        _LOGGER.info("Deleted file: %s (%d bytes)", rel_path, size)

        return {
            "success": True,
            "path": rel_path,
            "deleted_size": size,
            "message": f"File deleted successfully: {rel_path}",
        }

    handle_edit_yaml_config = _build_edit_yaml_config_handler(hass)

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
    return True
