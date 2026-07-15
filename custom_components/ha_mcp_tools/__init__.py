"""HA MCP Tools - Custom component for ha-mcp server.

Provides services that are not available through standard Home Assistant APIs,
enabling AI assistants to perform advanced operations like file management.
"""

from __future__ import annotations

import difflib
import errno
import fnmatch
import hashlib
import logging
import os
import posixpath
import re
import secrets
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any

import voluptuous as vol
import yaml  # type: ignore[import-untyped]
from homeassistant.components import persistent_notification
from homeassistant.config import async_check_ha_config_file
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
    ALLOWED_VOLUME_ROOTS,
    ALLOWED_WRITE_DIRS,
    ALLOWED_YAML_CONFIG_FILES,
    ALLOWED_YAML_KEYS,
    COMPONENT_VERSION,
    CONF_ENTRY_TYPE,
    DASHBOARD_URL_PATH_PATTERN,
    DENY_PATH_SEGMENTS,
    DENY_READ_BASENAMES,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    PACKAGES_ONLY_YAML_KEYS,
    RESERVED_DASHBOARD_URL_PATHS,
    TOOLS_ENTRY_LEGACY_TITLE,
    TOOLS_ENTRY_TITLE,
    YAML_KEY_DEFAULT_POST_ACTION,
    YAML_KEY_POST_ACTIONS,
)
from .websocket_api import async_register_commands
from .yaml_rt import apply_seq_indent, detect_seq_indent, make_yaml, yaml_dumps

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
# Read-only access to pre-#1579 YAML backups in .ha_mcp_tools_backups/, so the
# shared edits-backup interface can list/view/diff/restore them (#1579). These
# historical artifacts predate the fold into the shared store; new writes no
# longer land here.
SERVICE_LIST_LEGACY_BACKUPS = "list_legacy_backups"
SERVICE_READ_LEGACY_BACKUP = "read_legacy_backup"

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
        vol.Required("action"): vol.In(["add", "replace", "remove", "replace_file"]),
        vol.Required("yaml_path"): cv.string,
        vol.Optional("content"): cv.string,
        # Back-compat shim: the component reaches users via HACS ahead of the
        # server, so a 0.10.0 component runs against the prior stable server
        # until that server's next release. Pre-7.9.0 servers still send
        # "backup": true on every edit_yaml_config call; this strict
        # (PREVENT_EXTRA) schema would reject it ("extra keys not allowed")
        # and break ha_config_set_yaml for everyone in that window. Accept and
        # ignore it. Removable once the minimum supported server is >= 7.9.0.
        vol.Optional("backup"): cv.boolean,
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
        # Two-step preview/confirm flow (#1720). Both optional with
        # old-behavior defaults so a pre-confirm-flow server (which never
        # sends them) still gets an immediate write.
        vol.Optional("require_confirm", default=False): cv.boolean,
        vol.Optional("confirm_token"): cv.string,
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
        # When set, also return the round-trip text of the YAML subtree at this
        # dotted path under ``subtree`` (used by ha-mcp's per-edit auto-backup
        # to snapshot the prior value before ha_config_set_yaml edits it, #1579).
        vol.Optional("yaml_path"): cv.string,
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
# filesystem-directory editor (issues #1567, #1586). Both are caller-token +
# admin gated. set_allowed_paths receives the FULL replacement list (mirrors how
# disabled_packages_keys sends the whole set each call); the handler validates
# and drops any entry that hits the deny floor, or that escapes the config dir
# without being one of the fixed HAOS sibling-volume roots (#1586).
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

SERVICE_LIST_LEGACY_BACKUPS_SCHEMA = vol.Schema(
    {
        vol.Optional(CALLER_TOKEN_FIELD): cv.string,
    }
)

SERVICE_READ_LEGACY_BACKUP_SCHEMA = vol.Schema(
    {
        vol.Required("filename"): cv.string,
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


def _unified_diff(before: str, after: str, rel_path: str, max_lines: int = 200) -> str:
    """Unified diff of a prospective write, capped for response size."""
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{rel_path} (before)",
            tofile=f"{rel_path} (after)",
        )
    )
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        lines = lines[:max_lines] + [f"... diff truncated ({omitted} more lines)\n"]
    return "".join(lines)


def _confirm_token(normalized: str, new_content: str) -> str:
    """Stateless confirm token: hash of target path + exact bytes to write.

    Re-derived on the confirm call from CURRENT disk state, so any change
    to the file between preview and confirm invalidates the token
    (optimistic locking, same philosophy as utils/config_hash.py).
    """
    return hashlib.sha256(f"{normalized}\n{new_content}".encode()).hexdigest()[:16]


def _count_reindented_lines(diff_text: str) -> int:
    """Count untouched lines that only changed leading whitespace.

    A ``-``/``+`` pair whose stripped bodies match is a pure re-indent —
    collateral from normalizing a mixed-style file to one sequence style
    (ruamel supports a single style per dump). Multiset matching so
    repeated identical lines don't over-count.
    """
    removed: dict[str, int] = {}
    added: dict[str, int] = {}
    for line in diff_text.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            body = line[1:]
            removed[body.strip()] = removed.get(body.strip(), 0) + 1
        elif line.startswith("+"):
            body = line[1:]
            added[body.strip()] = added.get(body.strip(), 0) + 1
    return sum(min(n, added.get(key, 0)) for key, n in removed.items() if key)


def _reindent_warning(count: int) -> str:
    """Warning text for re-indented untouched lines (see ``diff``)."""
    return (
        f"{count} line(s) outside the requested edit were re-indented: the "
        "file mixes sequence-indent styles and the serializer normalizes to "
        "the first-detected style. Values are unchanged (guarded); review "
        "the diff if the whitespace matters."
    )


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


def _volume_root_for(abs_path: str) -> str | None:
    """Return the HAOS sibling-volume root (``const.ALLOWED_VOLUME_ROOTS``) that
    ``abs_path`` falls within, or ``None`` (issue #1586).

    Boundary-aware on a path-separator: ``/share`` and ``/share/x`` match
    ``/share``; ``/shared`` and ``/backups`` do NOT (a string-prefix match would
    wrongly admit them). ``abs_path`` must already be ``posixpath.normpath``'d —
    the volume roots are inherently POSIX and the component only runs on HA Core
    (Linux), so we never use the host ``os.sep`` here.
    """
    for root in ALLOWED_VOLUME_ROOTS:
        if abs_path == root or abs_path.startswith(root + "/"):
            return root
    return None


