"""Auto-backup of HA config domains before write/destructive operations.

Closes #1288. Captures per-entity pre-write state to a local directory.
Best-effort: failures log a WARNING but never block the underlying write.

Storage path resolution
-----------------------
``Settings.auto_backup_dir`` overrides; otherwise defaults to
``/data/ha_mcp_backups`` in the add-on (SUPERVISOR_TOKEN set + ``/data``
exists), else ``${XDG_DATA_HOME:-~/.local/share}/ha_mcp/backups``.

File format
-----------
One YAML file per snapshot, named
``<domain>.<safe_entity_id>.<YYYYMMDD_HHMMSS>.yaml``::

    # ha_mcp_backup
    schema_version: 1
    domain: automation
    entity_id: kitchen_lights
    captured: 2026-05-21T15:30:00+00:00
    tool: ha_config_set_automation
    config:
      alias: Kitchen lights
      trigger: ...

Domain handlers
---------------
``DomainHandler`` pairs a backup-domain string with two coroutines:

- ``fetch(client, entity_id) -> config dict``  — read pre-write state
- ``restore(client, entity_id, config) -> result`` — re-apply the saved state

One handler per backed-up domain. Helper types each register their own
handler keyed ``helper_<type>`` since each helper type has a distinct
WS endpoint shape.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

import yaml  # type: ignore[import-untyped]
from fastmcp.exceptions import ToolError

from .client.rest_client import HomeAssistantConnectionError, HomeAssistantError

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")

# Expected failure modes for the capture pipeline. Anything outside this
# tuple is a bug (TypeError, AttributeError, KeyError, etc.) and should
# propagate to the wrapped tool's caller, not be silently swallowed.
# ``ToolError`` is included because the fetch path now delegates to the
# tool-layer ``_get_<entity>_config_internal`` helpers, which raise
# ``ToolError`` for HA-side fetch failures.
_CAPTURE_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    HomeAssistantError,
    OSError,
    TimeoutError,
    asyncio.TimeoutError,
    ConnectionError,
    yaml.YAMLError,
    ToolError,
)


class MandatoryBackupError(Exception):
    """A required pre-write snapshot could not be captured.

    Raised by ``maybe_snapshot(..., mandatory=True)`` when capture genuinely
    fails (fetch error, snapshot-write failure such as disk-full, or an
    unusable backup directory) — as opposed to a legitimate skip (nothing to
    snapshot for a new file/key). Deliberately a plain ``Exception`` and NOT a
    member of ``_CAPTURE_TRANSIENT_ERRORS`` so the ``@with_auto_backup``
    decorator's best-effort handler can't swallow it; the decorator maps it to
    a structured ``BACKUP_CAPTURE_FAILED`` error that fails the write closed.

    ``suggestions`` carries remediation surfaced in that structured error.
    """

    def __init__(self, message: str, *, suggestions: list[str] | None = None) -> None:
        super().__init__(message)
        self.suggestions = suggestions or []


# Soft cap on per-entity throttle/lock tracker size. Auto-pruning kicks
# in once exceeded; protects long-running servers from unbounded growth
# while staying well above any realistic HA install (typical: dozens to
# hundreds of entities edited per session).
_TRACKER_SOFT_CAP = 10_000
_TRACKER_PRUNE_BATCH = 1_000

# Filename pattern: <domain>.<safe_entity_id>.<YYYYMMDD_HHMMSS>.yaml
# The middle ``.`` separators make the timestamp rsplit reliable even
# when entity_id contains dots (after sanitization, dots are kept).
_FILENAME_RE = re.compile(
    r"^(?P<domain>[A-Za-z0-9_]+)\."
    r"(?P<entity_id>[A-Za-z0-9._-]+)\."
    r"(?P<ts>\d{8}_\d{6})\.yaml$"
)

# Domains whose snapshot ``config`` is raw text (file/YAML content) rather
# than a structured dict. They carry a ``kind: "text"`` marker in the
# snapshot payload and diff via a unified text diff instead of JSON-Patch
# (#1579 PR2). Everything else is the implicit ``"dict"`` kind.
#
# ``yaml_file`` is a whole-file YAML-config snapshot: same fetch as ``file``
# (read_file), but restored via edit_yaml_config(action="replace_file") because
# write_file rejects config files. It backs the pre-restore safety snapshot and
# the legacy-store restore (#1579).
_TEXT_DOMAINS = frozenset({"file", "yaml", "yaml_file"})

# Pre-#1579 backups (``.ha_mcp_tools_backups/*.bak``) are surfaced through the
# same scope="edits" actions under a synthetic name ``legacy:<filename>``. The
# ":" never appears in a real snapshot filename (see ``_FILENAME_RE``), so the
# prefix is an unambiguous routing discriminator.
LEGACY_PREFIX = "legacy:"

# Discriminator values for the snapshot ``kind`` marker and the
# DiffResponse/DiffResponseText union. Typed as ``Literal`` so they satisfy
# the discriminated-union fields without widening to ``str``.
_TEXT_KIND: Literal["text"] = "text"
_DICT_KIND: Literal["dict"] = "dict"

# Output cap for the text (file/YAML) diff. Bounded like ``_MAX_PATCH_OPS``
# so a pathological whole-file rewrite stays token-friendly; the response
# sets ``truncated`` and points the caller at the full snapshot via view.
_MAX_DIFF_LINES = 400


# ----------------------------- handler protocol -----------------------------

FetchFn = Callable[[Any, str], Awaitable[Any]]
RestoreFn = Callable[[Any, str, Any], Awaitable[Any]]


@dataclass(frozen=True)
class DomainHandler:
    """Per-domain fetch + restore pair.

    ``domain`` is the backup-domain key — exactly what the decorator
    passes as ``domain=`` and what gets baked into snapshot filenames.
    """

    domain: str
    fetch: FetchFn
    restore: RestoreFn


# ----------------------------- backup manager -------------------------------


def _safe_entity_id(entity_id: str) -> str:
    """Sanitize an entity id for use in a filename.

    Replaces any character outside ``[A-Za-z0-9._-]`` with ``_``. Path
    separators get caught by this (the regex excludes both ``/`` and ``\\``).
    Strips leading dots to prevent dotfile collisions.
    """
    if not entity_id:
        return "_"
    cleaned = _SAFE_ID_RE.sub("_", entity_id).lstrip(".")
    return cleaned or "_"


def _now_ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_default_dir() -> Path:
    """Pick a sane default backup directory for the current deployment mode."""
    # Addon detection: Supervisor sets SUPERVISOR_TOKEN AND /data exists.
    if os.environ.get("SUPERVISOR_TOKEN") and Path("/data").is_dir():
        return Path("/data/ha_mcp_backups")
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "ha_mcp" / "backups"
    return (Path.home() / ".local" / "share" / "ha_mcp" / "backups").resolve()


class BackupManager:
    """Per-entity snapshot manager. One instance per server, cached on client."""

    def __init__(self, settings: Any, client: Any) -> None:
        self._settings = settings
        self._client = client
        self._handlers: dict[str, DomainHandler] = {}
        # Throttle tracker: maps "domain:entity_id" -> monotonic-time of
        # the last successful capture. Auto-pruned past _TRACKER_SOFT_CAP
        # so a long-running server editing many distinct entities cannot
        # leak memory through this dict.
        self._last_snapshot: dict[str, float] = {}
        # Per-key locks serialize fetch+write for the same entity, so
        # two concurrent writes to the same automation can't race and
        # produce duplicate snapshots within the throttle window. Kept
        # for the manager's lifetime — each lock is tiny (~64 bytes);
        # removing a lock while another task is awaiting it would race.
        self._locks: dict[str, asyncio.Lock] = {}
        self._init_dir_error: str | None = None
        self._dir = self._resolve_dir()

    # ----- configuration -------------------------------------------------

    def _resolve_dir(self) -> Path:
        configured = (getattr(self._settings, "auto_backup_dir", "") or "").strip()
        path = Path(configured).expanduser() if configured else _resolve_default_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            self._init_dir_error = f"{type(err).__name__}: {err}"
            logger.warning(
                "Auto-backup: could not create backup dir %s: %s. "
                "Captures will be silently skipped until startup is rerun.",
                path,
                err,
            )
        return path

    @property
    def backup_dir(self) -> Path:
        return self._dir

    @property
    def enabled(self) -> bool:
        # An unreachable backup dir effectively disables the feature —
        # surface that through ``enabled`` so callers (the list endpoint,
        # the settings UI status panel) report the truth instead of
        # advertising "enabled" while every capture silently no-ops.
        if self._init_dir_error is not None:
            return False
        return bool(getattr(self._settings, "enable_auto_backup", False))

    @property
    def init_dir_error(self) -> str | None:
        """The reason the backup dir could not be created, or None."""
        return self._init_dir_error

    @property
    def throttle_seconds(self) -> int:
        return max(
            0, int(getattr(self._settings, "auto_backup_throttle_minutes", 0)) * 60
        )

    @property
    def retain_per_entity(self) -> int:
        return max(
            1, int(getattr(self._settings, "auto_backup_retain_per_entity", 100))
        )

    # ----- handler registration ------------------------------------------

    def register(self, handler: DomainHandler) -> None:
        self._handlers[handler.domain] = handler

    def handler_for(self, domain: str) -> DomainHandler | None:
        return self._handlers.get(domain)

    def supported_domains(self) -> list[str]:
        """Return the sorted list of registered backup-domain keys.

        Public accessor for callers that need to surface "what domains
        are supported?" in user-facing error messages (e.g., the
        ``ha_manage_backup(scope='edits', action='create')`` handler
        when ``domain`` is unknown). Sorted for stable output.
        """
        return sorted(self._handlers.keys())

    # ----- capture -------------------------------------------------------

    async def maybe_snapshot(
        self,
        domain: str,
        entity_id: str,
        *,
        tool_name: str | None = None,
        force: bool = False,
        mandatory: bool = False,
    ) -> Path | None:
        """Capture a snapshot for ``domain:entity_id`` if throttle elapsed.

        Returns the Path written or None if skipped. In the default
        (best-effort) mode it never raises — all errors are logged at WARNING
        and swallowed so the wrapped write can proceed regardless.

        ``mandatory=True`` makes the snapshot a precondition (file/YAML writes,
        #1579): a *genuine* capture failure — an unusable backup dir, a failed
        fetch, or a failed snapshot write (e.g. disk-full) — raises
        ``MandatoryBackupError`` so the caller can fail the write closed instead
        of overwriting un-backed-up content. A *legitimate* skip still returns
        None and lets the write proceed: nothing to snapshot for a new file/key
        (``config is None``) or a no-id create call, and a throttle skip (a
        recent snapshot already covers this entity).

        ``force=True`` bypasses the ``enable_auto_backup`` toggle and the
        per-entity throttle window so the caller can drive an explicit
        on-demand capture (the ``(edits, create)`` action on
        ``ha_manage_backup``). Init-dir errors still short-circuit —
        without a writable backup dir there's nothing to do regardless.
        The ``handler is None`` and ``config is None`` skips still
        apply (force can't conjure a snapshot for an entity that
        doesn't exist or has no registered handler).
        """
        if self._init_dir_error is not None:
            if mandatory:
                raise MandatoryBackupError(
                    f"the auto-backup directory is unusable: {self._init_dir_error}",
                    suggestions=[
                        "Check the auto-backup directory's permissions and "
                        "free space, or set HAMCP_BACKUP_DIR to a writable "
                        "path",
                    ],
                )
            return None
        if not force and not self.enabled:
            return None
        if not entity_id:
            # Create-mode call with no ID yet — nothing to back up.
            return None
        handler = self._handlers.get(domain)
        if handler is None:
            if mandatory:
                raise MandatoryBackupError(
                    f"no auto-backup handler is registered for domain {domain!r}"
                )
            logger.warning(
                "Auto-backup: no handler registered for domain %r — skipping",
                domain,
            )
            return None

        key = f"{domain}:{entity_id}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            throttle = self.throttle_seconds
            # Skip throttle if no prior snapshot exists for this key.
            # Using ``get(key, 0.0)`` would falsely block the first capture
            # whenever ``monotonic()`` < throttle (typical on a fresh process
            # in CI), since 0.0 would be treated as "last snapshot at
            # monotonic time 0".
            if (
                not force
                and throttle
                and key in self._last_snapshot
                and (now - self._last_snapshot[key]) < throttle
            ):
                return None
            try:
                config = await handler.fetch(self._client, entity_id)
            except _CAPTURE_TRANSIENT_ERRORS as err:
                # Degraded fetches (a non-list WS envelope from an
                # auth-scope change or API drift) raise rather than return
                # None — see ``_require_list``. During auto-backup we skip
                # the snapshot with a WARNING (operator-visible) instead of
                # crashing the pipeline; the same error during a diff/
                # restore propagates to the tool layer as a structured
                # error. The warning level (vs the debug log below) is what
                # distinguishes "fetch broke" from "entity didn't exist".
                if mandatory:
                    raise MandatoryBackupError(
                        f"could not read the current state of {key} to back "
                        f"it up: {type(err).__name__}: {err}"
                    ) from err
                logger.warning(
                    "Auto-backup: fetch failed for %s — %s: %s",
                    key,
                    type(err).__name__,
                    err,
                )
                return None
            if config is None:
                # Entity didn't exist at fetch time (create operation, or
                # already-deleted at remove time before our pre-fetch).
                logger.debug(
                    "Auto-backup: fetch returned None for %s — skipping snapshot",
                    key,
                )
                return None
            try:
                path = await asyncio.to_thread(
                    self._write_snapshot, domain, entity_id, config, tool_name
                )
            except (OSError, yaml.YAMLError) as err:
                if mandatory:
                    raise MandatoryBackupError(
                        f"could not write the pre-write snapshot for {key}: "
                        f"{type(err).__name__}: {err}",
                        suggestions=[
                            "Free up disk space, or delete old snapshots via "
                            "ha_manage_backup(scope='edits', action='delete')",
                        ],
                    ) from err
                logger.warning(
                    "Auto-backup: write failed for %s — %s: %s",
                    key,
                    type(err).__name__,
                    err,
                )
                return None
            self._last_snapshot[key] = now
            self._maybe_prune_trackers()
            try:
                await asyncio.to_thread(self._rotate, domain, entity_id)
            except OSError as err:
                logger.warning(
                    "Auto-backup: rotation failed for %s — %s: %s",
                    key,
                    type(err).__name__,
                    err,
                )
            return path

    def _maybe_prune_trackers(self) -> None:
        """Cap per-entity tracker growth.

        Once ``_last_snapshot`` exceeds ``_TRACKER_SOFT_CAP``, drop the
        oldest ``_TRACKER_PRUNE_BATCH`` entries. The lock map is left
        alone — removing a lock that another task is awaiting would race;
        each lock is small (~64 bytes) and HA installs never reach the
        cap in practice.
        """
        if len(self._last_snapshot) <= _TRACKER_SOFT_CAP:
            return
        # Drop the oldest entries by monotonic timestamp.
        oldest = sorted(self._last_snapshot.items(), key=lambda kv: kv[1])[
            :_TRACKER_PRUNE_BATCH
        ]
        for key, _ in oldest:
            self._last_snapshot.pop(key, None)
        logger.info(
            "Auto-backup: pruned %d oldest tracker entries (cap=%d, now=%d entries)",
            len(oldest),
            _TRACKER_SOFT_CAP,
            len(self._last_snapshot),
        )

    def _write_snapshot(
        self, domain: str, entity_id: str, config: Any, tool_name: str | None
    ) -> Path:
        safe = _safe_entity_id(entity_id)
        ts = _now_ts()
        filename = f"{domain}.{safe}.{ts}.yaml"
        target = self._dir / filename
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "domain": domain,
            "entity_id": entity_id,
            "captured": _now_iso(),
            "tool": tool_name,
            "config": config,
        }
        if domain in _TEXT_DOMAINS:
            payload["kind"] = _TEXT_KIND
        body = yaml.safe_dump(payload, default_flow_style=False, sort_keys=False)
        # Atomic write via tmp+rename
        tmp = target.with_suffix(".yaml.tmp")
        tmp.write_text("# ha_mcp_backup\n" + body)
        os.replace(str(tmp), str(target))
        logger.info("Auto-backup: wrote %s", target.name)
        return target

    def _rotate(self, domain: str, entity_id: str) -> None:
        safe = _safe_entity_id(entity_id)
        pattern = f"{domain}.{safe}.*.yaml"
        files = sorted(self._dir.glob(pattern))
        excess = len(files) - self.retain_per_entity
        for old in files[: max(0, excess)]:
            try:
                old.unlink()
            except OSError as err:
                logger.warning("Auto-backup: failed to rotate %s: %s", old.name, err)

    # ----- list / read / delete ------------------------------------------

    def list_snapshots(
        self,
        *,
        domain: str | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._dir.exists():
            return []
        # ``entity_id`` comparison: filenames hold the sanitized form (see
        # ``_safe_entity_id`` — any char outside ``[A-Za-z0-9._-]`` becomes
        # ``_``), so composite IDs like ``area:foo`` / ``automation:UUID``
        # would otherwise never match a filter passed in original form.
        # Sanitize the filter once up front so the per-file comparison is
        # symmetric: filter and ``meta["entity_id"]`` both come from the
        # same sanitization function.
        safe_filter = _safe_entity_id(entity_id) if entity_id else None
        out: list[dict[str, Any]] = []
        # Reverse-sorted glob — newest filenames sort last lexicographically,
        # so reverse=True yields newest-first.
        for path in sorted(self._dir.glob("*.yaml"), reverse=True):
            meta = self._parse_filename(path.name)
            if meta is None:
                continue
            if domain and meta["domain"] != domain:
                continue
            if safe_filter and meta["entity_id"] != safe_filter:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            meta["size"] = stat.st_size
            meta["mtime"] = stat.st_mtime
            out.append(meta)
            if limit and len(out) >= limit:
                break
        return out

    def _parse_filename(self, name: str) -> dict[str, Any] | None:
        m = _FILENAME_RE.match(name)
        if m is None:
            return None
        return {
            "name": name,
            "domain": m.group("domain"),
            "entity_id": m.group("entity_id"),
            "timestamp": m.group("ts"),
        }

    def read_snapshot(self, name: str) -> dict[str, Any]:
        path = self._resolve_snapshot_path(name)
        text = path.read_text()
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as err:
            raise ValueError(f"Snapshot {name!r} is not valid YAML: {err}") from err
        if not isinstance(data, dict):
            raise ValueError(f"Snapshot {name!r} is not a YAML mapping")
        sv = data.get("schema_version")
        if sv != SCHEMA_VERSION:
            raise ValueError(
                f"Snapshot {name!r} has unsupported schema_version={sv!r} "
                f"(expected {SCHEMA_VERSION})"
            )
        return data

    def delete_snapshot(self, name: str) -> Path:
        path = self._resolve_snapshot_path(name)
        path.unlink()
        return path

    def delete_bulk(
        self,
        *,
        domain: str | None = None,
        entity_id: str | None = None,
        older_than_days: int | None = None,
    ) -> dict[str, list[str]]:
        """Delete snapshots matching ``domain`` / ``entity_id`` / age.

        Returns a dict ``{"deleted": [...], "failed": [...]}`` so callers
        can surface partial failures to the user rather than logging
        them silently. Each failure also gets a WARNING in the server
        log so the underlying OS error is preserved.
        """
        deleted: list[str] = []
        failed: list[str] = []
        cutoff: float | None = None
        if older_than_days is not None:
            if older_than_days < 0:
                raise ValueError("older_than_days must be >= 0")
            cutoff = time.time() - (older_than_days * 86400)
        for meta in self.list_snapshots(domain=domain, entity_id=entity_id):
            if cutoff is not None and meta["mtime"] >= cutoff:
                continue
            try:
                (self._dir / meta["name"]).unlink()
                deleted.append(meta["name"])
            except OSError as err:
                failed.append(meta["name"])
                logger.warning(
                    "Auto-backup: bulk-delete failed for %s: %s", meta["name"], err
                )
        return {"deleted": deleted, "failed": failed}

    def _resolve_snapshot_path(self, name: str) -> Path:
        """Validate a snapshot name and return its absolute Path.

        Rejects any name that contains path separators or escapes the
        backup directory.
        """
        if not name or os.sep in name or "/" in name or ".." in name:
            raise ValueError(f"Invalid snapshot name: {name!r}")
        path = (self._dir / name).resolve()
        # Defence-in-depth: post-resolve, verify still under backup_dir.
        try:
            path.relative_to(self._dir.resolve())
        except ValueError as err:
            raise ValueError(f"Invalid snapshot name: {name!r}") from err
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    # ----- restore -------------------------------------------------------

    async def restore_snapshot(
        self, name: str, *, take_safety_backup: bool = True
    ) -> dict[str, Any]:
        if name.startswith(LEGACY_PREFIX):
            return await self._restore_legacy(
                name[len(LEGACY_PREFIX) :], take_safety_backup=take_safety_backup
            )
        data = await asyncio.to_thread(self.read_snapshot, name)
        domain = data["domain"]
        entity_id = data["entity_id"]
        config = data["config"]
        handler = self._handlers.get(domain)
        if handler is None:
            raise LookupError(f"No restore handler registered for domain {domain!r}")

        safety_path: Path | None = None
        if take_safety_backup:
            safety_path = await self.maybe_snapshot(
                domain, entity_id, tool_name="ha_manage_backup.restore.safety"
            )
        result = await handler.restore(self._client, entity_id, config)
        return {
            "restored_from": name,
            "domain": domain,
            "entity_id": entity_id,
            "safety_backup": safety_path.name if safety_path else None,
            "result": result,
        }

    # ----- diff ----------------------------------------------------------

    async def diff_snapshot(self, name: str) -> DiffResponse | DiffResponseText:
        """Compare a stored snapshot against the live config of the same entity.

        For structured (``"dict"``) snapshots, returns an RFC 6902-shaped
        JSON-Patch — the ops a client would apply to ``current`` to recover
        ``stored``. For text snapshots (file/YAML, ``kind: "text"``) returns
        a unified text diff instead. ``entity_missing`` flags the case where
        the target is gone from HA, so the diff has no live target to compare
        against; ``truncated`` flags that the diff exceeded its bound and was
        cut short to keep the tool response token-friendly.

        ``unchanged`` means the live config matches the snapshot — it is
        ``True`` only when the target exists *and* the diff is empty.
        Under ``entity_missing=True`` it is ``False``: there is no live
        target to match, so "no action needed" would be wrong (the
        empty diff is an artefact of the missing target, not a match).
        """
        if name.startswith(LEGACY_PREFIX):
            return await self._diff_legacy(name[len(LEGACY_PREFIX) :])
        data = await asyncio.to_thread(self.read_snapshot, name)
        domain = data["domain"]
        entity_id = data["entity_id"]
        stored = data["config"]
        handler = self._handlers.get(domain)
        if handler is None:
            raise LookupError(f"No diff handler registered for domain {domain!r}")
        current = await handler.fetch(self._client, entity_id)
        captured_at = data.get("captured")
        if data.get("kind") == _TEXT_KIND:
            return _build_text_diff_response(
                name, domain, entity_id, captured_at, str(stored), current
            )
        if current is None:
            return _build_diff_response(
                name,
                domain,
                entity_id,
                captured_at,
                entity_missing=True,
                patch=[],
                counts=_summarize_patch_counts([]),
                truncated=False,
            )
        patch: list[dict[str, Any]] = []
        truncated = _compute_json_patch(stored, current, _MAX_PATCH_OPS, patch)
        return _build_diff_response(
            name,
            domain,
            entity_id,
            captured_at,
            entity_missing=False,
            patch=patch,
            counts=_summarize_patch_counts(patch),
            truncated=truncated,
        )

    # ----- legacy store (pre-#1579 .ha_mcp_tools_backups/) ---------------

    async def list_legacy(self) -> list[dict[str, Any]]:
        """List pre-#1579 ``.bak`` backups via the component service.

        Each entry is normalized to a synthetic ``name`` (``legacy:<file>``)
        plus ``source="legacy"`` and the decode hints (``file_path`` /
        ``path_ambiguous``) so the caller can route view/diff/restore and warn
        on un-restorable (ambiguous) names. Returns ``[]`` when the component
        is too old to expose the service, so ``list`` still works.
        """
        backups = await _list_legacy_backups(self._client)
        out: list[dict[str, Any]] = []
        for b in backups:
            filename = b.get("filename")
            if not isinstance(filename, str):
                continue
            out.append(
                {
                    "name": f"{LEGACY_PREFIX}{filename}",
                    "domain": "yaml_file",
                    "entity_id": b.get("file_path"),
                    "timestamp": b.get("timestamp"),
                    "size": b.get("size"),
                    "source": "legacy",
                    "path_ambiguous": b.get("path_ambiguous", True),
                }
            )
        return out

    async def read_legacy(self, filename: str) -> dict[str, Any]:
        """Read one legacy ``.bak`` (raw content + decode hints).

        Maps a service-level failure to the same error types the edits-store
        read raises, so the tool layer's existing handling applies unchanged: a
        missing backup → ``FileNotFoundError``, anything else → ``ValueError``.
        """
        info = await _read_legacy_backup(self._client, filename)
        if not info.get("success", False):
            err = str(info.get("error", ""))
            if "does not exist" in err or "not found" in err.lower():
                raise FileNotFoundError(filename)
            raise ValueError(f"Cannot read legacy backup {filename!r}: {err}")
        return info

    async def list_edits_and_legacy(
        self,
        *,
        domain: str | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Edits-store snapshots plus pre-#1579 legacy ``.bak`` entries (#1579).

        ``list_snapshots`` is sync (dir glob, run off-thread); the legacy store
        is an async component service call — so the merge lives here, off the
        sync path, keeping the tool layer source-agnostic. Legacy maps to the
        ``yaml_file`` domain and is merged only on an unfiltered (or explicitly
        ``yaml_file``) list: its decoded ``entity_id`` is a best-effort path,
        not the sanitized form the entity filter matches on.

        Legacy entries get reserved room within ``limit`` so a full edits store
        (>= ``limit`` snapshots) can't truncate the few historical legacy
        entries out of the listing — surfacing them is the whole point.
        """
        want_legacy = domain in (None, "yaml_file") and entity_id is None
        legacy = await self.list_legacy() if want_legacy else []
        edits_limit = max(1, limit - len(legacy)) if limit and legacy else limit
        entries = await asyncio.to_thread(
            self.list_snapshots, domain=domain, entity_id=entity_id, limit=edits_limit
        )
        entries.extend(legacy)
        if limit:
            entries = entries[:limit]
        return entries

    async def _diff_legacy(self, filename: str) -> DiffResponseText:
        info = await self.read_legacy(filename)
        stored = info.get("content")
        if not isinstance(stored, str):
            stored = ""
        file_path = info.get("file_path")
        # Ambiguous/undecodable name → no trustworthy live target to diff
        # against; show the stored content as a full add (entity_missing form).
        current: Any = None
        if file_path and not info.get("path_ambiguous", True):
            current = await _fetch_file(self._client, file_path)
        return _build_text_diff_response(
            f"{LEGACY_PREFIX}{filename}",
            "yaml_file",
            file_path or filename,
            info.get("timestamp"),
            stored,
            current,
        )

    async def _restore_legacy(
        self, filename: str, *, take_safety_backup: bool = True
    ) -> dict[str, Any]:
        info = await self.read_legacy(filename)
        file_path = info.get("file_path")
        if not file_path or info.get("path_ambiguous", True):
            raise ValueError(
                f"Cannot auto-restore {filename!r}: its original path can't be "
                "unambiguously recovered from the backup filename. View it "
                "(action='view') and restore the content manually to the "
                "intended file via ha_config_set_yaml."
            )
        content = info.get("content")
        if not isinstance(content, str):
            raise ValueError(f"Legacy backup {filename!r} has no readable content")
        handler = self._handlers.get("yaml_file")
        if handler is None:
            raise LookupError("No restore handler registered for domain 'yaml_file'")
        # Pre-restore safety: capture the file's CURRENT whole content so this
        # overwrite is itself undoable. MANDATORY (fail-closed): a legacy restore
        # overwrites the entire config file, so — unlike the per-key
        # restore_snapshot — a genuine capture failure raises MandatoryBackupError
        # and the restore never runs, matching Blocker B's "block the write when a
        # backup can't be taken" (#1579). The tool layer maps that to
        # BACKUP_CAPTURE_FAILED, exactly as the @with_auto_backup write path does.
        # A legitimate "nothing to snapshot" (target file absent) still returns
        # None and proceeds. force=True bypasses the throttle/toggle.
        safety_path: Path | None = None
        if take_safety_backup:
            safety_path = await self.maybe_snapshot(
                "yaml_file",
                file_path,
                tool_name="ha_manage_backup.restore.legacy.safety",
                force=True,
                mandatory=True,
            )
        result = await handler.restore(self._client, file_path, content)
        return {
            "restored_from": f"{LEGACY_PREFIX}{filename}",
            "domain": "yaml_file",
            "entity_id": file_path,
            "safety_backup": safety_path.name if safety_path else None,
            "result": result,
        }


# --------------------------- diff helpers -----------------------------------


class DiffCounts(TypedDict):
    """Per-op-class tallies for a diff patch. ``total`` is the op count;
    ``add + remove + replace`` equals it today (see ``_summarize_patch_counts``)."""

    add: int
    remove: int
    replace: int
    total: int


class DiffResponse(TypedDict):
    """Return shape of ``BackupManager.diff_snapshot``. Both the
    entity-present and ``entity_missing`` branches build this through
    ``_build_diff_response`` so the key set can't drift between them."""

    kind: Literal["dict"]
    backup_name: str
    domain: str
    entity_id: str
    captured_at: str | None
    entity_missing: bool
    patch: list[dict[str, Any]]
    counts: DiffCounts
    unchanged: bool
    truncated: bool


def _build_diff_response(
    name: str,
    domain: str,
    entity_id: str,
    captured_at: str | None,
    *,
    entity_missing: bool,
    patch: list[dict[str, Any]],
    counts: DiffCounts,
    truncated: bool,
) -> DiffResponse:
    """Assemble the diff return payload for either branch.

    ``unchanged`` means "live config matches the snapshot" — only true
    when the entity exists and the patch is empty. Under
    ``entity_missing`` it is forced ``False``: the empty patch is an
    artefact of the absent target, not evidence of a match.
    """
    return {
        "kind": _DICT_KIND,
        "backup_name": name,
        "domain": domain,
        "entity_id": entity_id,
        "captured_at": captured_at,
        "entity_missing": entity_missing,
        "patch": patch,
        "counts": counts,
        "unchanged": not entity_missing and counts["total"] == 0,
        "truncated": truncated,
    }


class DiffResponseText(TypedDict):
    """Return shape of ``diff_snapshot`` for text (file/YAML) snapshots.

    Mirrors ``DiffResponse``'s control keys (``entity_missing`` /
    ``unchanged`` / ``truncated``) so ``ha_manage_backup`` handles both
    kinds uniformly, but carries a unified text ``diff`` instead of a
    JSON-Patch."""

    kind: Literal["text"]
    backup_name: str
    domain: str
    entity_id: str
    captured_at: str | None
    entity_missing: bool
    diff: str
    unchanged: bool
    truncated: bool


def _build_text_diff_response(
    name: str,
    domain: str,
    entity_id: str,
    captured_at: str | None,
    stored: str,
    current: Any,
) -> DiffResponseText:
    """Assemble the unified-text-diff payload for a file/YAML snapshot.

    ``current is None`` means the target (file or YAML key) is gone, so
    there is no live text to diff against — ``entity_missing`` is set and
    the diff is empty (mirrors the dict branch's missing-target handling).
    The diff recovers ``stored`` *from* ``current`` (snapshot is the target
    state), consistent with the JSON-Patch direction. Output is bounded by
    ``_MAX_DIFF_LINES``; overflow sets ``truncated``.
    """
    if current is None:
        return {
            "kind": _TEXT_KIND,
            "backup_name": name,
            "domain": domain,
            "entity_id": entity_id,
            "captured_at": captured_at,
            "entity_missing": True,
            "diff": "",
            "unchanged": False,
            "truncated": False,
        }
    lines = list(
        difflib.unified_diff(
            str(current).splitlines(),
            stored.splitlines(),
            fromfile="current",
            tofile="snapshot",
            lineterm="",
        )
    )
    truncated = len(lines) > _MAX_DIFF_LINES
    if truncated:
        lines = lines[:_MAX_DIFF_LINES]
    return {
        "kind": _TEXT_KIND,
        "backup_name": name,
        "domain": domain,
        "entity_id": entity_id,
        "captured_at": captured_at,
        "entity_missing": False,
        "diff": "\n".join(lines),
        "unchanged": len(lines) == 0,
        "truncated": truncated,
    }


# Output cap for diff_snapshot. Bounded payload keeps the tool response
# token-friendly even when the user diffs against a freshly-rewritten
# automation. Picked to comfortably cover typical edits (a handful of
# field changes) while still cutting off pathological cases like "I
# renamed every step of a 500-step script".
_MAX_PATCH_OPS = 200


def _compute_json_patch(
    stored: Any, current: Any, max_ops: int, out: list[dict[str, Any]]
) -> bool:
    """Generate an RFC 6902 JSON-Patch from ``current`` to ``stored``.

    The patch is the op sequence a client would apply to ``current`` to
    recover ``stored`` (the captured snapshot is the target state).
    Appends ops to ``out`` in place (capped at ``max_ops`` entries).

    Returns True only when the diff genuinely exceeded ``max_ops``. The
    generator collects one op beyond the cap so an exactly-full patch
    (``len == max_ops``) isn't mistaken for a truncated one; the
    overflow op is trimmed before returning.
    """
    _diff_node(stored, current, "", out, max_ops + 1)
    truncated = len(out) > max_ops
    if truncated:
        del out[max_ops:]
    return truncated


def _diff_node(
    stored: Any,
    current: Any,
    path: str,
    out: list[dict[str, Any]],
    max_ops: int,
) -> None:
    if len(out) >= max_ops:
        return
    # ``type(s) is type(c)`` keeps ``True``/``1`` apart (both compare
    # equal but represent different states for HA toggles); YAML loaders
    # only emit plain dict/list/scalar containers, so subclass surprises
    # aren't in scope.
    if type(stored) is type(current):
        if isinstance(stored, dict):
            assert isinstance(current, dict)
            for key in stored:
                seg = _pointer_segment(str(key))
                sub_path = f"{path}/{seg}"
                if key not in current:
                    out.append({"op": "add", "path": sub_path, "value": stored[key]})
                    if len(out) >= max_ops:
                        return
                else:
                    _diff_node(stored[key], current[key], sub_path, out, max_ops)
                    if len(out) >= max_ops:
                        return
            for key in current:
                if key not in stored:
                    seg = _pointer_segment(str(key))
                    out.append({"op": "remove", "path": f"{path}/{seg}"})
                    if len(out) >= max_ops:
                        return
            return
        if isinstance(stored, list):
            assert isinstance(current, list)
            min_len = min(len(stored), len(current))
            for i in range(min_len):
                _diff_node(stored[i], current[i], f"{path}/{i}", out, max_ops)
                if len(out) >= max_ops:
                    return
            if len(stored) > len(current):
                for value in stored[len(current) :]:
                    out.append({"op": "add", "path": f"{path}/-", "value": value})
                    if len(out) >= max_ops:
                        return
            elif len(current) > len(stored):
                # Remove tail entries from highest to lowest index so
                # successive removes stay valid (RFC 6902 reindexes
                # after each op).
                for i in range(len(current) - 1, len(stored) - 1, -1):
                    out.append({"op": "remove", "path": f"{path}/{i}"})
                    if len(out) >= max_ops:
                        return
            return
        if stored != current:
            out.append({"op": "replace", "path": path or "", "value": stored})
        return
    # ``True == 1`` / ``False == 0`` in Python, so equality alone would
    # let a bool/int type swap pass silently even though it represents
    # a different state for HA toggles. The different-type branch
    # forces a replace unconditionally. No post-append length guard here
    # (unlike the loop sites above): this append is terminal, and
    # ``_compute_json_patch`` budgets ``max_ops + 1`` precisely to absorb
    # one final overflow op before trimming.
    out.append({"op": "replace", "path": path or "", "value": stored})


def _pointer_segment(key: str) -> str:
    """Escape one JSON-Pointer reference token per RFC 6901 §3.

    Order matters: ``~`` → ``~0`` must run before ``/`` → ``~1``. The
    reverse order would first turn a literal ``/`` into ``~1``, and the
    following ``~`` pass would then corrupt that fresh ``~1`` into
    ``~01``.
    """
    return key.replace("~", "~0").replace("/", "~1")


def _summarize_patch_counts(patch: list[dict[str, Any]]) -> DiffCounts:
    """Tally op classes. ``add + remove + replace == total`` holds today
    because ``_diff_node`` only emits those three ops; if a future change
    starts emitting ``move``/``copy``/``test``, the class counts would sum
    to less than ``total``. Warn on any unrecognized op so that drift is
    visible instead of silently undercounting.
    """
    classes: dict[str, int] = {"add": 0, "remove": 0, "replace": 0}
    for op in patch:
        op_type = op.get("op")
        if isinstance(op_type, str) and op_type in classes:
            classes[op_type] += 1
        else:
            logger.warning(
                "diff: unrecognized JSON-Patch op %r — not reflected in "
                "per-class counts (add/remove/replace)",
                op_type,
            )
    return {
        "add": classes["add"],
        "remove": classes["remove"],
        "replace": classes["replace"],
        "total": len(patch),
    }


# --------------------------- attach to client -------------------------------


def get_backup_manager(client: Any, settings: Any) -> BackupManager:
    """Get-or-create the singleton BackupManager attached to ``client``.

    Stored on the client object so tools that share a client share one
    manager (and one set of per-entity locks). Rebuilds when the
    ``settings`` object identity differs from the cached manager's —
    runtime env-var changes that reset the global settings singleton
    (see ``config._reset_global_settings``) yield a fresh ``settings``
    instance, which forces a manager rebuild so the new
    ``enable_auto_backup`` / throttle / retention values take effect.
    """
    mgr = getattr(client, "_auto_backup_manager", None)
    if mgr is None or mgr._settings is not settings:
        mgr = BackupManager(settings, client)
        register_default_handlers(mgr, client)
        try:
            client._auto_backup_manager = mgr
        except (AttributeError, TypeError):
            # Read-only client (e.g. a slotted mock) — manager still works,
            # just isn't cached.
            pass
    return mgr


# --------------------------- domain handlers --------------------------------
#
# Fetchers READ the entity's current config; restorers WRITE it back.
# Each pair calls the same HA endpoint that the underlying ``ha_config_set_*``
# / ``ha_config_remove_*`` tool already uses, so restore re-applies cleanly.


async def _rest_get_or_none(client: Any, path: str) -> Any:
    """Fetch via the client's internal ``_request``; return None on 404.

    The client doesn't expose a public ``get(path)`` — the convention is
    to call the typed wrappers (``get_automation_config``,
    ``get_states``, etc.) which internally call ``_request``. Domains
    without a typed wrapper use this helper. Narrow exception handling
    catches expected REST/transport failures; programming errors (e.g.
    ``AttributeError`` from a typo in the path) propagate.
    """
    try:
        return await client._request("GET", path)
    except HomeAssistantError as err:
        if getattr(err, "status_code", None) == 404:
            return None
        raise


async def _rest_post(client: Any, path: str, payload: Any) -> Any:
    """POST via the client's internal ``_request`` helper."""
    return await client._request("POST", path, json=payload)


async def _ws_send(client: Any, message: dict[str, Any]) -> Any:
    """Send a WS command using the same lazy-connect pattern as other tools.

    Builds a one-shot WS client each call. Latency is acceptable because
    captures are off the critical path (the wrapped write runs regardless)
    and are throttled per-entity by default.
    """
    # Import inside the function to avoid an import cycle:
    # backup_manager → tools.helpers → ... (tools depend on backup_manager).
    from .tools.helpers import get_connected_ws_client

    ws_client, error = await get_connected_ws_client(
        client.base_url, client.token, verify_ssl=client.verify_ssl
    )
    if error or ws_client is None:
        # ``error`` is a structured-error envelope; the message can live
        # under either error.error.message (nested) or error.message
        # (flat, as ``create_error_response`` actually produces).
        if isinstance(error, dict):
            err_obj = error.get("error", error)
            msg = (
                err_obj.get("message") if isinstance(err_obj, dict) else None
            ) or error.get("message", "WS connect failed")
        else:
            msg = "WS connect failed"
        # Typed connection error so the outer ``_CAPTURE_TRANSIENT_ERRORS``
        # tuple catches it; a bare ``RuntimeError`` would propagate past
        # ``maybe_snapshot``'s catch and break the wrapped write — exactly
        # what the best-effort contract on the decorator forbids.
        raise HomeAssistantConnectionError(msg)
    try:
        cmd_type = message.pop("type")
        envelope = await ws_client.send_command(cmd_type, **message)
    finally:
        # Best-effort close: narrow to transport/network errors; let
        # other exceptions propagate so they show up in logs rather
        # than getting silently swallowed during cleanup.
        try:
            await ws_client.disconnect()
        except (TimeoutError, OSError, ConnectionError) as err:
            logger.debug(
                "Auto-backup: ws disconnect failed (transport-level): %s: %s",
                type(err).__name__,
                err,
            )
    # ``send_command`` returns ``{"success": True, "result": <inner>}``
    # — unwrap so fetch / restore handlers downstream see the inner
    # shape directly (list for ``<type>/list`` calls, dict for
    # ``execute_script`` calls, etc.). Without the unwrap the
    # ``_require_list`` checks in every fetch handler would see the
    # envelope as a non-list and raise a spurious degraded-fetch error.
    if isinstance(envelope, dict) and "result" in envelope:
        return envelope["result"]
    return envelope


# Automation / Script / Scene — reuse the typed client helpers, which
# handle id-resolution (entity_id ↔ unique_id) and unwrap response envelopes
# identically to how ``ha_config_set_<domain>`` itself fetches state for
# the existing optimistic-locking flow. Going through these helpers
# guarantees the snapshot's ``config`` shape matches what the restorer
# will re-POST.


async def _fetch_automation(client: Any, entity_id: str) -> Any:
    """Fetch an automation through the same path the get tool uses.

    Applies ``_normalize_config_for_roundtrip`` so the snapshot matches
    what ``ha_config_get_automation`` returns and round-trips cleanly
    back through ``ha_config_set_automation`` on restore. Imported lazily
    to keep the manager import-cycle-free.
    """
    # Lazy import to avoid backup_manager → tools → backup_manager cycle.
    from .tools.tools_config_automations import _normalize_config_for_roundtrip

    try:
        raw = await client.get_automation_config(entity_id)
    except HomeAssistantError as err:
        if getattr(err, "status_code", None) == 404:
            return None
        raise
    if not isinstance(raw, dict):
        return raw
    return _normalize_config_for_roundtrip(raw)


async def _restore_automation(client: Any, entity_id: str, config: Any) -> Any:
    return await client.upsert_automation_config(config, identifier=entity_id)


async def _fetch_script(client: Any, entity_id: str) -> Any:
    try:
        result = await client.get_script_config(entity_id)
    except HomeAssistantError as err:
        if getattr(err, "status_code", None) == 404:
            return None
        raise
    # get_script_config returns a wrapper {"config": <body>, "script_id": ...};
    # the inner body is what upsert_script_config takes.
    return result.get("config", result) if isinstance(result, dict) else result


async def _restore_script(client: Any, entity_id: str, config: Any) -> Any:
    return await client.upsert_script_config(config, entity_id)


async def _fetch_scene(client: Any, entity_id: str) -> Any:
    try:
        result = await client.get_scene_config(entity_id)
    except HomeAssistantError as err:
        if getattr(err, "status_code", None) == 404:
            return None
        raise
    return result.get("config", result) if isinstance(result, dict) else result


async def _restore_scene(client: Any, entity_id: str, config: Any) -> Any:
    return await client.upsert_scene_config(config, entity_id)


# Dashboards — WS lovelace/config (fetch) and lovelace/config/save (restore).


async def _fetch_dashboard(client: Any, entity_id: str) -> Any:
    """Fetch a dashboard config via the same helper the get tool uses.

    The identifier is pre-resolved to its canonical url_path via the shared
    ``_resolve_dashboard`` (component ``list`` when available), then the config is
    read through the component ``get`` (one in-process frame) with a fall back to
    the legacy ``lovelace/config`` read (``_get_dashboard_config_internal``, which
    handles the WS envelope, force-cache-bypass, and structured error wrapping).
    The component refuses YAML bodies, so those capture through legacy unchanged.
    Imported lazily to avoid an import cycle.
    """
    from fastmcp.exceptions import ToolError

    from .tools.tools_config_dashboards import (
        _component_dashboard_config,
        _get_dashboard_config_internal,
        _resolve_dashboard,
    )

    # The set/delete tools accept BOTH the canonical hyphenated url_path
    # AND HA's internal (underscored) dashboard id, eagerly resolving the
    # latter before writing. ``_get_dashboard_config_internal`` does NOT
    # lazy-resolve, so an internal-id identifier 404s with "Unknown config
    # specified" and the pre-write snapshot is silently skipped. Pre-resolve
    # to the canonical url_path so capture works for whichever form the
    # caller passed (matching the form the write tool ultimately targets).
    fetch_path = entity_id
    try:
        match, _ = await _resolve_dashboard(client, entity_id)
        if match and match.get("url_path"):
            fetch_path = match["url_path"]
    except (HomeAssistantError, ToolError) as err:
        # Resolver failure (transport/shape) — fall through with the
        # original identifier; the canonical form is often already correct.
        logger.debug(
            "Auto-backup: dashboard resolve failed for %r: %s — using as-is",
            entity_id,
            err,
        )

    # Component fast path (freshness-safe in-memory read); None ⇒ legacy below,
    # which also covers YAML dashboards and not-found (nothing to back up).
    component_config = await _component_dashboard_config(client, fetch_path)
    if component_config is not None:
        return component_config

    try:
        config, _config_hash = await _get_dashboard_config_internal(client, fetch_path)
    except ToolError as err:
        # ToolError carries the structured failure payload; treat a
        # missing/unknown dashboard as "nothing to back up" (also covers a
        # brand-new dashboard on the create path). "Unknown config
        # specified" is HA's message for an unresolved url_path.
        msg = str(err).lower()
        if "not_found" in msg or "config_not_found" in msg or "unknown config" in msg:
            return None
        raise
    except HomeAssistantError as err:
        msg = str(err).lower()
        if "not_found" in msg or "config_not_found" in msg or "unknown config" in msg:
            return None
        raise
    return config


async def _restore_dashboard(client: Any, entity_id: str, config: Any) -> Any:
    return await _ws_send(
        client,
        {
            "type": "lovelace/config/save",
            "url_path": entity_id,
            "config": config,
        },
    )


def _require_list(value: Any, endpoint: str) -> list[Any]:
    """Return ``value`` if it's a list, else raise.

    The WS registry-list fetchers below distinguish two cases that used
    to both collapse to ``None`` (which the diff/capture callers read as
    "entity missing"): a genuine miss (entity not in the list) stays
    ``None``, but an unexpected non-list envelope — a degraded response
    from an auth-scope change or API drift — raises instead. The raise
    funnels through the diff tool's ``exception_to_structured_error`` and
    the capture pipeline's ``_CAPTURE_TRANSIENT_ERRORS`` warning, so a
    broken fetch is never reported as a confident ``entity_missing``.
    """
    if not isinstance(value, list):
        raise HomeAssistantError(
            f"Expected a list from {endpoint!r}, got {type(value).__name__}"
        )
    return value


def _require_dict(value: Any, endpoint: str) -> dict[str, Any]:
    """Return ``value`` if it's a dict, else raise.

    Dict-shaped counterpart to :func:`_require_list` for the
    ``execute_script``-backed fetchers (calendar / todo). Their service
    response is a dict envelope; a non-dict body is a degraded/malformed
    200 (auth-scope change, API drift), not a genuine miss. Raising
    funnels it through the diff tool's ``exception_to_structured_error``
    and the capture pipeline's ``_CAPTURE_TRANSIENT_ERRORS`` warning,
    instead of collapsing to ``None`` — which callers read as
    ``entity_missing``. The genuine-miss signal stays the nested ``uid``
    lookup returning ``None``.
    """
    if not isinstance(value, dict):
        raise HomeAssistantError(
            f"Expected a dict from {endpoint!r}, got {type(value).__name__}"
        )
    return value


# Dashboard resources — WS lovelace_resources commands.


async def _fetch_dashboard_resource(client: Any, entity_id: str) -> Any:
    resources = _require_list(
        await _ws_send(client, {"type": "lovelace/resources"}), "lovelace/resources"
    )
    for res in resources:
        if str(res.get("id")) == entity_id:
            return res
    return None


async def _restore_dashboard_resource(client: Any, entity_id: str, config: Any) -> Any:
    payload = _strip_readonly(config, "id")
    payload["resource_id"] = entity_id
    payload["type"] = "lovelace/resources/update"
    return await _ws_send(client, payload)


# Labels — config/label_registry/{list,update}
#
# Registry list endpoints return read-only metadata fields
# (``created_at``, ``modified_at``) that the matching ``/update`` endpoint
# rejects with ``extra keys not allowed``. The capture has to keep them
# (they're part of the snapshot's informational payload), so the restore
# strips them at the last moment. Same pattern applies to category /
# zone / area / floor / integration / helper registries.
_REGISTRY_READONLY_KEYS = frozenset({"created_at", "modified_at"})


def _strip_readonly(config: dict[str, Any], *extra: str) -> dict[str, Any]:
    """Return ``config`` with read-only registry fields removed.

    Always strips ``created_at`` / ``modified_at`` (universal across HA's
    registries). Caller passes additional per-registry id keys (e.g.
    ``label_id`` / ``category_id``) that the update endpoint re-injects
    separately and rejects when sent inside the payload body.
    """
    drop = _REGISTRY_READONLY_KEYS | set(extra)
    return {k: v for k, v in config.items() if k not in drop}


async def _fetch_label(client: Any, entity_id: str) -> Any:
    # Route the capture through the component's ``registries`` capability when
    # available (one in-process read of the label registry) instead of dumping
    # the whole registry via WS. Lazy import to avoid the backup_manager →
    # tools → backup_manager cycle (same pattern as ``_fetch_device``). ``None``
    # from the helper means "component unavailable"; fall back to the full list.
    from .tools.component_registries import fetch_registries_via_component

    component_result = await fetch_registries_via_component(client, ["label"])
    if component_result is not None:
        items = component_result.get("labels") or []
    else:
        items = _require_list(
            await _ws_send(client, {"type": "config/label_registry/list"}),
            "config/label_registry/list",
        )
    for item in items:
        if item.get("label_id") == entity_id:
            return item
    return None


async def _restore_label(client: Any, entity_id: str, config: Any) -> Any:
    payload = _strip_readonly(config, "label_id")
    payload["type"] = "config/label_registry/update"
    payload["label_id"] = entity_id
    return await _ws_send(client, payload)


# Categories — config/category_registry/{list,update}


async def _fetch_category(client: Any, entity_id: str) -> Any:
    scope, _, cat_id = entity_id.partition(":")
    if not cat_id:
        return None
    # Same component-first routing as ``_fetch_label``; categories are scoped,
    # so the requested scope rides ``category_scopes``.
    from .tools.component_registries import fetch_registries_via_component

    component_result = await fetch_registries_via_component(
        client, ["category"], category_scopes=[scope]
    )
    if component_result is not None:
        items = (component_result.get("categories") or {}).get(scope, [])
    else:
        items = _require_list(
            await _ws_send(
                client, {"type": "config/category_registry/list", "scope": scope}
            ),
            "config/category_registry/list",
        )
    for item in items:
        if item.get("category_id") == cat_id:
            return {"scope": scope, **item}
    return None


async def _restore_category(client: Any, entity_id: str, config: Any) -> Any:
    scope, _, cat_id = entity_id.partition(":")
    payload = _strip_readonly(config, "category_id", "scope")
    payload["type"] = "config/category_registry/update"
    payload["scope"] = scope or config.get("scope")
    payload["category_id"] = cat_id
    return await _ws_send(client, payload)


# Groups — group.set service. Fetch via state API.


async def _fetch_group(client: Any, entity_id: str) -> Any:
    eid = entity_id if entity_id.startswith("group.") else f"group.{entity_id}"
    state = await _rest_get_or_none(client, f"states/{eid}")
    if state is None:
        return None
    attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
    return {
        "object_id": eid.split(".", 1)[1],
        "name": attrs.get("friendly_name"),
        "entities": attrs.get("entity_id", []),
        "icon": attrs.get("icon"),
    }


async def _restore_group(client: Any, entity_id: str, config: Any) -> Any:
    object_id = config.get("object_id") or entity_id.split(".", 1)[-1]
    service_data: dict[str, Any] = {"object_id": object_id}
    if config.get("name"):
        service_data["name"] = config["name"]
    if config.get("entities"):
        service_data["entities"] = config["entities"]
    if config.get("icon"):
        service_data["icon"] = config["icon"]
    return await _rest_post(client, "services/group/set", service_data)


# Calendar events — calendar.get_events to fetch, calendar.create/update services.


async def _fetch_calendar_event(client: Any, entity_id: str) -> Any:
    # entity_id is "<calendar.entity>::<event_uid>"
    cal, _, uid = entity_id.partition("::")
    if not cal or not uid:
        return None
    # Configurable lookahead window. Default 7 days catches typical edits;
    # set HAMCP_AUTO_BACKUP_CALENDAR_LOOKAHEAD_DAYS to widen for far-future
    # events or narrow to skip noise. Bounded so a typo can't query
    # decades of history.
    try:
        from .config import get_global_settings

        days = int(
            getattr(get_global_settings(), "auto_backup_calendar_lookahead_days", 7)
        )
    except (AttributeError, ImportError, ValueError, TypeError):
        days = 7
    days = max(1, min(365, days))
    start = datetime.now(UTC).isoformat()
    payload = {
        "type": "execute_script",
        "sequence": [
            {
                "service": "calendar.get_events",
                "target": {"entity_id": cal},
                "data": {"duration": {"days": days}, "start_date_time": start},
                "response_variable": "events",
            },
            {"stop": "", "response_variable": "events"},
        ],
    }
    try:
        result = await _ws_send(client, payload)
    except HomeAssistantError as err:
        # Only treat 404 (calendar entity not present) as "skip silently".
        # Auth/transport/server errors deserve a WARNING so an operator
        # can spot a misconfigured calendar integration; matches the
        # ``status_code == 404`` narrowing the automation/script/scene
        # fetchers use.
        if getattr(err, "status_code", None) == 404:
            return None
        raise
    result = _require_dict(result, "execute_script")
    events = result.get("response", {}).get("events", {}).get(cal, {}).get("events", [])
    for ev in events:
        if ev.get("uid") == uid:
            return {"calendar_entity_id": cal, **ev}
    return None


async def _restore_calendar_event(client: Any, entity_id: str, config: Any) -> Any:
    cal = config.get("calendar_entity_id")
    if not cal:
        cal = entity_id.split("::", 1)[0]
    data = {k: v for k, v in config.items() if k != "calendar_entity_id"}
    return await _rest_post(
        client,
        "services/calendar/create_event",
        {"entity_id": cal, **data},
    )


# Zones — zone/{list,update} (no ``config/`` prefix per HA's actual WS API;
# matches ``tools_zones.py`` which is the authoritative usage).


async def _fetch_zone(client: Any, entity_id: str) -> Any:
    items = _require_list(await _ws_send(client, {"type": "zone/list"}), "zone/list")
    for item in items:
        if item.get("id") == entity_id or item.get("name") == entity_id:
            return item
    return None


async def _restore_zone(client: Any, entity_id: str, config: Any) -> Any:
    payload = _strip_readonly(config, "id")
    payload["type"] = "zone/update"
    payload["zone_id"] = config.get("id", entity_id)
    return await _ws_send(client, payload)


# Areas / floors — config/area_registry/{list,update}, config/floor_registry/{list,update}


async def _fetch_area_or_floor(client: Any, entity_id: str) -> Any:
    kind, _, real_id = entity_id.partition(":")
    if not real_id:
        return None
    # Same component-first routing as ``_fetch_label`` / ``_fetch_category``.
    from .tools.component_registries import fetch_registries_via_component

    if kind == "area":
        component_result = await fetch_registries_via_component(client, ["area"])
        if component_result is not None:
            items = component_result.get("areas") or []
        else:
            items = _require_list(
                await _ws_send(client, {"type": "config/area_registry/list"}),
                "config/area_registry/list",
            )
        for item in items:
            if item.get("area_id") == real_id:
                return {"kind": "area", **item}
    elif kind == "floor":
        component_result = await fetch_registries_via_component(client, ["floor"])
        if component_result is not None:
            items = component_result.get("floors") or []
        else:
            items = _require_list(
                await _ws_send(client, {"type": "config/floor_registry/list"}),
                "config/floor_registry/list",
            )
        for item in items:
            if item.get("floor_id") == real_id:
                return {"kind": "floor", **item}
    return None


async def _restore_area_or_floor(client: Any, entity_id: str, config: Any) -> Any:
    kind, _, real_id = entity_id.partition(":")
    payload = _strip_readonly(config, "kind", "area_id", "floor_id")
    if kind == "area":
        payload["type"] = "config/area_registry/update"
        payload["area_id"] = real_id
    elif kind == "floor":
        payload["type"] = "config/floor_registry/update"
        payload["floor_id"] = real_id
    else:
        raise ValueError(f"Unknown area/floor kind: {kind!r}")
    return await _ws_send(client, payload)


# Todo items — entity_id is "<todo.entity>::<item_uid>"


async def _fetch_todo_item(client: Any, entity_id: str) -> Any:
    # The second segment is whatever the tool's ``item`` param carried.
    # ha_set_todo_item / ha_remove_todo_item accept EITHER the item uid OR
    # its exact summary/name, so this can be either form.
    cal, _, item_ref = entity_id.partition("::")
    if not cal or not item_ref:
        return None
    payload = {
        "type": "execute_script",
        "sequence": [
            {
                "service": "todo.get_items",
                "target": {"entity_id": cal},
                "response_variable": "items",
            },
            {"stop": "", "response_variable": "items"},
        ],
    }
    try:
        result = await _ws_send(client, payload)
    except HomeAssistantError as err:
        # Same narrow-to-404 rule as the calendar fetcher: only treat
        # "todo entity not present" as a clean skip; let auth/transport
        # errors propagate to the WARNING log via the manager's outer
        # _CAPTURE_TRANSIENT_ERRORS catch.
        if getattr(err, "status_code", None) == 404:
            return None
        raise
    result = _require_dict(result, "execute_script")
    items = result.get("response", {}).get("items", {}).get(cal, {}).get("items", [])
    for item in items:
        # Match either form. Matching only on uid silently skipped the
        # snapshot whenever the caller passed the human-readable summary
        # (the documented/common case, e.g. ha_remove_todo_item(list, "Buy
        # milk")) — uid != summary, so the loop found nothing -> None.
        if item.get("uid") == item_ref or item.get("summary") == item_ref:
            return {"todo_entity_id": cal, **item}
    return None


async def _restore_todo_item(client: Any, entity_id: str, config: Any) -> Any:
    cal = config.get("todo_entity_id") or entity_id.split("::", 1)[0]
    data = {k: v for k, v in config.items() if k != "todo_entity_id"}
    return await _rest_post(
        client,
        "services/todo/add_item",
        {"entity_id": cal, **data},
    )


# Generic entity (ha_set_entity) — fetch via state API, restore by re-setting.


async def _fetch_entity_state(client: Any, entity_id: str) -> Any:
    return await _rest_get_or_none(client, f"states/{entity_id}")


async def _restore_entity_state(client: Any, entity_id: str, config: Any) -> Any:
    # ha_set_entity just calls /api/states/<entity_id> POST with state+attributes.
    if isinstance(config, dict):
        payload = {
            "state": config.get("state"),
            "attributes": config.get("attributes", {}),
        }
    else:
        payload = {"state": str(config)}
    return await _rest_post(client, f"states/{entity_id}", payload)


# Devices — config/device_registry/{list,update}. ``ha_set_device`` mutates
# the user-editable registry fields (name_by_user / area_id / disabled_by /
# labels); restore re-applies exactly those. A device deleted by
# ``ha_remove_device`` cannot be recreated through the registry, so for that
# path the snapshot is an informational pre-delete record and restore is
# best-effort.


async def _fetch_device(client: Any, device_id: str) -> Any:
    # Route the single-device capture through the component's ``device_get`` when
    # available (one in-process read of the raw DeviceEntry) instead of dumping the
    # whole registry — the same pre-write snapshot ``ha_set_device`` /
    # ``ha_remove_device`` capture. Lazy import to avoid the backup_manager →
    # tools → backup_manager cycle. ``None`` from the helper means "component
    # unavailable"; fall back to the full-list scan.
    from .tools.component_devices import fetch_device_via_component

    result = await fetch_device_via_component(client, device_id)
    if result is not None:
        return result.get("device")
    items = await _ws_send(client, {"type": "config/device_registry/list"})
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get("id") == device_id:
            return item
    return None


async def _restore_device(client: Any, entity_id: str, config: Any) -> Any:
    # Re-apply the captured registry state. Uses the same field NAMES as
    # ``_update_device_internal`` but, unlike that partial-update path, always
    # sends all four — restore reverts the device to the snapshot, so a
    # captured ``None`` area/name is intentionally re-applied (cleared).
    return await _ws_send(
        client,
        {
            "type": "config/device_registry/update",
            "device_id": entity_id,
            "name_by_user": config.get("name_by_user"),
            "area_id": config.get("area_id"),
            "disabled_by": config.get("disabled_by"),
            "labels": config.get("labels", []),
        },
    )


# Integration enable/disable — restore re-applies the disabled flag.


async def _fetch_integration(client: Any, entity_id: str) -> Any:
    items = _require_list(
        await _ws_send(client, {"type": "config_entries/get"}), "config_entries/get"
    )
    for item in items:
        if item.get("entry_id") == entity_id:
            return item
    return None


async def _restore_integration(client: Any, entity_id: str, config: Any) -> Any:
    disabled = config.get("disabled_by") is not None
    return await _ws_send(
        client,
        {
            "type": "config_entries/disable",
            "entry_id": entity_id,
            "disabled_by": "user" if disabled else None,
        },
    )


# Helpers — one handler family. Entity ID is "<helper_type>:<id>" so each
# helper type lists/restores via its native WS endpoints. The decorator
# constructs the domain key as ``helper_<type>`` so files group naturally.

_HELPER_LIST_TYPES = {
    "input_boolean",
    "input_text",
    "input_number",
    "input_select",
    "input_datetime",
    "input_button",
    "counter",
    "timer",
    "schedule",
}


async def _fetch_helper(client: Any, entity_id: str, helper_type: str) -> Any:
    """Fetch a helper's full config from its collection list.

    Only the storage-backed (``<helper_type>/list``) types are supported;
    flow-helper types (template, group, utility_meter, ...) live in
    config entries with a totally different shape and a separate
    update API. Returning a partial entity-state stub for those types
    would make restore re-POST it to ``/api/states/<id>``, which sets a
    state attribute rather than the helper's config — silently wrong.
    For unsupported types we return None so the capture-pipeline treats
    it as "entity didn't exist" rather than writing a bogus snapshot.
    """
    if helper_type not in _HELPER_LIST_TYPES:
        logger.debug(
            "Auto-backup: helper_type %r is config-entry-backed; "
            "snapshot/restore via /<type>/list is not supported. "
            "Capture skipped to avoid producing an unrestorable backup.",
            helper_type,
        )
        return None
    items = _require_list(
        await _ws_send(client, {"type": f"{helper_type}/list"}), f"{helper_type}/list"
    )
    object_id = entity_id.split(".", 1)[-1] if "." in entity_id else entity_id
    for item in items:
        if item.get("id") == object_id or item.get("id") == entity_id:
            return item
    # Fallback for renamed helpers: after an entity_id rename the object_id
    # no longer equals the storage collection id (which stays the original
    # create-time id == the registry unique_id), so the direct match above
    # misses and the snapshot was silently skipped. Resolve the unique_id
    # via the entity registry and match on that — the same key the helper
    # update tool itself resolves to.
    eid = entity_id if "." in entity_id else f"{helper_type}.{entity_id}"
    try:
        entry = await _ws_send(
            client, {"type": "config/entity_registry/get", "entity_id": eid}
        )
    except HomeAssistantError as err:
        # Only a genuine "entity not found" means there's nothing to back up;
        # transport/auth/5xx errors must propagate so maybe_snapshot logs a
        # WARNING rather than silently skipping. Same POLICY as _fetch_automation,
        # but matched on the message substring because config/entity_registry/get
        # failures arrive as a WS command error with no status_code to switch on.
        # Best-effort: if HA's not-found wording ever changes, a real miss
        # degrades to a WARNING + skip (never a swallowed fatal error).
        msg = str(err).lower()
        if "not_found" in msg or "not found" in msg:
            return None
        raise
    unique_id = entry.get("unique_id") if isinstance(entry, dict) else None
    if unique_id:
        for item in items:
            if str(item.get("id")) == str(unique_id):
                return item
    return None


async def _restore_helper(
    client: Any, entity_id: str, config: Any, helper_type: str
) -> Any:
    """Restore a storage-backed helper via ``<helper_type>/update``.

    Symmetric with ``_fetch_helper``: only list-backed types are
    supported. Unsupported types raise ``LookupError`` so the restore
    surface fails-loud rather than silently re-applying an
    entity-state stub that doesn't reflect the original helper config.
    """
    if helper_type not in _HELPER_LIST_TYPES:
        raise LookupError(
            f"Helper type {helper_type!r} is config-entry-backed and cannot "
            "be restored via the auto-backup snapshot path. Use the helper's "
            "native edit tool (``ha_config_set_helper``) instead."
        )
    payload = _strip_readonly(config, "id")
    payload["type"] = f"{helper_type}/update"
    payload[f"{helper_type}_id"] = config.get("id", entity_id)
    return await _ws_send(client, payload)


# Files & YAML (#1579 PR2) — capture is MCP-side via the ha_mcp_tools
# services, mirroring every other handler (the component runs in a
# separate process and cannot reach the shared backup store). ``file``
# snapshots the whole file content; ``yaml`` snapshots one config-key
# subtree, because ``write_file`` cannot write the config files that
# ``ha_config_set_yaml`` edits — restore must route through
# ``edit_yaml_config``. Both store the content as ``kind: "text"``.


async def _fetch_file(client: Any, entity_id: str) -> Any:
    """Read a file's current content via the read_file service.

    ``entity_id`` is the file path. Returns the content string, or None
    when the file does not exist — a brand-new write has no prior content
    to snapshot, so capture skips (same as creating a new entity). Other
    read failures raise so the capture pipeline logs them at WARNING.
    """
    from .tools.tools_filesystem import call_mcp_tools_service
    from .tools.util_helpers import unwrap_service_response

    result = await call_mcp_tools_service(client, "read_file", {"path": entity_id})
    if not isinstance(result, dict):
        return None
    result = unwrap_service_response(result)
    if result.get("success", False):
        content = result.get("content")
        return content if isinstance(content, str) else None
    error = str(result.get("error", ""))
    if "does not exist" in error or "not a file" in error:
        return None
    raise HomeAssistantError(f"read_file failed for {entity_id!r}: {error}")


async def _restore_file(client: Any, entity_id: str, config: Any) -> Any:
    """Re-write a file's captured content via the write_file service."""
    from .tools.tools_filesystem import call_mcp_tools_service
    from .tools.util_helpers import unwrap_service_response

    result = await call_mcp_tools_service(
        client,
        "write_file",
        {
            "path": entity_id,
            "content": str(config),
            "overwrite": True,
            "create_dirs": True,
        },
    )
    if isinstance(result, dict):
        result = unwrap_service_response(result)
        if not result.get("success", False):
            raise HomeAssistantError(
                f"write_file restore failed for {entity_id!r}: {result.get('error')}"
            )
    return result


async def _fetch_yaml(client: Any, entity_id: str) -> Any:
    """Read the current YAML subtree for a ``{file}::{yaml_path}`` target.

    Delegates the round-trip subtree extraction to the ha_mcp_tools
    ``read_file`` service (its ``yaml_path`` param): the component carries
    ``ruamel`` (a manifest requirement, so comments and HA tags like
    ``!secret`` / ``!include`` survive), whereas the MCP server's runtime
    does not. Returns the subtree text, or None when the file or key is
    absent (new-key write — nothing to snapshot). A non-not-found read
    failure raises so the capture pipeline logs it at WARNING rather than
    silently producing no backup (the mandatory gate let this write through
    on the promise that it is backed up).
    """
    from .tools.tools_filesystem import call_mcp_tools_service
    from .tools.util_helpers import unwrap_service_response

    # Split on the LAST "::": yaml_path never contains "::" but a file path
    # legally can, so partitioning from the right keeps an exotic filename
    # from being mis-split into the wrong (file, key) pair.
    file, sep, yaml_path = entity_id.rpartition("::")
    if not sep or not file or not yaml_path:
        return None
    result = await call_mcp_tools_service(
        client, "read_file", {"path": file, "yaml_path": yaml_path}
    )
    if not isinstance(result, dict):
        return None
    result = unwrap_service_response(result)
    if not result.get("success", False):
        error = str(result.get("error", ""))
        if "does not exist" in error or "not a file" in error:
            return None
        raise HomeAssistantError(f"read_file failed for {file!r}: {error}")
    # The component extracts the subtree (it has ruamel); None = key absent.
    # ``yaml_path`` is a backward-compatible read_file enhancement, so it is
    # NOT gated by MIN_COMPONENT_VERSION: a component too old to support it
    # returns no ``subtree`` (or rejects the key), and capture degrades to a
    # logged skip — the yaml edit still works, it just isn't snapshotted. The
    # add-on always ships the matching component, so this only affects a
    # mismatched standalone install.
    return result.get("subtree")


async def _restore_yaml(client: Any, entity_id: str, config: Any) -> Any:
    """Re-apply a captured YAML subtree via the edit_yaml_config service.

    ``edit_yaml_config`` is the only write path that reaches HA config
    files (``write_file`` rejects them), so YAML restore goes through it
    with ``action="replace"``.
    """
    from .tools.tools_filesystem import call_mcp_tools_service
    from .tools.util_helpers import unwrap_service_response

    # Split on the LAST "::" (see _fetch_yaml) so an exotic file path
    # containing "::" still restores to the right file and key.
    file, sep, yaml_path = entity_id.rpartition("::")
    if not sep or not file or not yaml_path:
        raise ValueError(f"Invalid yaml snapshot target: {entity_id!r}")
    result = await call_mcp_tools_service(
        client,
        "edit_yaml_config",
        {
            "file": file,
            "action": "replace",
            "yaml_path": yaml_path,
            "content": str(config),
        },
    )
    if isinstance(result, dict):
        result = unwrap_service_response(result)
        if not result.get("success", False):
            raise HomeAssistantError(
                f"edit_yaml_config restore failed for {entity_id!r}: "
                f"{result.get('error')}"
            )
    return result


async def _restore_yaml_file(client: Any, entity_id: str, config: Any) -> Any:
    """Re-write a whole YAML config file via edit_yaml_config(replace_file).

    ``entity_id`` is the config-relative file path. ``write_file`` rejects HA
    config files, so a whole-file restore goes through edit_yaml_config's
    ``replace_file`` action (#1579): it validates the path against the same
    allowlist and writes the content verbatim + atomically.
    """
    from .tools.tools_filesystem import call_mcp_tools_service
    from .tools.util_helpers import unwrap_service_response

    result = await call_mcp_tools_service(
        client,
        "edit_yaml_config",
        {
            "file": entity_id,
            "action": "replace_file",
            "yaml_path": "",
            "content": str(config),
        },
    )
    if isinstance(result, dict):
        result = unwrap_service_response(result)
        if not result.get("success", False):
            raise HomeAssistantError(
                f"edit_yaml_config replace_file restore failed for "
                f"{entity_id!r}: {result.get('error')}"
            )
    return result


async def _list_legacy_backups(client: Any) -> list[dict[str, Any]]:
    """Fetch pre-#1579 ``.bak`` backups via the component list_legacy_backups
    service.

    Returns ``[]`` (logged at debug) when the component predates the service —
    a service-unavailable rejection surfaces either as a ``success: False``
    response or a ``HomeAssistantError`` — so the edits ``list`` still works
    against an older standalone component. Genuine programming errors propagate.
    """
    from .tools.tools_filesystem import call_mcp_tools_service
    from .tools.util_helpers import unwrap_service_response

    try:
        result = await call_mcp_tools_service(client, "list_legacy_backups", {})
    except HomeAssistantError as err:
        logger.debug("legacy backup list unavailable: %s", err)
        return []
    if not isinstance(result, dict):
        return []
    result = unwrap_service_response(result)
    if not result.get("success", False):
        return []
    backups = result.get("backups")
    return backups if isinstance(backups, list) else []


async def _read_legacy_backup(client: Any, filename: str) -> dict[str, Any]:
    """Read one legacy ``.bak`` via the component read_legacy_backup service.

    Returns the unwrapped service response (carries ``success`` / ``content`` /
    ``file_path`` / ``path_ambiguous`` / ``timestamp``). A service-unavailable
    ``HomeAssistantError`` is mapped to a ``success: False`` dict so the caller
    surfaces a not-found rather than crashing.
    """
    from .tools.tools_filesystem import call_mcp_tools_service
    from .tools.util_helpers import unwrap_service_response

    try:
        result = await call_mcp_tools_service(
            client, "read_legacy_backup", {"filename": filename}
        )
    except HomeAssistantError as err:
        return {"success": False, "error": str(err)}
    if not isinstance(result, dict):
        return {"success": False, "error": f"no response for {filename!r}"}
    return unwrap_service_response(result)


def _make_helper_handler(helper_type: str) -> DomainHandler:
    async def fetch(client: Any, entity_id: str) -> Any:
        return await _fetch_helper(client, entity_id, helper_type)

    async def restore(client: Any, entity_id: str, config: Any) -> Any:
        return await _restore_helper(client, entity_id, config, helper_type)

    return DomainHandler(domain=f"helper_{helper_type}", fetch=fetch, restore=restore)


# --------------------------- registry assembly ------------------------------

# Helper types we register backup handlers for. Only list-backed types
# (those served by ``<helper_type>/list`` WebSocket commands) have
# round-trippable snapshot/restore — flow-helper types (template, group,
# utility_meter, ...) live in config entries with a separate API and
# would silently produce unrestorable backups if included here. The
# decorator's ``domain_fn`` builds ``helper_<type>`` keys; if the user
# edits a flow-helper, ``handler_for`` returns None and the capture
# logs a single WARNING — neutral failure, not a silent corruption.
_KNOWN_HELPER_TYPES = sorted(_HELPER_LIST_TYPES)


def register_default_handlers(mgr: BackupManager, _client: Any) -> None:
    mgr.register(DomainHandler("automation", _fetch_automation, _restore_automation))
    mgr.register(DomainHandler("script", _fetch_script, _restore_script))
    mgr.register(DomainHandler("scene", _fetch_scene, _restore_scene))
    mgr.register(DomainHandler("dashboard", _fetch_dashboard, _restore_dashboard))
    mgr.register(
        DomainHandler(
            "dashboard_resource", _fetch_dashboard_resource, _restore_dashboard_resource
        )
    )
    mgr.register(DomainHandler("label", _fetch_label, _restore_label))
    mgr.register(DomainHandler("category", _fetch_category, _restore_category))
    mgr.register(DomainHandler("group", _fetch_group, _restore_group))
    mgr.register(
        DomainHandler("calendar_event", _fetch_calendar_event, _restore_calendar_event)
    )
    mgr.register(DomainHandler("zone", _fetch_zone, _restore_zone))
    mgr.register(
        DomainHandler("area_or_floor", _fetch_area_or_floor, _restore_area_or_floor)
    )
    mgr.register(DomainHandler("todo_item", _fetch_todo_item, _restore_todo_item))
    mgr.register(DomainHandler("entity", _fetch_entity_state, _restore_entity_state))
    mgr.register(DomainHandler("device", _fetch_device, _restore_device))
    mgr.register(DomainHandler("integration", _fetch_integration, _restore_integration))
    mgr.register(DomainHandler("file", _fetch_file, _restore_file))
    mgr.register(DomainHandler("yaml", _fetch_yaml, _restore_yaml))
    # Whole-file YAML config: same fetch as "file" (read_file), but restored via
    # edit_yaml_config(replace_file) since write_file rejects config files.
    # Backs the legacy-restore write path and its pre-restore safety snapshot.
    mgr.register(DomainHandler("yaml_file", _fetch_file, _restore_yaml_file))
    for helper_type in _KNOWN_HELPER_TYPES:
        mgr.register(_make_helper_handler(helper_type))
