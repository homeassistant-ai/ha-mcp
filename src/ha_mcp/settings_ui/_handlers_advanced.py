"""Advanced-settings route handlers for the settings UI.

Factory returning the ``get_advanced_settings`` / ``save_advanced_settings``
handlers over the ``ADVANCED_SETTINGS_FIELDS`` registry. Most advanced
fields write to the shared ``feature_flags.json`` override file;
``ADDON_SYNCED_ADVANCED_FIELDS`` (``backup_hint``, ``verify_ssl``) route
through the Supervisor add-on options in add-on mode instead.

Handlers are module-level (own C901 budget); ``build_advanced_handlers``
binds ``server`` into request-only wrappers.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .._version import is_running_in_addon
from ..config import _reset_global_settings, get_global_settings
from ..errors import ErrorCode, create_error_response
from . import _persistence, _supervisor

if TYPE_CHECKING:
    from ..server import HomeAssistantSmartMCPServer

logger = logging.getLogger(__name__)


def _origin_for_advanced_field(
    env_name: str, overrides: dict[str, Any] | None = None
) -> str:
    """Origin for an ADVANCED_SETTINGS_FIELDS entry.

    Returns ``'addon' | 'env' | 'file' | 'default'``.

    ``'addon'`` is returned in addon mode for fields that live in both the
    registry and the addon's config.yaml schema (the
    ``ADDON_SYNCED_ADVANCED_FIELDS`` set). For those, writes route through
    Supervisor instead of the override file so the addon Configuration tab
    and this web UI share state. Other env-pinned fields stay ``'env'``.

    Callers iterating ADVANCED_SETTINGS_FIELDS should pass a pre-read
    ``overrides`` dict so the override file isn't re-read N times per render.
    """
    from ..config import (
        ADDON_SYNCED_ADVANCED_FIELDS,
        ADVANCED_SETTINGS_FIELDS,
        _read_feature_flag_override_file,
    )

    fname = next((f for f, e, *_ in ADVANCED_SETTINGS_FIELDS if e == env_name), None)
    if (
        is_running_in_addon()
        and fname is not None
        and fname in ADDON_SYNCED_ADVANCED_FIELDS
    ):
        return "addon"
    if os.environ.get(env_name) is not None:
        return "env"
    if overrides is None:
        overrides = _read_feature_flag_override_file()
    if fname is not None and fname in overrides:
        return "file"
    return "default"


def _advanced_field_row(
    fname: str,
    env_name: str,
    ftype: type,
    section: str,
    registry_editable: bool,
    *,
    settings: Any,
    overrides: dict[str, Any],
    live_options: dict[str, Any],
) -> dict[str, Any]:
    """Build one ``_get_advanced_settings`` field row (value + origin + bounds)."""
    from ..config import (
        _ADVANCED_SETTINGS_BOUNDS,
        _ADVANCED_SETTINGS_CHOICES,
        _ADVANCED_SETTINGS_SENTINELS,
        OAUTH_MODE_TOKEN,
    )

    origin = _origin_for_advanced_field(env_name, overrides=overrides)
    value: Any = getattr(settings, fname, None)
    if origin == "addon" and fname in live_options:
        value = live_options[fname]
    # Mask the token: never echo the actual long-lived access token to the
    # UI. The OAuth-mode sentinel survives so operators can tell mode.
    if fname == "homeassistant_token":
        value = "*****" if value and value != OAUTH_MODE_TOKEN else value
    row: dict[str, Any] = {
        "field": fname,
        "env_var": env_name,
        "value": value,
        "type": ftype.__name__,
        "section": section,
        "origin": origin,
        # Env-pin makes the field read-only regardless of the registry's
        # ``editable`` flag. Display-only rows stay locked forever.
        "editable": registry_editable and origin != "env",
    }
    bounds = _ADVANCED_SETTINGS_BOUNDS.get(fname)
    if bounds is not None:
        row["min"], row["max"] = bounds
        # Sentinel fields (e.g. sidecar_pin_port: 0 = off) need the number
        # input to reach below the bounded range, so expose the sentinel
        # as the UI minimum.
        sentinel = _ADVANCED_SETTINGS_SENTINELS.get(fname)
        if sentinel is not None:
            row["min"] = sentinel
    choices = _ADVANCED_SETTINGS_CHOICES.get(fname)
    if choices is not None:
        row["choices"] = list(choices)
    return row


async def _get_advanced_settings(
    server: HomeAssistantSmartMCPServer | None, _: Request
) -> JSONResponse:
    """Return advanced (non-feature-flag, non-backup) settings + per-field
    origin + editable flag.

    Mirrors ``_get_feature_flags`` / ``_get_backup_config`` but for the
    ``ADVANCED_SETTINGS_FIELDS`` registry. Most advanced fields write to
    ``feature_flags.json`` via the shared override file in either deployment
    mode. ``ADDON_SYNCED_ADVANCED_FIELDS`` (``backup_hint``, ``verify_ssl``)
    are an exception: in addon mode they have ``origin="addon"`` (editable)
    and saves route through Supervisor so the add-on Configuration tab and
    the web UI share state.
    """
    from ha_mcp.settings_ui import get_http_settings_prefix

    from ..config import ADVANCED_SETTINGS_FIELDS, _read_feature_flag_override_file

    settings = get_global_settings()
    # Read the override file ONCE for this GET — origin lookup is called per
    # field, and re-reading the file 17+ times would produce duplicate
    # WARNINGs on a corrupt file (one per field).
    overrides = _read_feature_flag_override_file()
    # Read-consistency with the add-on Configuration tab: addon-synced fields
    # (origin "addon") live in /data/options.json, so the boot-env value goes
    # stale after a config edit without a restart. Surface the latest SAVED
    # options value for those.
    live_options = await _supervisor._live_addon_options(server)
    fields = [
        _advanced_field_row(
            fname,
            env_name,
            ftype,
            section,
            registry_editable,
            settings=settings,
            overrides=overrides,
            live_options=live_options,
        )
        for fname, env_name, ftype, section, registry_editable in (
            ADVANCED_SETTINGS_FIELDS
        )
    ]
    # is_stdio: the sidecar-port field only applies when this settings page
    # is served by the stdio settings-UI sidecar. In HTTP/SSE/OAuth/addon
    # deployments there is no sidecar, so the UI greys the section.
    return JSONResponse(
        {
            "fields": fields,
            "is_addon": is_running_in_addon(),
            "is_stdio": get_http_settings_prefix() is None,
        }
    )


def _bad_advanced_type(fname: str, ftype: type, raw: Any) -> JSONResponse:
    return JSONResponse(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"{fname!r} expects {ftype.__name__}, got {type(raw).__name__}.",
            suggestions=[f"Send {fname} as a {ftype.__name__} value."],
        ),
        status_code=400,
    )


def _advanced_env_pin_rejection(
    fname: str, env_name: str, addon_mode: bool
) -> JSONResponse:
    """409 for an env-pinned (non-addon-synced) advanced field."""
    if addon_mode:
        # Add-on mode has no env-var surface for non-schema keys (e.g.
        # CODE_MODE_SAVED_TOOLS_PATH, set by start.py), so "unset it to edit
        # here" is unactionable there — give add-on-aware copy instead.
        message = (
            f"{fname!r} is fixed by the App (add-on) runtime and "
            "cannot be changed from the web UI."
        )
        suggestions = [
            "This value is baked into the App (add-on) and is not "
            "exposed as an editable setting.",
        ]
    else:
        message = f"{fname!r} is set via {env_name} env var. Unset it to edit here."
        suggestions = [
            f"Unset the {env_name} environment variable (or "
            "remove it from your Docker config), then restart "
            "to edit this setting from the UI.",
        ]
    return JSONResponse(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            message,
            suggestions=suggestions,
            context={"env_var": env_name},
        ),
        status_code=409,
    )


def _coerce_advanced_value(
    fname: str, ftype: type, raw: Any
) -> tuple[Any, JSONResponse | None]:
    """Coerce a posted advanced value against its declared type."""
    if ftype is bool:
        if not isinstance(raw, bool):
            return None, _bad_advanced_type(fname, ftype, raw)
        return raw, None
    if ftype is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, _bad_advanced_type(fname, ftype, raw)
        return int(raw), None
    if ftype is float:
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            return None, _bad_advanced_type(fname, ftype, raw)
        return float(raw), None
    if ftype is str:
        if not isinstance(raw, str):
            return None, _bad_advanced_type(fname, ftype, raw)
        if "\x00" in raw:
            return None, JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{fname!r} contains a null byte; rejected.",
                ),
                status_code=400,
            )
        return raw, None
    return None, _bad_advanced_type(fname, ftype, raw)


def _check_advanced_bounds_choices(fname: str, coerced: Any) -> JSONResponse | None:
    """Enforce the bounds + choices constraints for a coerced value."""
    from ..config import _ADVANCED_SETTINGS_BOUNDS, _ADVANCED_SETTINGS_CHOICES
    from ..config import _ADVANCED_SETTINGS_SENTINELS as _SENTINELS

    bounds = _ADVANCED_SETTINGS_BOUNDS.get(fname)
    sentinel = _SENTINELS.get(fname)
    if (
        bounds is not None
        and coerced != sentinel
        and not (bounds[0] <= coerced <= bounds[1])
    ):
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{fname!r} must be between {bounds[0]} and "
                f"{bounds[1]} (got {coerced}).",
                suggestions=[
                    f"Provide a value for {fname} between {bounds[0]} and {bounds[1]}.",
                ],
            ),
            status_code=400,
        )
    choices = _ADVANCED_SETTINGS_CHOICES.get(fname)
    if choices is not None and coerced not in choices:
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{fname!r} must be one of {list(choices)} (got {coerced!r}).",
                suggestions=[
                    f"Set {fname} to one of: {', '.join(map(str, choices))}.",
                ],
            ),
            status_code=400,
        )
    return None


def _validate_one_advanced_field(
    fname: str,
    raw: Any,
    registry: dict[str, tuple[str, type, Any, bool]],
    addon_mode: bool,
) -> tuple[Any, bool, JSONResponse | None]:
    """Validate one posted advanced field. Returns
    ``(coerced_value, is_addon_synced, error)``."""
    from ..config import ADDON_SYNCED_ADVANCED_FIELDS

    if fname not in registry:
        return (
            None,
            False,
            JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Unknown advanced field: {fname!r}",
                ),
                status_code=400,
            ),
        )
    env_name, ftype, _section, registry_editable = registry[fname]
    if not registry_editable:
        return (
            None,
            False,
            JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{fname!r} is display-only. Modify via env var "
                    "or App (add-on) configuration.",
                ),
                status_code=409,
            ),
        )
    # Addon-synced fields (e.g. backup_hint, verify_ssl) are editable in
    # addon mode even though their env vars are set — start.py rewrites them
    # from /data/options.json on every boot, so route the write through
    # Supervisor instead of the override file.
    is_addon_synced = addon_mode and fname in ADDON_SYNCED_ADVANCED_FIELDS
    if not is_addon_synced and os.environ.get(env_name) is not None:
        return None, False, _advanced_env_pin_rejection(fname, env_name, addon_mode)

    coerced, coerce_err = _coerce_advanced_value(fname, ftype, raw)
    if coerce_err is not None:
        return None, is_addon_synced, coerce_err
    bounds_err = _check_advanced_bounds_choices(fname, coerced)
    if bounds_err is not None:
        return None, is_addon_synced, bounds_err
    return coerced, is_addon_synced, None


def _validate_advanced_batch(
    body: dict[str, Any],
    registry: dict[str, tuple[str, type, Any, bool]],
    addon_mode: bool,
) -> tuple[dict[str, Any], bool, JSONResponse | None]:
    """Validate the posted advanced-settings body. Returns
    ``(new_overrides, addon_writes_present, error)``."""
    new_overrides: dict[str, Any] = {}
    addon_writes_present = False
    for fname, raw in body.items():
        coerced, is_addon_synced, err = _validate_one_advanced_field(
            fname, raw, registry, addon_mode
        )
        if err is not None:
            return {}, addon_writes_present, err
        if is_addon_synced:
            addon_writes_present = True
        new_overrides[fname] = coerced
    return new_overrides, addon_writes_present, None


async def _save_advanced_addon(
    server: HomeAssistantSmartMCPServer | None,
    new_overrides: dict[str, Any],
    addon_mode: bool,
) -> JSONResponse:
    """Add-on route: batch every advanced write into one Supervisor POST.

    Enforces the "single persistence path" invariant — a batch mixing
    addon-synced and override-file fields is rejected rather than routed
    through two sinks.
    """
    from ..config import ADDON_SYNCED_ADVANCED_FIELDS

    if not addon_mode:
        # Defensive: addon_writes_present should imply addon_mode.
        return JSONResponse(
            create_error_response(
                ErrorCode.INTERNAL_ERROR, "Inconsistent addon-route classification"
            ),
            status_code=500,
        )
    file_only = {
        k: v for k, v in new_overrides.items() if k not in ADDON_SYNCED_ADVANCED_FIELDS
    }
    if file_only:
        return JSONResponse(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                (
                    "Mixed addon-synced and override-file advanced "
                    "writes in one batch; the UI should split these "
                    f"into separate POSTs ({sorted(file_only)})."
                ),
                suggestions=[
                    "Submit addon-synced fields (e.g. backup_hint, "
                    "verify_ssl) and override-file fields in separate "
                    "save requests.",
                ],
            ),
            status_code=500,
        )
    if server is None:
        return JSONResponse(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "Advanced settings POST requires a live MCP server",
            ),
            status_code=500,
        )
    ok, sup_err = await _supervisor._supervisor_merge_and_post_options(
        server.settings.verify_ssl, new_overrides
    )
    if not ok:
        if sup_err is None:
            # ``ok=False`` with no error is a contract bug in the helper.
            return JSONResponse(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    "Supervisor helper returned ok=False with no error",
                    suggestions=[
                        "Check the Home Assistant Supervisor logs and "
                        + "the App (add-on) logs for the underlying failure.",
                        "Report this at "
                        + "https://github.com/homeassistant-ai/ha-mcp/issues "
                        + "if it persists. This indicates an internal bug.",
                    ],
                ),
                status_code=500,
            )
        logger.warning(
            "Supervisor advanced-settings update failed (%s): %s",
            sup_err.kind,
            sup_err.message,
        )
        code = (
            ErrorCode.CONNECTION_FAILED
            if sup_err.kind == "transport"
            else ErrorCode.CONFIG_VALIDATION_FAILED
        )
        return JSONResponse(
            create_error_response(code, sup_err.message),
            status_code=sup_err.status_code,
        )
    return JSONResponse(
        {
            "success": True,
            "applied": new_overrides,
            "mode": "addon",
            "restart_required": True,
        }
    )


async def _write_advanced_overrides_file(
    new_overrides: dict[str, Any],
) -> JSONResponse | None:
    """File route: RMW-merge advanced overrides into the shared override
    file under lock. Returns an error ``JSONResponse`` or ``None``.

    Uses the same ``feature_flags.json`` the feature-flag saves use, so a
    single file holds both advanced + feature-flag overrides.
    """
    from ..config import _FEATURE_FLAG_OVERRIDE_FILENAME
    from ..utils.data_paths import get_data_dir

    path = get_data_dir() / _FEATURE_FLAG_OVERRIDE_FILENAME
    async with _persistence._get_override_file_lock():
        existing: dict[str, Any] = {}
        try:
            existing_raw = path.read_text()
        except FileNotFoundError:
            existing_raw = None
        except OSError as exc:
            logger.warning("Cannot read %s", path, exc_info=True)
            return JSONResponse(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Could not read existing override file "
                    f"({type(exc).__name__}: {exc}); refusing to "
                    "overwrite to preserve prior toggles.",
                ),
                status_code=500,
            )
        if existing_raw is not None:
            try:
                parsed = json.loads(existing_raw)
            except json.JSONDecodeError as exc:
                logger.warning("Existing %s is corrupt: %s", path, exc, exc_info=True)
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Existing override file at {path} is not "
                        f"valid JSON ({exc}); refusing to overwrite. "
                        "Inspect or delete the file manually and retry.",
                    ),
                    status_code=409,
                )
            if isinstance(parsed, dict):
                existing = parsed
        existing.update(new_overrides)

        try:
            _persistence._atomic_write_json(path, existing)
        except OSError as exc:
            logger.warning("Could not write %s", path, exc_info=True)
            return JSONResponse(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Could not persist advanced settings: {exc}",
                ),
                status_code=500,
            )
    return None


async def _save_advanced_settings(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    """Persist UI-edited advanced settings.

    Addon-synced fields (``backup_hint``, ``verify_ssl``) in add-on mode
    route through Supervisor; everything else atomically merges into the
    shared ``feature_flags.json`` override file. Either sink responds with
    ``restart_required=True`` (most advanced fields gate one-time startup
    paths).
    """
    from ..config import ADVANCED_SETTINGS_FIELDS

    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_JSON, "Invalid JSON body"
            ),
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Request body must be a JSON object",
            ),
            status_code=400,
        )

    registry = {f: (e, t, s, ed) for f, e, t, s, ed in ADVANCED_SETTINGS_FIELDS}
    addon_mode = is_running_in_addon()
    new_overrides, addon_writes_present, err = _validate_advanced_batch(
        body, registry, addon_mode
    )
    if err is not None:
        return err

    if not new_overrides:
        return JSONResponse(
            {"success": True, "applied": {}, "mode": "file", "restart_required": False}
        )

    if addon_writes_present:
        return await _save_advanced_addon(server, new_overrides, addon_mode)

    write_err = await _write_advanced_overrides_file(new_overrides)
    if write_err is not None:
        return write_err

    _reset_global_settings()
    return JSONResponse(
        {
            "success": True,
            "applied": new_overrides,
            "mode": "file",
            "restart_required": True,
        }
    )


def build_advanced_handlers(
    server: HomeAssistantSmartMCPServer | None,
) -> dict[str, Any]:
    """Construct the advanced-settings route handlers."""

    async def get_advanced_settings(request: Request) -> JSONResponse:
        return await _get_advanced_settings(server, request)

    async def save_advanced_settings(request: Request) -> JSONResponse:
        return await _save_advanced_settings(server, request)

    return {
        "get_advanced_settings": get_advanced_settings,
        "save_advanced_settings": save_advanced_settings,
    }