def _resolves_within(base: Path, raw_path: str) -> bool:
    """Resolve ``raw_path`` exactly as ``open(2)`` will — following symlinks,
    THEN applying ``..`` against the real target — and confirm it stays within
    ``base`` (symlink-safe containment; issue #1586 review).

    The lexical allow checks run on ``os.path.normpath(raw_path)``, which
    collapses ``..`` *textually* and so erases an intermediate symlink component
    (``<allowed>/<symlink>/..``) that the kernel would actually traverse at open
    time — the divergence that let a configured volume reach arbitrary files.
    Resolving the RAW input (not the normpath-collapsed form) closes it, because
    the handlers open ``config_dir / raw_path`` and the OS resolves it the same
    way ``Path.resolve()`` does. Fails closed on any resolution error.
    """
    try:
        candidate = Path(raw_path) if raw_path.startswith("/") else base / raw_path
        real = candidate.resolve()
        base_real = base.resolve()
    except (OSError, ValueError):
        return False
    return real == base_real or real.is_relative_to(base_real)


def _violates_volume_deny_floor(abs_path: str) -> bool:
    """True if an absolute HAOS volume path hits the non-overridable deny floor
    (issue #1586).

    The same floor as the config dir — a ``.storage`` segment anywhere, or a
    ``secrets.yaml`` basename — applied to both the requested path and its
    symlink-resolved target, case-insensitively. Unlike the config dir there is
    NO canonical ``secrets.yaml`` exception: volume reads are never masked, so a
    ``secrets.yaml`` on any volume is always denied. Fails closed on any
    resolution error.
    """
    req = PurePosixPath(abs_path)
    if any(seg.lower() in DENY_PATH_SEGMENTS for seg in req.parts):
        return True
    if req.name.lower() in DENY_READ_BASENAMES:
        return True
    try:
        resolved = Path(abs_path).resolve()
    except (OSError, ValueError):
        return True
    if any(seg.lower() in DENY_PATH_SEGMENTS for seg in resolved.parts):
        return True
    return resolved.name.lower() in DENY_READ_BASENAMES


def _normalize_volume_dir(entry: str) -> str | None:
    """Validate one absolute HAOS sibling-volume directory (issue #1586).

    Returns the cleaned absolute path, or ``None`` if it is not at/under a known
    volume root or hits the deny floor. POSIX-normalized regardless of host OS
    (the roots are inherently POSIX and the component only runs on HA Core).
    Existence is NOT required (mirrors config-relative extra dirs); an unmounted
    volume just yields ``not found`` at use time.
    """
    normalized = posixpath.normpath(entry)
    root = _volume_root_for(normalized)
    if root is None:
        return None
    if _violates_volume_deny_floor(normalized):
        return None
    return normalized


def _is_volume_path_allowed(abs_path: str, extra_dirs: list[str] | None) -> bool:
    """Enforce a read/write/list/delete against an absolute HAOS volume path
    (issue #1586).

    Allowed iff the path is at/under a configured extra dir that is itself a
    volume path, clears the deny floor, and — after FULL symlink + ``..``
    resolution of the RAW path (matching ``open(2)``) — stays within its volume
    root. Resolving the raw path rather than the lexically ``normpath``'d form
    closes the ``<volume>/<symlink>/..`` escape (issue #1586 review). Read and
    write share this gate — a configured volume grants read+write (#1567).
    """
    normalized = posixpath.normpath(abs_path)
    root = _volume_root_for(normalized)
    if root is None:
        return False
    if _violates_volume_deny_floor(normalized):
        return False
    # POSIX-explicit allowlist match (not _matches_extra_dir, which joins on
    # os.sep): a volume path is inherently "/"-separated, so match on a "/"
    # boundary so a configured "/share" grants "/share" and "/share/..." but
    # never "/shared". Config-relative entries in the mixed list never match an
    # absolute path here.
    if not extra_dirs or not any(
        normalized == d or normalized.startswith(d + "/") for d in extra_dirs
    ):
        return False
    return _resolves_within(Path(root), abs_path)


def _normalize_extra_dir(entry: str, config_dir: Path) -> str | None:
    """Validate and normalize one user-supplied extra directory (issues #1567,
    #1586).

    Returns the cleaned directory, or ``None`` if the entry must be rejected.
    Two accepted shapes:

    * A config-relative directory (issue #1567) — rejected if empty, the config
      root, uses ``..`` traversal, resolves outside the config dir, or hits the
      deny floor.
    * An absolute HAOS sibling-volume path (issue #1586) — kept absolute and
      validated against its volume root (``const.ALLOWED_VOLUME_ROOTS``) instead
      of the config dir. Any other absolute path is still rejected.

    Rejected entries are dropped (mirrors the filter-to-known-good discipline
    used for ``disabled_packages_keys``).
    """
    if not isinstance(entry, str):
        return None
    stripped = entry.strip()
    if not stripped:
        return None
    # Absolute HAOS sibling-volume path (issue #1586) — validated against its
    # volume root, not the config dir. Detected by a POSIX-absolute leading "/"
    # (not os.path.isabs, which is False for "/share" on a non-POSIX host) so the
    # logic is identical on every OS the tests run on. Any non-volume absolute
    # path is rejected inside _normalize_volume_dir.
    if stripped.startswith("/"):
        return _normalize_volume_dir(stripped)
    cleaned = stripped.strip("/")
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


class _PackagesDir(str):
    """Marker for the folder argument of a packages ``!include_dir_*named``."""


def _capture_packages_dir(loader: yaml.Loader, node: yaml.nodes.Node) -> _PackagesDir:
    """Construct a packages include-dir tag as its folder-name argument."""
    return _PackagesDir(str(node.value))


