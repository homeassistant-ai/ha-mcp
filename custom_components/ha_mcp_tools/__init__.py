"""HA MCP Tools - Custom component for ha-mcp server.

Provides services that are not available through standard Home Assistant APIs,
enabling AI assistants to perform advanced operations like file management.
"""

from __future__ import annotations

import errno
import fnmatch
import logging
import os
import secrets
import shutil
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from ruamel.yaml import YAMLError

from .const import (
    ALLOWED_READ_DIRS,
    ALLOWED_WRITE_DIRS,
    ALLOWED_YAML_CONFIG_FILES,
    ALLOWED_YAML_KEYS,
    DASHBOARD_URL_PATH_PATTERN,
    DENY_PATH_SEGMENTS,
    DENY_READ_BASENAMES,
    DOMAIN,
    PACKAGES_ONLY_YAML_KEYS,
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
SERVICE_GET_CALLER_TOKEN = "get_caller_token"
SERVICE_GET_ALLOWED_PATHS = "get_allowed_paths"
SERVICE_SET_ALLOWED_PATHS = "set_allowed_paths"

# Caller-token auth (PR: restrict ha_mcp_tools.* to ha-mcp callers).
# ha-mcp injects this field in every service-call payload; non-ha-mcp callers
# (HA UI, automations, other integrations, the ha_call_service LLM bypass)
# omit it and are rejected with a structured unauthorized response.
CALLER_TOKEN_FIELD = "_ha_mcp_token"
_TOKEN_STORAGE_KEY = f"{DOMAIN}_auth"
_TOKEN_STORAGE_VERSION = 1
_HASS_DATA_TOKEN_KEY = "caller_token"

# User-configurable extra read/write directories (issue #1567). Persisted in a
# SEPARATE Store from the caller token so a corrupt/edited allowlist can never
# affect token bootstrap, and the security credential is never mixed with user
# config. Loaded into hass.data at setup and updated in place by
# set_allowed_paths so enforcement picks up changes with no HA restart.
_ALLOWED_PATHS_STORAGE_KEY = f"{DOMAIN}_allowed_paths"
_ALLOWED_PATHS_STORAGE_VERSION = 1
_HASS_DATA_ALLOWED_PATHS_KEY = "allowed_paths"

# Service schemas
SERVICE_EDIT_YAML_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("file"): cv.string,
        vol.Required("action"): vol.In(["add", "replace", "remove"]),
        vol.Required("yaml_path"): cv.string,
        vol.Optional("content"): cv.string,
        vol.Optional("backup", default=True): cv.boolean,
        # Caller-provided list of PACKAGES_ONLY_YAML_KEYS that the
        # caller wants the component to reject. Empty list (the
        # default) means no extra restrictions on top of the
        # component's existing allowlist. ha-mcp populates this from
        # its per-key Settings flags so the wrapper and the component
        # stay symmetric — if ha-mcp's flag is off, the component also
        # refuses, defending against bypass attempts.
        vol.Optional("disabled_packages_keys", default=list): vol.All(
            cv.ensure_list, [cv.string]
        ),
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_LIST_FILES_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("pattern"): cv.string,
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_READ_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("tail_lines"): vol.Coerce(int),
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_WRITE_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Required("content"): cv.string,
        vol.Optional("overwrite", default=False): cv.boolean,
        vol.Optional("create_dirs", default=True): cv.boolean,
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_DELETE_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

# get_caller_token is the bootstrap surface: ha-mcp does not know the token
# yet on first run, so this service intentionally does NOT require the token
# itself. HA's default admin-auth still applies to the service call.
SERVICE_GET_CALLER_TOKEN_SCHEMA = vol.Schema(
    {
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

# get_allowed_paths / set_allowed_paths back the ha-mcp settings UI's custom
# filesystem-directory editor (issue #1567). Both are caller-token + admin
# gated. set_allowed_paths receives the FULL replacement list (mirrors how
# disabled_packages_keys sends the whole set each call); the handler validates
# and drops any entry that hits the deny floor or escapes the config dir.
SERVICE_GET_ALLOWED_PATHS_SCHEMA = vol.Schema(
    {
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_SET_ALLOWED_PATHS_SCHEMA = vol.Schema(
    {
        vol.Optional("paths", default=list): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
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


async def _load_or_create_caller_token(hass: HomeAssistant) -> str:
    """Return the persisted caller token, generating + saving one on first use.

    The token authorizes a caller as ha-mcp. It's stored under
    ``.storage/ha_mcp_tools_auth`` and remains stable across restarts so the
    ha-mcp server can re-bootstrap without user intervention.

    A corrupt/unreadable store must NOT propagate out of async_setup_entry and
    take down the integration: on a load failure we log and fall through to
    generating a fresh token (same path as first run), overwriting the bad
    blob. ha-mcp transparently re-bootstraps the new token via its
    unauthorized-retry, so the only cost is a one-time token rotation.
    """
    store: Store = Store(hass, _TOKEN_STORAGE_VERSION, _TOKEN_STORAGE_KEY)
    try:
        data = await store.async_load()
    except Exception:
        _LOGGER.warning(
            "ha_mcp_tools: could not load the caller-token store; generating a "
            "fresh token and overwriting it.",
            exc_info=True,
        )
        data = None
    if isinstance(data, dict):
        existing = data.get("token")
        if isinstance(existing, str) and existing:
            return existing
    token = secrets.token_urlsafe(32)
    await store.async_save({"token": token})
    return token


async def _load_allowed_paths(hass: HomeAssistant) -> list[str]:
    """Return the persisted user-configurable extra directories.

    Each stored entry is re-validated through :func:`_normalize_extra_dir`, so a
    hand-edited or corrupted store can never load a traversal / deny-floor /
    out-of-config entry into ``hass.data`` (defense in depth — the deny floor is
    also re-checked at enforcement time). Anything dropped is logged so a
    "my custom directories disappeared" case is one grep away. Empty list on
    first run or a malformed store — fail safe to "no extra access" rather than
    raising, mirroring :func:`_load_or_create_caller_token`.
    """
    store: Store = Store(
        hass, _ALLOWED_PATHS_STORAGE_VERSION, _ALLOWED_PATHS_STORAGE_KEY
    )
    try:
        data = await store.async_load()
    except Exception:
        # Honour the documented fail-safe contract: a corrupt/unreadable
        # allowed-paths blob must NOT propagate out of async_setup_entry and
        # take down the integration. Log loudly and fall back to no extra
        # access.
        _LOGGER.warning(
            "ha_mcp_tools: could not load the allowed-paths store; ignoring it "
            "and granting no extra directories.",
            exc_info=True,
        )
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("paths")
    if not isinstance(raw, list):
        if raw is not None:
            _LOGGER.warning(
                "ha_mcp_tools allowed-paths store is malformed (paths is %s, "
                "expected list); ignoring it.",
                type(raw).__name__,
            )
        return []
    config_dir = Path(hass.config.config_dir)
    normalized: list[str] = []
    dropped: list[Any] = []
    for entry in raw:
        norm = (
            _normalize_extra_dir(entry, config_dir) if isinstance(entry, str) else None
        )
        if norm is None:
            dropped.append(entry)
        elif norm not in normalized:
            normalized.append(norm)
    if dropped:
        _LOGGER.warning(
            "ha_mcp_tools: dropped %d invalid entr%s from the persisted "
            "allowed-paths store: %r",
            len(dropped),
            "y" if len(dropped) == 1 else "ies",
            dropped,
        )
    return normalized


async def _save_allowed_paths(hass: HomeAssistant, paths: list[str]) -> None:
    """Persist the user-configurable extra directories to .storage."""
    store: Store = Store(
        hass, _ALLOWED_PATHS_STORAGE_VERSION, _ALLOWED_PATHS_STORAGE_KEY
    )
    await store.async_save({"paths": paths})


def _unauthorized_response(service_name: str, **extra: Any) -> dict[str, Any]:
    """Structured 'unauthorized' response.

    ha-mcp clients detect this via ``error_code == "unauthorized"`` and
    re-fetch the token before retrying.
    """
    return {
        "success": False,
        "error": (
            f"Unauthorized: caller token missing or invalid for "
            f"{DOMAIN}.{service_name}. This service is restricted to the "
            "ha-mcp server; other callers should not invoke it directly."
        ),
        "error_code": "unauthorized",
        **extra,
    }


def _caller_token_ok(hass: HomeAssistant, call: ServiceCall) -> bool:
    """Return True if the caller presented the configured token."""
    domain_data = hass.data.get(DOMAIN)
    expected = (
        domain_data.get(_HASS_DATA_TOKEN_KEY) if isinstance(domain_data, dict) else None
    )
    presented = call.data.get(CALLER_TOKEN_FIELD)
    # token_urlsafe(32) is 256-bit; the timing-side-channel risk is already
    # negligible, but secrets.compare_digest is the right reflex regardless.
    if not isinstance(expected, str) or not isinstance(presented, str):
        return False
    return secrets.compare_digest(expected, presented)


async def _caller_is_admin(hass: HomeAssistant, call: ServiceCall) -> bool:
    """Return True if the caller is an admin user (or a no-user-context call).

    HA's service registry has no built-in admin requirement — `Service`
    has no admin flag, `async_call` performs no permission check, and
    WS / REST `call_service` lack `@require_admin`. Gate explicitly here.

    Calls without `context.user_id` (system-internal events) are treated
    as trusted, matching HA's `async_admin_handler_factory` convention.
    The supported deployment shapes all use admin tokens: addon's
    SUPERVISOR_TOKEN maps to HA's `hassio_user`, which HA force-promotes
    into `GROUP_ID_ADMIN` (hassio/__init__.py); standalone Docker/pip
    deployments use a user-supplied admin LLAT.
    """
    if not call.context.user_id:
        return True
    user = await hass.auth.async_get_user(call.context.user_id)
    return user is not None and bool(user.is_admin)


def _is_within_config_dir(config_dir: Path, normalized: str) -> bool:
    """Resolve ``config_dir / normalized`` and confirm it stays within
    ``config_dir`` (symlink-aware).

    Uses ``Path.is_relative_to`` rather than a string prefix so a sibling like
    ``<config>-evil`` can't masquerade as being inside the config dir. Fails
    closed on any resolution error.
    """
    try:
        resolved = (config_dir / normalized).resolve()
        config_resolved = config_dir.resolve()
    except (OSError, ValueError):
        return False
    return resolved == config_resolved or resolved.is_relative_to(config_resolved)


def _violates_deny_floor(config_dir: Path, normalized: str) -> bool:
    """True if ``normalized`` (a ``normpath``'d, config-relative path) hits the
    non-overridable deny floor (issue #1567).

    Checked BEFORE any allow decision on every read/write/list/delete, so a
    user-configured extra directory — whether stored or supplied in-flight —
    can never reach these locations. See ``const.DENY_PATH_SEGMENTS`` /
    ``const.DENY_READ_BASENAMES`` for the rationale.
    """
    # All comparisons are case-insensitive: on a case-insensitive filesystem
    # (macOS APFS, some Docker bind mounts / SMB) ".STORAGE" opens the real
    # ".storage", so an exact-case match would let a mixed-case entry slip
    # through. The deny sets are already lowercase, so lowering the input is
    # enough. This only ever denies MORE — no legitimate path is a case-variant
    # of ".storage"/"secrets.yaml".
    parts = [p for p in normalized.split(os.sep) if p]
    # A .storage segment anywhere in the relative path (".storage",
    # ".storage/auth", "x/.storage/y").
    if any(p.lower() in DENY_PATH_SEGMENTS for p in parts):
        return True
    # Symlink defence: resolve and reject if the real target passes THROUGH a
    # denied segment (e.g. an in-config symlink pointing at .storage). Scan ONLY
    # the portion of the resolved path that is *under the config dir* — not the
    # full absolute path — so the floor doesn't blanket-ban every access when the
    # config dir itself happens to live below a ".storage" component (e.g.
    # ``/var/.storage/config``). This mirrors the pre-PR allowlist model, which
    # likewise only ever reasons about the config-relative path. ``relative_to``
    # raising ValueError means the resolved target escaped the config dir (e.g. a
    # symlink out) — fail closed. Fail closed on any resolution error too.
    try:
        resolved = (config_dir / normalized).resolve()
        rel_parts = resolved.relative_to(config_dir.resolve()).parts
    except (OSError, ValueError):
        return True
    if any(seg.lower() in DENY_PATH_SEGMENTS for seg in rel_parts):
        return True
    # secrets.yaml — by basename of BOTH the requested path AND the resolved
    # target, so a renamed symlink (``www/notes.txt`` → ``secrets.yaml``) can't
    # dodge it and then escape masking (the read handler masks only the literal
    # ``secrets.yaml``). The canonical config-root file is the one exception,
    # matched EXACTLY (not lowercased) because that is the only path the handler
    # masks — a mixed-case ``SECRETS.YAML`` at the root is NOT masked, so it
    # must be denied.
    return (
        os.path.basename(normalized).lower() in DENY_READ_BASENAMES
        or resolved.name.lower() in DENY_READ_BASENAMES
    ) and normalized != "secrets.yaml"


def _normalize_extra_dir(entry: str, config_dir: Path) -> str | None:
    """Validate and normalize one user-supplied extra directory (issue #1567).

    Returns the cleaned, config-relative directory, or ``None`` if the entry
    must be rejected: not a string, empty, the config root, absolute, uses
    ``..`` traversal, resolves outside the config dir, or hits the deny floor.
    Rejected entries are dropped (mirrors the filter-to-known-good discipline
    used for ``disabled_packages_keys``).
    """
    if not isinstance(entry, str) or os.path.isabs(entry):
        return None
    cleaned = entry.strip().strip("/")
    if not cleaned:
        return None
    normalized = os.path.normpath(cleaned)
    if (
        normalized in (".", "")
        or normalized.startswith("..")
        or normalized.startswith("/")
    ):
        return None
    if not _is_within_config_dir(config_dir, normalized):
        return None
    if _violates_deny_floor(config_dir, normalized):
        return None
    return normalized


def _matches_extra_dir(normalized: str, extra_dirs: list[str] | None) -> bool:
    """True if ``normalized`` IS one of the user-configured extra directories
    or a path under one (issue #1567).

    Prefix match on a path-separator boundary so a multi-segment entry like
    ``foo/bar`` grants ``foo/bar`` and ``foo/bar/...`` exactly as configured
    (the built-in allowlists are single-segment and matched on ``parts[0]``,
    but extra dirs may be nested), while ``foo`` never matches ``foobar``.
    """
    if not extra_dirs:
        return False
    return any(normalized == d or normalized.startswith(d + os.sep) for d in extra_dirs)


def _is_path_allowed_for_dir(
    config_dir: Path,
    rel_path: str,
    allowed_dirs: list[str],
    extra_dirs: list[str] | None = None,
) -> bool:
    """Check if a path is within allowed directories.

    ``extra_dirs`` are the user-configured custom directories (issue #1567),
    granted in addition to ``allowed_dirs``. The non-overridable deny floor is
    checked first, so a custom directory can never grant access to ``.storage``
    or other floored paths.
    """
    # Normalize the path
    normalized = os.path.normpath(rel_path)

    # Check for path traversal attempts
    if normalized.startswith("..") or normalized.startswith("/"):
        return False

    # NON-OVERRIDABLE deny floor — before any allow decision (issue #1567).
    if _violates_deny_floor(config_dir, normalized):
        return False

    # Built-in allowlist matches on the first segment; user-configured extra
    # dirs match on a path-boundary prefix (so nested entries work).
    parts = normalized.split(os.sep)
    builtin_ok = bool(parts) and parts[0] in allowed_dirs
    if not builtin_ok and not _matches_extra_dir(normalized, extra_dirs):
        return False

    # Resolve full path and verify it's still under config_dir
    return _is_within_config_dir(config_dir, normalized)


def _is_path_allowed_for_read(
    config_dir: Path, rel_path: str, extra_dirs: list[str] | None = None
) -> bool:
    """Check if a path is allowed for reading.

    Allowed:
    - Files directly in config dir: configuration.yaml, automations.yaml, etc.
    - Files in allowed directories: www/, themes/, custom_templates/
    - Files matching patterns: packages/*.yaml, custom_components/**/*.py
    - User-configured extra directories (``extra_dirs``), granted read+write
      (issue #1567)

    The non-overridable deny floor is checked first, so a custom directory can
    never reach ``.storage`` or an unmasked ``secrets.yaml``.
    """
    normalized = os.path.normpath(rel_path)

    # Check for path traversal attempts
    if normalized.startswith("..") or normalized.startswith("/"):
        return False

    # Resolve full path and verify it's still under config_dir
    if not _is_within_config_dir(config_dir, normalized):
        return False

    # NON-OVERRIDABLE deny floor — before any allow decision (issue #1567).
    if _violates_deny_floor(config_dir, normalized):
        return False

    # Check if it's one of the explicitly allowed files in config root
    if normalized in ALLOWED_READ_FILES:
        return True

    # Check if path starts with an allowed directory
    parts = normalized.split(os.sep)
    if parts and parts[0] in ALLOWED_READ_DIRS:
        return True

    # Check for packages/*.yaml pattern. ``fnmatch``'s ``*`` matches
    # ``/`` too, so this pattern alone covers nested paths
    # (``packages/sub/foo.yaml``) — no explicit recursive variant
    # needed.
    if fnmatch.fnmatch(normalized, "packages/*.yaml"):
        return True

    # Check for custom_components/**/*.py pattern
    if fnmatch.fnmatch(normalized, "custom_components/**/*.py"):
        return True

    # User-configured extra directories (read+write) — issue #1567. Prefix
    # match so nested entries (e.g. "foo/bar") grant paths under them.
    return _matches_extra_dir(normalized, extra_dirs)


def _mask_secrets_content(content: str) -> str:
    """Return secrets.yaml content with every secret value masked.

    Parses the document structurally (ruamel — the same YAML stack used
    elsewhere in this component) and emits ``key: [MASKED]`` for each top-level
    key. This closes the gap in the previous line-by-line regex, which masked
    only single-line ``key: value`` pairs and leaked multi-line block scalars
    (``|``, ``>``) whose continuation lines have no colon — SSH keys, TLS
    material, and service-account JSON are commonly stored that way.

    Fails closed: any content that cannot be parsed and masked as a key-value
    mapping is withheld rather than returned raw, so a failure on this path never
    leaks secrets. The catch is deliberately broad — masking is a security
    boundary, so every failure mode (parse error, a pathological tag constructor,
    a YAML init/threading error) must withhold, not propagate to the caller with
    the raw content still in scope.
    """
    try:
        parsed = make_yaml().load(content)
        if not isinstance(parsed, dict):
            # Empty file (None) or a top-level list/scalar: no top-level keys to
            # mask, so withhold rather than risk returning unmasked content.
            return (
                "# secrets.yaml is empty or not a key-value mapping — content withheld"
            )
        return "\n".join(f"{key}: [MASKED]" for key in parsed)
    except YAMLError:
        return "# secrets.yaml could not be parsed — content withheld to avoid leaking secrets"
    except Exception:
        return "# secrets.yaml could not be masked — content withheld to avoid leaking secrets"


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
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_EDIT_YAML_CONFIG)
        rel_path = call.data["file"]
        action = call.data["action"]
        yaml_path = call.data["yaml_path"]
        content = call.data.get("content")
        do_backup = call.data.get("backup", True)
        # Caller-provided per-key opt-out for PACKAGES_ONLY_YAML_KEYS.
        # Filter to the recognised set so a caller that types
        # ``automatoin`` doesn't accidentally pass through; only
        # actually-known keys count.
        disabled_packages_keys = {
            key
            for key in call.data.get("disabled_packages_keys", [])
            if key in PACKAGES_ONLY_YAML_KEYS
        }

        # Validate file path — only configuration.yaml and packages/*.yaml
        normalized = os.path.normpath(rel_path)  # noqa: ASYNC240
        if normalized.startswith("..") or normalized.startswith("/"):
            return {
                "success": False,
                "error": "Path traversal is not allowed.",
            }

        is_config_yaml = normalized in ALLOWED_YAML_CONFIG_FILES
        # ``fnmatch``'s ``*`` matches ``/`` too, so this single
        # pattern covers both flat ``packages/foo.yaml`` and nested
        # ``packages/sub/foo.yaml``. The recursive variant
        # ``packages/**/*.yaml`` is mathematically a subset of this
        # one (``**`` reduces to ``*`` in fnmatch), so it's omitted.
        is_package = fnmatch.fnmatch(normalized, "packages/*.yaml")
        if not is_config_yaml and not is_package:
            return {
                "success": False,
                "error": (
                    f"File '{rel_path}' is not allowed. "
                    f"Only {', '.join(ALLOWED_YAML_CONFIG_FILES)} and packages/*.yaml are supported."
                ),
            }

        # Per-key gate fires only for packages/*.yaml writes. Writes to
        # configuration.yaml fall through to ``_parse_and_validate_yaml_path``
        # which rejects PACKAGES_ONLY_YAML_KEYS with the storage-mode-
        # tools advisory regardless of this flag.
        top_segment = yaml_path.split(".", 1)[0] if yaml_path else ""
        if is_package and top_segment in disabled_packages_keys:
            return {
                "success": False,
                "error": (
                    f"yaml_path key {top_segment!r} is disabled by the "
                    f"caller's runtime configuration. Re-enable it in the "
                    f"caller (the ha-mcp Server Settings → YAML config "
                    f"editing → 'Allow {top_segment} in packages/*.yaml' "
                    f"toggle), or use the storage-mode equivalent."
                ),
            }

        # Parse and validate yaml_path (replaces the old ALLOWED_YAML_KEYS check)
        kind, path_parts, path_err = _parse_and_validate_yaml_path(
            yaml_path, is_package=is_package
        )
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
                parsed_content = await hass.async_add_executor_job(
                    lambda: make_yaml().load(StringIO(content))
                )
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
                    data = await hass.async_add_executor_job(
                        lambda: make_yaml().load(StringIO(raw_content)) or {}
                    )
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

            # Create backup before editing (from already-read content, not disk).
            # Backups go under .ha_mcp_tools_backups/ at the config root — NOT
            # under www/, which Home Assistant serves unauthenticated at /local/
            # (GHSA-g39v-cvjh-8fpf).
            if do_backup and raw_content:
                backup_dir = config_dir / ".ha_mcp_tools_backups"
                await hass.async_add_executor_job(
                    lambda: backup_dir.mkdir(parents=True, exist_ok=True)
                )
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_name = normalized.replace(os.sep, "_")
                backup_file = backup_dir / f"{safe_name}.{timestamp}.bak"
                await hass.async_add_executor_job(backup_file.write_text, raw_content)
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
                        if isinstance(existing, dict) and isinstance(
                            parsed_content, dict
                        ):
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
                        if isinstance(existing, list) and isinstance(
                            parsed_content, list
                        ):
                            data[yaml_key] = existing + parsed_content
                        elif isinstance(existing, dict) and isinstance(
                            parsed_content, dict
                        ):
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
                new_content = await hass.async_add_executor_job(
                    lambda: yaml_dumps(make_yaml(), data)
                )
            except YAMLError as err:
                return {
                    "success": False,
                    "error": f"Failed to serialize YAML: {err}",
                }

            # Validate the result parses cleanly
            try:
                await hass.async_add_executor_job(
                    lambda: make_yaml().load(StringIO(new_content))
                )
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
    *,
    is_package: bool = False,
) -> tuple[str, tuple[str, ...], str | None]:
    """Parse and validate a yaml_path argument.

    Two accepted shapes:
    1. Single segment in ALLOWED_YAML_KEYS -> kind='single'
       When ``is_package=True``, single segments in PACKAGES_ONLY_YAML_KEYS
       (automation, script, scene) are also accepted.
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
        if is_package and key in PACKAGES_ONLY_YAML_KEYS:
            return "single", parts, None
        # Reaching here means the key was not accepted. If it is a
        # PACKAGES_ONLY key, we know is_package=False (otherwise the
        # preceding branch would have returned) — emit the targeted
        # "move it to a package file" guidance instead of the generic
        # allowlist dump below.
        if key in PACKAGES_ONLY_YAML_KEYS:
            return (
                "",
                (),
                (
                    f"Key '{yaml_path}' is only allowed in packages/*.yaml "
                    "files, not in configuration.yaml. Move the edit to a "
                    "package file (e.g., packages/automations.yaml) or use "
                    "ha_config_set_automation, ha_config_set_script, or "
                    "ha_config_set_scene for storage-mode."
                ),
            )
        allowed = (
            ALLOWED_YAML_KEYS | PACKAGES_ONLY_YAML_KEYS
            if is_package
            else ALLOWED_YAML_KEYS
        )
        return (
            "",
            (),
            (
                f"Key '{yaml_path}' is not in the allowed list. "
                f"Allowed keys: {', '.join(sorted(allowed))}. "
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


def _migrate_legacy_backup_dir(config_dir: Path) -> tuple[int, int]:
    """Move pre-fix backups out of www/ (GHSA-g39v-cvjh-8fpf).

    Pre-fix versions wrote backups under www/yaml_backups/, which HA serves
    unauthenticated at /local/. Move .bak files into the new
    .ha_mcp_tools_backups/ directory and remove the old directory if empty.
    Returns (moved, failed) — counts of files successfully relocated and
    files that could not be moved (left in legacy_dir, still exposed).
    """
    legacy_dir = config_dir / "www" / "yaml_backups"
    if not legacy_dir.is_dir():
        return 0, 0

    new_dir = config_dir / ".ha_mcp_tools_backups"
    new_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    failed = 0
    for src in legacy_dir.iterdir():
        # Only migrate regular .bak files. Skip directories, symlinks,
        # sockets/fifos, and any user-deposited stray files.
        if not src.is_file() or src.is_symlink() or src.suffix != ".bak":
            continue

        # Pick a non-colliding destination name. If <name>.bak exists, try
        # <name>.legacy.bak, then .legacy1.bak, .legacy2.bak, etc. Required
        # because Path.rename / shutil.move overwrite the destination
        # silently on POSIX, which would lose data.
        dest = new_dir / src.name
        if dest.exists():
            dest = new_dir / f"{src.stem}.legacy{src.suffix}"
            counter = 1
            while dest.exists():
                dest = new_dir / f"{src.stem}.legacy{counter}{src.suffix}"
                counter += 1

        try:
            # shutil.move falls back to copy+unlink on EXDEV (cross-device),
            # which Path.rename does not handle — required for setups where
            # /config/www is a separate mount (Docker bind, LXC, NFS).
            shutil.move(str(src), str(dest))
            moved += 1
        except OSError as err:
            failed += 1
            _LOGGER.error(
                "Failed to migrate %s out of www/: %s. File remains "
                "exposed via /local/yaml_backups/.",
                src.name,
                err,
            )

    # Remove the legacy dir if we emptied it. Narrow the swallowed errno
    # to ENOTEMPTY (user-dropped non-.bak files block removal — fine);
    # surface anything else (permissions, read-only FS, etc.).
    try:
        legacy_dir.rmdir()
    except OSError as err:
        # ENOTEMPTY on Linux/APFS; EEXIST on some pre-APFS macOS filesystems.
        if err.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            _LOGGER.warning(
                "Could not remove legacy backup dir %s: [%s] %s",
                legacy_dir,
                type(err).__name__,
                err,
            )

    return moved, failed


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA MCP Tools from a config entry."""
    config_dir = Path(hass.config.config_dir)

    # Bootstrap the caller-auth token. Generated on first setup, persisted
    # to .storage/ha_mcp_tools_auth, cached in hass.data for fast handler
    # access. ha-mcp fetches it via the get_caller_token service.
    caller_token = await _load_or_create_caller_token(hass)
    hass.data.setdefault(DOMAIN, {})[_HASS_DATA_TOKEN_KEY] = caller_token

    # Load the user-configurable extra read/write directories (issue #1567)
    # into hass.data so the file handlers read them with no I/O. set_allowed_paths
    # updates this in place, so changes apply live (no HA restart).
    hass.data.setdefault(DOMAIN, {})[
        _HASS_DATA_ALLOWED_PATHS_KEY
    ] = await _load_allowed_paths(hass)

    def _current_extra_dirs() -> list[str]:
        """Return the live user-configured extra directories from hass.data."""
        domain_data = hass.data.get(DOMAIN)
        if isinstance(domain_data, dict):
            paths = domain_data.get(_HASS_DATA_ALLOWED_PATHS_KEY)
            if isinstance(paths, list):
                return paths
        return []

    # One-time migration of pre-fix YAML backups out of the publicly-served
    # www/ directory (GHSA-g39v-cvjh-8fpf). Wrapped so a migration failure
    # cannot prevent the integration from loading — the integration's
    # normal value (file ops, edit_yaml_config) is unaffected by this.
    try:
        moved, failed = await hass.async_add_executor_job(
            _migrate_legacy_backup_dir, config_dir
        )
    except Exception as err:
        # Defensive: a migration failure must not block setup_entry, since
        # the integration's normal value (file ops, edit_yaml_config) is
        # unaffected by whether old backups got relocated.
        _LOGGER.error(
            "GHSA-g39v-cvjh-8fpf migration failed: %s. Pre-fix backups "
            "may still be present in www/yaml_backups/ and reachable "
            "via /local/yaml_backups/ — manual cleanup required.",
            err,
        )
        moved, failed = 0, 0

    if moved or failed:
        if failed:
            heading = (
                f"Migrated {moved} YAML backup file(s); **{failed} could "
                "not be moved** and remain in `www/yaml_backups/`, still "
                "reachable without authentication via `/local/yaml_backups/`. "
                "Move or delete them manually before continuing."
            )
        else:
            heading = (
                f"Moved {moved} YAML backup file(s) from `www/yaml_backups/` "
                "to `.ha_mcp_tools_backups/`. The previous location was "
                "reachable without authentication via `/local/yaml_backups/`."
            )
        message = (
            f"{heading} See "
            "[GHSA-g39v-cvjh-8fpf](https://github.com/homeassistant-ai/ha-mcp/security/advisories/GHSA-g39v-cvjh-8fpf). "
            "**Rotate any secrets** that appeared in those YAML files "
            "(MQTT/REST credentials, webhook IDs, `shell_command` "
            "definitions, geofence coordinates). If you version-control "
            "your Home Assistant config, also add `.ha_mcp_tools_backups/` "
            "to your `.gitignore` so future backups are not committed."
        )
        _LOGGER.error(message)
        try:
            persistent_notification.async_create(
                hass,
                message,
                title="HA MCP Tools — credential exposure (GHSA-g39v-cvjh-8fpf)",
                notification_id="ha_mcp_tools_ghsa_g39v_cvjh_8fpf",
            )
        except Exception as err:
            # Defensive: log line above is the source of truth; the
            # notification is best-effort UX and must not block setup.
            _LOGGER.warning(
                "Could not create persistent notification for "
                "GHSA-g39v-cvjh-8fpf migration: [%s] %s",
                type(err).__name__,
                err,
            )

    async def handle_list_files(call: ServiceCall) -> ServiceResponse:
        """Handle the list_files service call."""
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_LIST_FILES, files=[])
        rel_path = call.data["path"]
        pattern = call.data.get("pattern")

        # Security check
        extra_dirs = _current_extra_dirs()
        if not _is_path_allowed_for_dir(
            config_dir, rel_path, ALLOWED_READ_DIRS, extra_dirs
        ):
            _LOGGER.warning("Attempted to list files in disallowed path: %s", rel_path)
            return {
                "success": False,
                "error": f"Path not allowed. Must be in: {', '.join(ALLOWED_READ_DIRS + extra_dirs)}",
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
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_READ_FILE)
        rel_path = call.data["path"]
        tail_lines = call.data.get("tail_lines")

        # Security check
        extra_dirs = _current_extra_dirs()
        if not _is_path_allowed_for_read(config_dir, rel_path, extra_dirs):
            _LOGGER.warning("Attempted to read disallowed path: %s", rel_path)
            allowed_patterns = (
                ALLOWED_READ_FILES
                + [f"{d}/**" for d in ALLOWED_READ_DIRS]
                + ["packages/*.yaml", "custom_components/**/*.py"]
                + [f"{d}/**" for d in extra_dirs]
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
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_WRITE_FILE)
        rel_path = call.data["path"]
        content = call.data["content"]
        overwrite = call.data.get("overwrite", False)
        create_dirs = call.data.get("create_dirs", True)

        # Security check - only allow writes to specific directories
        extra_dirs = _current_extra_dirs()
        if not _is_path_allowed_for_dir(
            config_dir, rel_path, ALLOWED_WRITE_DIRS, extra_dirs
        ):
            _LOGGER.warning("Attempted to write to disallowed path: %s", rel_path)
            return {
                "success": False,
                "error": f"Write not allowed. Must be in: {', '.join(ALLOWED_WRITE_DIRS + extra_dirs)}",
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
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_DELETE_FILE)
        rel_path = call.data["path"]

        # Security check - only allow deletes from specific directories
        extra_dirs = _current_extra_dirs()
        if not _is_path_allowed_for_dir(
            config_dir, rel_path, ALLOWED_WRITE_DIRS, extra_dirs
        ):
            _LOGGER.warning("Attempted to delete from disallowed path: %s", rel_path)
            return {
                "success": False,
                "error": f"Delete not allowed. Must be in: {', '.join(ALLOWED_WRITE_DIRS + extra_dirs)}",
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

    async def handle_get_caller_token(call: ServiceCall) -> ServiceResponse:
        """Return the caller-auth token to the ha-mcp bootstrap caller.

        Admin-gated explicitly (see `_caller_is_admin`): HA's service
        registry has no built-in admin requirement, so the gate prevents
        a non-admin caller from bootstrapping the token. The supported
        deployments (addon supervisor user, standalone admin LLAT) all
        pass this gate.
        """
        if not await _caller_is_admin(hass, call):
            return {
                "success": False,
                "error_code": "unauthorized",
                "error": "ha_mcp_tools.get_caller_token requires admin auth.",
            }
        domain_data = hass.data.get(DOMAIN)
        token = (
            domain_data.get(_HASS_DATA_TOKEN_KEY)
            if isinstance(domain_data, dict)
            else None
        )
        if not isinstance(token, str) or not token:
            return {
                "success": False,
                "error_code": "not_initialized",
                "error": (
                    "Caller token not initialized — integration may not have "
                    "completed setup. Reload the ha_mcp_tools integration."
                ),
            }
        # Report the manifest version so ha-mcp can enforce a minimum
        # compatible component version. The integration loader reads
        # ``manifest.json`` once at startup; ``async_get_integration``
        # is cheap and avoids hard-coding the version twice.
        # Pathological-but-belt-and-suspenders: a corrupted manifest
        # would otherwise surface as HA's generic handler error rather
        # than the actionable structured response shape this service
        # uses everywhere else.
        try:
            integration = await async_get_integration(hass, DOMAIN)
            version = str(integration.version)
        except Exception as exc:  # pragma: no cover — manifest sanity
            _LOGGER.warning(
                "Could not read ha_mcp_tools manifest version for "
                "get_caller_token response: %s",
                exc,
            )
            return {
                "success": False,
                "error_code": "manifest_unreadable",
                "error": (
                    "ha_mcp_tools manifest version could not be read. "
                    "Reinstall the integration via HACS."
                ),
            }
        return {
            "success": True,
            "token": token,
            "version": version,
        }

    async def handle_get_allowed_paths(call: ServiceCall) -> ServiceResponse:
        """Return the user-configurable extra directories plus the built-in
        allowlists and the non-overridable deny floor (issue #1567).

        Backs the ha-mcp settings UI's custom filesystem-directory editor.
        Caller-token + admin gated (matching get_caller_token): it reveals the
        filesystem layout, so it is not a public read.
        """
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_GET_ALLOWED_PATHS, paths=[])
        if not await _caller_is_admin(hass, call):
            return {
                "success": False,
                "error_code": "unauthorized",
                "error": "ha_mcp_tools.get_allowed_paths requires admin auth.",
                "paths": [],
            }
        return {
            "success": True,
            "paths": _current_extra_dirs(),
            "builtin_read_dirs": list(ALLOWED_READ_DIRS),
            "builtin_write_dirs": list(ALLOWED_WRITE_DIRS),
            "deny_floor": sorted(DENY_PATH_SEGMENTS | DENY_READ_BASENAMES),
        }

    async def handle_set_allowed_paths(call: ServiceCall) -> ServiceResponse:
        """Replace the user-configurable extra directories (issue #1567).

        Receives the FULL replacement list. Each entry is validated and
        normalized; entries that use traversal, are absolute, escape the config
        directory, or hit the non-overridable deny floor are dropped and
        reported in ``rejected``. Persists to .storage AND updates hass.data so
        enforcement applies live with no HA restart. Caller-token + admin gated.
        """
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_SET_ALLOWED_PATHS, paths=[])
        if not await _caller_is_admin(hass, call):
            return {
                "success": False,
                "error_code": "unauthorized",
                "error": "ha_mcp_tools.set_allowed_paths requires admin auth.",
                "paths": [],
            }
        raw_paths = call.data.get("paths", [])
        normalized: list[str] = []
        rejected: list[str] = []
        for entry in raw_paths:
            norm = _normalize_extra_dir(entry, config_dir)
            if norm is None:
                rejected.append(entry)
            elif norm not in normalized:
                normalized.append(norm)

        await _save_allowed_paths(hass, normalized)
        hass.data.setdefault(DOMAIN, {})[_HASS_DATA_ALLOWED_PATHS_KEY] = normalized
        _LOGGER.info(
            "Updated ha_mcp_tools custom filesystem directories: %s (%d rejected)",
            normalized,
            len(rejected),
        )
        return {
            "success": True,
            "paths": normalized,
            "rejected": rejected,
        }

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
        SERVICE_GET_CALLER_TOKEN,
        handle_get_caller_token,
        schema=SERVICE_GET_CALLER_TOKEN_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ALLOWED_PATHS,
        handle_get_allowed_paths,
        schema=SERVICE_GET_ALLOWED_PATHS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ALLOWED_PATHS,
        handle_set_allowed_paths,
        schema=SERVICE_SET_ALLOWED_PATHS_SCHEMA,
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
    hass.services.async_remove(DOMAIN, SERVICE_GET_CALLER_TOKEN)
    hass.services.async_remove(DOMAIN, SERVICE_GET_ALLOWED_PATHS)
    hass.services.async_remove(DOMAIN, SERVICE_SET_ALLOWED_PATHS)

    # Drop the cached token + allowlist from hass.data so a subsequent
    # setup_entry re-reads from storage (covers the reload-after-rotate path).
    domain_data = hass.data.get(DOMAIN)
    if isinstance(domain_data, dict):
        domain_data.pop(_HASS_DATA_TOKEN_KEY, None)
        domain_data.pop(_HASS_DATA_ALLOWED_PATHS_KEY, None)
    return True
