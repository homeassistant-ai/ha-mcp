"""Registry view, registry accessors and coercion helpers for the commands."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(f"{__package__}.websocket_api")


# =============================================================================
# ha_mcp_tools/search
# =============================================================================
@dataclass
class _RegistryView:
    """Bundle of the five HA registries (any may be ``None`` if unavailable)."""

    entity: Any = None
    area: Any = None
    floor: Any = None
    label: Any = None
    device: Any = None
    _access_failures: set[str] = dataclass_field(
        default_factory=set, init=False, repr=False, compare=False
    )

    # One request-local, conflict-filtered semantic snapshot plus the identities
    # removed from it. Visibility filtering consumes the former and its warning
    # projection consumes the latter, so both paths observe the same evidence.
    _device_entries_by_id_cache: dict[str, Any] | None = dataclass_field(
        default=None, init=False, repr=False, compare=False
    )
    _device_conflicting_ids_cache: frozenset[str] | None = dataclass_field(
        default=None, init=False, repr=False, compare=False
    )
    _device_invalid_area_ids_cache: frozenset[str] | None = dataclass_field(
        default=None, init=False, repr=False, compare=False
    )


def _resolve_registries(hass: HomeAssistant) -> _RegistryView:
    """Snapshot the five registries. Test seam — monkeypatched in unit tests."""
    return _RegistryView(
        entity=_safe(er.async_get, hass),
        area=_safe(ar.async_get, hass),
        floor=_safe(fr.async_get, hass),
        label=_safe(lr.async_get, hass),
        device=_safe(dr.async_get, hass),
    )


def _safe(fn: Any, hass: HomeAssistant) -> Any:
    try:
        return fn(hass)
    except Exception:  # pragma: no cover - defensive; core drift  # noqa: BLE001
        return None


def _substrate_unavailable(name: str) -> Exception:
    """Build a ``HomeAssistantError`` for a drifted / unavailable core substrate.

    A core registry / service / state accessor that RAISED or was renamed comes
    back as ``None`` (registries, via ``_safe``) or a non-``Mapping``
    (services / descriptions) from the guarded readers. For the reads whose WHOLE
    answer is that substrate — ``entity_lookup``, ``registries``,
    ``reference_data``, ``services_list`` — returning a well-formed EMPTY would let
    the server trust an authoritative-negative it should instead fall back to legacy
    for (mistaking core DRIFT for "no such entry" / "empty catalog"). Raising routes
    the server's command-error path to its legacy WS/REST read, mirroring
    :func:`_backup_unavailable` / :func:`_registries_missing_category_scopes`. A
    genuinely-EMPTY-but-present substrate (a real empty registry) is NOT drift and
    keeps returning its empty result — the guards below key off unavailability
    (``None`` / non-``Mapping``), never off an empty-but-valid collection.
    """
    from homeassistant.exceptions import HomeAssistantError

    err: Exception = HomeAssistantError(
        f"ha_mcp_tools: the {name} is unavailable (core drift); the server should "
        "fall back to its legacy read"
    )
    return err


def _plainify(value: Any) -> Any:
    """Best-effort conversion of registry/state objects to plain JSON-able data.

    Recurses on any ``Mapping`` (not just ``dict``): a nested ``MappingProxyType``
    (common in ``ConfigEntry.options``) must be walked into a plain dict, NOT
    stringified — otherwise a secret buried inside it survives embedded in the repr
    string, past the equality scrub that runs on the result.
    """
    if isinstance(value, Mapping):
        return {str(k): _plainify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plainify(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# =============================================================================
# Registry accessors (all getattr-guarded against core drift)
# =============================================================================
def _iter_states(hass: HomeAssistant) -> list[Any]:
    states = getattr(hass, "states", None)
    getter = getattr(states, "async_all", None) if states is not None else None
    if getter is None:
        return []
    try:
        return list(getter())
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return []


def _iter_config_entries(hass: HomeAssistant) -> list[Any]:
    config_entries = getattr(hass, "config_entries", None)
    getter = (
        getattr(config_entries, "async_entries", None)
        if config_entries is not None
        else None
    )
    if getter is None:
        return []
    try:
        return list(getter())
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return []


def _reg_entity(view: _RegistryView, entity_id: str) -> Any:
    return _call_lookup(view, "entity", "async_get", entity_id)


def _device(view: _RegistryView, device_id: str | None) -> Any:
    if not device_id:
        return None
    return _call_lookup(view, "device", "async_get", device_id)


def _device_collection_values(
    view: _RegistryView,
    collection: Any,
    *,
    collection_name: str,
    mapping_like: bool = False,
) -> list[Any]:
    """Enumerate a Core device collection across old and 2026.9 shapes.

    Before Core 2026.9 ``registry.devices`` was a mapping-like container. Core
    2026.9 exposes supported iterable collections for both ``devices`` and
    ``child_devices``. Mapping fakes and older containers still use ``values``;
    modern collections are consumed by iteration so the deprecated mapping API
    on ``registry.devices`` is not invoked.
    """
    if collection is None:
        return []
    if mapping_like or isinstance(collection, Mapping):
        try:
            return list(collection.values())
        except Exception:  # pragma: no cover - defensive
            view._access_failures.add("device")
            _LOGGER.warning(
                "failed to enumerate device registry collection %s",
                collection_name,
                exc_info=True,
            )
            return []
    try:
        return list(collection)
    except Exception:  # pragma: no cover - defensive
        view._access_failures.add("device")
        _LOGGER.warning(
            "failed to enumerate device registry collection %s",
            collection_name,
            exc_info=True,
        )
        return []


def _unambiguous_device_entries(view: _RegistryView) -> dict[str, Any]:
    """Return one request-local map of unambiguous main and child devices.

    Core 2026.9 stores child devices in a separate collection. Older releases
    have only the mapping-like ``devices`` container. Duplicate ids cannot occur
    in a valid Core registry; if a drifted/corrupt view supplies conflicting
    entries, remove that identity rather than choosing one arbitrarily.
    """
    if view._device_entries_by_id_cache is not None:
        return view._device_entries_by_id_cache
    reg = view.device
    if reg is None:
        view._device_entries_by_id_cache = {}
        view._device_conflicting_ids_cache = frozenset()
        view._device_invalid_area_ids_cache = frozenset()
        return view._device_entries_by_id_cache
    main_collection = getattr(reg, "devices", None)
    if hasattr(reg, "child_devices"):
        candidates = _device_collection_values(
            view, main_collection, collection_name="devices"
        )
        candidates.extend(
            _device_collection_values(
                view,
                getattr(reg, "child_devices", None),
                collection_name="child_devices",
            )
        )
    else:
        # The pre-2026.9 container is mapping-like and iterates ids, not entries.
        candidates = _device_collection_values(
            view, main_collection, collection_name="devices", mapping_like=True
        )

    by_id: dict[str, Any] = {}
    conflicts: set[str] = set()
    for entry in candidates:
        device_id = getattr(entry, "id", None)
        if not isinstance(device_id, str) or not device_id or device_id in conflicts:
            continue
        if device_id not in by_id:
            by_id[device_id] = entry
            continue
        prior = _device_dict_repr(by_id[device_id])
        current = _device_dict_repr(entry)
        if prior != current or prior is None:
            conflicts.add(device_id)
            del by_id[device_id]
            _LOGGER.warning(
                "device registry contained conflicting device identity %r; "
                "excluding it from this request",
                device_id,
            )
    view._device_entries_by_id_cache = by_id
    view._device_conflicting_ids_cache = frozenset(conflicts)
    return view._device_entries_by_id_cache


def _all_device_entries(view: _RegistryView) -> list[Any]:
    """Return every unambiguous main and child device entry once."""
    return list(_unambiguous_device_entries(view).values())


def _conflicting_device_ids(view: _RegistryView) -> frozenset[str]:
    """Return device identities excluded from this request as conflicting."""
    _unambiguous_device_entries(view)
    return view._device_conflicting_ids_cache or frozenset()


def _invalid_device_area_ids(view: _RegistryView) -> frozenset[str]:
    """Return device ids whose area evidence is malformed or has invalid ancestry."""
    devices_by_id = _unambiguous_device_entries(view)
    if view._device_invalid_area_ids_cache is not None:
        return view._device_invalid_area_ids_cache
    invalid: set[str] = set()
    for device_id, device in devices_by_id.items():
        row = _device_dict_repr(device)
        if row is None:
            invalid.add(device_id)
            continue
        direct_area = row.get("area_id")
        if direct_area is not None:
            if not isinstance(direct_area, str) or not direct_area:
                invalid.add(device_id)
            continue
        parent_id = row.get("parent_device_id")
        if parent_id is None:
            continue
        if not isinstance(parent_id, str) or not parent_id:
            invalid.add(device_id)
            continue
        parent = devices_by_id.get(parent_id)
        parent_row = _device_dict_repr(parent) if parent is not None else None
        if parent_row is None or parent_row.get("parent_device_id") is not None:
            invalid.add(device_id)
            continue
        parent_area = parent_row.get("area_id")
        if parent_area is not None and (
            not isinstance(parent_area, str) or not parent_area
        ):
            invalid.add(device_id)
    view._device_invalid_area_ids_cache = frozenset(invalid)
    return view._device_invalid_area_ids_cache


def _effective_device_area_id(view: _RegistryView, device: Any) -> str | None:
    """Return Core 2026.9's direct-or-parent effective device area."""
    devices_by_id = _unambiguous_device_entries(view)
    device_row = _device_dict_repr(device)
    if device_row is None:
        return None
    device_id = device_row.get("id")
    if not isinstance(device_id, str) or not device_id:
        return None
    device = devices_by_id.get(device_id)
    if device is None:
        # Conflicting identities never contribute semantic placement.
        return None
    device_row = _device_dict_repr(device)
    if device_row is None:
        return None
    direct_area = device_row.get("area_id")
    if direct_area is not None:
        return direct_area if isinstance(direct_area, str) and direct_area else None
    parent_id = device_row.get("parent_device_id")
    if not isinstance(parent_id, str) or not parent_id:
        return None
    parent = devices_by_id.get(parent_id)
    parent_row = _device_dict_repr(parent) if parent is not None else None
    if parent_row is None or parent_row.get("parent_device_id") is not None:
        # Core requires a main-device parent. This also bounds malformed cycles.
        return None
    parent_area = parent_row.get("area_id")
    return parent_area if isinstance(parent_area, str) and parent_area else None


