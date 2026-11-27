"""``@with_auto_backup`` decorator for write/destructive MCP tools (#1288).

Applied above ``@mcp.tool(...)`` on each backed-up tool. Best-effort by
default: backup capture failures log WARNING but never block the wrapped
tool. Tools decorated ``mandatory=True`` (file/YAML writes, #1579) instead
fail closed — a genuine capture failure blocks the write with a structured
``BACKUP_CAPTURE_FAILED`` error so content is never overwritten un-backed-up.

Usage
-----
Simple case (entity ID lives in a single kwarg): wrap the existing tool
function with ``@with_auto_backup(domain="<domain>", id_param="<kwarg>")``
placed above the ``@mcp.tool``/``@tool`` decorator and below it the
``@log_tool_usage`` line, so the order from outermost to innermost is
``@tool`` -> ``@with_auto_backup`` -> ``@log_tool_usage`` -> the async def.

Computed-key case (helpers — domain encodes ``helper_type``): use
``domain_fn`` / ``id_fn`` instead of ``domain`` / ``id_param``; both take
a single ``kw`` dict argument and return a string. Helpers typically use
``domain_fn=lambda kw: f"helper_{kw['helper_type']}"``.

The decorator must be applied **above** ``@mcp.tool`` so FastMCP sees the
final wrapped callable as the tool. ``functools.wraps`` preserves the
underlying signature for FastMCP schema generation.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..backup_manager import (
    _CAPTURE_TRANSIENT_ERRORS,
    MandatoryBackupError,
    _is_flow_helper_domain,
    get_backup_manager,
)
from ..client.rest_client import HomeAssistantAuthError
from ..config import get_global_settings
from ..errors import TOOL_ERROR_LOG_LEVEL, ErrorCode, create_error_response
from ..utils.registry_update_lock import registry_update_lock
from .helpers import (
    exception_to_structured_error,
    raise_tool_error,
    tool_error_from_response,
)
from .resource_patch import resource_write_lock

logger = logging.getLogger(__name__)

# Decorator-layer expected failures. Settings lookup / get_backup_manager
# may surface AttributeError on a malformed Settings instance during
# tests; otherwise the inner pipeline already raises its own transient
# tuple. Programming errors (TypeError on a bad ``id_fn`` lambda,
# KeyError on a missing kwarg) propagate to surface the bug.
_DECORATOR_TRANSIENT_ERRORS = _CAPTURE_TRANSIENT_ERRORS


class PathNotAllowedError(ToolError):
    """The File & YAML Tools component refused the file path itself.

    Raised by the ``file`` capture's read. The write and delete allowlist is a
    subset of the read gate, so a path the read refuses, the wrapped write or
    delete would refuse too (#2908). The message names no operation and leaves
    out the component's read allowlist, which would mislead a write; the
    component's own text stays in ``context``. The component's refusal also
    says whether its settings UI would take the path as a custom path
    (``custom_path_allowed``, ``None`` from a component that predates it) and
    which absolute roots that UI accepts (``volume_roots``).
    """

    def __init__(
        self,
        path: str,
        component_error: str,
        *,
        custom_path_allowed: bool | None = None,
        volume_roots: list[str] | None = None,
        message: str | None = None,
        suggestions: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.component_error = component_error
        self.custom_path_allowed = custom_path_allowed
        self.volume_roots = volume_roots or []
        super().__init__(
            json.dumps(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message or f"Path not allowed: {path}",
                    suggestions=suggestions or self.custom_path_suggestions(),
                    context={
                        **(context or {}),
                        "path": path,
                        "component_error": component_error,
                    },
                ),
                indent=2,
                default=str,
            ),
            log_level=TOOL_ERROR_LOG_LEVEL,
        )

    def custom_path_suggestions(self) -> list[str]:
        """Suggest a custom path only where the settings UI can accept one."""
        if self.custom_path_allowed is None:
            return []
        if self.custom_path_allowed:
            return [
                "If it isn't listed already, add it or a folder above it under "
                "Custom filesystem paths in the ha-mcp settings UI"
            ]
        roots = self.volume_roots
        if self.path.startswith("/") and not any(
            self.path == root or self.path.startswith(root + "/") for root in roots
        ):
            return [
                "Custom filesystem paths accept absolute paths only under "
                f"{', '.join(roots)}; this one cannot be allowed"
            ]
        return ["This path is never available to the file tools"]


def path_refusal(err: BaseException) -> PathNotAllowedError | None:
    """The component's path refusal that is, or caused, ``err``; else ``None``."""
    if isinstance(err, PathNotAllowedError):
        return err
    cause = refusal_behind_capture_failure(err)
    return cause if isinstance(cause, PathNotAllowedError) else None


def raise_if_snapshot_path_refused(
    err: BaseException, action: str, backup_name: str, data: Any = None
) -> None:
    """Re-frame a refused live-file read during a diff or restore of a snapshot.

    A snapshot of a path that is no longer allowed (a removed custom path, or
    any ``/backup`` path after #2875) can't be compared with or written back to
    its live file. Say that, instead of the write-oriented path refusal. A
    restore passes its outcome as ``data``, so callers still see that nothing
    was applied. (On-demand create has no snapshot yet and reports the plain
    refusal.)
    """
    refusal = path_refusal(err)
    if refusal is None:
        return
    raise PathNotAllowedError(
        refusal.path,
        refusal.component_error,
        custom_path_allowed=refusal.custom_path_allowed,
        volume_roots=refusal.volume_roots,
        message=(
            f"Cannot {action} {backup_name!r}: its live file {refusal.path} is "
            "no longer readable through the file tools (the path is not allowed)"
        ),
        suggestions=[
            "View the snapshot with ha_manage_backup(scope='edits', "
            f"action='view', backup_name={backup_name!r}) to recover its content",
            *refusal.custom_path_suggestions(),
        ],
        context={
            "backup_name": backup_name,
            "action": action,
            **({"data": data} if data is not None else {}),
        },
    ) from None


def refusal_behind_capture_failure(err: BaseException) -> ToolError | None:
    """Return the ``ToolError`` to report instead of a capture failure, else None.

    A mandatory pre-write snapshot reads the current content through the same
    backend the wrapped write needs, so when that refuses the call the capture
    fails FIRST and the write would be refused as ``BACKUP_CAPTURE_FAILED``,
    burying the real error one level deep. Three refusals are surfaced:
    ``COMPONENT_NOT_INSTALLED`` (no tools entry, #2292) and
    ``PathNotAllowedError`` (the path is outside the allowlist, #2908) as they
    were raised, and a ``HomeAssistantAuthError`` from any capture backend, not
    only the File & YAML Tools services, as a new error under the auth code.
    ``capture_on_demand`` uses the same check, and is where a capture reaches
    the other backends. Retrying the backup cannot succeed for any of them, and the remediation is
    not the backup pipeline's.
    """
    seen: set[int] = set()
    # Worklist over BOTH chain links: an explicit ``raise ... from other``
    # sets an unrelated ``__cause__`` while the component error stays in
    # ``__context__`` — following only one link per node would miss it.
    stack: list[BaseException] = [
        link for link in (err.__cause__, err.__context__) if link is not None
    ]
    while stack:
        cause = stack.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        if isinstance(cause, PathNotAllowedError):
            return cause
        if isinstance(cause, HomeAssistantAuthError):
            return tool_error_from_response(
                exception_to_structured_error(cause, raise_error=False)
            )
        if isinstance(cause, ToolError):
            try:
                payload = json.loads(str(cause))
            except TypeError, ValueError:
                payload = None
            if (
                isinstance(payload, dict)
                and isinstance(payload.get("error"), dict)
                and payload["error"].get("code")
                == ErrorCode.COMPONENT_NOT_INSTALLED.value
            ):
                return cause
        stack.extend(
            link for link in (cause.__cause__, cause.__context__) if link is not None
        )
    return None


def _resolve_str(value: Any) -> str:
    """Stringify a kwarg value defensively. ``None`` → empty string."""
    if value is None:
        return ""
    return str(value)


def automation_backup_target(kw: dict[str, Any]) -> str:
    """Resolve the actual storage target for an automation write.

    HA's automation storage uses the body's ``id`` field as the primary
    key (#1404). When the caller passes both ``entity_id`` and a
    ``config.id`` that differ, HA writes to ``config.id`` — the
    snapshot must capture *that* entity, not the user-provided
    ``entity_id`` whose target is left untouched.

    Returns ``str(config["id"])`` when present, else
    ``str(entity_id or "")``. Empty string skips capture (matches the
    create path where there's nothing to back up yet).

    ``config`` accepts dict or JSON-string per the tool signature; both
    shapes are handled.
    """
    config = kw.get("config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError, TypeError:
            config = None
    if isinstance(config, dict):
        config_id = config.get("id")
        if config_id:
            return str(config_id)
    # Return the entity_id UNCHANGED — do NOT strip the ``automation.``
    # prefix. Capture and restore resolve the target through
    # ``client.get_automation_config`` -> ``_resolve_automation_id``, which
    # converts an entity_id ("automation.<slug>") to the real numeric
    # ``unique_id`` via a state lookup ONLY when the prefix is present;
    # otherwise it assumes the string already IS a unique_id. Stripping the
    # prefix produced a bare object_id slug that the resolver mis-treats as
    # a unique_id -> GET /config/automation/config/<slug> 404s -> the
    # pre-write snapshot is silently skipped (and, had it resolved, restore
    # would POST to the wrong key and create a stray automation). The
    # doubled domain segment in the snapshot filename
    # ("automation.automation.<slug>.<ts>.yaml") is purely cosmetic and is
    # exactly what the remove path (id_param="entity_id") already produces.
    return _resolve_str(kw.get("entity_id"))


async def back_up_entities(
    client: Any, entity_ids: list[str], tool_name: str
) -> list[str]:
    """Snapshot each entity of a call with many targets, before its first write.

    The decorator captures one target, so a tool that takes a list calls this
    after its own validation. Returns why a backup that was due was not taken.
    """
    settings = get_global_settings()
    if not getattr(settings, "enable_auto_backup", False):
        return []
    skipped: list[str] = []
    try:
        mgr = get_backup_manager(client, settings)
        for eid in entity_ids:
            await mgr.maybe_snapshot(
                "entity", eid, tool_name=tool_name, skip_reasons=skipped
            )
    except _CAPTURE_TRANSIENT_ERRORS as err:
        logger.warning("Entity backup failed: %s", err)
        skipped.append(f"No pre-write backup was taken: {type(err).__name__}")
    return skipped


def with_auto_backup(
    *,
    domain: str | None = None,
    id_param: str | None = None,
    domain_fn: Callable[[dict[str, Any]], str] | None = None,
    id_fn: Callable[[dict[str, Any]], str] | None = None,
    client: Any = None,
    mandatory: bool = False,
    skip_fn: Callable[[dict[str, Any]], bool] | None = None,
    domain_resolver: Callable[[Any, dict[str, Any], str, str], Awaitable[str]]
    | None = None,
    precheck: Callable[[Any, str, dict[str, Any]], Awaitable[None]] | None = None,
) -> Callable[..., Any]:
    """Decorate a write/destructive tool with pre-write auto-backup capture.

    Either provide ``(domain, id_param)`` for the simple case, or provide
    ``domain_fn`` and ``id_fn`` for cases where the domain key or entity
    ID is computed from multiple kwargs (helpers, area_or_floor).

    The client is resolved at call time in this order:
    1. Explicit ``client`` kwarg passed to the decorator (used by tools
       defined as inline functions that close over ``client`` in their
       ``register_*_tools`` function — see ``tools_config_helpers.py``).
    2. ``self._client`` when the wrapped function is a class method (the
       common case in modules like ``tools_config_automations.py``).

    Backup capture is best-effort: failure logs a WARNING and the wrapped
    write proceeds regardless.

    ``skip_fn`` (optional) short-circuits the whole decorator — including
    the ``mandatory`` gate — for calls that cannot write (e.g. a yaml-edit
    preview) or delegate capture and write coordination to an inner owner.
    The wrapped tool runs directly without an outer snapshot or refusal.

    ``domain_resolver`` optionally refines the capture domain from HA metadata.
    It receives the client, tool kwargs, initial domain and entity ID inside
    the write guard, before capture. Its failures follow the capture policy.

    ``precheck`` optionally refuses the call before anything is captured, with
    the client, the tool's name and its kwargs, so a refused call leaves no
    snapshot behind. It runs after the mandatory toggle check, inside the write
    guard and whether or not a capture would be taken, so another file or YAML
    write tool of this server cannot change the file between the check and the
    capture (a ``scope="edits"`` restore takes no such lock); it must not take
    the guard's lock. An error it raises other than a ``ToolError`` is reported
    as a structured error.

    ``mandatory=True`` makes auto-backup a precondition (file/YAML writes,
    #1579 — those formerly kept their own private backups). It fails the
    write closed in two cases, both with a structured error and without
    calling the wrapped tool:

    1. The master toggle is off — refused *before* the best-effort ``try``
       so the refusal can't be swallowed (``ToolError`` is in the transient
       tuple).
    2. The toggle is on but capture genuinely fails — ``maybe_snapshot``
       raises ``MandatoryBackupError`` (an unusable backup dir, a failed
       fetch, or a failed snapshot write such as disk-full), caught here and
       mapped to ``BACKUP_CAPTURE_FAILED``. A legitimate "nothing to
       snapshot" skip (new file/key) is NOT a failure and lets the write
       proceed.
    """
    if (domain is None) == (domain_fn is None):
        raise ValueError("with_auto_backup needs exactly one of domain or domain_fn")
    if (id_param is None) == (id_fn is None):
        raise ValueError("with_auto_backup needs exactly one of id_param or id_fn")

    explicit_client = client

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Calls that cannot write (previews, unconfirmed deletes) need no
            # capture or precheck. Delegated writes acquire their own guard and
            # snapshot after resolving or confirming the target, so they also
            # bypass this outer capture and refusal.
            if skip_fn is not None and skip_fn(kwargs):
                return await func(*args, **kwargs)
            # Settings + target resolution happen OUTSIDE the best-effort
            # ``try`` below: under ``mandatory`` a transient-tuple error from
            # ``get_global_settings`` / ``domain_fn`` / ``id_fn`` must NOT be
            # swallowed (that would let an un-backed-up write through). Only the
            # capture dispatch itself is wrapped.
            settings = get_global_settings()
            enabled = bool(getattr(settings, "enable_auto_backup", False))
            # Mandatory gate — the toggle-off refusal is raised here, before the
            # try, because ``ToolError`` is in ``_DECORATOR_TRANSIENT_ERRORS``
            # and would otherwise be swallowed.
            if mandatory and not enabled:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.CONFIG_VALIDATION_FAILED,
                        f"'{func.__name__}' requires auto-backup, which is "
                        "currently disabled. Enable it in the Backups tab of "
                        "the ha-mcp settings UI before using this tool — the "
                        "write was blocked and nothing was changed.",
                        suggestions=[
                            "Enable auto-backup in the ha-mcp settings UI "
                            + "(Backups tab), or set ENABLE_AUTO_BACKUP=true",
                            "Once enabled, this write is snapshotted and "
                            + "becomes restorable via "
                            + "ha_manage_backup(scope='edits')",
                        ],
                        context={
                            "tool_name": func.__name__,
                            "enable_auto_backup": False,
                        },
                    )
                )
            backup_client = _resolve_backup_client(explicit_client, args)
            snap_domain, entity_id = _resolve_snapshot_target(
                kwargs,
                domain=domain,
                domain_fn=domain_fn,
                id_param=id_param,
                id_fn=id_fn,
            )
            async with _template_write_context(
                backup_client,
                settings,
                snap_domain,
                entity_id,
            ):
                await _run_precheck(precheck, backup_client, func.__name__, kwargs)
                skipped = None
                if enabled:
                    skipped = await _capture_pre_write_snapshot(
                        func,
                        args,
                        kwargs,
                        settings=settings,
                        explicit_client=explicit_client,
                        domain=domain,
                        domain_fn=domain_fn,
                        id_param=id_param,
                        id_fn=id_fn,
                        mandatory=mandatory,
                        domain_resolver=domain_resolver,
                    )
                result = await func(*args, **kwargs)
                if skipped and isinstance(result, dict):
                    # The caller, not only the log, learns no backup was taken.
                    result.setdefault("warnings", []).append(skipped)
                return result

        return wrapper

    return decorator


