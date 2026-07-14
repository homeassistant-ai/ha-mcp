"""Theme / accessibility preference route handlers for the settings UI.

Factory returning the ``get_theme_prefs`` / ``save_theme_prefs`` handlers.
Depends only on the ``_theme`` / ``_persistence`` leaf modules and carries
no ``server`` / ``is_sidecar`` state, so the handlers are referenced
directly (no request-only wrappers to bind ``server``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..errors import ErrorCode, create_error_response
from . import _persistence
from ._theme import (
    _get_theme_prefs_lock,
    _load_theme_prefs,
    _sanitize_theme_prefs,
    theme_prefs_path,
)

logger = logging.getLogger(__name__)


async def _get_theme_prefs(_: Request) -> JSONResponse:
    return JSONResponse({"prefs": _load_theme_prefs()})


async def _save_theme_prefs(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Body must be valid JSON",
            ),
            status_code=400,
        )
    sanitized = _sanitize_theme_prefs(body)
    if sanitized is None:
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Body must be a JSON object",
            ),
            status_code=400,
        )
    if not sanitized:
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "No valid theme preference fields in body",
            ),
            status_code=400,
        )
    path = theme_prefs_path()
    # Same RMW-under-lock + shared atomic-write helper as the feature-flag
    # save. One deliberate divergence: a corrupt existing file is overwritten
    # (with a warning) instead of returning 409 — theme prefs are cosmetic and
    # trivially re-settable, so recovering beats refusing.
    async with _get_theme_prefs_lock():
        existing = _load_theme_prefs()
        existing.update(sanitized)
        try:
            _persistence._atomic_write_json(path, existing)
        except OSError as exc:
            logger.warning("Could not write %s", path, exc_info=True)
            return JSONResponse(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Could not persist theme prefs: {exc}",
                ),
                status_code=500,
            )
    return JSONResponse({"success": True, "applied": sanitized})


def build_theme_handlers() -> dict[str, Any]:
    """Construct the theme-preference route handlers."""
    return {
        "get_theme_prefs": _get_theme_prefs,
        "save_theme_prefs": _save_theme_prefs,
    }
