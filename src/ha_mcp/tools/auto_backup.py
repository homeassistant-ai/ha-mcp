"""``@with_auto_backup`` decorator for write/destructive MCP tools (#1288).

Applied above ``@mcp.tool(...)`` on each backed-up tool. Best-effort:
backup capture failures log WARNING but never block the wrapped tool.

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
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..backup_manager import _CAPTURE_TRANSIENT_ERRORS, get_backup_manager
from ..config import get_global_settings

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


def with_auto_backup(
    *,
    domain: str | None = None,
    id_param: str | None = None,
    domain_fn: Callable[[dict[str, Any]], str] | None = None,
    id_fn: Callable[[dict[str, Any]], str] | None = None,
    client: Any = None,
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
    """
    if (domain is None) == (domain_fn is None):
        raise ValueError("with_auto_backup needs exactly one of domain or domain_fn")
    if (id_param is None) == (id_fn is None):
        raise ValueError("with_auto_backup needs exactly one of id_param or id_fn")

    explicit_client = client

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                settings = get_global_settings()
                if getattr(settings, "enable_auto_backup", False):
                    client_obj = explicit_client
                    if client_obj is None and args:
                        client_obj = getattr(args[0], "_client", None) or getattr(
                            args[0], "client", None
                        )
                    if client_obj is not None:
                        snap_domain: str = (
                            domain_fn(kwargs) if domain_fn is not None else domain or ""
                        )
                        if id_fn is not None:
                            entity_id = _resolve_str(id_fn(kwargs))
                        else:
                            entity_id = _resolve_str(kwargs.get(id_param or ""))
                        if entity_id:
                            mgr = get_backup_manager(client_obj, settings)
                            await mgr.maybe_snapshot(
                                snap_domain, entity_id, tool_name=func.__name__
                            )
            except _DECORATOR_TRANSIENT_ERRORS as err:
                logger.warning(
                    "Auto-backup: pre-write hook raised %s: %s — write proceeding",
                    type(err).__name__,
                    err,
                )
            return await func(*args, **kwargs)

        return wrapper

    return decorator