@asynccontextmanager
async def _template_write_context(
    client: Any, settings: Any, domain: str, entity_id: str
) -> AsyncIterator[None]:
    """Keep a write's check, capture and mutation in one critical section.

    Flow helpers and integrations share the restore critical section. The lock
    is needed even when ordinary auto-backup is disabled: restores of existing
    flow helpers require a safety snapshot before replacing their current
    options.

    Dashboard resources take their per-resource write lock here, so the
    snapshot, the tool's own read (an ``edits=`` patch) and its write cannot
    interleave with another write through this server. Writes from the Home
    Assistant UI or other processes are not serialized, so the snapshot can
    predate an edit made there that the write then replaces. The resource
    tools must not take the lock again: ``asyncio.Lock`` is not reentrant.

    File tools share one lock rather than one per path, because a link and its
    target are the same file under two names. YAML edits take it as well: a
    theme file is written by both, and two snapshots taken before either write
    would let a restore of the later one remove the earlier change.
    """
    # Both options-update tools take stable config-entry IDs. Alias-based helper
    # removal and dotted subentry targets have separate resolution paths.
    is_entry_write = (
        _is_flow_helper_domain(domain) or domain == "integration"
    ) and "." not in entity_id
    if is_entry_write and entity_id and client is not None:
        manager = get_backup_manager(client, settings)
        async with manager.config_entry_write_guard(entity_id):
            yield
    elif domain == "dashboard_resource" and entity_id:
        async with resource_write_lock(entity_id):
            yield
    elif domain in ("file", "yaml") and entity_id:
        # YAML edits too: ha_config_set_yaml and ha_write_file both write themes/.
        async with registry_update_lock("file", "all_files"):
            yield
    else:
        yield


