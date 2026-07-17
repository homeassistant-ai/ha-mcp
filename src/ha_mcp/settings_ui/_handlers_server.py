"""Server-settings route handlers: restart, settings-info, feature flags.

Factory returning the ``restart_addon`` / ``settings_info`` /
``get_feature_flags`` / ``save_feature_flags`` handlers. Handlers are
module-level (own C901 budget); ``build_server_handlers`` binds ``server``
and ``is_sidecar`` into request-only wrappers.

Also owns the per-process identity (``_PROCESS_INSTANCE_ID`` /
``_PROCESS_STARTED_AT``) surfaced via ``settings_info`` so the restart UI
can tell whether the add-on actually restarted, and the
``_reject_child_flags_without_parent`` parent/child feature-flag guard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Container
from typing import TYPE_CHECKING, Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from .._version import get_version, is_embedded, is_running_in_addon
from ..config import _reset_global_settings, get_global_settings
from ..errors import ErrorCode, create_error_response
from . import _persistence, _supervisor

if TYPE_CHECKING:
    from ..server import HomeAssistantSmartMCPServer

logger = logging.getLogger(__name__)

# Per-process identity surfaced via ``/api/settings/info`` so the
# restart UI can tell whether the addon actually restarted (vs. the
# poll-cycle succeeding against the still-running OLD instance because
# the supervisor restart silently no-op'd). Generated once at module
# import; a fresh Python process gets a fresh value, so any restart
# that actually swaps processes flips both. ``started_at`` is Unix
# epoch seconds for human debuggability; ``instance_id`` is the
# load-bearing identifier the JS poll compares against.
_PROCESS_INSTANCE_ID: str = uuid.uuid4().hex
_PROCESS_STARTED_AT: float = time.time()


def _reject_child_flags_without_parent(
    raw_flags: dict[str, Any],
    parent_field: str,
    child_fields: Container[str],
    message: Callable[[list[str]], str],
    suggestions: list[str],
) -> JSONResponse | None:
    """Reject a feature-flag save that enables child flags while their
    parent stays off after the merge.

    A child flag is only valid when ``parent_field`` is truthy AFTER the
    merge. The post-merge parent is derived from the payload (if present),
    else the live ``Settings`` value — the same value the runtime gate
    will see. A child turned truthy against an
    off parent would be forced back off at runtime, so reject it now
    rather than let the user learn the save was a no-op at next startup.
    Turning the parent off alone is NOT rejected: children absent from the
    payload keep their persisted value and the runtime gate handles them.

    Returns a 409 ``JSONResponse`` — ``message`` receives every offending
    child, which is also echoed in ``context["rejected"]`` — or ``None``
    when there is nothing to reject.
    """
    rejected = [k for k in raw_flags if k in child_fields and bool(raw_flags[k])]
    effective_parent = bool(
        raw_flags.get(
            parent_field,
            getattr(get_global_settings(), parent_field),
        )
    )
    if rejected and not effective_parent:
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                message(rejected),
                suggestions=suggestions,
                context={"rejected": rejected},
            ),
            status_code=409,
        )
    return None


def _reject_strict_bps_without_skill_tool(
    raw_flags: dict[str, Any],
) -> JSONResponse | None:
    """Strict-BPS ⇒ skill-guide dependency (#1886).

    A save that leaves strict best-practices mode configured on (parent
    AND child flags, post-merge like _reject_child_flags_without_parent)
    while ha_get_skill_guide is disabled in the Tools tab would silently
    re-lock a tool the user explicitly turned off — strict mode publishes
    its acknowledgment key only through it. Reject with the fix instead.
    Gated on the pair actually appearing in this payload so unrelated
    feature-flag saves can't trip over a pre-existing (hand-edited)
    conflict, which apply_tool_visibility already strips-and-warns at
    startup. Env-pinned disables (DISABLED_TOOLS) are deliberately NOT
    considered: they can't be lifted from the Tools tab, and under strict
    mode they become the documented stays-on no-op via the same
    strip-and-warn. The other direction (disabling the tool while strict
    mode is on) is rejected in _handlers_tools._save_tools.
    """
    if (
        "enable_mandatory_bps" not in raw_flags
        and "enable_strict_mandatory_bps" not in raw_flags
    ):
        return None
    live = get_global_settings()
    parent_on = bool(raw_flags.get("enable_mandatory_bps", live.enable_mandatory_bps))
    strict_on = bool(
        raw_flags.get("enable_strict_mandatory_bps", live.enable_strict_mandatory_bps)
    )
    if not (parent_on and strict_on):
        return None
    from ._tools_meta import BPS_MANDATORY_TOOLS

    # load_tool_config (user-set file state), NOT effective_tool_config
    # (env-merged): a DISABLED_TOOLS env pin can't be lifted from the
    # Tools tab, so rejecting on it would strand the user — and the
    # documented semantics for an env-listed BPS tool under strict mode
    # are "stays on even if listed": apply_tool_visibility's
    # strip-and-warn keeps the tool enabled at runtime.
    tool_states = _persistence.load_tool_config().get("tools", {})
    bps_blocked = sorted(
        name for name in BPS_MANDATORY_TOOLS if tool_states.get(name) == "disabled"
    )
    if not bps_blocked:
        return None
    return JSONResponse(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "Cannot turn on strict best-practices mode while "
            f"{', '.join(bps_blocked)} is disabled in the Tools tab — "
            "strict mode publishes its acknowledgment key only through "
            "that tool.",
            suggestions=[
                f"Re-enable {', '.join(bps_blocked)} in the Tools tab "
                "first, then turn strict mode on.",
            ],
            context={"rejected": bps_blocked},
        ),
        status_code=409,
    )


# ---- Restart ----


async def _restart_embedded(server: HomeAssistantSmartMCPServer) -> JSONResponse:
    """Embedded (in-process custom-component) restart: reload the
    ha_mcp_tools server config entry instead of calling Supervisor."""
    from ..tools.tools_dev import (
        abort_options_flow_quietly,
        find_server_config_entry,
        schedule_deferred_entry_reload,
    )

    try:
        found = await find_server_config_entry(server.client)
    except Exception as exc:
        # A discovery FAILURE is not "entry not found" — masking a
        # WS/connection hiccup as not-found steers users toward
        # reinstalling a running component. Also: the restart JS
        # treats any 5xx as "restart in flight" and starts its
        # poll-reload cycle, so a restart that was never initiated
        # must answer BELOW 500.
        logger.warning("Embedded restart: entry discovery failed: %s", exc)
        return JSONResponse(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                "Could not reach Home Assistant to locate the "
                f"in-process server's config entry: {exc}",
                suggestions=["Retry once Home Assistant is responsive"],
            ),
            status_code=409,
        )
    if found is None:
        return JSONResponse(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "Could not locate the in-process server's config entry to reload",
                suggestions=[
                    "Reload the HA-MCP integration from Settings > "
                    "Devices & Services instead",
                ],
            ),
            # Below 500 on purpose: no restart was initiated, and the
            # restart JS interprets 5xx as restart-in-flight.
            status_code=409,
        )
    entry_id, flow, _options = found
    await abort_options_flow_quietly(server.client, flow)
    schedule_deferred_entry_reload(server.client, entry_id)
    return JSONResponse(
        {"success": True, "message": "In-process server reload scheduled"}
    )


def _parse_restart_slug(payload: Any) -> str:
    """Extract a sanitized sibling add-on slug from the request body.

    The slug is interpolated into the Supervisor endpoint URL, so reject
    anything outside ``[A-Za-z0-9_-]`` (path-traversal defense at the edge)
    and fall back to ``self`` — same outcome as an empty body.
    """
    if isinstance(payload, dict):
        requested = payload.get("slug")
        if (
            isinstance(requested, str)
            and requested.strip()
            and all(c.isalnum() or c in "_-" for c in requested.strip())
        ):
            return requested.strip()
    return "self"


async def _restart_sibling_addon(
    server: HomeAssistantSmartMCPServer, target_slug: str
) -> JSONResponse:
    """Synchronously POST ``/addons/<slug>/restart`` for a non-self target.

    Non-self slugs target a sibling addon: the inaddon E2E suite exercises
    that path against a non-test-critical addon and wants the supervisor
    response (including 4xx) to surface synchronously so the test can assert
    on it.
    """
    endpoint = f"/addons/{target_slug}/restart"
    try:
        async with _supervisor.make_supervisor_httpx_client(
            timeout=5.0, verify=server.settings.verify_ssl
        ) as client:
            resp = await client.post(endpoint)
    except (httpx.ReadError, httpx.RemoteProtocolError):
        # Connection dropped mid-request — restart is happening.
        # `ConnectError` is deliberately NOT in this tuple: it fires
        # before a connection is established (DNS failure, TCP refused,
        # Supervisor socket misconfigured) and means the restart was
        # never initiated. Falls through to the `httpx.HTTPError`
        # handler below, which returns 502 + CONNECTION_FAILED.
        logger.info("Restart request connection dropped (expected during self-restart)")
        return JSONResponse({"success": True, "message": "Restart initiated"})
    except httpx.HTTPError as e:
        logger.exception("Failed to reach Supervisor for restart")
        return JSONResponse(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                f"Failed to reach Supervisor: {e}",
            ),
            status_code=502,
        )

    if resp.status_code >= 400:
        body = resp.text
        logger.error(
            "Supervisor restart failed (slug=%s): %d %s",
            target_slug,
            resp.status_code,
            body,
        )
        return JSONResponse(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Supervisor returned {resp.status_code}: {body[:500]}",
            ),
            status_code=502,
        )
    return JSONResponse({"success": True, "message": "Restart initiated"})


async def _restart_addon(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    # Embedded (in-process custom-component server): "restart" means
    # reloading the ha_mcp_tools server config entry. Checked before the
    # Supervisor path — the HA core container carries SUPERVISOR_TOKEN,
    # but the Supervisor add-on API is not ours to call from here.
    if server is not None and is_embedded():
        return await _restart_embedded(server)

    # The sidecar process (server is None) has no Supervisor context
    # and no live server settings — refuse cleanly. The HTTP modes
    # that do pass a server still go through the SUPERVISOR_TOKEN check.
    if server is None or not os.environ.get("SUPERVISOR_TOKEN"):
        return JSONResponse(
            create_error_response(
                ErrorCode.CONFIG_VALIDATION_FAILED,
                "Restart only available when running as an App (add-on)",
                details="SUPERVISOR_TOKEN environment variable is not set",
            ),
            status_code=400,
        )
    # Optional slug from the request body lets callers restart a sibling
    # addon instead of self. The UI's restart button posts an empty body
    # and gets the historical self-restart behavior.
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        payload = None
    target_slug = _parse_restart_slug(payload)

    # Self-restart races the response flush: supervisor kills the addon
    # mid-response, ingress sees the upstream drop, and converts it into
    # a 5xx Bad Gateway at the browser. Schedule the supervisor POST from
    # a background task so the JSON response below flushes BEFORE
    # supervisor can kill us.
    if target_slug == "self":
        _supervisor._schedule_supervisor_self_restart(server.settings.verify_ssl)
        return JSONResponse({"success": True, "message": "Restart initiated"})

    return await _restart_sibling_addon(server, target_slug)


# ---- Settings info ----


async def _settings_info(
    server: HomeAssistantSmartMCPServer | None, is_sidecar: bool, _: Request
) -> JSONResponse:
    # Sidecar is never the add-on entrypoint regardless of inherited
    # SUPERVISOR_TOKEN. ``is_sidecar`` drives the in-page Stop Sidecar
    # button; it MUST NOT leak True for HTTP modes since stopping the
    # FastMCP-mounted route would mean killing the MCP server itself.
    #
    # ``instance_id`` + ``started_at`` are surfaced so the
    # restart-then-reload JS cycle can prove a restart actually happened
    # (the value flips across processes).
    addon = False if is_sidecar else is_running_in_addon()
    try:
        # Executor: get_version's distribution-ownership scan reads
        # metadata for every installed package — too heavy for the
        # event loop on an endpoint the restart cycle polls.
        version = await asyncio.to_thread(get_version)
    except Exception:  # pragma: no cover — defensive only
        logger.warning("get_version() raised; omitting version from info")
        version = None
    # ``deployment_mode`` reuses the bug-report detector so the UI and
    # ha_report_issue can never disagree about where the server runs.
    from ..tools.tools_bug_report import _detect_installation_method

    return JSONResponse(
        {
            "is_addon": addon,
            "is_sidecar": is_sidecar,
            "deployment_mode": (
                "sidecar" if is_sidecar else _detect_installation_method()
            ),
            "instance_id": _PROCESS_INSTANCE_ID,
            "started_at": _PROCESS_STARTED_AT,
            "version": version,
        }
    )


# ---- Feature flags ----


async def _get_feature_flags(
    server: HomeAssistantSmartMCPServer | None, _: Request
) -> JSONResponse:
    """Return live feature-flag values + per-field origin + editable flag."""
    from ..config import (
        _FEATURE_FLAG_INT_BOUNDS,
        BETA_FEATURE_FIELDS,
        FEATURE_FLAG_FIELDS,
        get_feature_flag_origin,
    )

    settings = get_global_settings()
    # Read-consistency with the add-on Configuration tab: surface the
    # latest SAVED value for any flag present in live_options — not just
    # origin=="addon" rows. Only origin=="env" stays pinned, and
    # live_options is {} outside add-on mode so this is a no-op standalone.
    live_options = await _supervisor._live_addon_options(server)
    flags: dict[str, Any] = {}
    for field_name, env_name, ftype in FEATURE_FLAG_FIELDS:
        origin = get_feature_flag_origin(env_name)
        value = getattr(settings, field_name)
        if field_name in live_options and origin != "env":
            value = live_options[field_name]
        entry: dict[str, Any] = {
            "value": value,
            "origin": origin,
            "editable": origin in ("addon", "file", "default"),
            "type": ftype.__name__,
            "env_var": env_name,
        }
        if ftype is int:
            bounds = _FEATURE_FLAG_INT_BOUNDS.get(field_name)
            if bounds is not None:
                entry["min"], entry["max"] = bounds
        flags[field_name] = entry

    return JSONResponse(
        {
            "flags": flags,
            "beta_sub_flags": list(BETA_FEATURE_FIELDS),
            # Drives addon-aware locked-banner copy in the JS —
            # "unset env var" is misleading where HA Supervisor owns the env.
            "is_addon": is_running_in_addon(),
        }
    )


def _coerce_flag_value(
    field_name: str, ftype: type, raw: Any
) -> tuple[Any, JSONResponse | None]:
    """Coerce/bounds-check a single posted flag value against its type."""
    from ..config import _FEATURE_FLAG_INT_BOUNDS

    if ftype is bool:
        if not isinstance(raw, bool):
            return None, JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{field_name!r} must be a boolean",
                ),
                status_code=400,
            )
        return bool(raw), None
    if ftype is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{field_name!r} must be an integer",
                ),
                status_code=400,
            )
        bounds = _FEATURE_FLAG_INT_BOUNDS.get(field_name)
        if bounds is not None and not bounds[0] <= raw <= bounds[1]:
            return None, JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{field_name!r} must be between {bounds[0]} and {bounds[1]}",
                ),
                status_code=400,
            )
        return int(raw), None
    return None, JSONResponse(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"{field_name!r} has an unsupported type for UI editing",
        ),
        status_code=400,
    )


def _validate_feature_flag_batch(
    raw_flags: dict[str, Any],
) -> tuple[dict[str, Any], bool, bool, JSONResponse | None]:
    """Validate the posted flags. Returns
    ``(new_overrides, addon_writes, file_or_default_writes, error)``.

    Rejects unknown and env-locked fields up front so the user gets a
    precise error instead of a silent no-op. ``addon``-origin fields are
    editable — they route through Supervisor.
    """
    from ..config import FEATURE_FLAG_FIELDS, get_feature_flag_origin

    known: dict[str, tuple[str, type]] = {
        fname: (ename, ftype) for fname, ename, ftype in FEATURE_FLAG_FIELDS
    }
    new_overrides: dict[str, Any] = {}
    addon_writes = False
    file_or_default_writes = False
    for field_name, raw in raw_flags.items():
        if field_name not in known:
            return (
                {},
                False,
                False,
                JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Unknown feature flag: {field_name!r}",
                    ),
                    status_code=400,
                ),
            )
        env_name, ftype = known[field_name]
        origin = get_feature_flag_origin(env_name)
        if origin == "addon":
            addon_writes = True
        elif origin in ("file", "default"):
            file_or_default_writes = True
        else:
            return (
                {},
                addon_writes,
                file_or_default_writes,
                JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        (
                            f"{field_name!r} is locked by {origin}. "
                            f"Adjust the {env_name} env var "
                            "(or App (add-on) configuration) instead."
                        ),
                    ),
                    status_code=400,
                ),
            )
        value, err = _coerce_flag_value(field_name, ftype, raw)
        if err is not None:
            return {}, addon_writes, file_or_default_writes, err
        new_overrides[field_name] = value
    return new_overrides, addon_writes, file_or_default_writes, None


async def _save_feature_flags_addon(
    server: HomeAssistantSmartMCPServer | None, new_overrides: dict[str, Any]
) -> JSONResponse:
    """Add-on-mode branch: POST the merged options to Supervisor."""
    if server is None:
        return JSONResponse(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "Feature-flag POST requires a live MCP server",
            ),
            status_code=500,
        )
    ok, err = await _supervisor._supervisor_merge_and_post_options(
        server.settings.verify_ssl, new_overrides
    )
    if not ok:
        if err is None:
            # ``ok=False`` with no error is a contract bug — bail with
            # INTERNAL_ERROR rather than letting an AttributeError leak
            # under ``python -O``.
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
            "Supervisor feature-flag update failed (%s): %s", err.kind, err.message
        )
        # Transport failures get CONNECTION_FAILED; supervisor schema
        # rejections get CONFIG_VALIDATION_FAILED with supervisor's real
        # status code preserved. See _SupervisorOptionsError.
        code = (
            ErrorCode.CONNECTION_FAILED
            if err.kind == "transport"
            else ErrorCode.CONFIG_VALIDATION_FAILED
        )
        return JSONResponse(
            create_error_response(code, err.message),
            status_code=err.status_code,
        )
    # Unified restart flow — don't auto-restart from the save handler.
    return JSONResponse(
        {
            "success": True,
            "applied": new_overrides,
            "mode": "addon",
            "restart_required": True,
        }
    )


async def _write_feature_flag_overrides_file(
    new_overrides: dict[str, Any],
) -> JSONResponse | None:
    """File-mode branch: RMW-merge ``new_overrides`` into the override
    file under lock. Returns an error ``JSONResponse`` or ``None`` on
    success.

    A partial POST only updates the keys it includes. An unreadable or
    corrupt existing file is refused (500/409) rather than overwritten,
    which would silently drop every previously-persisted flag.
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
                    (
                        f"Could not read existing feature flags "
                        f"({type(exc).__name__}: {exc}); refusing to "
                        "overwrite to preserve prior toggles. "
                        "Check filesystem permissions and retry."
                    ),
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
                        (
                            f"Existing override file at {path} is not "
                            f"valid JSON ({exc}); refusing to overwrite "
                            "to preserve prior toggles. Inspect or "
                            "delete the file manually and retry."
                        ),
                    ),
                    status_code=409,
                )
            if isinstance(parsed, dict):
                existing = parsed
            # else: non-dict JSON — treat as empty; we're about to write a
            # dict either way and there's no prior toggle state to preserve.
        existing.update(new_overrides)

        # Atomic write via the shared tmp+rename helper (a crash mid-write
        # would otherwise leave a truncated file the next read refuses,
        # losing prior toggles). Same helper the advanced-settings write uses.
        try:
            _persistence._atomic_write_json(path, existing)
        except OSError as exc:
            logger.warning("Could not write %s", path, exc_info=True)
            return JSONResponse(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Could not persist feature flags: {exc}",
                ),
                status_code=500,
            )
    return None


