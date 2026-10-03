"""System commands: info, system snapshot, backup prep, server entry, diagnose."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .. import template_diagnose
from ..const import (
    COMPONENT_VERSION,
    CONF_ENTRY_TYPE,
    DEFAULT_PIP_SPEC,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    OPT_CHANNEL,
    OPT_PIP_SPEC,
)
from .constants import (
    CAPABILITIES,
    LIMITS,
    SCHEMA_VERSION,
    SERVER_ENTRY_UPDATE_FLUSH_DELAY_S,
    WS_API_PREFIX,
)
from .overview import _overview_repairs
from .registry import (
    _all_entity_entries,
    _entity_partial_dict,
    _enum_value,
    _iter_config_entries,
    _iter_states,
    _resolve_registries,
    _state_as_dict,
)

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(__package__)


async def _template_diagnose_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Async pre-step for ``template_diagnose``: the guarded in-process render."""
    return {
        "diagnosis": await template_diagnose.async_diagnose(
            hass,
            msg["template"],
            msg.get("variables"),
            msg["strict"],
            msg["timeout"],
        )
    }


def _do_template_diagnose(
    hass: HomeAssistant, params: dict[str, Any], *, diagnosis: dict[str, Any]
) -> dict[str, Any]:
    """Return the diagnosis :func:`_template_diagnose_prep` produced."""
    return diagnosis


# =============================================================================
# ha_mcp_tools/info
# =============================================================================
def _do_info(hass: HomeAssistant | None = None) -> dict[str, Any]:
    """Return the handshake payload.

    ``timezone`` (``hass.config.time_zone``) and ``tools_services`` (whether
    the tools entry's HA services are registered) are additive fields consumers
    detect by presence — they carry NO capability entry and do NOT bump
    ``schema_version``. ``hass`` is optional (defaulting to ``None`` so a direct
    ``_do_info()`` still works for callers that only need the static handshake);
    when absent, both degrade to ``None``.

    ``tools_services`` exists because 2.1.0 registers this command surface from
    BOTH entry types (#2289): ``info`` answering no longer implies the tools
    entry — and its filesystem/YAML services — are present (#2292). The server's
    filesystem/YAML gate reads this field to keep raising its actionable
    "tools entry not set up" error on server-entry-only installs.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "component_version": COMPONENT_VERSION,
        "capabilities": list(CAPABILITIES),
        "limits": dict(LIMITS),
        "timezone": _config_time_zone(hass),
        "tools_services": _tools_services_loaded(hass),
    }


def _config_time_zone(hass: HomeAssistant | None) -> str | None:
    """``hass.config.time_zone`` as a string, guarded against a hass-less call."""
    config = getattr(hass, "config", None)
    tz = getattr(config, "time_zone", None)
    return tz if isinstance(tz, str) and tz else None


def _tools_services_loaded(hass: HomeAssistant | None) -> bool | None:
    """True when the tools entry's filesystem/YAML HA services are registered.

    Probes the service registry itself (``read_file`` — one of the services
    ``_async_setup_tools_entry`` registers and its unload removes) rather than
    the config-entry list, so the answer tracks what a caller can actually
    invoke. ``None`` when ``hass`` is absent (the direct ``_do_info()`` call);
    the WS handler always passes ``hass``, so the wire value is a bool.
    """
    services = getattr(hass, "services", None)
    if services is None:
        return None
    # Literal matches SERVICE_READ_FILE in __init__.py (importing it here would
    # be circular).
    return bool(services.has_service(DOMAIN, "read_file"))


# =============================================================================
# ha_mcp_tools/system_snapshot
# =============================================================================
def _do_system_snapshot(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """One consistent synchronous pass over the live objects the health path reads.

    ``{config_entries: [...], issues: [...], entities: [...], states: [...]}`` —
    every section read from the live in-memory objects in a SINGLE synchronous
    handler call, which is the point: it collapses the server's former 3x
    ``config_entries/get`` fetches (a TOCTOU where entries changed mid-read) into
    one coherent snapshot. ``include_*`` flags gate each section; a disabled
    section is an empty list (present, so the consumer need not special-case a
    missing key).

    * ``config_entries`` — identity fields only (``entry_id`` / ``domain`` /
      ``title`` / ``state`` / ``source`` / ``disabled_by``); the health view does
      not need ``options`` / ``subentries`` (and skipping them avoids the secret
      scrub here).
    * ``issues`` — the ``_overview_repairs`` slice, reused verbatim.
    * ``entities`` — the ``registry_lookup`` row shape (``as_partial_dict``).
    * ``states`` — ``State.as_dict()`` per state (REST-parity bodies).
    """
    view = _resolve_registries(hass)
    result: dict[str, Any] = {}
    result["config_entries"] = (
        [_config_entry_identity_row(e) for e in _iter_config_entries(hass)]
        if params.get("include_config_entries", True)
        else []
    )
    result["issues"] = (
        _overview_repairs(hass) if params.get("include_issues", True) else []
    )
    result["entities"] = (
        [
            row
            for row in (_entity_partial_dict(e) for e in _all_entity_entries(view))
            if row is not None
        ]
        if params.get("include_entities", True)
        else []
    )
    result["states"] = (
        _snapshot_states(hass) if params.get("include_states", True) else []
    )
    return result


def _config_entry_identity_row(entry: Any) -> dict[str, Any]:
    """Identity-only config-entry row for the health snapshot (no options/subentries)."""
    return {
        "entry_id": getattr(entry, "entry_id", None),
        "domain": getattr(entry, "domain", None),
        "title": getattr(entry, "title", None),
        "state": _enum_value(getattr(entry, "state", None)),
        "source": getattr(entry, "source", None),
        "disabled_by": _enum_value(getattr(entry, "disabled_by", None)),
    }


def _snapshot_states(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every live state as ``State.as_dict()`` (REST-parity body, unmodified)."""
    out: list[dict[str, Any]] = []
    for state in _iter_states(hass):
        if not getattr(state, "entity_id", None):
            continue
        as_dict = _state_as_dict(state)
        if as_dict is not None:
            out.append(as_dict)
    return out