async def _run_precheck(
    precheck: Callable[[Any, str, dict[str, Any]], Awaitable[None]] | None,
    client: Any,
    tool_name: str,
    kwargs: dict[str, Any],
) -> None:
    """Run ``with_auto_backup``'s precheck, outside the tool's own error handling."""
    if precheck is None:
        return
    try:
        await precheck(client, tool_name, kwargs)
    except ToolError:
        raise
    except Exception as err:  # noqa: BLE001 - classified, not swallowed
        exception_to_structured_error(err, {"tool_name": tool_name})


def _resolve_backup_client(explicit_client: Any, args: tuple[Any, ...]) -> Any:
    """Resolve the HA client for capture: the explicit decorator kwarg, else the
    bound ``self._client`` / ``self.client`` of a wrapped class method."""
    client_obj = explicit_client
    if client_obj is None and args:
        client_obj = getattr(args[0], "_client", None) or getattr(
            args[0], "client", None
        )
    return client_obj


def _resolve_snapshot_target(
    kwargs: dict[str, Any],
    *,
    domain: str | None,
    domain_fn: Callable[[dict[str, Any]], str] | None,
    id_param: str | None,
    id_fn: Callable[[dict[str, Any]], str] | None,
) -> tuple[str, str]:
    """Compute the ``(domain, entity_id)`` snapshot target from the tool kwargs."""
    snap_domain: str = domain_fn(kwargs) if domain_fn is not None else domain or ""
    if id_fn is not None:
        entity_id = _resolve_str(id_fn(kwargs))
    else:
        entity_id = _resolve_str(kwargs.get(id_param or ""))
    return snap_domain, entity_id