def _parse_feature_flags_payload(
    body: Any,
) -> tuple[dict[str, Any], JSONResponse | None]:
    """Validate the save-feature-flags body shape. Returns ``(raw_flags, error)``.

    Requires the nested ``{"flags": {...}}`` shape. A flat body
    (e.g. ``{"enable_lite_docstrings": true}``) has no ``flags`` key, so a
    lenient ``body.get("flags", {})`` default silently dropped every field
    and still returned ``success=True`` / ``restart_required=True`` — worse
    than a visible no-op, because the caller believed the flag changed and a
    restart was pending when nothing happened (#1840).
    """
    if not isinstance(body, dict):
        return {}, JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Request body must be a JSON object",
            ),
            status_code=400,
        )
    raw_flags = body.get("flags")
    if not isinstance(raw_flags, dict):
        return {}, JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Request body must contain a 'flags' object mapping "
                'field names to values, e.g. {"flags": '
                '{"enable_lite_docstrings": true}}. A flat body such '
                'as {"enable_lite_docstrings": true} is not accepted.',
                suggestions=[
                    'Wrap the flags in a "flags" object, e.g. '
                    '{"flags": {"enable_lite_docstrings": true}}.',
                ],
            ),
            status_code=400,
        )
    if not raw_flags:
        return {}, JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "'flags' object is empty; include at least one "
                "feature-flag field to update.",
                suggestions=[
                    'Include at least one field inside "flags", e.g. '
                    '{"flags": {"enable_lite_docstrings": true}}.',
                ],
            ),
            status_code=400,
        )
    return raw_flags, None