def _area_name(view: _RegistryView, area_id: str | None) -> str | None:
    if not area_id:
        return None
    area = _call_lookup(view, "area", "async_get_area", area_id)
    name = getattr(area, "name", None) if area is not None else None
    return str(name) if name else None


def _floor_name_for_area(view: _RegistryView, area_id: str | None) -> str | None:
    if not area_id:
        return None
    area = _call_lookup(view, "area", "async_get_area", area_id)
    floor_id = getattr(area, "floor_id", None) if area is not None else None
    if not floor_id:
        return None
    floor = _call_lookup(view, "floor", "async_get_floor", floor_id)
    name = getattr(floor, "name", None) if floor is not None else None
    return str(name) if name else None


def _label_names(view: _RegistryView, label_ids: Any) -> list[str]:
    names: list[str] = []
    for label_id in sorted(label_ids or []):
        label = _call_lookup(view, "label", "async_get_label", label_id)
        name = getattr(label, "name", None) if label is not None else None
        names.append(str(name) if name else str(label_id))
    return names


def _call_lookup(view: _RegistryView, registry_name: str, method: str, key: str) -> Any:
    registry = getattr(view, registry_name)
    if registry is None:
        return None
    getter = getattr(registry, method, None)
    if getter is None:
        return None
    try:
        return getter(key)
    except Exception:  # noqa: BLE001
        view._access_failures.add(registry_name)
        return None


