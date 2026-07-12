"""Theme / accessibility preference route handlers for the settings UI.

Factory returning the ``get_theme_prefs`` / ``save_theme_prefs`` handlers.
Depends only on the ``_theme`` leaf module, so it carries no ``server`` /
``is_sidecar`` state.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..errors import ErrorCode, create_error_response
from ..utils.data_paths import get_data_dir
from ._theme import (
    _THEME_PREFS_FILENAME,
    _get_theme_prefs_lock,
    _load_theme_prefs,
    _sanitize_theme_prefs,
)

logger = logging.getLogger(__name__)


def build_theme_handlers() -> dict[str, Any]:
    """Construct the theme-preference route handlers."""

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
        path = get_data_dir() / _THEME_PREFS_FILENAME
        # Same RMW-under-lock + tmp-then-rename shape as the feature-flag
        # save. One deliberate divergence: a corrupt existing file is
        # overwritten (with a warning) instead of returning 409 — theme
        # prefs are cosmetic and trivially re-settable, so recovering
        # beats refusing.
        async with _get_theme_prefs_lock():
            existing = _load_theme_prefs()
            existing.update(sanitized)
            tmp = path.with_suffix(path.suffix + ".tmp")
            try:
                tmp.write_text(json.dumps(existing, indent=2))
                os.replace(tmp, path)
            except OSError as exc:
                logger.warning("Could not write %s", path, exc_info=True)
                with contextlib.suppress(FileNotFoundError, OSError):
                    tmp.unlink()
                return JSONResponse(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        f"Could not persist theme prefs: {exc}",
                    ),
                    status_code=500,
                )
        return JSONResponse({"success": True, "applied": sanitized})

    return {
        "get_theme_prefs": _get_theme_prefs,
        "save_theme_prefs": _save_theme_prefs,
    }
