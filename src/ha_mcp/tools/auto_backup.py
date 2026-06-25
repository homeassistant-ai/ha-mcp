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
from collections.abc import Awaitable, Callable
from typing import Any

from ..backup_manager import (
    _CAPTURE_TRANSIENT_ERRORS,
    MandatoryBackupError,
    get_backup_manager,
)
from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

# Decorator-layer expected failures. Settings lookup / get_backup_manager
# may surface AttributeError on a malformed Settings instance during
# tests; otherwise the inner pipeline already raises its own transient
# tuple. Programming errors (TypeError on a bad ``id_fn`` lambda,
# KeyError on a missing kwarg) propagate to surface the bug.
_DECORATOR_TRANSIENT_ERRORS = _CAPTURE_TRANSIENT_ERRORS


def _resolve_str(value: Any) -> str:
    """Stringify a kwarg value defensively. ``None`` → empty string."""
    if value is None:
        return ""
    return str(value)


def automation_backup_target(kw: dict[str, Any]) -> str:
    """Resolve the actual storage target for an automation write.

    HA's automation storage uses the body's ``id`` field as the primary
    key (#1404). When the caller passes both ``identifier`` and a
    ``config.id`` that differ, HA writes to ``config.id`` — the
    snapshot must capture *that* entity, not the user-provided
    ``identifier`` whose target is left untouched.

    Returns ``str(config["id"])`` when present, else
    ``str(identifier or "")``. Empty string skips capture (matches the
    create path where there's nothing to back up yet).

    ``config`` accepts dict or JSON-string per the tool signature; both
    shapes are handled.
    """
    config = kw.get("config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (ValueError, TypeError):
            config = None
    if isinstance(config, dict):
        config_id = config.get("id")
        if config_id:
            return str(config_id)
    # Return the identifier UNCHANGED — do NOT strip the ``automation.``
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
    # exactly what the remove path (id_param="identifier") already produces.
    return _resolve_str(kw.get("identifier"))


def with_auto_backup(
    *,
    domain: str | None = None,
    id_param: str | None = None,
    domain_fn: Callable[[dict[str, Any]], str] | None = None,
    id_fn: Callable[[dict[str, Any]], str] | None = None,
    client: Any = None,
    mandatory: bool = False,
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
            if enabled:
                client_obj = explicit_client
                if client_obj is None and args:
                    client_obj = getattr(args[0], "_client", None) or getattr(
                        args[0], "client", None
                    )
                snap_domain: str = (
                    domain_fn(kwargs) if domain_fn is not None else domain or ""
                )
                if id_fn is not None:
                    entity_id = _resolve_str(id_fn(kwargs))
                else:
                    entity_id = _resolve_str(kwargs.get(id_param or ""))
                if entity_id:
                    try:
                        if client_obj is not None:
                            mgr = get_backup_manager(client_obj, settings)
                            await mgr.maybe_snapshot(
                                snap_domain,
                                entity_id,
                                tool_name=func.__name__,
                                mandatory=mandatory,
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
            return await func(*args, **kwargs)

        return wrapper

    return decorator