async def _capture_pre_write_snapshot(
    func: Callable[..., Awaitable[Any]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    settings: Any,
    explicit_client: Any,
    domain: str | None,
    domain_fn: Callable[[dict[str, Any]], str] | None,
    id_param: str | None,
    id_fn: Callable[[dict[str, Any]], str] | None,
    mandatory: bool,
    domain_resolver: Callable[[Any, dict[str, Any], str, str], Awaitable[str]] | None,
) -> str | None:
    """Resolve the snapshot target and capture a pre-write backup.

    Extracted from ``with_auto_backup``'s wrapper. Best-effort: a transient
    capture failure logs a WARNING, lets the write proceed and returns the
    warning text for the tool response; a ``mandatory``
    failure maps to a structured ``BACKUP_CAPTURE_FAILED`` error that fails the
    write closed (nothing is changed).
    """
    client_obj = _resolve_backup_client(explicit_client, args)
    snap_domain, entity_id = _resolve_snapshot_target(
        kwargs, domain=domain, domain_fn=domain_fn, id_param=id_param, id_fn=id_fn
    )
    skipped: list[str] = []
    if entity_id:
        try:
            if client_obj is not None:
                if domain_resolver is not None:
                    snap_domain = await domain_resolver(
                        client_obj, kwargs, snap_domain, entity_id
                    )
                mgr = get_backup_manager(client_obj, settings)
                await mgr.maybe_snapshot(
                    snap_domain,
                    entity_id,
                    tool_name=func.__name__,
                    mandatory=mandatory,
                    skip_reasons=skipped,
                )
            elif mandatory:
                # No client to capture with, but this tool requires a
                # backup — fail closed rather than write un-backed-up.
                raise MandatoryBackupError(
                    "no Home Assistant client is available to capture "
                    "the pre-write backup"
                )
        except MandatoryBackupError as err:
            # A required pre-write snapshot genuinely failed (not a
            # legitimate "nothing to snapshot" skip). Fail closed: the
            # wrapped write never runs, so nothing has been changed.
            # The ToolError this raises propagates rather than being
            # swallowed by the best-effort handler below.
            refusal = refusal_behind_capture_failure(err)
            if refusal is not None:
                # The capture was refused — the same reason the wrapped write
                # itself would fail. Surface that actionable error, not a
                # backup complaint (#2292, #2908).
                # The write is still blocked. ``from None`` because this IS the
                # underlying error being unwrapped; chaining it back onto the
                # wrapper it came out of would only print the chain twice (and
                # B904 wants the intent explicit). ``from None`` also drops the
                # wrapper's own text from the traceback, so record it here or
                # the only account of WHICH mandatory capture was re-pointed at
                # the refusal is gone.
                logger.warning(
                    "Auto-backup: mandatory capture for '%s' failed (%s) — "
                    "surfacing the refusal instead; the write is blocked "
                    "either way",
                    func.__name__,
                    err,
                )
                raise refusal from None
            raise_tool_error(
                create_error_response(
                    ErrorCode.BACKUP_CAPTURE_FAILED,
                    f"'{func.__name__}' requires a pre-write backup, "
                    f"but the snapshot could not be captured: {err}. "
                    "The write was blocked and nothing was changed.",
                    suggestions=err.suggestions
                    or ["Retry once the underlying issue is resolved"],
                    context={"tool_name": func.__name__},
                )
            )
        except _DECORATOR_TRANSIENT_ERRORS as err:
            logger.warning(
                "Auto-backup: capture raised %s: %s — write proceeding",
                type(err).__name__,
                err,
            )
            skipped.append(f"No pre-write backup was taken: {type(err).__name__}")
    return "; ".join(skipped) or None