def _call_no_arg(obj: Any, method: str) -> Any:
    """Call a no-argument accessor (e.g. ``async_services``), guarded."""
    if obj is None:
        return None
    fn = getattr(obj, method, None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return None


def _iso(value: Any) -> Any:
    """Serialize a datetime-ish value to an ISO string; pass through otherwise.

    HA registry/state timestamps are ``datetime`` objects. The WS layer can
    encode them, but the REST shapes the overview consumer mirrors carry ISO
    strings, so normalize here for a stable wire contract.
    """
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:  # pragma: no cover - defensive  # noqa: BLE001
            return None
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def _enum_value(value: Any) -> Any:
    """Unwrap a StrEnum-ish registry field (``entity_category``/``hidden_by``/…).

    HA stores these as enums whose ``.value`` is the wire string; a plain string
    (or None) passes through unchanged. Also unwraps ``ConfigEntryState`` (a plain
    ``Enum`` whose ``.value`` is the wire string, e.g. ``"loaded"``) — core's
    ``config_entries/get`` serializes it as ``entry.state.value``.
    """
    if value is None or isinstance(value, str):
        return value
    return getattr(value, "value", str(value))


def _timestamp(value: Any) -> float | None:
    """Serialize a datetime-ish registry timestamp as a float, like core.

    core's registry WS list responses emit ``created_at`` / ``modified_at`` via
    ``entry.created_at.timestamp()`` (a float, seconds since epoch), so mirror
    that. A value that is already numeric passes through; anything else (or a
    ``.timestamp()`` that raises) degrades to ``None``.
    """
    if value is None:
        return None
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        try:
            return float(ts())
        except Exception:  # pragma: no cover - defensive  # noqa: BLE001
            return None
    return float(value) if isinstance(value, (int, float)) else None


def _reg_name(reg: Any) -> str | None:
    """Current display name from a registry entry: user override, else original."""
    if reg is None:
        return None
    name = getattr(reg, "name", None) or getattr(reg, "original_name", None)
    return str(name) if name else None


def _current_friendly_name(
    hass: HomeAssistant, entity_id: str | None, fallback: str | None
) -> str | None:
    """Current friendly_name from the state machine, falling back to the config name."""
    if entity_id:
        for state in _iter_states(hass):
            if getattr(state, "entity_id", None) != entity_id:
                continue
            attrs = getattr(state, "attributes", None) or {}
            friendly = (
                attrs.get("friendly_name") if isinstance(attrs, Mapping) else None
            )
            if friendly:
                return str(friendly)
            break
    if fallback:
        return str(fallback)
    return entity_id


def _entities_by_config_entry(view: _RegistryView) -> dict[Any, Any]:
    """Index the first registry entity bound to each config entry (flow helpers)."""
    index: dict[Any, Any] = {}
    for entry in _all_entity_entries(view):
        config_entry_id = getattr(entry, "config_entry_id", None)
        if config_entry_id and config_entry_id not in index:
            index[config_entry_id] = entry
    return index


def _all_entity_entries(view: _RegistryView) -> list[Any]:
    """All entity-registry entries (``registry.entities`` is a mapping in HA)."""
    reg = view.entity
    entities = getattr(reg, "entities", None) if reg is not None else None
    if entities is None:
        return []
    try:
        return list(entities.values())
    except Exception:  # pragma: no cover - defensive
        _LOGGER.warning("entity registry enumeration failed", exc_info=True)
        return []


def _all_area_entries(view: _RegistryView) -> list[Any]:
    """All area-registry entries via ``async_list_areas()`` or the ``areas`` mapping."""
    reg = view.area
    if reg is None:
        return []
    listed = _call_no_arg(reg, "async_list_areas")
    if listed is not None:
        try:
            return list(listed)
        except Exception:  # pragma: no cover - defensive  # noqa: BLE001
            return []
    return _mapping_values(getattr(reg, "areas", None))


def _mapping_values(mapping: Any) -> list[Any]:
    """``list(mapping.values())`` guarded against a non-mapping / drift."""
    if mapping is None:
        return []
    try:
        return list(mapping.values())
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return []


def _state_get(hass: HomeAssistant, entity_id: str) -> Any:
    """``hass.states.get(entity_id)`` guarded against core drift (``None`` if absent)."""
    states = getattr(hass, "states", None)
    getter = getattr(states, "get", None) if states is not None else None
    if getter is None:
        return None
    try:
        return getter(entity_id)
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return None


def _state_as_dict(state: Any) -> Any:
    """core ``State.as_dict()`` verbatim — the REST ``/api/states/<id>`` shape.

    Returned unmodified so the WS transport encodes its datetimes with the same
    ``isoformat`` the REST layer uses (byte-parity — see :func:`_do_states`).
    """
    as_dict = getattr(state, "as_dict", None)
    if callable(as_dict):
        try:
            return as_dict()
        except Exception:  # pragma: no cover - defensive  # noqa: BLE001
            return None
    return None


def _device_dict_repr(entry: Any) -> dict[str, Any] | None:
    """core ``DeviceEntry.dict_repr`` verbatim — the ``config/device_registry/list`` shape.

    Returned UNMODIFIED so the WS transport encodes it with the same JSON
    serializer ``config/device_registry/list`` uses (byte-parity — see
    :func:`_do_device_get`). Guarded against core drift: a missing/raising
    ``dict_repr`` yields ``None`` rather than propagating.
    """
    try:
        repr_dict = entry.dict_repr
    except Exception:  # pragma: no cover - defensive; core drift  # noqa: BLE001
        return None
    return repr_dict if isinstance(repr_dict, dict) else None


def _entity_partial_dict(entry: Any) -> dict[str, Any] | None:
    """core ``RegistryEntry.as_partial_dict`` verbatim — the ``config/entity_registry/list`` shape.

    Returned UNMODIFIED so the WS transport encodes it with the same serializer
    ``config/entity_registry/list`` uses (byte-parity, mirroring
    :func:`_device_dict_repr`). Guarded against core drift.
    """
    try:
        partial = entry.as_partial_dict
    except Exception:  # pragma: no cover - defensive; core drift  # noqa: BLE001
        return None
    return partial if isinstance(partial, dict) else None


def _all_floor_entries(view: _RegistryView) -> list[Any]:
    """All floor-registry entries via ``async_list_floors()`` or the ``floors`` mapping."""
    reg = view.floor
    if reg is None:
        return []
    listed = _call_no_arg(reg, "async_list_floors")
    if listed is not None:
        try:
            return list(listed)
        except Exception:  # pragma: no cover - defensive  # noqa: BLE001
            return []
    return _mapping_values(getattr(reg, "floors", None))


def _all_label_entries(view: _RegistryView) -> list[Any]:
    """All label-registry entries via ``async_list_labels()`` or the ``labels`` mapping."""
    reg = view.label
    if reg is None:
        return []
    listed = _call_no_arg(reg, "async_list_labels")
    if listed is not None:
        try:
            return list(listed)
        except Exception:  # pragma: no cover - defensive  # noqa: BLE001
            return []
    return _mapping_values(getattr(reg, "labels", None))


def _effective_area_for_entry(view: _RegistryView, entry: Any) -> str | None:
    """Resolve entity direct area, then its device's direct-or-parent effective area."""
    area_id = getattr(entry, "area_id", None)
    if area_id is not None:
        return area_id if isinstance(area_id, str) and area_id else None
    device_id = getattr(entry, "device_id", None)
    if isinstance(device_id, str) and device_id:
        dev = _unambiguous_device_entries(view).get(device_id)
        dev_area = _effective_device_area_id(view, dev) if dev is not None else None
        return dev_area if isinstance(dev_area, str) and dev_area else None
    return None


def _effective_labels_for_entry(view: _RegistryView, entry: Any) -> set[str]:
    """An entity's labels plus its device's labels (device labels apply to entities)."""
    labels = set(getattr(entry, "labels", None) or [])
    device_id = getattr(entry, "device_id", None)
    if isinstance(device_id, str) and device_id:
        dev = _unambiguous_device_entries(view).get(device_id)
        if dev is not None:
            dev_row = _device_dict_repr(dev)
            if dev_row is not None:
                labels |= set(dev_row.get("labels") or [])
    return labels
