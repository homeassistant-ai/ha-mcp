"""
Filesystem access tools for Home Assistant MCP Server.

This module provides tools for reading and managing files within the Home Assistant
configuration directory, enabling AI assistants to:
- Read configuration files, logs, and other allowed files
- List files in allowed directories
- Write/delete files in restricted directories (www/, themes/, custom_templates/)

**Dependency:** Requires the ha_mcp_tools custom component, added in HA via its
"HA-MCP File & YAML Tools" config entry (NOT the "HA-MCP Server" entry, which
starts a redundant in-process server). The tools will gracefully fail with
installation instructions if the component is not available.

Feature Flag: Set HAMCP_ENABLE_FILESYSTEM_TOOLS=true to enable these tools.
"""

import asyncio
import json
import logging
import weakref
from typing import Annotated, Any, NoReturn

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .component_api import ComponentCaps, get_component_caps
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .util_helpers import unwrap_service_response

logger = logging.getLogger(__name__)

# Feature flag - disabled by default for safety
FEATURE_FLAG = "HAMCP_ENABLE_FILESYSTEM_TOOLS"

# Domain for the custom component
MCP_TOOLS_DOMAIN = "ha_mcp_tools"

# Caller-token auth (mirrors custom_components/ha_mcp_tools).
# The custom component's handlers reject every call that doesn't present
# this field with the matching token. ha-mcp fetches the token once via
# the get_caller_token bootstrap service, caches it, and re-fetches if a
# subsequent call comes back unauthorized (covers token rotation).
CALLER_TOKEN_FIELD = "_ha_mcp_token"
CALLER_TOKEN_BOOTSTRAP_SERVICE = "get_caller_token"

