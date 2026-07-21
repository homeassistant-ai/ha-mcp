"""Tool enable/disable/pin + LLM-API-exposure route handlers.

Factory returning the ``get_tools`` / ``save_tools`` handlers. The
handlers are module-level (own C901 budget); ``build_tools_handlers``
binds ``server`` into request-only wrappers. Persistence goes through the
``_persistence`` leaf module and the tool list through ``_tools_meta``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .._version import is_embedded
from ..errors import ErrorCode, create_error_response
from ..llm_exposure import LLM_API_CONFIG_KEY
from ..transforms import DEFAULT_PINNED_TOOLS
from ..utils.config_write_lock import get_config_write_lock
from . import _persistence
from ._tools_meta import _VALID_STATES, BPS_MANDATORY_TOOLS, _get_tool_metadata

if TYPE_CHECKING:
    from ..server import HomeAssistantSmartMCPServer

logger = logging.getLogger(__name__)


def _coerce_tool_states(raw_states: dict[str, Any]) -> dict[str, str]:
    """Keep only str→valid-state entries from the posted states map."""
    states: dict[str, str] = {}
    for name, state in raw_states.items():
        if not isinstance(name, str) or not isinstance(state, str):
            continue
        if state not in _VALID_STATES:
            continue
        states[name] = state
    return states


def _coerce_llm_overrides(raw_llm_api: dict[str, Any]) -> dict[str, bool]:
    """Keep only str→bool entries from the posted llm_api overrides map."""
    return {
        name: value
        for name, value in raw_llm_api.items()
        if isinstance(name, str) and isinstance(value, bool)
    }


def _padded_pins(tool_states: dict[str, str]) -> dict[str, str]:
    """Overlay the default-pinned tools onto a states map.

    ``_get_tools`` pads its response with these and the JS posts the padded
    map back verbatim, so ``_save_tools`` must compare with the same padding
    or every first save looks like a states change (#1745).
    """
    padded = dict(tool_states)
    for name in DEFAULT_PINNED_TOOLS:
        padded.setdefault(name, "pinned")
    return padded


def _bps_locked_tools() -> list[str]:
    """BPS-dependent tools currently locked enabled (#1886).

    ``ha_get_skill_guide`` may only be disabled while strict
    best-practices mode is off — the strict gate publishes its
    acknowledgment key exclusively through that tool, so disabling it in
    strict mode would lock out every gated write. Regular (non-strict)
    mandatory BPS attaches skill content server-side and does not need
    the tool. A settings load failure locks rather than unlocks: a
    wrongly locked row costs an explanatory note, while a wrongly
    unlocked one lets a save through that ``apply_tool_visibility``
    would strip at startup anyway.
    """
    from ..config import get_global_settings

    try:
        settings = get_global_settings()
        if not (settings.enable_mandatory_bps and settings.enable_strict_mandatory_bps):
            return []
    except Exception:
        logger.warning(
            "settings lookup failed while computing BPS-locked tools; "
            "locking %s conservatively",
            ", ".join(sorted(BPS_MANDATORY_TOOLS)),
            exc_info=True,
        )
    return sorted(BPS_MANDATORY_TOOLS)


def _env_pinned_conflicts(
    states: dict[str, str], env_pinned: dict[str, str]
) -> list[str]:
    """Return tool names the caller tried to flip away from their env-pinned
    value (no-op re-sends of the env-pinned value are allowed)."""
    return [
        name
        for name, state in states.items()
        if name in env_pinned and env_pinned[name] != state
    ]


async def _get_tools(
    server: HomeAssistantSmartMCPServer | None, _: Request
) -> JSONResponse:
    if server is not None:
        tools = await _get_tool_metadata(server)
    else:
        tools = _persistence.load_tool_metadata_cache()
        if not tools:
            # The sidecar's main failure mode (and the most common
            # reason a user lands on a perpetually-loading settings
            # page) is that the parent stdio process didn't write
            # the metadata cache before the sidecar served its
            # first tools request. Log loudly to the sidecar log
            # so post-mortem is one ``cat ~/.ha-mcp/sidecar.log``
            # away. The JS shows a matching diagnostic to the user.
            logger.warning(
                "tool metadata cache is empty or missing at %s — "
                "the parent stdio process likely did not dump it. "
                "Check the MCP-client log for 'Failed to dump tool "
                "metadata cache' from ha_mcp.__main__.",
                _persistence._get_tool_metadata_cache_path(),
            )
    config = _persistence.effective_tool_config()
    states = config.get("tools", {})
    pinned = _persistence.env_pinned_tools()
    for name in DEFAULT_PINNED_TOOLS:
        if name not in states:
            states[name] = "pinned"
    # Mixed read/write tools that stay enabled in Read Only Mode
    # (their write actions are blocked at call time instead). The JS
    # uses this to keep their toggles live while force-disabling the
    # other write-capable tools' rows when the mode is on.
    # Conversation-agent LLM API exposure (#1745): effective value per
    # tool (override else default) so the UI toggle renders the truth,
    # plus the raw overrides so it can tell "user-set" from "default".
    from ..llm_exposure import effective_llm_api_exposed, load_llm_api_overrides
    from ..read_only import READ_ONLY_EXEMPT_TOOLS

    llm_overrides = load_llm_api_overrides()
    # Feature-gated stub rows carry their primary tag but NOT the "beta"
    # tag the registered tool declares (_render_stub renders from
    # FEATURE_GATED_TOOLS metadata, whose tags are never empty) — append
    # it whenever disabled_by is set so the toggle renders
    # hidden-by-default, matching what the stamp will say once the flag
    # turns the real tool on. Every feature-gated tool is beta by
    # definition. (Review finding: a previous `or`-fallback here was dead
    # code, so beta stubs rendered as exposed.)
    llm_effective = {
        t["name"]: effective_llm_api_exposed(
            t["name"],
            [*(t.get("tags") or []), *(["beta"] if t.get("disabled_by") else [])],
            llm_overrides,
        )
        for t in tools
    }

    return JSONResponse(
        {
            "tools": tools,
            "states": states,
            "env_pinned": pinned,
            "read_only_exempt": sorted(READ_ONLY_EXEMPT_TOOLS),
            "llm_api": llm_effective,
            "llm_api_overrides": llm_overrides,
            # LLM API exposure is registered by the ha_mcp_tools custom
            # component (in-process/embedded server). On the add-on,
            # Docker, or standalone server nothing consumes it, so the UI
            # hides the per-tool "LLM API" toggle rather than showing a
            # no-op control.
            "llm_api_available": is_embedded(),
            # BPS-dependent tools whose rows the UI must lock enabled
            # while strict best-practices mode is on (#1886).
            "bps_locked_tools": _bps_locked_tools(),
        }
    )


async def _save_tools(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_JSON,
                "Invalid JSON body",
                suggestions=["Ensure the request body is valid JSON"],
            ),
            status_code=400,
        )

    # A valid-JSON-but-non-object payload (`null`, `[]`, `42`, `"x"`)
    # would otherwise blow up on body.get below as a 500 Internal
    # Server Error — convert to a structured 400 instead.
    if not isinstance(body, dict):
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Request body must be a JSON object",
            ),
            status_code=400,
        )

    raw_states = body.get("states", {})
    if not isinstance(raw_states, dict):
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "'states' must be an object mapping tool names to state values",
            ),
            status_code=400,
        )
    states = _coerce_tool_states(raw_states)

    # Reject attempts to flip env-pinned tools. DISABLED_TOOLS /
    # PINNED_TOOLS are operator-level constraints that cannot be
    # overridden via the UI; callers must unset the env var first.
    # Accept no-op re-sends (state matches the env-pinned value)
    # so the periodic save fired by ``saveConfig`` after every UI
    # change doesn't 409 just because the GET payload echoed
    # env-pinned rows back unchanged. Previously every save with
    # DISABLED_TOOLS / PINNED_TOOLS non-empty failed because the JS
    # POSTs the whole ``toolStates`` map.
    env_pinned = _persistence.env_pinned_tools()
    rejected = _env_pinned_conflicts(states, env_pinned)
    if rejected:
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Refusing to flip env-pinned tools: {', '.join(rejected)}. "
                "Unset DISABLED_TOOLS / PINNED_TOOLS first.",
                suggestions=[
                    "Unset the DISABLED_TOOLS / PINNED_TOOLS environment "
                    "variables (or remove them from your App (add-on)/Docker "
                    "config), then restart to edit these tools from the UI.",
                ],
                context={"rejected": rejected},
            ),
            status_code=409,
        )
    # Drop env-pinned entries from the persisted file so the env
    # vars stay the single source of truth — preserving them in
    # tool_config.json would let a future env-var unset leave the
    # old env-pinned values mis-applied as user-set state.
    states = {name: state for name, state in states.items() if name not in env_pinned}

    # Conversation-agent LLM API exposure overrides (#1745): a sparse
    # {tool_name: bool} map, orthogonal to the states enum. Only bools
    # persist — tools the user never flipped keep tracking their
    # (deny-by-default for beta/dev/restart) defaults across releases.
    raw_llm_api = body.get("llm_api", {})
    if not isinstance(raw_llm_api, dict):
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "'llm_api' must be an object mapping tool names to booleans",
            ),
            status_code=400,
        )
    llm_api_overrides = _coerce_llm_overrides(raw_llm_api)

    # Serialize the load-modify-save against the developer tool
    # (set_tool runs its RMW in a worker thread, in parallel with this
    # loop) via the shared write lock so neither loses the other's update.
    async with get_config_write_lock():
        config = _persistence.load_tool_config()

        # BPS-dependency guard (#1886): reject disabling a BPS-locked tool
        # while strict best-practices mode is on. Mirrors the env-pinned
        # semantics above — only a *change to* "disabled" is rejected, so a
        # payload echoing an already-persisted disabled state (reachable via
        # a hand-edited tool_config.json) still saves and is handled by
        # apply_tool_visibility's strip-and-warn at startup.
        prior_states = config.get("tools", {})
        bps_conflicts = sorted(
            name
            for name in _bps_locked_tools()
            if states.get(name) == "disabled" and prior_states.get(name) != "disabled"
        )
        if bps_conflicts:
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Refusing to disable {', '.join(bps_conflicts)} while "
                    "strict best-practices mode "
                    "(enable_strict_mandatory_bps) is on — strict mode "
                    "publishes its acknowledgment key only through that "
                    "tool, so disabling it would block every gated write.",
                    suggestions=[
                        "Turn off 'Strict best-practices mode' in the Server "
                        "Settings tab first, then disable the tool.",
                    ],
                    context={"rejected": bps_conflicts},
                ),
                status_code=409,
            )

        # The enable/disable/pin half still needs a restart to apply
        # (visibility is wired at server build); the LLM-API exposure half
        # applies live (stamped per tools/list). Only demand a restart when
        # the half that needs one actually changed. Compare with the SAME
        # default-pinned padding _get_tools applies to its response — the JS
        # posts that padded map back verbatim, so an unpadded compare would
        # flag every first save as a states change (live-found on #1745).
        states_changed = _padded_pins(config.get("tools", {})) != _padded_pins(states)
        config["tools"] = states
        config[LLM_API_CONFIG_KEY] = llm_api_overrides
        if not _persistence.save_tool_config(config):
            return JSONResponse(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to persist tool config to disk",
                    suggestions=[
                        "Set HA_MCP_CONFIG_DIR to a writable path "
                        + "(read-only filesystem?)",
                        "Check the server logs for the underlying OSError",
                    ],
                ),
                status_code=500,
            )

    disabled_count = sum(1 for s in states.values() if s == "disabled")
    pinned_count = sum(1 for s in states.values() if s == "pinned")
    logger.info(
        "Saved tool config (%s): %d disabled, %d pinned, %d LLM-API overrides",
        "restart required to apply"
        if states_changed
        else "LLM-API exposure applies live",
        disabled_count,
        pinned_count,
        len(llm_api_overrides),
    )

    # Same response shape as ``_save_feature_flags`` and
    # ``_save_backup_config``: every save endpoint returns
    # ``{success, applied, mode, restart_required}`` so the JS can
    # branch on a single field and BroadcastChannel listeners in
    # other tabs can react uniformly. Tool config writes only ever
    # land in the on-disk JSON (no Supervisor round-trip), hence
    # ``mode="file"`` regardless of addon/standalone deployment.
    return JSONResponse(
        {
            "success": True,
            "applied": states,
            "llm_api_applied": llm_api_overrides,
            "mode": "file",
            "restart_required": states_changed,
        }
    )


def build_tools_handlers(server: HomeAssistantSmartMCPServer | None) -> dict[str, Any]:
    """Construct the tool enable/disable/pin + LLM-API route handlers."""

    async def get_tools(request: Request) -> JSONResponse:
        return await _get_tools(server, request)

    async def save_tools(request: Request) -> JSONResponse:
        return await _save_tools(server, request)

    return {"get_tools": get_tools, "save_tools": save_tools}