def _follow_include(loader: yaml.Loader, node: yaml.nodes.Node) -> Any:
    """Follow ``!include`` so a split ``homeassistant:`` / ``packages:`` block in
    another file is still seen during folder discovery (#1854 review).

    Depth-guarded and best-effort: an unreadable/unparseable target (or a
    string-stream loader with no base path) yields ``None``, like any other tag.
    Only the referenced file's structure is read; every other HA tag inside it
    is still dropped by the ignore constructor.
    """
    depth = getattr(loader, "_pkg_include_depth", 0)
    name = getattr(loader, "name", "")
    if depth >= 8 or not isinstance(name, str) or not name or name.startswith("<"):
        return None
    path = os.path.join(os.path.dirname(name), str(node.value))
    try:
        with open(path, encoding="utf-8") as handle:
            sub = _PackagesDirLoader(handle)
            sub._pkg_include_depth = depth + 1
            try:
                return sub.get_single_data()
            finally:
                sub.dispose()
    except (OSError, yaml.YAMLError):
        return None


def _ignore_unknown_tag(
    loader: yaml.Loader, tag_suffix: str, node: yaml.nodes.Node
) -> None:
    """Drop any other HA custom tag (``!secret`` / ``!env_var`` / ...).

    Folder discovery only needs the ``homeassistant: packages:`` structure, so
    unresolved tags are irrelevant — turning them into ``None`` lets
    configuration.yaml parse structurally without resolving secrets, which is
    what keeps this from being fragile hand-parsing.
    """
    return None


class _PackagesDirLoader(yaml.SafeLoader):
    """SafeLoader that captures packages include-dir folder names, follows
    ``!include``, and ignores every other HA custom tag."""


_PackagesDirLoader.add_constructor("!include_dir_named", _capture_packages_dir)
_PackagesDirLoader.add_constructor("!include_dir_merge_named", _capture_packages_dir)
_PackagesDirLoader.add_constructor("!include", _follow_include)
_PackagesDirLoader.add_multi_constructor("!", _ignore_unknown_tag)


def _extract_package_dir_markers(data: object) -> set[str]:
    """Return the raw folder argument(s) of the packages include directive(s) in
    a parsed configuration mapping. Inline packages declare no folder."""
    if not isinstance(data, dict):
        return set()
    core = data.get("homeassistant")
    if not isinstance(core, dict):
        return set()
    packages = core.get("packages")
    markers: list[_PackagesDir] = []
    if isinstance(packages, _PackagesDir):
        markers.append(packages)
    elif isinstance(packages, dict):
        markers.extend(v for v in packages.values() if isinstance(v, _PackagesDir))
    return {str(m) for m in markers}


def _load_package_dir_markers(config_path: str) -> set[str]:
    """Parse configuration.yaml (following ``!include``) and return the raw
    folder argument(s) of every packages ``!include_dir_*named`` directive.

    Blocking (opens files) — call via an executor. Returns raw strings, possibly
    absolute; the caller relativizes and filters them against the config dir.
    """
    try:
        with open(config_path, encoding="utf-8") as handle:
            loader = _PackagesDirLoader(handle)
            try:
                data = loader.get_single_data()
            finally:
                loader.dispose()
    except (OSError, yaml.YAMLError):
        return set()
    return _extract_package_dir_markers(data)


def _package_folder_relative_to_config(raw: str, config_dir: str) -> str | None:
    """Normalize a captured packages-folder argument to a config-relative folder
    name, or ``None`` if it escapes the config dir.

    An absolute include under the config dir (``!include_dir_named
    /config/integrations``) is expressed relative to it; an absolute path
    elsewhere, a parent escape, or the config root itself is dropped.
    """
    if raw.startswith("/"):
        norm = os.path.normpath(raw)
        if norm == config_dir or norm.startswith(config_dir + os.sep):
            rel = os.path.relpath(norm, config_dir)
            return rel if rel != "." else None
        return None
    folder = os.path.normpath(raw)
    if folder and folder != "." and not folder.startswith(".."):
        return folder
    return None


async def _detect_package_dirs(hass: HomeAssistant) -> set[str]:
    """Return the config-relative folder name(s) HA loads packages from.

    Home Assistant binds packages via ``homeassistant: packages:
    !include_dir_named <folder>`` (or ``!include_dir_merge_named``), where
    ``<folder>`` is any user-chosen directory — not necessarily ``packages``
    (issue #1854). The folder is read straight from that directive in
    configuration.yaml (following ``!include`` for a split ``homeassistant:``
    section), so it is found even when the folder is still empty (the first
    package is about to be created), a nested layout resolves to the configured
    include root, and an in-config absolute include is relativized.

    Always includes the built-in ``"packages"`` default, and degrades to just
    that if configuration.yaml can't be read.
    """
    dirs = {"packages"}
    config_path = hass.config.path(ALLOWED_YAML_CONFIG_FILES[0])
    # normpath is a pure string transform (no I/O), so ASYNC240 doesn't apply.
    config_dir = os.path.normpath(hass.config.config_dir)  # noqa: ASYNC240
    markers = await hass.async_add_executor_job(_load_package_dir_markers, config_path)
    for raw in markers:
        folder = _package_folder_relative_to_config(raw, config_dir)
        if folder:
            dirs.add(folder)
    return dirs


def _path_in_package_dir(normalized: str, package_dirs: set[str] | None) -> bool:
    """True if ``normalized`` is a ``*.yaml`` file at any depth under one of the
    configured package folders.

    Literal folder match (not ``fnmatch``), so a folder name containing glob
    metacharacters — valid on HA's Linux filesystem — is matched as the literal
    include root rather than a wildcard (#1854 review).
    """
    if not normalized.endswith(".yaml"):
        return False
    return any(
        normalized.startswith(folder + "/") for folder in (package_dirs or {"packages"})
    )


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
    # Absolute HAOS sibling-volume path (issue #1586) — enforced against its
    # volume root rather than the config dir. Detected by a POSIX-absolute
    # leading "/" and passed RAW (not normpath'd) so symlink resolution matches
    # what the handler's open() does. Any non-volume absolute path is rejected
    # inside the helper.
    if rel_path.startswith("/"):
        return _is_volume_path_allowed(rel_path, extra_dirs)

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

    # Symlink-safe containment on the REAL path the handler will open (issue
    # #1586 review): resolve the RAW rel_path — open(2) follows symlinks then
    # applies "..", which the lexical normpath above cannot model — and confirm
    # it stays under the config dir. Closes the `<allowed>/<symlink>/../escape`
    # traversal the lexical checks miss.
    if not _resolves_within(config_dir, rel_path):
        return False

    # Resolve full path and verify it's still under config_dir
    return _is_within_config_dir(config_dir, normalized)