# =============================================================================
# ha_mcp_tools/backup_prep
# =============================================================================
def _do_backup_prep(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the backup identity the server needs before a create.

    ``{agent_ids: [...], local_agent_id: <str|None>, default_password:
    <str|None>}`` read from the backup integration's in-process manager
    (``hass.data[DATA_MANAGER]``). ``local_agent_id`` uses the SAME preference the
    server's ``_get_local_backup_agent_id`` does — an agent whose ``name`` is
    ``"local"``, preferring ``hassio.local`` (Supervised) over ``backup.local``
    (Core). ``default_password`` is getattr-chained off
    ``manager.config.data.create_backup.password``.

    A missing backup integration (``ImportError``) or an uninitialized manager
    raises ``HomeAssistantError`` so the server's command-error path falls back to
    its legacy WS reads — NOT a silent empty result the server could mistake for
    "no agents". The password is sensitive, but the legacy ``backup/config/info``
    already serves it to the same admin connection (parity, not new exposure).

    STRUCTURAL core drift raises for the same reason, rather than degrading to a
    well-formed authoritative negative: a non-Mapping ``manager.backup_agents``
    (below) or a broken config→data→create_backup chain
    (:func:`_backup_default_password`) would otherwise produce a "no agents" / "no
    password" answer the server trusts and hard-fails on (or, for the password,
    silently drops the restore safety backup) with no fallback. Value-level reads
    (an id string, a genuinely-``None`` password on an intact chain) stay
    getattr-guarded and pass through.
    """
    try:
        from homeassistant.components.backup import DATA_MANAGER
    except ImportError as exc:
        raise _backup_unavailable("backup integration is not available") from exc
    manager = _hass_data_get(hass, DATA_MANAGER)
    if manager is None:
        raise _backup_unavailable("backup manager is not initialized")
    agents = getattr(manager, "backup_agents", None)
    if not isinstance(agents, Mapping):
        raise _backup_unavailable("backup manager exposes no agent mapping")
    return {
        "agent_ids": [str(a) for a in agents],
        "local_agent_id": _preferred_local_agent_id(agents),
        "default_password": _backup_default_password(manager),
    }


def _backup_unavailable(message: str) -> Exception:
    """Build a ``HomeAssistantError`` (imported function-locally — test-stubbable)."""
    from homeassistant.exceptions import HomeAssistantError

    err: Exception = HomeAssistantError(message)
    return err


def _hass_data_get(hass: HomeAssistant, key: Any) -> Any:
    """``hass.data.get(key)`` guarded against a non-mapping / drift."""
    data = getattr(hass, "data", None)
    if not isinstance(data, Mapping):
        return None
    try:
        return data.get(key)
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return None


def _preferred_local_agent_id(agents: Any) -> str | None:
    """The local backup agent id, mirroring the server's hassio-over-core preference.

    Collects agent ids whose agent ``name`` is exactly ``"local"``
    (``hassio.local`` on Supervised, ``backup.local`` on Core both use that
    name), prefers ``hassio.local`` then ``backup.local``, else the first local
    agent, else ``None``.
    """
    if not isinstance(agents, Mapping):
        return None
    local_ids: list[str] = [
        str(agent_id)
        for agent_id, agent in agents.items()
        if getattr(agent, "name", None) == "local"
    ]
    for preferred in ("hassio.local", "backup.local"):
        if preferred in local_ids:
            return preferred
    return local_ids[0] if local_ids else None


def _backup_default_password(manager: Any) -> str | None:
    """``manager.config.data.create_backup.password`` (getattr-chained; ``str``/None).

    A STRUCTURALLY broken chain (a missing ``config`` / ``data`` / ``create_backup``
    link — core drift) RAISES ``HomeAssistantError`` so the server falls back to its
    legacy ``backup/config/info`` read, rather than returning ``None`` the server
    reads as "no default password configured" — which on restore silently drops the
    safety backup while telling the user the password is unset. Only a genuine
    ``None`` ``password`` on an INTACT chain is the authoritative "not configured".
    """
    config = getattr(manager, "config", None)
    data = getattr(config, "data", None)
    create_backup = getattr(data, "create_backup", None)
    if config is None or data is None or create_backup is None:
        raise _backup_unavailable("backup manager config chain is unavailable")
    password = getattr(create_backup, "password", None)
    return password if isinstance(password, str) else None


# =============================================================================
# ha_mcp_tools/server_entry
# =============================================================================
def _find_server_config_entry(hass: HomeAssistant) -> Any | None:
    """Return the component's OWN server config entry, or ``None`` if absent.

    Picks the single ``DOMAIN`` entry stamped with
    ``entry.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_SERVER`` (the one ``entry.data``
    key the component reads — the documented data-minimization exception). Shared
    by the ``server_entry`` READ cap (:func:`_do_server_entry`) and the
    ``server_entry_update`` WRITE cap (:func:`_server_entry_update_prep`), so both
    discriminate the entry identically.
    """
    for entry in _iter_config_entries(hass):
        if getattr(entry, "domain", None) != DOMAIN:
            continue
        if _entry_marker_type(entry) != ENTRY_TYPE_SERVER:
            continue
        return entry
    return None


def _do_server_entry(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Locate the component's OWN server config entry and return its identity.

    Discriminates the server-type entry via :func:`_find_server_config_entry`
    (the ``entry.data[CONF_ENTRY_TYPE] == ENTRY_TYPE_SERVER`` marker). ``channel`` /
    ``pip_spec`` come from ``entry.options`` (``None`` when absent); ``entry_id`` is
    ``None`` when no server entry exists.
    """
    entry = _find_server_config_entry(hass)
    if entry is None:
        return {"entry_id": None, "channel": None, "pip_spec": None}
    options = getattr(entry, "options", None)
    opts = options if isinstance(options, Mapping) else {}
    return {
        "entry_id": getattr(entry, "entry_id", None),
        "channel": opts.get(OPT_CHANNEL),
        "pip_spec": opts.get(OPT_PIP_SPEC),
    }