# Minimum version of the ha_mcp_tools custom component that this ha-mcp
# release expects. Bumps in lockstep with ``manifest.json`` whenever a
# server-side behavior change requires it. Older components (no
# ``version`` in the get_caller_token response, or a version below this)
# get an actionable "update via HACS" error.
# 0.8.0: ``ha_config_set_yaml`` now depends on the component's
# ``themes/*.yaml`` yaml_path scope; a <0.8.0 component reaches the old
# handler and rejects ``themes/<name>.yaml`` with a misleading "not
# allowed" message instead of this actionable update prompt.
# 0.9.0: the file tools accept absolute HAOS sibling-volume paths (/share,
# /media, /ssl, /backup — issue #1586). A <0.9.0 component's allowlist
# normalizer rejects every absolute path, so adding a volume would silently
# do nothing; the version gate surfaces an actionable "update" prompt instead.
# 0.10.0: legacy-backup restore + whole-file YAML replace add new component
# services (``replace_file``, ``list_legacy_backups``, ``read_legacy_backup``)
# and a ``yaml_path`` arg on ``read_file``. A <0.10.0 component lacks these
# services; the gate surfaces an actionable "update" instead of a raw
# "service not found".
# 0.11.0: the confirm-flow + diff work (#1720) adds ``require_confirm`` /
# ``confirm_token`` args to ``edit_yaml_config`` and ``diff`` / ``written``
# response fields. The server always sends ``require_confirm`` (from
# ENABLE_YAML_EDIT_CONFIRM, default on); a <0.11.0 component's strict
# (PREVENT_EXTRA) schema rejects the unknown arg with a raw voluptuous
# error — the gate surfaces an actionable "update" prompt instead.
# 1.2.0: the YAML fragment read (#1788, PR #1882) needs two component
# behaviours that a component reports no differently from a missing key.
# ``list_files`` honours the configured packages folder, so ha_config_get_yaml's
# glob/discovery would otherwise come back "Path not allowed" instead of
# enumerating packages; and ``include_parsed`` on ``read_file`` is a new arg
# that an older strict (PREVENT_EXTRA) schema rejects with a raw voluptuous
# error. The gate turns both into the actionable "update" prompt. The floor is
# 1.2.0, not the 1.1.0 that was current when #1882 landed: #1882 added these
# behaviours under an *un-bumped* 1.1.0 component, so 1.1.0 splits into builds
# with and without them and cannot distinguish the two (#1946). 1.2.0 is the
# first component version cut after #1882, hence the first that guarantees both.
MIN_COMPONENT_VERSION = "1.2.0"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse ``'0.5.1'`` → ``(0, 5, 1)`` for tuple-comparison.

    Raises ``ValueError`` on any non-numeric segment. Coercing a bad
    segment to ``0`` would not actually achieve the "fail closed"
    intent: a malformed high-order segment like ``"1.x.0"`` would
    still parse to ``(1, 0, 0)`` and pass a ``>= (0, 5, 1)`` gate.
    The caller surfaces the ValueError as a distinct "malformed version"
    error (reinstall / file-issue remediation), separate from the
    "too old" update prompt.
    """
    return tuple(int(segment) for segment in version.split("."))


# Weak-keyed by client object to support multi-client setups and self-evict
# when a client is garbage-collected (avoids id() reuse if a freed client's
# address gets recycled before the unauthorized-retry fires).
_CALLER_TOKEN_CACHE: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
_CALLER_TOKEN_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _get_token_lock(client: Any) -> asyncio.Lock:
    """Per-client lock so concurrent first-callers fetch the token once."""
    lock = _CALLER_TOKEN_LOCKS.get(client)
    if lock is None:
        lock = asyncio.Lock()
        _CALLER_TOKEN_LOCKS[client] = lock
    return lock


async def _is_bootstrap_service_registered(client: Any) -> bool:
    """Returns True if ha_mcp_tools.get_caller_token is present in HA's
    service registry. Old (<0.5.0) versions of the custom component
    didn't ship this service; the bootstrap call would otherwise fail
    with an opaque 400 from HA."""
    services = await client.get_services()
    for entry in services:
        if not isinstance(entry, dict):
            continue
        if entry.get("domain") != MCP_TOOLS_DOMAIN:
            continue
        domain_services = entry.get("services") or {}
        return CALLER_TOKEN_BOOTSTRAP_SERVICE in domain_services
    return False


def _raise_component_too_old(detail: str) -> NoReturn:
    """Single actionable 'update via HACS' error path."""
    raise_tool_error(
        create_error_response(
            ErrorCode.COMPONENT_NOT_INSTALLED,
            f"The installed ha_mcp_tools custom component is too old: {detail}. "
            f"This ha-mcp release requires >= {MIN_COMPONENT_VERSION}. "
            "Update via HACS and restart Home Assistant.",
            suggestions=[
                "HACS → Integrations → HA-MCP Custom Component → Update",
                "Restart Home Assistant after update completes",
                "Then retry the operation",
            ],
        )
    )


async def _fetch_caller_token(client: Any) -> str:
    """Call the bootstrap service and cache the returned token.

    Two version gates:

    1. ``_is_bootstrap_service_registered`` — pre-0.5.0 components don't
       ship ``get_caller_token`` at all. Surface an actionable "update"
       error instead of letting HA return an opaque 400 to the caller.
    2. ``MIN_COMPONENT_VERSION`` — even when the bootstrap service is
       present, the response now carries the component's manifest
       version. ha-mcp releases that depend on newer custom-component
       behavior (e.g. a new accepted yaml_path key, a new schema field)
       bump ``MIN_COMPONENT_VERSION`` together with the manifest, and
       this check rejects 0.5.0+ components that are still behind that
       bar with the same actionable update prompt.

    Components that pre-date the version-reporting field (returned no
    ``version`` in the response) are treated as "too old" for the same
    reason: the absence of the field IS the signal that the component
    doesn't yet know how to report its capabilities to ha-mcp.
    """
    if not await _is_bootstrap_service_registered(client):
        _raise_component_too_old(
            "the get_caller_token bootstrap service is not registered (pre-0.5.0)"
        )
    result = await client.call_service(
        MCP_TOOLS_DOMAIN,
        CALLER_TOKEN_BOOTSTRAP_SERVICE,
        {},
        return_response=True,
    )
    unwrapped = unwrap_service_response(result) if isinstance(result, dict) else {}
    token = unwrapped.get("token") if isinstance(unwrapped, dict) else None
    if not isinstance(token, str) or not token:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "ha_mcp_tools.get_caller_token did not return a usable token.",
                suggestions=[
                    "Reload the ha_mcp_tools integration in Home Assistant",
                    "Verify the HA token used by ha-mcp has admin rights",
                    "Then retry the operation",
                ],
            )
        )
    raw_version: Any = unwrapped.get("version") if isinstance(unwrapped, dict) else None
    if not isinstance(raw_version, str) or not raw_version:
        _raise_component_too_old(
            "the get_caller_token response did not include a version "
            f"field (pre-{MIN_COMPONENT_VERSION})"
        )
    version: str = raw_version
    try:
        parsed = _version_tuple(version)
    except ValueError:
        # A malformed version (non-numeric segment like "1.x.0", or a
        # future suffixed/date scheme _version_tuple can't parse) is a
        # different failure mode from "too old": a HACS update won't help
        # if the reported version itself is wrong. Point at reinstall /
        # issue-filing instead of the version-bump remediation.
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "The installed ha_mcp_tools custom component reports a "
                f"malformed version: {version!r}, so ha-mcp can't verify it "
                f"meets the required >= {MIN_COMPONENT_VERSION}.",
                suggestions=[
                    "Reinstall ha_mcp_tools via HACS",
                    "Restart Home Assistant after reinstalling",
                    "If the version is still malformed, file an issue at "
                    + "https://github.com/homeassistant-ai/ha-mcp/issues",
                ],
            )
        )
    if parsed < _version_tuple(MIN_COMPONENT_VERSION):
        _raise_component_too_old(f"reported version is {version}")
    _CALLER_TOKEN_CACHE[client] = token
    return token


async def _ensure_caller_token(client: Any, *, force_refresh: bool = False) -> str:
    """Return a cached or freshly-fetched caller token."""
    if not force_refresh:
        cached = _CALLER_TOKEN_CACHE.get(client)
        if cached:
            return cached
    async with _get_token_lock(client):
        cached = _CALLER_TOKEN_CACHE.get(client)
        if cached and not force_refresh:
            return cached
        return await _fetch_caller_token(client)


def _is_unauthorized_response(response: Any) -> bool:
    """Detect the custom component's structured 'unauthorized' reply."""
    if not isinstance(response, dict):
        return False
    inner = unwrap_service_response(response) if response else None
    if not isinstance(inner, dict):
        return False
    return inner.get("error_code") == "unauthorized"