def _is_path_allowed_for_read(
    config_dir: Path,
    rel_path: str,
    extra_dirs: list[str] | None = None,
    package_dirs: set[str] | None = None,
) -> bool:
    """Check if a path is allowed for reading.

    Allowed:
    - Files directly in config dir: configuration.yaml, automations.yaml, etc.
    - Files in allowed directories: www/, themes/, custom_templates/
    - Files matching patterns: <packages-folder>/*.yaml, custom_components/**/*.py
    - User-configured extra directories (``extra_dirs``), granted read+write
      (issue #1567)

    ``package_dirs`` is the set of configured packages folder name(s) (issue
    #1854); it defaults to ``{"packages"}`` when not supplied so callers that
    don't resolve the live config keep the historical behaviour.

    The non-overridable deny floor is checked first, so a custom directory can
    never reach ``.storage`` or an unmasked ``secrets.yaml``.
    """
    # Absolute HAOS sibling-volume path (issue #1586) — enforced against its
    # volume root rather than the config dir. Detected by a POSIX-absolute
    # leading "/" and passed RAW so symlink resolution matches the handler's
    # open(). A configured volume grants read too, so reads route through the
    # same gate as dir/write.
    if rel_path.startswith("/"):
        return _is_volume_path_allowed(rel_path, extra_dirs)

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

    # Symlink-safe containment on the REAL path the handler will open (issue
    # #1586 review): resolve the RAW rel_path so a `<allowed>/<symlink>/..` that
    # lexically stays inside but physically escapes is rejected.
    if not _resolves_within(config_dir, rel_path):
        return False

    # Check if it's one of the explicitly allowed files in config root
    if normalized in ALLOWED_READ_FILES:
        return True

    # Check if path starts with an allowed directory
    parts = normalized.split(os.sep)
    if parts and parts[0] in ALLOWED_READ_DIRS:
        return True

    # Check for <packages-folder>/*.yaml at any depth. The configured folder(s)
    # default to {"packages"} (issue #1854); literal match so a folder name with
    # glob metacharacters is treated as the include root, not a wildcard.
    if _path_in_package_dir(normalized, package_dirs):
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
        # Config-relative paths report relative to the config dir; absolute
        # HAOS sibling-volume paths (issue #1586) are not under it, so report
        # them absolute (the caller passes absolute paths for volumes too).
        try:
            reported_path = str(item.relative_to(config_dir))
        except ValueError:
            reported_path = str(item)
        files.append(
            {
                "name": item.name,
                "path": reported_path,
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
        # Config-relative parents report relative to the config dir; an absolute
        # HAOS sibling-volume parent (issue #1586) is not under it, so report it
        # absolute rather than raising ValueError on relative_to.
        try:
            parent = str(target_file.parent.relative_to(config_dir))
        except ValueError:
            parent = str(target_file.parent)
        return {
            "_error": "no_parent",
            "parent": parent,
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


def _replace_file_sync(target_file: Path, content: str) -> dict[str, Any]:
    """Bundle whole-file replace I/O for a single executor offload.

    mkdir parent + atomic temp-write + rename + stat, mirroring
    ``_write_file_sync``. Used by edit_yaml_config(action="replace_file").
    """
    target_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = target_file.with_suffix(".tmp")
    tmp_file.write_text(content)
    os.replace(str(tmp_file), str(target_file))
    stat = target_file.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime}


# Pre-#1579 YAML backups live here (config-root dotdir, never under www/ —
# GHSA-g39v-cvjh-8fpf). New writes no longer land here; these are read-only
# historical artifacts surfaced through the shared edits-backup interface.
_LEGACY_BACKUP_DIRNAME = ".ha_mcp_tools_backups"

# Legacy .bak filename shape: "<safe_name>.<YYYYMMDD>_<HHMMSS>.bak", where
# safe_name = os.path.normpath(rel_path).replace(os.sep, "_") and the timestamp
# is strftime("%Y%m%d_%H%M%S") (the pre-#1579 component-side naming).
_LEGACY_BACKUP_RE = re.compile(r"^(?P<safe>.+)\.(?P<date>\d{8})_(?P<time>\d{6})\.bak$")


def _decode_legacy_backup_name(filename: str) -> dict[str, Any]:
    """Best-effort inverse of the legacy .bak naming.

    Returns ``{file_path, timestamp, path_ambiguous}``. ``file_path`` is the
    config-relative path the backup was taken from, or ``None`` when it can't be
    recovered. The original naming replaced every ``os.sep`` with ``_``, which is
    lossy: only the flat allowlisted shapes (``configuration.yaml``,
    ``packages/<name>.yaml``, ``themes/<name>.yaml``) invert unambiguously. A
    nested path (``packages/sub/foo.yaml``), a literal underscore in the name, or
    a pre-fix ``www/yaml_backups`` artifact cannot be distinguished and is
    flagged ``path_ambiguous`` so a caller never restores to a guessed target.
    """
    match = _LEGACY_BACKUP_RE.match(filename)
    if not match:
        return {"file_path": None, "timestamp": None, "path_ambiguous": True}
    safe = match.group("safe")
    timestamp = f"{match.group('date')}_{match.group('time')}"
    if safe in ALLOWED_YAML_CONFIG_FILES:
        return {"file_path": safe, "timestamp": timestamp, "path_ambiguous": False}
    # Mirror the subdir write-allowlist in handle_edit_yaml_config
    # (packages/*.yaml + themes/*.yaml) — keep in sync if that set grows. A
    # pattern outside this list just stays path_ambiguous (still listable/
    # readable), so drift degrades safely rather than mis-restoring.
    for prefix in ("packages", "themes"):
        if safe.startswith(f"{prefix}_"):
            rest = safe.removeprefix(f"{prefix}_")
            return {
                "file_path": f"{prefix}/{rest}",
                "timestamp": timestamp,
                # A remaining "_" is either a literal char or a collapsed nested
                # separator — indistinguishable, so the decode can't be trusted.
                "path_ambiguous": "_" in rest,
            }
    return {"file_path": None, "timestamp": timestamp, "path_ambiguous": True}


def _list_legacy_backups_sync(legacy_dir: Path) -> list[dict[str, Any]]:
    """Enumerate regular ``.bak`` files in the legacy backup dir (no recursion).

    Skips directories, symlinks, and non-``.bak`` strays (mirrors the GHSA
    migration's filter). Newest first by mtime.
    """
    if not legacy_dir.is_dir():
        return []
    backups: list[dict[str, Any]] = []
    for item in legacy_dir.iterdir():
        if not item.is_file() or item.is_symlink() or item.suffix != ".bak":
            continue
        stat = item.stat()
        decoded = _decode_legacy_backup_name(item.name)
        backups.append(
            {
                "filename": item.name,
                "file_path": decoded["file_path"],
                "path_ambiguous": decoded["path_ambiguous"],
                "timestamp": decoded["timestamp"],
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
        )
    backups.sort(key=lambda backup: backup["modified"], reverse=True)
    return backups


def _read_legacy_backup_sync(target_file: Path) -> dict[str, Any]:
    """Read a single legacy ``.bak`` file (text) for one executor offload.

    Rejects symlinks up front (defense-in-depth for this surface), then reuses
    ``_read_file_sync`` for the exists/is_file/stat/read.
    """
    if target_file.is_symlink():
        return {"_error": "not_a_file"}
    return _read_file_sync(target_file)


async def _run_config_check(hass: HomeAssistant, rel_path: str) -> dict[str, Any]:
    """Validate the whole HA config after a write and return fields to merge
    into the write response.

    Uses ``async_check_ha_config_file``, which returns an error string when the
    resulting config is invalid (and ``None`` when it is valid). The
    ``homeassistant.check_config`` *service* is deliberately not used: it is
    registered with ``SupportsResponse.NONE`` and signals errors only by
    raising, so a response-returning call to it always failed and the check
    never actually ran (#1660).

    Never raises: a check that cannot run surfaces as
    ``config_check: "unavailable"`` rather than failing an already-completed
    write.
    """
    try:
        errors = await async_check_ha_config_file(hass)
    except Exception as check_err:
        _LOGGER.warning(
            "Config check unavailable after editing %s: %s", rel_path, check_err
        )
        return {"config_check": "unavailable", "config_check_error": str(check_err)}
    if errors:
        _LOGGER.warning(
            "Config check found errors after editing %s: %s", rel_path, errors
        )
        return {"config_check": "errors", "config_check_errors": errors}
    return {"config_check": "ok"}


def _build_edit_yaml_config_handler(
    hass: HomeAssistant,
) -> Callable[[ServiceCall], Awaitable[dict[str, Any]]]:
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
        # Caller-provided per-key opt-out for PACKAGES_ONLY_YAML_KEYS.
        # Filter to the recognised set so a caller that types
        # ``automatoin`` doesn't accidentally pass through; only
        # actually-known keys count.
        disabled_packages_keys = {
            key
            for key in call.data.get("disabled_packages_keys", [])
            if key in PACKAGES_ONLY_YAML_KEYS
        }

        # Validate file path — only configuration.yaml, packages/*.yaml, and themes/*.yaml
        normalized = os.path.normpath(rel_path)  # noqa: ASYNC240
        if normalized.startswith("..") or normalized.startswith("/"):
            return {
                "success": False,
                "error": "Path traversal is not allowed.",
            }

        is_config_yaml = normalized in ALLOWED_YAML_CONFIG_FILES
        is_theme = fnmatch.fnmatch(normalized, "themes/*.yaml")
        # Package files may live under any folder the user binds via
        # ``homeassistant: packages: !include_dir_named <folder>`` — not just the
        # default ``packages`` (issue #1854). The configured folder(s) are
        # detected at runtime (parsing configuration.yaml), so only do that work
        # when the target isn't already a config-root or theme file. The set
        # always includes ``"packages"`` as a fallback.
        package_dirs: set[str] = set()
        is_package = False
        if not is_config_yaml and not is_theme:
            package_dirs = await _detect_package_dirs(hass)
            is_package = _path_in_package_dir(normalized, package_dirs)
        if not is_config_yaml and not is_package and not is_theme:
            allowed_pkg = ", ".join(
                f"{folder}/*.yaml" for folder in sorted(package_dirs)
            )
            return {
                "success": False,
                "error": (
                    f"File '{rel_path}' is not allowed. "
                    f"Only {', '.join(ALLOWED_YAML_CONFIG_FILES)}, {allowed_pkg}, and themes/*.yaml are supported."
                ),
            }

        # Symlink-safe containment (parity with the read path, issue #1586): the
        # write target is ``config_dir / normalized``, so a package/theme folder
        # that is (or contains) a symlink escaping the config dir — or a path
        # resolving into the deny floor (.storage / secrets.yaml) — must be
        # rejected before writing, exactly as read_file already does.
        if _violates_deny_floor(config_dir, normalized) or not _resolves_within(
            config_dir, rel_path
        ):
            _LOGGER.warning("Rejected YAML edit escaping the config dir: %s", rel_path)
            return {
                "success": False,
                "error": (
                    f"Path '{rel_path}' resolves outside the config directory or "
                    "into a protected location and cannot be edited."
                ),
            }

        # ``themes/*.yaml`` matches dot-prefixed paths (fnmatch's ``*`` matches a
        # leading ``.`` and spans ``/``), but HA's ``!include_dir_merge_named``
        # loader skips them: ``annotatedyaml``'s ``_find_files`` filters every
        # walked directory AND file basename through ``_is_file_valid`` (which
        # is ``return not name.startswith(".")``), so a dot-prefixed file
        # (``themes/.hidden.yaml``) OR directory (``themes/.foo/bar.yaml``) is
        # pruned and the theme never loads — a phantom ``reload_performed``.
        # Reject if any path segment is dot-prefixed, mirroring the loader,
        # rather than only checking the basename.
        if is_theme and any(seg.startswith(".") for seg in normalized.split("/")):
            return {
                "success": False,
                "error": (
                    f"Theme path '{rel_path}' has a dot-prefixed file or "
                    "directory segment. Home Assistant's !include_dir_merge_named "
                    "skips any path segment whose name starts with '.', so it "
                    "would never load. Use a path with no dot-prefixed segment."
                ),
            }

        # Whole-file replace (restore a legacy .bak wholesale, #1579). The
        # content IS the entire file, so this bypasses the per-key merge — but
        # reuses the path allowlist already enforced above, plus YAML
        # validation, the atomic temp-write+rename, and the post-write config
        # check. No new files become writable; yaml_path is ignored here.
        if action == "replace_file":
            if not content:
                return {
                    "success": False,
                    "error": "'content' is required for action 'replace_file'.",
                }
            try:
                whole = await hass.async_add_executor_job(
                    lambda: make_yaml().load(StringIO(content))
                )
            except YAMLError as err:
                return {"success": False, "error": f"Invalid YAML content: {err}"}
            if not isinstance(whole, dict):
                return {
                    "success": False,
                    "error": "Whole-file content must be a YAML mapping at the root.",
                }
            target_file = config_dir / normalized
            try:
                # Write the backup content verbatim (faithful restore). Bundles
                # mkdir + atomic temp-write+rename + stat into one offload, like
                # _write_file_sync.
                write_meta = await hass.async_add_executor_job(
                    _replace_file_sync, target_file, content
                )
            except PermissionError:
                _LOGGER.error("Permission denied editing: %s", rel_path)
                return {
                    "success": False,
                    "error": f"Permission denied: {rel_path}",
                }
            except OSError as err:
                _LOGGER.error("Error editing YAML config %s: %s", rel_path, err)
                return {"success": False, "error": str(err)}

            _LOGGER.info("YAML config restored whole-file: %s", rel_path)
            restore_result: dict[str, Any] = {
                "success": True,
                "file": rel_path,
                "action": action,
                "yaml_path": yaml_path,
                # Verbatim whole-file write; shares the write-path response
                # shape so ``written`` stays a reliable discriminator.
                "written": True,
                "size": write_meta["size"],
                "modified": datetime.fromtimestamp(write_meta["mtime"]).isoformat(),
                # A whole-file restore can touch any number of keys; a restart
                # is the always-correct activation path.
                "post_action": "restart_required",
            }
            restore_result.update(await _run_config_check(hass, rel_path))
            return restore_result

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
            yaml_path, is_package=is_package, is_theme=is_theme
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
            # Theme content must be a YAML mapping (theme variables)
            if kind == "theme" and not isinstance(parsed_content, dict):
                return {
                    "success": False,
                    "error": "Theme content must be a YAML mapping (theme variables).",
                }

        target_file = config_dir / normalized

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

            # Pre-write backups are captured MCP-side by ha-mcp's shared
            # auto-backup layer (#1579): ha_config_set_yaml snapshots the
            # prior key state before calling this service, so the edit is
            # restorable via ha_manage_backup(scope="edits"). The component
            # no longer writes its own .ha_mcp_tools_backups/ copy. (Pre-fix
            # backups already on disk there stay readable; the separate
            # GHSA-g39v-cvjh-8fpf startup migration is unaffected.)

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
                # Single-key apply logic, shared by plain config keys and
                # theme names (kind == "theme"): a theme file is a mapping of
                # theme-name -> variables, and theme content is pre-validated
                # as a mapping above, so the list-merge arm never fires for it.
                yaml_key = path_parts[0]
                label = "Theme" if kind == "theme" else "Key"
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
                                    f"Type mismatch for {label.lower()} '{yaml_key}': "
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
                            "error": f"{label} '{yaml_key}' not found in '{rel_path}'.",
                        }
                    del data[yaml_key]

            # Serialize back to YAML, keeping the file's dominant top-level
            # sequence-dash style (#1720: "changed indentation I didn't ask
            # to change"). ruamel supports only ONE sequence style per dump,
            # so in a MIXED-style file the minority-style sequences are
            # normalized to the first-detected style — that collateral is
            # counted below and surfaced as a warning + visible in ``diff``.
            seq_style = detect_seq_indent(raw_content)

            def _dump() -> str:
                ry = make_yaml()
                apply_seq_indent(ry, seq_style)
                return yaml_dumps(ry, data)

            try:
                new_content = await hass.async_add_executor_job(_dump)
            except YAMLError as err:
                return {
                    "success": False,
                    "error": f"Failed to serialize YAML: {err}",
                }

            # Validate the result parses cleanly AND round-trips to the
            # same data we intend to write. The emitter must not alter any
            # parsed value (e.g. by re-wrapping a long line inside a folded
            # scalar, which embeds a literal newline in an untouched string
            # — #1720); if it would, refuse to write rather than corrupt
            # the file silently.
            try:
                reparsed = await hass.async_add_executor_job(
                    lambda: make_yaml().load(StringIO(new_content))
                )
            except YAMLError as err:
                return {
                    "success": False,
                    "error": f"Generated YAML failed validation: {err}",
                }
            if reparsed != data:
                return {
                    "success": False,
                    "error": (
                        "Aborted: re-serializing the file would have altered "
                        "content outside the requested edit (the YAML "
                        "serializer changed at least one value elsewhere in "
                        "the file). The file was NOT modified."
                    ),
                }

            diff_text = _unified_diff(raw_content, new_content, rel_path)
            reindent_count = _count_reindented_lines(diff_text)

            require_confirm = bool(call.data.get("require_confirm", False))
            supplied_token = call.data.get("confirm_token")
            expected_token = _confirm_token(normalized, new_content)
            if require_confirm and supplied_token != expected_token:
                preview: dict[str, Any] = {
                    "success": True,
                    "preview": True,
                    "written": False,
                    "file": rel_path,
                    "action": action,
                    "yaml_path": yaml_path,
                    "diff": diff_text,
                    "confirm_token": expected_token,
                    "message": (
                        "Preview only — NOTHING was written. Review the diff "
                        "for unintended changes outside the requested edit, "
                        "then repeat the exact same call with confirm_token "
                        "to apply."
                    ),
                }
                if reindent_count:
                    preview["warnings"] = [_reindent_warning(reindent_count)]
                if supplied_token is not None:
                    preview["confirm_token_mismatch"] = True
                    preview["message"] = (
                        "confirm_token did not match — the file changed "
                        "since the preview (or the token was wrong). NOTHING "
                        "was written. Review the fresh diff and retry with "
                        "the new confirm_token."
                    )
                return preview

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
                "written": True,
                "diff": diff_text,
            }
            if reindent_count:
                result["warnings"] = [_reindent_warning(reindent_count)]

            # Surface the post-edit action required to activate the change
            if kind == "lovelace_dashboard":
                post_info = {"post_action": "restart_required"}
            elif kind == "theme":
                # Trigger frontend.reload_themes to load the theme change
                try:
                    await hass.services.async_call(
                        "frontend",
                        "reload_themes",
                        {},
                        blocking=True,
                    )
                    post_info = {
                        "post_action": "reload_performed",
                        "reload_service": "frontend.reload_themes",
                    }
                except Exception as reload_err:
                    post_info = {
                        "post_action": "reload_available",
                        "reload_service": "frontend.reload_themes",
                        "reload_error": str(reload_err),
                    }
                    _LOGGER.warning(
                        "frontend.reload_themes failed after theme edit: %s",
                        reload_err,
                    )
            else:
                post_info = YAML_KEY_POST_ACTIONS.get(
                    path_parts[0], YAML_KEY_DEFAULT_POST_ACTION
                )
            result.update(post_info)
            result.update(await _run_config_check(hass, rel_path))
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


def _extract_yaml_subtree(content: str, yaml_path: str) -> str | None:
    """Return the YAML subtree at the dotted ``yaml_path`` as round-trip text.

    Used by ``read_file``'s optional ``yaml_path`` to let ha-mcp's per-edit
    auto-backup snapshot the prior value of a key before ``edit_yaml_config``
    changes it (#1579). Runs here, in the component, because the round-trip
    parse needs ``ruamel`` (a component requirement) which the MCP server's
    runtime does not carry. Comments and HA tags (``!secret`` / ``!include``)
    are preserved. Returns ``None`` when the root is not a mapping or the key
    is absent (new-key write — nothing to snapshot); malformed YAML also
    yields ``None`` (the edit itself would then fail and report the error).
    """
    try:
        ry = make_yaml()
        # The per-thread cached instance may carry a sequence style applied
        # by a prior edit's dump on this executor thread — reset it so the
        # snapshot never inherits another file's indentation.
        apply_seq_indent(ry, None)
        node = ry.load(StringIO(content))
        for seg in yaml_path.split("."):
            if not isinstance(node, dict) or seg not in node:
                return None
            node = node[seg]
        return yaml_dumps(ry, node)
    except YAMLError:
        return None


def _parse_and_validate_yaml_path(
    yaml_path: str,
    *,
    is_package: bool = False,
    is_theme: bool = False,
) -> tuple[str, tuple[str, ...], str | None]:
    """Parse and validate a yaml_path argument.

    Three accepted shapes:
    1. Single segment in ALLOWED_YAML_KEYS -> kind='single'
       When ``is_package=True``, single segments in PACKAGES_ONLY_YAML_KEYS
       (automation, script, scene) are also accepted.
    2. Exactly 'lovelace.dashboards.<url_path>' -> kind='lovelace_dashboard'
    3. Single segment theme name (no dots) when ``is_theme=True`` -> kind='theme'

    Returns (kind, parts, error). On error, kind is '' and parts is ().
    """
    if not yaml_path or not isinstance(yaml_path, str):
        return "", (), "yaml_path must be a non-empty string"

    parts = tuple(yaml_path.split("."))

    # Shape 3: theme name (single segment, no dots)
    if is_theme:
        if len(parts) == 1:
            return "theme", parts, None
        return (
            "",
            (),
            (
                f"Theme name '{yaml_path}' cannot contain dots. "
                "Provide a simple theme name (e.g., 'my-theme', not 'my.theme')."
            ),
        )

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

    new_dir = config_dir / _LEGACY_BACKUP_DIRNAME
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
    """Set up a config entry, dispatching on its entry type.

    The integration serves two entry types under one domain: the ``tools``
    services entry (the original component) and the in-process ``server`` entry
    (issue #1527). A missing ``entry_type`` means ``tools`` so pre-existing
    entries keep working across the component update with no migration.
    """
    # Lazy import (matches the embedded chain's own convention in
    # embedded_entry.py): keeps homeassistant.const's import surface out of
    # every hermetic unit test that merely imports this package, not just the
    # ones that actually call async_setup_entry.
    from .install_source_check import async_schedule_install_source_check

    # Runs for BOTH entry types: HACS delivers the component as a whole, not
    # just the server entry, so a legacy-repo install needs detecting either way.
    async_schedule_install_source_check(hass)

    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
        from .embedded_entry import async_setup_server_entry

        return await async_setup_server_entry(hass, entry)
    return await _async_setup_tools_entry(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry, dispatching on its entry type."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
        from .embedded_entry import async_unload_server_entry

        return await async_unload_server_entry(hass, entry)
    return await _async_unload_tools_entry(hass, entry)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config-entry removal, dispatching on its entry type.

    The server entry revokes the credentials it provisioned; the tools entry has
    nothing to clean up beyond what unload already did.
    """
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
        from .embedded_entry import async_remove_server_entry

        await async_remove_server_entry(hass, entry)


async def _async_setup_tools_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the HA MCP Tools services from a config entry."""
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
        yaml_path = call.data.get("yaml_path")

        # Security check. Package files may live under a non-default folder
        # (issue #1854), so resolve the configured folder(s) — the same way the
        # YAML editor does — and honour them here too, otherwise the pre-write
        # backup snapshot (a read of the target) blocks edits to those files.
        extra_dirs = _current_extra_dirs()
        package_dirs = await _detect_package_dirs(hass)
        if not _is_path_allowed_for_read(
            config_dir, rel_path, extra_dirs, package_dirs
        ):
            _LOGGER.warning("Attempted to read disallowed path: %s", rel_path)
            allowed_patterns = (
                ALLOWED_READ_FILES
                + [f"{d}/**" for d in ALLOWED_READ_DIRS]
                + [f"{d}/*.yaml" for d in sorted(package_dirs)]
                + ["custom_components/**/*.py"]
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

        response: dict[str, Any] = {
            "success": True,
            "path": rel_path,
            "content": content,
            "size": stat_size,
            "modified": modified_dt.isoformat(),
        }
        if yaml_path:
            response["subtree"] = await hass.async_add_executor_job(
                _extract_yaml_subtree, content, yaml_path
            )
        return response

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
        """Replace the user-configurable extra directories (issues #1567, #1586).

        Receives the FULL replacement list. Each entry is validated and
        normalized; entries that use traversal, escape the config directory, or
        hit the non-overridable deny floor are dropped and reported in
        ``rejected``. Config-relative dirs (#1567) and the fixed HAOS
        sibling-volume roots (#1586 — see ``const.ALLOWED_VOLUME_ROOTS``) are
        accepted; any other absolute path is dropped. Persists to .storage AND
        updates hass.data so enforcement applies live with no HA restart.
        Caller-token + admin gated.
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

    legacy_backup_dir = config_dir / _LEGACY_BACKUP_DIRNAME

    async def handle_list_legacy_backups(call: ServiceCall) -> ServiceResponse:
        """List pre-#1579 YAML backups in .ha_mcp_tools_backups/ (read-only)."""
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_LIST_LEGACY_BACKUPS, backups=[])
        try:
            backups = await hass.async_add_executor_job(
                _list_legacy_backups_sync, legacy_backup_dir
            )
        except OSError as err:
            _LOGGER.error("Error listing legacy backups: %s", err)
            return {"success": False, "error": str(err), "backups": []}
        return {"success": True, "backups": backups, "count": len(backups)}

    async def handle_read_legacy_backup(call: ServiceCall) -> ServiceResponse:
        """Read one pre-#1579 YAML backup by filename (read-only)."""
        if not _caller_token_ok(hass, call):
            return _unauthorized_response(SERVICE_READ_LEGACY_BACKUP)
        filename = call.data["filename"]
        # Bare basename only, .bak only, confined to the legacy dir. Reject any
        # path separator, traversal, or symlink escape before touching the FS —
        # this surface only ever serves files inside .ha_mcp_tools_backups/.
        if (
            not filename
            or "/" in filename
            or "\\" in filename
            or filename in (".", "..")
            or not filename.endswith(".bak")
            or not _resolves_within(legacy_backup_dir, filename)
        ):
            return {
                "success": False,
                "error": (
                    f"Invalid backup filename {filename!r}. Pass a bare "
                    f"<name>.bak from {_LEGACY_BACKUP_DIRNAME}/ "
                    "(no path separators)."
                ),
            }
        target_file = legacy_backup_dir / filename
        try:
            result = await hass.async_add_executor_job(
                _read_legacy_backup_sync, target_file
            )
        except UnicodeDecodeError:
            _LOGGER.error("Cannot read binary legacy backup: %s", filename)
            return {
                "success": False,
                "error": f"Cannot read binary backup: {filename}.",
            }
        except OSError as err:
            _LOGGER.error("Error reading legacy backup %s: %s", filename, err)
            return {"success": False, "error": str(err)}

        err_kind = result.get("_error")
        if err_kind == "not_found":
            return {"success": False, "error": f"Backup does not exist: {filename}"}
        if err_kind == "not_a_file":
            return {"success": False, "error": f"Path is not a file: {filename}"}

        modified_dt = datetime.fromtimestamp(result["mtime"])
        decoded = _decode_legacy_backup_name(filename)
        return {
            "success": True,
            "filename": filename,
            "file_path": decoded["file_path"],
            "path_ambiguous": decoded["path_ambiguous"],
            "timestamp": decoded["timestamp"],
            "content": result["content"],
            "size": result["size"],
            "modified": modified_dt.isoformat(),
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

    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_LEGACY_BACKUPS,
        handle_list_legacy_backups,
        schema=SERVICE_LIST_LEGACY_BACKUPS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_LEGACY_BACKUP,
        handle_read_legacy_backup,
        schema=SERVICE_READ_LEGACY_BACKUP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    # Register the in-process ha_mcp_tools/* WebSocket commands (info + search)
    # the server calls behind a capability gate. Idempotent, and independent of
    # the caller-token gate above — HA core authenticates the WS connection and
    # @require_admin gates each command (see websocket_api.py).
    async_register_commands(hass)

    # Entry-level finalization: pick up the #1853 rename on existing installs and
    # give the tools entry a device. Both are independent of the services above
    # and cosmetic to the integration's core value, so a read failure degrades
    # rather than blocking setup.
    # Retitle an entry still carrying the pre-rename default; a user-customized
    # title is left untouched (only the exact old default migrates). The tools
    # entry registers no update listener, so this async_update_entry cannot
    # trigger a reload loop — and the guard is idempotent regardless (the title
    # no longer matches on the next setup).
    if entry.title == TOOLS_ENTRY_LEGACY_TITLE:
        hass.config_entries.async_update_entry(entry, title=TOOLS_ENTRY_TITLE)

    # Register a device (parity with the server entry, which gets one via
    # update.py's DeviceInfo). Tied to the config entry, so HA removes it with
    # the entry — no unload cleanup needed. The component version comes from the
    # manifest like the options-form version hint, degrading to the compiled-in
    # COMPONENT_VERSION if that read fails so a manifest hiccup never breaks setup.
    from homeassistant.helpers import device_registry as dr

    component_version = COMPONENT_VERSION
    try:
        integration = await async_get_integration(hass, DOMAIN)
        component_version = str(integration.version)
    except Exception as err:
        _LOGGER.debug(
            "Could not read the component version for the tools device: %s", err
        )
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=TOOLS_ENTRY_TITLE,
        manufacturer="homeassistant-ai",
        model="File & YAML editing services",
        sw_version=component_version,
        configuration_url="https://github.com/homeassistant-ai/ha-mcp",
    )

    _LOGGER.info("HA MCP Tools initialized with file management services")
    return True


async def _async_unload_tools_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the HA MCP Tools services config entry."""
    # Remove all services
    hass.services.async_remove(DOMAIN, SERVICE_EDIT_YAML_CONFIG)
    hass.services.async_remove(DOMAIN, SERVICE_LIST_FILES)
    hass.services.async_remove(DOMAIN, SERVICE_READ_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_WRITE_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_FILE)
    hass.services.async_remove(DOMAIN, SERVICE_GET_CALLER_TOKEN)
    hass.services.async_remove(DOMAIN, SERVICE_GET_ALLOWED_PATHS)
    hass.services.async_remove(DOMAIN, SERVICE_SET_ALLOWED_PATHS)
    hass.services.async_remove(DOMAIN, SERVICE_LIST_LEGACY_BACKUPS)
    hass.services.async_remove(DOMAIN, SERVICE_READ_LEGACY_BACKUP)

    # Drop the cached token + allowlist from hass.data so a subsequent
    # setup_entry re-reads from storage (covers the reload-after-rotate path).
    domain_data = hass.data.get(DOMAIN)
    if isinstance(domain_data, dict):
        domain_data.pop(_HASS_DATA_TOKEN_KEY, None)
        domain_data.pop(_HASS_DATA_ALLOWED_PATHS_KEY, None)
    return True