def _entry_marker_type(entry: Any) -> Any:
    """Read ONLY ``entry.data[CONF_ENTRY_TYPE]`` — the entry-type marker key."""
    data = getattr(entry, "data", None)
    if isinstance(data, Mapping):
        return data.get(CONF_ENTRY_TYPE)
    return None


# =============================================================================
# ha_mcp_tools/server_entry_update  (the server_entry WRITE counterpart — Phase 3)
# =============================================================================
def _do_server_entry_update(
    hass: HomeAssistant, params: dict[str, Any], *, result: dict[str, Any]
) -> dict[str, Any]:
    """Pure sync formatter for ``server_entry_update``.

    ALL of the work — locating the entry, merging the delta against the LIVE
    options, and (unless it is a no-op) scheduling the deferred
    ``async_update_entry`` — happens in the async :func:`_server_entry_update_prep`,
    which hands the finished envelope in as ``result``. This only returns it (the WS
    wrapper's ``send_result`` adds the outer success frame), so no scheduling /
    awaiting work ever runs in a ``_do_*`` step.
    """
    return result


async def _server_entry_update_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Apply a ``channel`` / ``pip_spec`` delta to the server entry, DEFERRED.

    Returns ``{"result": <envelope>}``. The order is load-bearing:

    1. Locate the server entry (:func:`_find_server_config_entry`); a missing entry
       raises ``HomeAssistantError`` so the server's command-error path falls back to
       its legacy options-flow submit.
    2. Require at least one of ``channel`` / ``pip_spec`` (the server always sends
       one — this is defence-in-depth).
    3. Snapshot the ``delta`` (the provided fields, keyed by the OPT_* option keys)
       and the CURRENT ``entry.options`` — used ONLY for the no-op check and the
       ``previous``/``applying`` response envelope. Every UNtouched key is preserved
       because the actual write MERGES the delta against the LIVE ``entry.options``
       at APPLY time (step 5), NOT against this snapshot — so a concurrent change to
       another key during the flush window is not clobbered, and none of the
       URL/secret overrides get blanked (which is why the server drops its
       preserved-key resend on this path).
    4. No-op short-circuit: if the delta applied to the snapshot equals the current
       options, return ``{scheduled: False, unchanged: True, ...}`` WITHOUT
       scheduling. (The update listener's own ``DATA_LAST_OPTIONS`` guard would also
       make such an update not reload, but skipping the schedule keeps it explicit.)
    5. Otherwise schedule the deferred ``_apply`` — which re-reads the LIVE
       ``entry.options`` and merges the delta against THAT before calling
       ``async_update_entry`` — on a HASS-owned background task after
       :data:`SERVER_ENTRY_UPDATE_FLUSH_DELAY_S`, and return
       ``{scheduled: True, ...}`` immediately.

    **The deferred-reload crux.** ``async_update_entry`` fires the server entry's
    update listener, which reloads the entry — tearing down the very in-process
    server thread answering THIS frame. Calling it inline would kill that thread
    before the WS ``{scheduled: True}`` response flushed, so the caller would never
    get its confirmation. It is therefore scheduled after a flush delay. The task is
    created with :func:`hass.async_create_background_task` (hass-owned), NOT
    ``entry.async_create_background_task``: an entry-owned task is cancelled by the
    unload the reload performs, so it could cancel itself before firing — the exact
    trap ``embedded_entry._on_version_update`` documents. Being hass-owned, the task
    survives to invoke ``async_update_entry`` (a synchronous ``@callback`` that
    returns as soon as it schedules the listener), then completes; the reload it
    triggers runs as its own hass task after the response has flushed.
    """
    import asyncio

    from homeassistant.exceptions import HomeAssistantError

    entry = _find_server_config_entry(hass)
    if entry is None:
        raise HomeAssistantError(
            "no ha_mcp_tools in-process server config entry to update"
        )

    has_channel = "channel" in msg
    has_pip_spec = "pip_spec" in msg
    if not has_channel and not has_pip_spec:
        raise HomeAssistantError(
            "server_entry_update needs at least one of channel / pip_spec"
        )

    options = getattr(entry, "options", None)
    current = dict(options) if isinstance(options, Mapping) else {}
    # ``delta`` is the applied write (merged against the LIVE options at APPLY time);
    # ``applying`` is its response-envelope view. The prep-time ``new_options`` is a
    # snapshot used ONLY for the no-op check below, never for the write.
    delta: dict[str, Any] = {}
    applying: dict[str, Any] = {}
    if has_channel:
        delta[OPT_CHANNEL] = msg["channel"]
        applying["channel"] = msg["channel"]
    if has_pip_spec:
        # Normalize like the options flow's ``_normalize`` (config_flow.py): a
        # whitespace-only value OR the default unpinned dist (``DEFAULT_PIP_SPEC``)
        # means "no override" — collapse it to "" so the channel keeps
        # auto-updating. Persisting it verbatim would read as an intentional
        # override and disable auto-updates. This keeps the no-op check honest: a
        # frame that normalizes to the stored value is unchanged, not a schedule.
        # 'clear' (case-insensitive) is the empty string's mangling-proof
        # alias (see ha_dev_manage_server): recognize it here too so raw WS
        # callers and older servers cannot persist the literal word as a pip
        # requirement that fails at install time.
        pip_spec = msg["pip_spec"]
        if str(pip_spec).strip() in ("", DEFAULT_PIP_SPEC) or (
            str(pip_spec).strip().lower() == "clear"
        ):
            pip_spec = ""
        delta[OPT_PIP_SPEC] = pip_spec
        applying["pip_spec"] = pip_spec
    new_options = {**current, **delta}

    entry_id = getattr(entry, "entry_id", None)
    previous = {
        "channel": current.get(OPT_CHANNEL),
        "pip_spec": current.get(OPT_PIP_SPEC),
    }

    if new_options == current:
        return {
            "result": {
                "scheduled": False,
                "unchanged": True,
                "entry_id": entry_id,
                "applying": applying,
                "previous": previous,
            }
        }

    async def _apply() -> None:
        # Deferred so the WS response flushes before the reload this triggers tears
        # down the serving thread. See the docstring for why it is hass-owned. The
        # delta is merged against the LIVE ``entry.options`` HERE (not at prep) so a
        # concurrent change to another key during the flush window is preserved.
        await asyncio.sleep(SERVER_ENTRY_UPDATE_FLUSH_DELAY_S)
        try:
            live = getattr(entry, "options", None)
            merged = {**(dict(live) if isinstance(live, Mapping) else {}), **delta}
            applied = hass.config_entries.async_update_entry(entry, options=merged)
            if applied is False:
                # ``async_update_entry`` returns False when the merged options already
                # match (e.g. a concurrent write applied the same delta first). No
                # caller is left to answer, so surface the no-apply in the log.
                _LOGGER.warning(
                    "server_entry_update: no change applied to entry %s (options "
                    "already current)",
                    entry_id,
                )
            else:
                _LOGGER.info(
                    "server_entry_update applied %s to entry %s", applying, entry_id
                )
        except Exception:  # pragma: no cover - defensive; no caller left to raise to
            _LOGGER.exception("server_entry_update deferred apply failed")

    hass.async_create_background_task(
        _apply(), name=f"{WS_API_PREFIX} server_entry_update"
    )
    return {
        "result": {
            "scheduled": True,
            "entry_id": entry_id,
            "applying": applying,
            "previous": previous,
        }
    }