async def call_mcp_tools_service(
    client: Any,
    service: str,
    service_data: dict[str, Any],
    *,
    return_response: bool = True,
) -> Any:
    """Call an ha_mcp_tools.* service with the caller token injected.

    On `unauthorized` response (token rotation or stale cache): refetch the
    token from the bootstrap service and retry once. Subsequent unauthorized
    responses are returned as-is — the wrapper tool surfaces the error.
    """
    token = await _ensure_caller_token(client)
    payload = dict(service_data)
    payload[CALLER_TOKEN_FIELD] = token
    result = await client.call_service(
        MCP_TOOLS_DOMAIN,
        service,
        payload,
        return_response=return_response,
    )
    if _is_unauthorized_response(result):
        logger.info("ha_mcp_tools rejected cached token; refetching and retrying once")
        token = await _ensure_caller_token(client, force_refresh=True)
        payload[CALLER_TOKEN_FIELD] = token
        result = await client.call_service(
            MCP_TOOLS_DOMAIN,
            service,
            payload,
            return_response=return_response,
        )
    return result


def _reset_caller_token_cache() -> None:
    """Test hook: clear the module-level token cache."""
    _CALLER_TOKEN_CACHE.clear()
    _CALLER_TOKEN_LOCKS.clear()


def is_filesystem_tools_enabled() -> bool:
    """Check if the filesystem tools feature is enabled.

    Reads through :func:`config.get_global_settings` so the same
    env-var / override-file / default precedence path applies as
    every other runtime-editable Settings field.
    """
    from ..config import get_global_settings

    return bool(get_global_settings().enable_filesystem_tools)


