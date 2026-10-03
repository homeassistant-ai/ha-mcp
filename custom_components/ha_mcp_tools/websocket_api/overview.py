"""The ``helpers_list``, ``overview`` and ``states`` read commands."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .constants import FLOW_HELPER_DOMAINS, HELPERS_LIST_COLLECTION_DOMAINS
from .registry import (
    _all_area_entries,
    _all_device_entries,
    _all_entity_entries,
    _call_no_arg,
    _current_friendly_name,
    _device_dict_repr,
    _effective_device_area_id,
    _entities_by_config_entry,
    _enum_value,
    _iso,
    _iter_config_entries,
    _iter_states,
    _mapping_values,
    _plainify,
    _reg_entity,
    _reg_name,
    _RegistryView,
    _resolve_registries,
    _safe,
    _state_as_dict,
    _state_get,
)
from .search_config import _collection_storage_index
from .secrets import _load_secret_scrub, _scrub_secret_values

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(__package__)


# =============================================================================
# ha_mcp_tools/helpers_list
# =============================================================================
def _do_helpers_list(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    secret_values: frozenset[str] = frozenset(),
    secret_scrub_degraded: bool = False,
) -> dict[str, Any]:
    """List collection helpers (live state bodies) + flow helpers (config-entry options).

    Flow-helper ``options`` come straight from ``ConfigEntry.options`` — no
    OptionsFlow start/abort dance, and NEVER ``entry.data`` (integration
    credentials). Every record carries the CURRENT entity_id + display name from
    the entity registry so a renamed helper shows current values (issue #1794),
    not the stale storage-collection name.

    Flow-helper ``options`` share the credential-bearing exposure class of
    ``config_entries``'s ``options`` (a flow helper IS a config entry), so they pass
    through the SAME best-effort resolved-``!secret`` scrub for uniformity — a
    present-but-unreadable ``secrets.yaml`` degrades the scrub to a no-op and sets
    ``secret_scrub_degraded: true`` (present ONLY when degraded). Collection-helper
    bodies (storage collection / live state attributes) are not scrubbed — they carry
    no YAML-resolved secret.

    ``covered_types`` names exactly the helper_type values this command can
    enumerate (the state-machine collection domains + the flow domains, minus the
    flow set when ``include_flow_helpers`` is false). It is the anti-silent-wrong
    signal: for a requested helper_type NOT in ``covered_types`` (e.g. ``tag``,
    which has no state entity), an empty ``helpers`` list means "cannot
    enumerate", NOT "none exist" — the server must fall back to its legacy
    ``<type>/list`` path rather than trust the emptiness.
    """
    requested = params.get("helper_types")
    type_filter = frozenset(requested) if requested else None
    include_flow = params.get("include_flow_helpers", True)

    view = _resolve_registries(hass)
    helpers = _collection_helpers_list(hass, view, type_filter)
    covered = set(HELPERS_LIST_COLLECTION_DOMAINS)
    if include_flow:
        helpers.extend(_flow_helpers_list(hass, view, type_filter, secret_values))
        covered |= FLOW_HELPER_DOMAINS
    result: dict[str, Any] = {
        "helpers": helpers,
        "count": len(helpers),
        "covered_types": sorted(covered),
    }
    if include_flow and secret_scrub_degraded:
        result["secret_scrub_degraded"] = True
    return result


async def _helpers_list_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Async pre-step for ``helpers_list``: load the secret-scrub set off the loop.

    Only the flow-helper ``options`` are scrubbed, so the blocking ``secrets.yaml``
    read is skipped entirely when ``include_flow_helpers`` is false (perf gate,
    mirroring ``search``'s entity-only skip). The read runs in the executor via
    :meth:`hass.async_add_executor_job`, keeping :func:`_do_helpers_list` a pure
    in-memory read. See :func:`_load_secret_scrub`.
    """
    if not msg.get("include_flow_helpers", True):
        return {"secret_values": frozenset(), "secret_scrub_degraded": False}
    values, degraded = await hass.async_add_executor_job(_load_secret_scrub, hass)
    return {"secret_values": values, "secret_scrub_degraded": degraded}


def _collection_helpers_list(
    hass: HomeAssistant, view: _RegistryView, type_filter: frozenset[str] | None
) -> list[dict[str, Any]]:
    """Collection helpers from the state machine (input_*, counter, timer, zone, …).

    The record's ``config`` is the entity's real storage ``_config`` body when
    reachable — so a schedule surfaces its weekday blocks, which the live state
    attributes omit — falling back to the state attributes otherwise (see
    :func:`_collection_storage_index`). ``name`` stays the CURRENT display name
    (a rename updates the registry, not the storage body — issue #1794).
    """
    out: list[dict[str, Any]] = []
    storage = _collection_storage_index(hass, HELPERS_LIST_COLLECTION_DOMAINS)
    for state in _iter_states(hass):
        entity_id = getattr(state, "entity_id", "") or ""
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain not in HELPERS_LIST_COLLECTION_DOMAINS:
            continue
        if type_filter is not None and domain not in type_filter:
            continue
        attrs = getattr(state, "attributes", None) or {}
        object_id = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
        reg = _reg_entity(view, entity_id)
        # Current display name: state friendly_name reflects a registry rename;
        # fall back to the registry name, then the object_id.
        current = attrs.get("friendly_name") if isinstance(attrs, Mapping) else None
        name = current or _reg_name(reg) or object_id
        # Prefer the real storage body + id; the state attributes omit fields like
        # a schedule's weekday blocks.
        stored = storage.get(entity_id)
        if stored is not None:
            body, storage_id = stored
        else:
            body = dict(attrs) if isinstance(attrs, Mapping) else {}
            storage_id = getattr(reg, "unique_id", None) or object_id
        out.append(
            {
                "helper_type": domain,
                "kind": "collection",
                "entity_id": entity_id,
                "object_id": object_id,
                "name": str(name),
                "storage_id": storage_id,
                "config": _plainify(body),
            }
        )
    return out


def _flow_helpers_list(
    hass: HomeAssistant,
    view: _RegistryView,
    type_filter: frozenset[str] | None,
    secret_values: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Flow (config-entry-backed) helpers — options + title + entry_id, never data.

    ``options`` is passed through the same resolved-``!secret`` scrub
    ``config_entries`` applies (a flow helper is a config entry, so its ``options``
    share the same exposure class); an empty ``secret_values`` is a no-op.
    """
    out: list[dict[str, Any]] = []
    entity_by_entry = _entities_by_config_entry(view)
    for entry in _iter_config_entries(hass):
        domain = getattr(entry, "domain", None)
        if domain not in FLOW_HELPER_DOMAINS:
            continue
        if type_filter is not None and domain not in type_filter:
            continue
        entry_id = getattr(entry, "entry_id", None)
        title = getattr(entry, "title", None) or ""
        raw_options = getattr(entry, "options", None)
        options = (
            _scrub_secret_values(_plainify(dict(raw_options)), secret_values)
            if isinstance(raw_options, Mapping)
            else {}
        )
        reg = entity_by_entry.get(entry_id)
        entity_id = getattr(reg, "entity_id", None) if reg is not None else None
        name = _reg_name(reg) or _current_friendly_name(hass, entity_id, title)
        out.append(
            {
                "helper_type": domain,
                "kind": "flow",
                "entry_id": entry_id,
                "entity_id": entity_id,
                "name": str(name) if name else title,
                "storage_id": entry_id,
                # Data minimization: options only, never entry.data.
                "options": options,
            }
        )
    return out


# =============================================================================
# ha_mcp_tools/overview
# =============================================================================
def _do_overview(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the raw in-process reads the server's overview path consumes.

    NOT the assembled overview envelope — the RAW slices the server's
    ``get_system_overview`` + ``ha_get_overview`` wrapper fetch today (states,
    services, entity/device/area registries, ``hass.config``, persistent
    notifications, repairs issues). The server runs its existing overview logic
    over these, so detail_level / domains / pagination stay server-side and no
    logic is duplicated (or drifts) in the component. Registries are BARE lists
    (not the ``{success, result}`` WS wrapper); the server adapts. Collapses the
    ~8 round-trips to one in-process call.

    ``slice_errors`` names any slice whose accessor RAISED (empty list when
    clean). A missing/None registry degrades to an empty slice WITHOUT an entry —
    that is "nothing here", not "failed". A genuine raise is caught per slice,
    logged, and named here so the server can tell "empty" from "failed" and fall
    back to its legacy REST read for just that slice instead of trusting the
    empty value.
    """
    include_notifications = params.get("include_notifications", True)
    include_repairs = params.get("include_repairs", True)

    view = _resolve_registries(hass)
    slice_errors: list[str] = []

    def _slice(name: str, fn: Any, default: Any) -> Any:
        try:
            return fn()
        except Exception:
            _LOGGER.warning("overview slice %r degraded", name, exc_info=True)
            slice_errors.append(name)
            return default

    result: dict[str, Any] = {
        "states": _slice("states", lambda: _overview_states(hass), []),
        "services": _slice("services", lambda: _overview_services(hass), []),
        "entity_registry": _slice(
            "entity_registry", lambda: _overview_entity_registry(view), []
        ),
        "device_registry": _slice(
            "device_registry", lambda: _overview_device_registry(view), []
        ),
        "area_registry": _slice(
            "area_registry", lambda: _overview_area_registry(view), []
        ),
        "config": _slice("config", lambda: _overview_config(hass), {}),
        "notifications": _slice(
            "notifications", lambda: _overview_notifications(hass), []
        )
        if include_notifications
        else [],
        "repairs": _slice("repairs", lambda: _overview_repairs(hass), [])
        if include_repairs
        else [],
    }
    result["slice_errors"] = slice_errors
    return result


def _overview_states(hass: HomeAssistant) -> list[dict[str, Any]]:
    """States in the ``client.get_states()`` shape the overview consumer reads."""
    out: list[dict[str, Any]] = []
    for state in _iter_states(hass):
        entity_id = getattr(state, "entity_id", None)
        if not entity_id:
            continue
        attrs = getattr(state, "attributes", None) or {}
        out.append(
            {
                "entity_id": entity_id,
                "state": getattr(state, "state", "unknown"),
                "attributes": _plainify(dict(attrs))
                if isinstance(attrs, Mapping)
                else {},
                "last_changed": _iso(getattr(state, "last_changed", None)),
                "last_updated": _iso(getattr(state, "last_updated", None)),
            }
        )
    return out


def _overview_services(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Service catalog in the ``client.get_services()`` list shape.

    The consumer's ``_build_service_stats`` reads only the per-domain service
    *names*, so each service maps to an empty dict — keeps the frame small while
    preserving the ``{domain, services: {name: {...}}}`` structure.
    """
    services = _call_no_arg(getattr(hass, "services", None), "async_services")
    if not isinstance(services, Mapping):
        return []
    out: list[dict[str, Any]] = []
    for domain, svcs in services.items():
        names = list(svcs.keys()) if isinstance(svcs, Mapping) else []
        out.append({"domain": domain, "services": {name: {} for name in names}})
    return out


def _overview_entity_registry(view: _RegistryView) -> list[dict[str, Any]]:
    """Entity registry as a bare list, with the fields the overview + visibility
    consumers read (area/device/labels/entity_category/hidden_by/options/…)."""
    out: list[dict[str, Any]] = []
    for entry in _all_entity_entries(view):
        entity_id = getattr(entry, "entity_id", None)
        if not entity_id:
            continue
        out.append(
            {
                "entity_id": entity_id,
                "area_id": getattr(entry, "area_id", None),
                "device_id": getattr(entry, "device_id", None),
                "labels": sorted(
                    str(x) for x in (getattr(entry, "labels", None) or [])
                ),
                "entity_category": _enum_value(getattr(entry, "entity_category", None)),
                "hidden_by": _enum_value(getattr(entry, "hidden_by", None)),
                "categories": _plainify(getattr(entry, "categories", None) or {}),
                "options": _plainify(getattr(entry, "options", None) or {}),
                "name": getattr(entry, "name", None),
                "original_name": getattr(entry, "original_name", None),
                "platform": getattr(entry, "platform", None),
                "unique_id": getattr(entry, "unique_id", None),
                "disabled_by": _enum_value(getattr(entry, "disabled_by", None)),
            }
        )
    return out


def _overview_device_registry(view: _RegistryView) -> list[dict[str, Any]]:
    """Device registry as a bare list (id + area + labels + name/manufacturer/model)."""
    out: list[dict[str, Any]] = []
    for dev in _all_device_entries(view):
        dev_row = _device_dict_repr(dev)
        if dev_row is None:
            continue
        dev_id = dev_row.get("id")
        if not dev_id:
            continue
        out.append(
            {
                "id": dev_id,
                "area_id": _effective_device_area_id(view, dev),
                "labels": sorted(str(x) for x in (dev_row.get("labels") or [])),
                "name": dev_row.get("name"),
                "name_by_user": dev_row.get("name_by_user"),
                "manufacturer": dev_row.get("manufacturer"),
                "model": dev_row.get("model"),
            }
        )
    return out


def _overview_area_registry(view: _RegistryView) -> list[dict[str, Any]]:
    """Area registry as a bare list (area_id + name + floor_id)."""
    out: list[dict[str, Any]] = []
    for area in _all_area_entries(view):
        area_id = getattr(area, "id", None) or getattr(area, "area_id", None)
        if not area_id:
            continue
        out.append(
            {
                "area_id": area_id,
                "name": getattr(area, "name", None),
                "floor_id": getattr(area, "floor_id", None),
            }
        )
    return out


def _overview_config(hass: HomeAssistant) -> dict[str, Any]:
    """The ``hass.config`` fields the wrapper's ``_fetch_system_info`` reads.

    ``base_url`` is intentionally omitted — the server supplies it from its own
    client; only HA-core config values are the component's to provide.
    """
    config = getattr(hass, "config", None)
    raw = _call_no_arg(config, "as_dict")
    if not isinstance(raw, Mapping):
        return {}
    keys = (
        "version",
        "location_name",
        "time_zone",
        "language",
        "state",
        "country",
        "currency",
        "unit_system",
        "latitude",
        "longitude",
        "elevation",
        "components",
        "safe_mode",
        "internal_url",
        "external_url",
        "allowlist_external_dirs",
    )
    return {k: _plainify(raw[k]) for k in keys if k in raw}


def _overview_notifications(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Active persistent notifications (``persistent_notification/get`` shape)."""
    store = getattr(hass, "data", None)
    data = store.get("persistent_notification") if isinstance(store, Mapping) else None
    return [
        {
            "notification_id": _field(note, "notification_id"),
            "title": _field(note, "title"),
            "message": _field(note, "message"),
            "created_at": _iso(_field(note, "created_at")),
        }
        for note in _notification_values(data)
    ]


def _field(obj: Any, key: str) -> Any:
    """Read ``key`` from a mapping (``.get``) or an object (``getattr``)."""
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _notification_values(data: Any) -> list[Any]:
    """Notification records: ``{id: note}`` mapping values, or a bare list."""
    if isinstance(data, Mapping):
        return list(data.values())
    if isinstance(data, list):
        return list(data)
    return []


def _overview_repairs(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Raw issue-registry entries (the server filters/projects them itself).

    ``ignored`` is derived from ``dismissed_version`` so the server's
    ``filter_active_repairs`` (which keys off ``ignored``) works unchanged.

    Inactive entries are skipped to match HA core's ``ws_list_issues``: on
    restart the registry reloads each stored non-persistent issue as an
    inactive stub (``active=False``), and it stays inactive until the
    integration re-raises or deletes it.
    """
    registry = _safe(ir.async_get, hass)
    issues = getattr(registry, "issues", None) if registry is not None else None
    out: list[dict[str, Any]] = []
    for issue in _mapping_values(issues):
        if getattr(issue, "active", True) is False:
            continue
        dismissed = getattr(issue, "dismissed_version", None)
        out.append(
            {
                "issue_id": getattr(issue, "issue_id", None),
                "domain": getattr(issue, "domain", None),
                "severity": _enum_value(getattr(issue, "severity", None)),
                "translation_key": getattr(issue, "translation_key", None),
                "translation_placeholders": _plainify(
                    getattr(issue, "translation_placeholders", None) or {}
                ),
                "ignored": dismissed is not None,
                "dismissed_version": dismissed,
                "is_fixable": getattr(issue, "is_fixable", None),
                "breaks_in_ha_version": getattr(issue, "breaks_in_ha_version", None),
                "created": _iso(getattr(issue, "created", None)),
                "issue_domain": getattr(issue, "issue_domain", None),
                "learn_more_url": getattr(issue, "learn_more_url", None),
                "active": getattr(issue, "active", None),
            }
        )
    return out


# =============================================================================
# ha_mcp_tools/states
# =============================================================================
def _do_states(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return ``State.as_dict()`` for each requested entity_id + a ``missing`` list.

    ``hass.states.get(id)`` is a pure O(1) in-memory dict read, and core's
    ``State.as_dict()`` is exactly the serialization the REST ``/api/states/<id>``
    endpoint emits — so a component-served record is byte-identical to the legacy
    per-id REST fetch by construction (the WS transport JSON-encodes the same
    datetimes to the same ISO strings the REST layer does). The body is returned
    UNMODIFIED — never ``_plainify``'d — precisely so that byte-parity holds:
    ``_plainify``'s ``str()`` would render a datetime with a space separator where
    both REST and WS use ``isoformat``'s ``T``. No freshness or secrets concern:
    state bodies are always live and carry no ``!secret`` plaintext. The server
    enforces its own ``MAX_ENTITIES`` cap before calling, so no per-frame guard is
    needed here (100 full states is well within one frame — ``overview`` already
    returns every state in one call).
    """
    entity_ids = params.get("entity_ids") or []
    states: dict[str, Any] = {}
    missing: list[str] = []
    for entity_id in entity_ids:
        state = _state_get(hass, entity_id)
        if state is None:
            missing.append(entity_id)
            continue
        as_dict = _state_as_dict(state)
        if as_dict is None:
            # A live state that could not be serialized (core drift) goes to
            # ``missing`` rather than emitting a null state indistinguishable from
            # a real value — the server maps ``missing`` onto its per-id contract.
            missing.append(entity_id)
            continue
        states[entity_id] = as_dict
    return {"states": states, "missing": missing}