async def _save_feature_flags(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    """Persist UI-edited feature-flag values (addon → Supervisor; else file)."""
    from ..config import BETA_FEATURE_FIELDS as _BETA_SUB

    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_JSON, "Invalid JSON body"
            ),
            status_code=400,
        )
    raw_flags, payload_err = _parse_feature_flags_payload(body)
    if payload_err is not None:
        return payload_err

    # Master beta-gate + strict-mandatory-BPS parent/child gates. Applied
    # in BOTH standalone and addon mode. See _reject_child_flags_without_parent.
    beta_rejection = _reject_child_flags_without_parent(
        raw_flags,
        "enable_beta_features",
        _BETA_SUB,
        lambda rejected: (
            "Cannot enable beta sub-flag(s) "
            f"{', '.join(rejected)} while the master "
            "'Enable beta features' toggle is off. Include "
            "enable_beta_features=true in the same save, or "
            "flip the master on first."
        ),
        [
            "Include enable_beta_features=true in the same save "
            + "payload as the sub-flag(s).",
            "Or turn on the master 'Enable beta features' toggle "
            + "first, then enable the sub-flag(s).",
        ],
    )
    if beta_rejection is not None:
        return beta_rejection

    strict_rejection = _reject_child_flags_without_parent(
        raw_flags,
        "enable_mandatory_bps",
        ("enable_strict_mandatory_bps",),
        lambda _rejected: (
            "Cannot enable strict best-practices mode "
            "('enable_strict_mandatory_bps') while the parent "
            "'Attach best-practice skills on writes' "
            "(enable_mandatory_bps) toggle is off. Strict mode is "
            "a child of that toggle and has no effect without it. "
            "Include enable_mandatory_bps=true in the same save, or "
            "turn the parent on first."
        ),
        [
            "Include enable_mandatory_bps=true in the same save "
            + "payload as enable_strict_mandatory_bps.",
            "Or turn on the parent 'Attach best-practice skills on "
            + "writes' toggle first, then enable strict mode.",
        ],
    )
    if strict_rejection is not None:
        return strict_rejection

    bps_tool_rejection = _reject_strict_bps_without_skill_tool(raw_flags)
    if bps_tool_rejection is not None:
        return bps_tool_rejection

    new_overrides, addon_writes, file_or_default_writes, err = (
        _validate_feature_flag_batch(raw_flags)
    )
    if err is not None:
        return err

    # Reject mixed-origin batches loudly. get_feature_flag_origin guarantees
    # a single mode per request; a future change breaking that would
    # otherwise silently route a file/default field through Supervisor.
    if addon_writes and file_or_default_writes:
        return JSONResponse(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                (
                    "Batch contains a mix of addon-origin and "
                    "file/default-origin fields; route each batch "
                    "through a single persistence path."
                ),
            ),
            status_code=500,
        )
    if addon_writes:
        return await _save_feature_flags_addon(server, new_overrides)

    write_err = await _write_feature_flag_overrides_file(new_overrides)
    if write_err is not None:
        return write_err

    # Publish the change so the same process picks it up on the next
    # ``get_global_settings()`` call. ``restart_required=True`` because
    # feature flags gate tool registration / transforms at startup.
    _reset_global_settings()
    return JSONResponse(
        {
            "success": True,
            "applied": new_overrides,
            "mode": "file",
            "restart_required": True,
        }
    )


def build_server_handlers(
    server: HomeAssistantSmartMCPServer | None, *, is_sidecar: bool
) -> dict[str, Any]:
    """Construct the restart / settings-info / feature-flag route handlers."""

    async def restart_addon(request: Request) -> JSONResponse:
        return await _restart_addon(server, request)

    async def settings_info(request: Request) -> JSONResponse:
        return await _settings_info(server, is_sidecar, request)

    async def get_feature_flags(request: Request) -> JSONResponse:
        return await _get_feature_flags(server, request)

    async def save_feature_flags(request: Request) -> JSONResponse:
        return await _save_feature_flags(server, request)

    return {
        "restart_addon": restart_addon,
        "settings_info": settings_info,
        "get_feature_flags": get_feature_flags,
        "save_feature_flags": save_feature_flags,
    }