async def _is_mcp_tools_available(client: Any) -> bool:
    """Return True if the ha_mcp_tools custom component is registered in HA services.

    Raises if the services API call fails — callers handle API errors via
    their own exception_to_structured_error blocks.
    """
    # HA /api/services returns a list of {"domain": str, "services": {...}} objects.
    # This format has been stable since before HA 0.7 (the first public release).
    services = await client.get_services()
    return any(
        isinstance(s, dict) and s.get("domain") == MCP_TOOLS_DOMAIN for s in services
    )


def _assert_caps_version_ok(caps: ComponentCaps) -> None:
    """Reject a caps-present component below ``MIN_COMPONENT_VERSION``.

    ``get_component_caps`` only returns caps for a component new enough to
    answer ``ha_mcp_tools/info`` (shipped in 1.1.0, already past the 0.11.0
    floor), so this is a defensive belt mirroring the authoritative gate in
    ``_fetch_caller_token`` and never fires in practice. An empty / unparseable
    ``component_version`` (never emitted by a real caps-present component) is
    left to that downstream token-fetch gate rather than rejected here.
    """
    try:
        parsed = _version_tuple(caps.component_version)
    except ValueError:
        return
    if parsed < _version_tuple(MIN_COMPONENT_VERSION):
        _raise_component_too_old(f"reported version is {caps.component_version}")


async def _assert_mcp_tools_available(client: Any) -> None:
    """Raise ToolError if ha_mcp_tools is not available.

    Caps-first: a component that answers ``ha_mcp_tools/info``
    (``get_component_caps`` returns caps) obviously exists — reuse that shared
    cached probe and enforce ``MIN_COMPONENT_VERSION`` from
    ``caps.component_version`` (info shipped in 1.1.0, already past the floor).
    Legacy fallback: caps is None for a component in the 0.11.0-1.1.0 band
    (services, no info command) or an absent one, so fall back to the per-call
    ``get_services()`` existence probe.

    Must be called within a try block that handles API errors via
    exception_to_structured_error, so connection failures are classified
    correctly rather than masked as COMPONENT_NOT_INSTALLED. ``get_component_caps``
    returns None (not raises) on a transport failure, so the legacy
    ``get_services()`` probe still surfaces the connection error to that handler.
    """
    caps = await get_component_caps(client)
    if caps is not None:
        _assert_caps_version_ok(caps)
        return
    if not await _is_mcp_tools_available(client):
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                f"The {MCP_TOOLS_DOMAIN} custom component is not installed.",
                suggestions=[
                    'Add the repository to HACS: ha_manage_hacs(action="add_repository",'
                    + ' repository="homeassistant-ai/ha-mcp-integration", category="integration")',
                    'Download the component: ha_manage_hacs(action="download",'
                    + ' repository_id="homeassistant-ai/ha-mcp-integration")',
                    "Restart Home Assistant (ha_restart) so the integration loads",
                    'In HA, add the "HA-MCP Custom Component" integration and choose the'
                    + ' "HA-MCP File & YAML Tools" entry — NOT "HA-MCP Server", which starts'
                    + " a second in-process server this ha-mcp server does not need",
                ],
            )
        )


class FilesystemTools:
    """Filesystem access tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_list_files",
        tags={"Files", "beta"},
        annotations={
            "openWorldHint": False,
            "readOnlyHint": True,
            "title": "List Files",
        },
    )
    @log_tool_usage
    async def ha_list_files(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "Directory path. Relative to the config dir for the built-in "
                    "allowlist (www/, themes/, custom_templates/, dashboards/). "
                    "Custom directories and HAOS sibling volumes "
                    "(/share, /media, /ssl, /backup) configured in the ha-mcp "
                    "settings UI are also allowed (pass the absolute path). "
                    "Example: 'www/' or '/share/llm'"
                ),
            ),
        ],
        pattern: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Optional glob pattern to filter files. "
                    "Example: '*.css', '*.yaml', '*.js'"
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List files in a directory within the Home Assistant config directory.

        Lists files in allowed directories (www/, themes/, custom_templates/, dashboards/) with
        optional glob pattern filtering. Returns file names, sizes, and modification times.

        **Allowed Directories:**
        - `www/` - Web assets (CSS, JS, images for dashboards)
        - `themes/` - Theme files
        - `custom_templates/` - Jinja2 template files
        - `dashboards/` - YAML-mode dashboard files
        - Plus any custom directories OR HAOS sibling volumes (`/share`,
          `/media`, `/ssl`, `/backup`) configured in the ha-mcp settings UI
          (pass the absolute path for volumes)

        **Security:** Only directories in the allowed list can be accessed.
        Path traversal attempts (../) are blocked.

        **Returns:**
        - success: Whether the operation succeeded
        - path: The directory path that was listed
        - files: List of file info objects with name, size, is_dir, modified
        - count: Number of files found

        **Example:**
        ```python
        # List all CSS files in www/
        result = ha_list_files(path="www/", pattern="*.css")
        ```
        """
        try:
            # Check if custom component is available
            await _assert_mcp_tools_available(self._client)

            # Build service data
            service_data: dict[str, Any] = {"path": path}
            if pattern:
                service_data["pattern"] = pattern

            # Call the custom component service (token injected by helper)
            result = await call_mcp_tools_service(
                self._client,
                "list_files",
                service_data,
            )

            # Mirror ha_config_set_yaml: raise on success=false so callers
            # see a ToolError rather than a success-shaped response that
            # carries an error payload.
            if isinstance(result, dict):
                result = unwrap_service_response(result)
                if not result.get("success", True):
                    raise_tool_error(result)
                return result

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from list_files service",
                    context={"path": path},
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"tool": "ha_list_files", "path": path, "pattern": pattern},
            )
            return None
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    @tool(
        name="ha_read_file",
        tags={"Files", "beta"},
        annotations={
            "openWorldHint": False,
            "readOnlyHint": True,
            "title": "Read File",
        },
    )
    @log_tool_usage
    async def ha_read_file(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "File path. Relative to the config dir for the built-in "
                    "allowlist; absolute for a configured HAOS sibling volume "
                    "(/share, /media, /ssl, /backup). Examples: "
                    "'configuration.yaml', 'www/custom.css', '/share/llm/notes.md'"
                ),
            ),
        ],
        tail_lines: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                le=10000,
                description=(
                    "For log files, return only the last N lines. "
                    "Recommended for home-assistant.log to avoid large responses. "
                    "Default: None (return full file, or last 1000 lines for logs)"
                ),
            ),
        ] = None,
        yaml_path: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Dotted YAML key path (e.g. 'alert2', 'mqtt.sensor'). When "
                    "set, the response also carries 'subtree': the round-trip "
                    "text of just that key's value. To look a key up across "
                    "packages/*.yaml, or to get it as structured data, use "
                    "ha_config_get_yaml instead."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Read a file from the Home Assistant config directory.

        Reads files from allowed paths within the config directory. Some files
        have special handling:
        - `secrets.yaml`: Values are masked for security
        - `home-assistant.log`: Limited to tail (last N lines) by default

        **Allowed Read Paths:**
        - `configuration.yaml`, `automations.yaml`, `scripts.yaml`, `scenes.yaml`
        - `secrets.yaml` (values masked)
        - `packages/*.yaml`
        - `home-assistant.log` (tail only)
        - `www/**`, `themes/**`, `custom_templates/**`, `dashboards/**`
        - `custom_components/**/*.py` (read-only)
        - Plus any custom directories OR HAOS sibling volumes (`/share`,
          `/media`, `/ssl`, `/backup`) configured in the ha-mcp settings UI
          (pass the absolute path for volumes)

        **Security:**
        - Path traversal (../) is blocked
        - Only allowed paths can be read
        - Sensitive data in secrets.yaml is masked

        **Returns:**
        - success: Whether the operation succeeded
        - content: The file content (may be truncated for logs)
        - size: File size in bytes
        - modified: Last modification timestamp
        - path: The file path that was read
        - subtree: Round-trip text of the `yaml_path` key, when that arg is set
          (null when the key is absent). Comments and HA tags (`!secret`,
          `!include`) survive as written — a `!secret` is never resolved.

        **Example:**
        ```python
        # Read configuration
        result = ha_read_file(path="configuration.yaml")

        # Read last 100 lines of log
        result = ha_read_file(path="home-assistant.log", tail_lines=100)

        # Read just the alert2 block out of a package file
        result = ha_read_file(path="packages/alert2.yaml", yaml_path="alert2")
        ```
        """
        try:
            # Check if custom component is available
            await _assert_mcp_tools_available(self._client)

            # Build service data
            service_data: dict[str, Any] = {"path": path}
            if tail_lines is not None:
                service_data["tail_lines"] = tail_lines
            if yaml_path is not None:
                service_data["yaml_path"] = yaml_path

            # Call the custom component service
            result = await call_mcp_tools_service(
                self._client,
                "read_file",
                service_data,
            )

            if isinstance(result, dict):
                result = unwrap_service_response(result)
                if not result.get("success", True):
                    raise_tool_error(result)
                return result

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from read_file service",
                    context={"path": path},
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"tool": "ha_read_file", "path": path},
            )
            return None
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    @tool(
        name="ha_write_file",
        tags={"Files", "beta"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Write File",
        },
    )
    @with_auto_backup(domain="file", id_param="path", mandatory=True)
    @log_tool_usage
    async def ha_write_file(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "File path. Must be in a writable built-in dir (www/, "
                    "themes/, custom_templates/, dashboards/), a configured "
                    "custom directory, or a configured HAOS sibling volume "
                    "(/share, /media, /ssl, /backup — pass the absolute path). "
                    "Example: 'www/custom.css', '/share/llm/out.txt'"
                ),
            ),
        ],
        content: Annotated[
            str,
            Field(
                description="The content to write to the file.",
            ),
        ],
        overwrite: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Whether to overwrite if file exists. "
                    "Default is False to prevent accidental overwrites."
                ),
            ),
        ] = False,
        create_dirs: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Whether to create parent directories if they don't exist. "
                    "Default is True."
                ),
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Write a file to allowed directories in the Home Assistant config.

        Creates or updates files in restricted directories only. This is useful for:
        - Creating custom CSS/JS for dashboards
        - Creating Jinja2 templates

        **Allowed Write Directories:**
        - `www/` - Web assets for dashboards
        - `themes/` - Theme YAML files
        - `custom_templates/` - Jinja2 template files
        - `dashboards/` - YAML-mode dashboard files
        - Plus any custom directories OR HAOS sibling volumes (`/share`,
          `/media`, `/ssl`, `/backup`) configured in the ha-mcp settings UI
          (pass the absolute path for volumes)

        **Security:**
        - Only the directories above allow writes
        - Configuration files (configuration.yaml, etc.) cannot be written
        - Path traversal (../) is blocked

        Text content only. Overwriting a file that currently holds binary
        content still succeeds, but its prior bytes cannot be captured by
        auto-backup (only modifications/deletions of text files are
        snapshotted); the skip is logged, the write is not blocked.

        **Returns:**
        - success: Whether the operation succeeded
        - path: The file path that was written
        - size: Size of the written file in bytes
        - created: Whether this was a new file (vs overwrite)

        **Example:**
        ```python
        # Create a custom CSS file
        result = ha_write_file(
            path="www/custom-dashboard.css",
            content=".card { background: #333; }",
            overwrite=True
        )

        # Create a custom Jinja template file
        result = ha_write_file(
            path="custom_templates/formatters.jinja",
            content="{% macro shout(text) %}{{ text | upper }}{% endmacro %}",
            overwrite=False
        )
        ```
        """
        try:
            # Check if custom component is available
            await _assert_mcp_tools_available(self._client)

            # Build service data
            service_data: dict[str, Any] = {
                "path": path,
                "content": content,
                "overwrite": overwrite,
                "create_dirs": create_dirs,
            }

            # Call the custom component service
            result = await call_mcp_tools_service(
                self._client,
                "write_file",
                service_data,
            )

            if isinstance(result, dict):
                result = unwrap_service_response(result)
                if not result.get("success", True):
                    raise_tool_error(result)
                return result

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from write_file service",
                    context={"path": path},
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"tool": "ha_write_file", "path": path},
            )
            return None
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    @tool(
        name="ha_delete_file",
        tags={"Files", "beta"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Delete File",
        },
    )
    @with_auto_backup(domain="file", id_param="path", mandatory=True)
    @log_tool_usage
    async def ha_delete_file(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "File path. Must be in a writable built-in dir (www/, "
                    "themes/, custom_templates/, dashboards/), a configured "
                    "custom directory, or a configured HAOS sibling volume "
                    "(/share, /media, /ssl, /backup — pass the absolute path). "
                    "Example: 'www/old-file.css'"
                ),
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Must be True to confirm deletion. "
                    "This is a safety measure to prevent accidental deletions."
                ),
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Delete a file from allowed directories in the Home Assistant config.

        Permanently removes a file from the allowed directories. This action
        cannot be undone.

        **Allowed Delete Directories:**
        - `www/` - Web assets
        - `themes/` - Theme files
        - `custom_templates/` - Template files
        - `dashboards/` - YAML-mode dashboard files
        - Plus any custom directories OR HAOS sibling volumes (`/share`,
          `/media`, `/ssl`, `/backup`) configured in the ha-mcp settings UI
          (pass the absolute path for volumes)

        **Security:**
        - Only the directories above allow deletions
        - Configuration files cannot be deleted
        - Path traversal (../) is blocked
        - Requires confirm=True to prevent accidents

        **Returns:**
        - success: Whether the operation succeeded
        - path: The file path that was deleted
        - message: Confirmation message

        **Example:**
        ```python
        # Delete an old CSS file
        result = ha_delete_file(
            path="www/deprecated-style.css",
            confirm=True
        )
        ```
        """
        try:
            if not confirm:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Deletion not confirmed. Set confirm=True to delete a file.",
                        suggestions=[
                            f"Call ha_delete_file(path={json.dumps(path)}, confirm=True) to proceed",
                        ],
                        context={"path": path},
                    )
                )

            # Check if custom component is available
            await _assert_mcp_tools_available(self._client)

            # Build service data
            service_data: dict[str, Any] = {"path": path}

            # Call the custom component service
            result = await call_mcp_tools_service(
                self._client,
                "delete_file",
                service_data,
            )

            if isinstance(result, dict):
                result = unwrap_service_response(result)
                if not result.get("success", True):
                    raise_tool_error(result)
                return result

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from delete_file service",
                    context={"path": path},
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"tool": "ha_delete_file", "path": path},
            )
            return None
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def register_filesystem_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register filesystem access tools with the MCP server.

    This function only registers tools if the feature flag is enabled.
    Set HAMCP_ENABLE_FILESYSTEM_TOOLS=true to enable.
    """
    if not is_filesystem_tools_enabled():
        logger.debug(f"Filesystem tools disabled (set {FEATURE_FLAG}=true to enable)")
        return

    logger.info("Filesystem tools enabled via feature flag")
    register_tool_methods(mcp, FilesystemTools(client))
