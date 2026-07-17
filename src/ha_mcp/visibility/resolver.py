"""Pure resolver: compute the hidden entity_id set from a registry list + config.

The filter is a conjunction of independent hide dimensions: an entity is shown
only if it passes *all* active dimensions (deny, allowlist, Assist exposure,
excludes). Because every dimension can only *hide*, never un-hide, the order in
which they are applied does not affect the final set - there is no priority
ladder to reason about, only a union of hide reasons.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..utils.data_paths import get_data_dir
from .model import VisibilityConfig
from .persistence import load_visibility_config

logger = logging.getLogger(__name__)

# HA's EntityCategory enum has exactly these two values (homeassistant.const).
# Unknown categories are dropped with a warning rather than hard-rejected, so a
# future HA category can never turn a config into a load failure that fails the
# whole filter open; the drop is surfaced through the response warnings channel.
KNOWN_ENTITY_CATEGORIES = frozenset({"config", "diagnostic"})

# Mirror of HA-core's default-exposure constants for the Assist ("conversation")
# assistant, from homeassistant/components/homeassistant/exposed_entities.py
# (home-assistant/core dev, verified 2026-07-04). HA has no websocket command
# that returns computed effective exposure, so respect_assist_exposure must
# reconstruct async_should_expose client-side. These are small and stable but do
# couple to HA internals: if HA changes them, the mirror drifts until updated.
DEFAULT_EXPOSED_DOMAINS = frozenset(
    {
        "climate",
        "cover",
        "fan",
        "humidifier",
        "light",
        "media_player",
        "scene",
        "switch",
        "todo",
        "vacuum",
        "water_heater",
    }
)
DEFAULT_EXPOSED_BINARY_SENSOR_DEVICE_CLASSES = frozenset(
    {"door", "garage_door", "lock", "motion", "opening", "presence", "window"}
)
DEFAULT_EXPOSED_SENSOR_DEVICE_CLASSES = frozenset(
    {
        "aqi",
        "co",
        "co2",
        "humidity",
        "pm10",
        "pm25",
        "temperature",
        "volatile_organic_compounds",
    }
)

_REGISTRY_UNAVAILABLE_WARNING = (
    "Entity visibility filter is enabled but the entity registry was "
    "unavailable; results are unfiltered (the denylist still applies)."
)
_ASSIST_UNAVAILABLE_WARNING = (
    "Entity visibility filter is enabled with respect_assist_exposure but the "
    "Assist exposure data was unavailable; that dimension is skipped for this "
    "request (other dimensions still apply)."
)
_ALLOWLIST_REGISTRY_EMPTY_WARNING = (
    "Entity visibility filter is enabled with an area/label allowlist but the "
    "entity registry returned no entries; those allow dimensions are skipped for "
    "this request (an allow_entity_ids list, if set, still applies) so the filter "
    "does not blank every entity."
)


def _normalize_labels(raw: object) -> list[str]:
    """Coerce a registry entry's ``labels`` field to a list for set ops.

    A bare string counts as one label (not char-iterated); an unexpected
    non-iterable payload skips label matching for that entry rather than raising
    and fail-open-disabling the whole filter.
    """
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    return []


def _parse_device_registry(
    device_registry_result: object,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Parse ``config/device_registry/list`` into device_id -> area_id / labels.

    Tolerates a missing/degraded payload (returns empty maps) so the area/label
    dimensions simply fall back to entity-level matching instead of failing.
    """
    device_area: dict[str, str] = {}
    device_labels: dict[str, list[str]] = {}
    if not isinstance(device_registry_result, dict):
        return device_area, device_labels
    devices = device_registry_result.get("result", [])
    if not isinstance(devices, list):
        return device_area, device_labels
    for device in devices:
        if not isinstance(device, dict):
            continue
        device_id = device.get("id")
        if not device_id:
            continue
        area_id = device.get("area_id")
        if area_id:
            device_area[device_id] = area_id
        labels = _normalize_labels(device.get("labels"))
        if labels:
            device_labels[device_id] = labels
    return device_area, device_labels


def _effective_area(entry: dict[str, Any], device_area: dict[str, str]) -> str | None:
    """Entity ``area_id`` falling back to its device's area (HA's inheritance)."""
    area_id = entry.get("area_id")
    if isinstance(area_id, str) and area_id:
        return area_id
    device_id = entry.get("device_id")
    if isinstance(device_id, str) and device_id:
        return device_area.get(device_id)
    return None


def _effective_labels(
    entry: dict[str, Any], device_labels: dict[str, list[str]]
) -> list[str]:
    """Entity labels plus its device's labels (device labels apply to entities)."""
    result = _normalize_labels(entry.get("labels"))
    device_id = entry.get("device_id")
    if isinstance(device_id, str) and device_id in device_labels:
        result = result + device_labels[device_id]
    return result


def _is_assist_exposed(
    eid: str,
    entry: dict[str, Any] | None,
    device_class: str | None,
    overrides: dict[str, bool],
    expose_new: bool,
) -> bool:
    """Reconstruct HA-core ``async_should_expose`` for the conversation assistant.

    Precedence (exposed_entities.py, verified 2026-07-04): an explicit per-entity
    override wins outright (even over entity_category/hidden_by); otherwise, if
    the instance exposes new entities, fall back to ``_is_default_exposed``
    (blocked by entity_category/hidden_by, then domain, then device-class);
    otherwise not exposed.
    """
    if eid in overrides:
        return overrides[eid]
    if not expose_new:
        return False
    # _is_default_exposed: config/diagnostic or hidden entities are never a
    # default exposure, regardless of domain.
    if entry is not None and (
        entry.get("entity_category") is not None or entry.get("hidden_by") is not None
    ):
        return False
    domain = eid.split(".")[0]
    if domain in DEFAULT_EXPOSED_DOMAINS:
        return True
    if domain == "binary_sensor":
        return device_class in DEFAULT_EXPOSED_BINARY_SENSOR_DEVICE_CLASSES
    if domain == "sensor":
        return device_class in DEFAULT_EXPOSED_SENSOR_DEVICE_CLASSES
    return False


def hidden_entity_ids(
    registry_result: object,
    config: VisibilityConfig,
    states_result: object | None = None,
    assist_overrides: dict[str, bool] | None = None,
    expose_new: bool = False,
    device_registry_result: object | None = None,
) -> tuple[set[str], list[str]]:
    """Return ``(hidden_entity_ids, warnings)``.

    ``hidden`` is empty when disabled or the registry payload is unusable
    (fail-open — never hide on bad input), except the denylist which needs no
    registry data and is honored regardless. ``warnings`` carries operator-facing
    notes (degraded registry, dropped unknown categories, an empty-registry
    allowlist degradation, missing Assist data) for the caller to surface at the
    response level.

    ``states_result`` (the live states list the seam already holds) is used for
    two things when provided: it widens the allowlist and Assist dimensions to
    states-only entities (YAML/template entities absent from the registry), and
    it supplies the effective ``device_class`` (HA reads it from the live entity,
    not the registry) for the Assist default-exposure check. Without it, both
    dimensions degrade to registry-only.

    ``device_registry_result`` (``config/device_registry/list``) supplies the
    device area/labels an entity inherits: HA resolves an entity's effective area
    as its own ``area_id`` falling back to its device's, and a device's labels
    apply to its entities. The area/label exclude and allow dimensions match on
    that effective area/labels, so a device-bound entity (registry ``area_id``
    None + a ``device_id``) is filtered by its device's area/labels. Without the
    payload those dimensions fall back to entity-level matching only, which misses
    device-bound entities (most real ones).

    This is a pure function. For the Assist dimension, a registry entry's explicit
    ``conversation`` ``should_expose`` (True *or* False) is read directly from the
    entry ``options`` in this payload (``config/entity_registry/list`` already
    carries ``options``). ``assist_overrides`` supplies the True-only exposures
    ``load_hidden_set`` reads from ``homeassistant/expose_entity/list`` — the only
    exposure source for states-only entities with no registry entry — and
    ``expose_new`` is HA's "expose new entities" flag. Note the ha-mcp visibility
    precedence (a conjunction of hide dimensions) is separate from HA's own
    async_should_expose precedence, which only the Assist dimension mirrors.
    """
    if not config.enabled:
        return set(), []

    warnings: list[str] = []
    # Categories are validated independently of the registry: an unknown value is
    # dropped with a warning (not hard-rejected), so a typo silently hides nothing
    # yet is still surfaced.
    requested_categories = set(config.exclude_categories)
    categories = requested_categories & KNOWN_ENTITY_CATEGORIES
    unknown_categories = requested_categories - KNOWN_ENTITY_CATEGORIES
    if unknown_categories:
        warnings.append(
            "Entity visibility: ignoring unknown exclude_categories "
            f"{sorted(unknown_categories)} (valid: config, diagnostic)."
        )

    denied = set(config.deny_entity_ids)

    # Enabled past this point, so an unusable registry is a real degradation the
    # operator should see as a warning. Honor the denylist regardless (it needs no
    # registry data); only the registry-derived dimensions degrade to open.
    if not isinstance(registry_result, dict) or not registry_result.get("success"):
        logger.warning(
            "entity visibility filter enabled but the registry payload was "
            "unusable; degrading to unfiltered for this request"
        )
        warnings.append(_REGISTRY_UNAVAILABLE_WARNING)
        return denied, warnings

    entries: Any = registry_result.get("result", [])
    if not isinstance(entries, list):
        logger.warning(
            "entity visibility filter enabled but the registry 'result' was not "
            "a list; degrading to unfiltered for this request"
        )
        warnings.append(_REGISTRY_UNAVAILABLE_WARNING)
        return denied, warnings

    # Index the registry by entity_id and index the live states for device_class
    # lookups (Assist reads the effective device_class from the entity, not the
    # registry) plus the states-only entity universe (allowlist/Assist must be
    # able to hide YAML/template entities that have no registry entry).
    registry_by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("entity_id"):
            registry_by_id[entry["entity_id"]] = entry

    state_device_class: dict[str, str | None] = {}
    if isinstance(states_result, list):
        for state in states_result:
            if isinstance(state, dict) and state.get("entity_id"):
                attrs = state.get("attributes")
                dc = attrs.get("device_class") if isinstance(attrs, dict) else None
                state_device_class[state["entity_id"]] = dc

    # Device-inherited area/labels: HA resolves an entity's effective area as its
    # own area_id else its device's, and device labels apply to its entities. A
    # missing/degraded device payload leaves both maps empty, so the area/label
    # dimensions fall back to entity-level matching.
    device_area, device_labels = _parse_device_registry(device_registry_result)

    areas = set(config.exclude_areas)
    labels = set(config.exclude_labels)
    allow_areas = set(config.allow_areas)
    allow_labels = set(config.allow_labels)
    allow_entity_ids = set(config.allow_entity_ids)
    # An allowlist is active only when at least one allow_* dimension is set. When
    # active it inverts the default: an entity is hidden unless it matches one of
    # the allow dimensions. Empty allow_* => inactive => nothing hidden by allow.
    allow_active = bool(allow_areas or allow_labels or allow_entity_ids)

    # Fail-open guard: an area/label allowlist needs registry data to match. If
    # the registry came back empty (success but no usable entries) those
    # dimensions can match nothing, so restrict mode would hide every candidate -
    # the fail-closed blank the design forbids. Drop the registry-derived allow
    # dimensions and warn; a registry-independent allow_entity_ids list still
    # applies. Only fires when there are states-only candidates to protect.
    if (allow_areas or allow_labels) and not registry_by_id and state_device_class:
        warnings.append(_ALLOWLIST_REGISTRY_EMPTY_WARNING)
        allow_areas = set()
        allow_labels = set()
        allow_active = bool(allow_entity_ids)

    # Seed with the explicit denylist: deny is a literal entity_id match and must
    # hide an entity even when it has no entity-registry entry (legacy YAML /
    # template entities live only in states, not the registry). Because deny needs
    # no registry data, the degraded early returns above still honor it
    # (fail-fully-open was not the goal); only the registry-derived dimensions
    # degrade to open on a bad read.
    hidden: set[str] = set(denied)

    # Exclude dimensions are registry-derived: they hide an entity that *has* a
    # matching category/hidden flag/area/label. A states-only entity has none of
    # these, so (correctly) is never excluded - the loop stays registry-based.
    for eid, entry in registry_by_id.items():
        if eid in denied:
            continue  # already hidden via the seed above
        if categories and entry.get("entity_category") in categories:
            hidden.add(eid)
            continue
        if config.exclude_hidden and entry.get("hidden_by") is not None:
            hidden.add(eid)
            continue
        if areas and _effective_area(entry, device_area) in areas:
            hidden.add(eid)
            continue
        if labels and labels.intersection(_effective_labels(entry, device_labels)):
            hidden.add(eid)

    # Allowlist + Assist are conjunctive filters that must also reach states-only
    # entities, so they iterate the full candidate universe (registry ∪ states)
    # rather than just the registry. Without a states list they degrade to the
    # registry set.
    if allow_active or config.respect_assist_exposure:
        candidate_ids = set(registry_by_id)
        candidate_ids |= set(state_device_class)
        assist_active = config.respect_assist_exposure and assist_overrides is not None
        if config.respect_assist_exposure and assist_overrides is None:
            # The seam could not supply Assist data; skip that dimension rather
            # than hide everything, and tell the operator.
            warnings.append(_ASSIST_UNAVAILABLE_WARNING)
        # Effective explicit-override map for the Assist dimension. Start with the
        # True-only exposures from expose_entity/list (the only source for
        # states-only entities), then layer each registry entry's explicit
        # should_expose (True *or* False) read from the payload options — it is
        # authoritative and wins over the list's True-only view.
        overrides: dict[str, bool] = {}
        if assist_active:
            overrides.update(assist_overrides or {})
            for eid, entry in registry_by_id.items():
                explicit = _registry_assist_override(entry)
                if explicit is not None:
                    overrides[eid] = explicit
        for eid in candidate_ids:
            if eid in hidden:
                continue
            entry = registry_by_id.get(eid)
            if allow_active and not (
                eid in allow_entity_ids
                or (
                    entry is not None
                    and _effective_area(entry, device_area) in allow_areas
                )
                or (
                    entry is not None
                    and allow_labels.intersection(
                        _effective_labels(entry, device_labels)
                    )
                )
            ):
                hidden.add(eid)
                continue
            if assist_active:
                # Effective device_class: a registry override wins, else the live
                # entity's (from state attributes) - matching get_device_class.
                device_class = (
                    entry.get("device_class") if entry is not None else None
                ) or state_device_class.get(eid)
                if not _is_assist_exposed(
                    eid, entry, device_class, overrides, expose_new
                ):
                    hidden.add(eid)

    return hidden, warnings


def _registry_assist_override(entry: dict[str, Any]) -> bool | None:
    """Return a registry entry's explicit conversation ``should_expose``, else None.

    Read from the ``options`` the ``config/entity_registry/list`` payload already
    carries (HA's ``as_partial_dict`` includes ``options``), so no extra
    per-entity websocket read is needed to see an explicit expose *or* un-expose.
    """
    options = entry.get("options")
    conv = options.get("conversation") if isinstance(options, dict) else None
    if isinstance(conv, dict) and "should_expose" in conv:
        return bool(conv["should_expose"])
    return None


async def _fetch_assist_exposure(
    client: Any,
) -> tuple[dict[str, bool] | None, bool]:
    """Fetch the ``conversation`` Assist exposure inputs over websocket.

    Two reads, in parallel: ``homeassistant/expose_entity/list`` (the set of
    entities explicitly exposed to an assistant) and
    ``homeassistant/expose_new_entities/get`` (the "expose new entities" flag that
    drives the default-exposure branch).

    Returns ``(overrides, expose_new)`` where ``overrides`` maps each
    conversation-exposed entity_id to ``True``. HA-core's list command only ever
    returns exposed (True) entities and omits an explicitly un-exposed one, so this
    contributes True-only overrides — the only exposure source for states-only
    entities that have no registry entry. A registry entity's explicit setting
    (True *or* False) is read separately from its ``options`` in the registry list
    payload the resolver already holds, so it does not depend on this fetch; an
    explicit un-expose of a states-only entity cannot be expressed by the list
    command and stays fail-open (it falls to its domain/device-class default).

    Fails soft: on any error, or if either read is unsuccessful, returns
    ``(None, False)`` so only the Assist dimension degrades (the resolver warns and
    applies the other dimensions) rather than the whole filter failing.
    """
    try:
        exposed_res, new_res = await asyncio.gather(
            client.send_websocket_message({"type": "homeassistant/expose_entity/list"}),
            client.send_websocket_message(
                {
                    "type": "homeassistant/expose_new_entities/get",
                    "assistant": "conversation",
                }
            ),
        )
        if not (isinstance(exposed_res, dict) and exposed_res.get("success")):
            return None, False
        if not (isinstance(new_res, dict) and new_res.get("success")):
            # Without the expose_new flag the default-exposure branch can't be
            # computed; degrade the whole dimension (skip + warn) rather than
            # assume a value - assuming False would wrongly hide default-domain
            # entities whenever the instance actually exposes new entities.
            return None, False
        overrides: dict[str, bool] = {}
        result = exposed_res.get("result", {})
        exposed = result.get("exposed_entities") if isinstance(result, dict) else None
        if isinstance(exposed, dict):
            for eid, assistants in exposed.items():
                if (
                    isinstance(assistants, dict)
                    and assistants.get("conversation") is True
                ):
                    overrides[eid] = True
        new_result = new_res.get("result", {})
        expose_new = (
            bool(new_result.get("expose_new", False))
            if isinstance(new_result, dict)
            else False
        )
        return overrides, expose_new
    except Exception:
        logger.warning("assist exposure fetch failed; dimension skipped", exc_info=True)
        return None, False


def config_needs_device_registry(config: VisibilityConfig) -> bool:
    """Whether an enabled visibility config has a dimension that reads the device registry.

    The device registry (``config/device_registry/list``) only supplies the
    device-inherited area/labels consumed by the area/label exclude *and* allow
    dimensions (see ``_effective_area`` / ``_effective_labels``). A disabled
    config, or an enabled one with none of those four dimensions set (the
    default), never touches it, so fetching it there is pure waste.
    """
    return config.enabled and bool(
        config.exclude_areas
        or config.exclude_labels
        or config.allow_areas
        or config.allow_labels
    )


def config_has_active_hide_dimensions(config: VisibilityConfig) -> bool:
    """Whether an enabled visibility config has any dimension that can hide.

    An ``enabled=True`` config with every dimension cleared (including
    ``exclude_categories=[]``) hides nothing, so a query search can still route
    through the in-process ha_mcp_tools component. Any active dimension — a known
    ``exclude_category``, ``exclude_hidden``, a deny/allow list, an area/label
    exclude/allow, or ``respect_assist_exposure`` — means the component (which
    applies no visibility filtering) would surface entities the server hides, so
    the query must stay on the legacy path. Mirrors the dimension set the
    ``hidden_entity_ids`` resolver actually consults, so a config that hides
    something here is exactly one that would hide something there.
    """
    if not config.enabled:
        return False
    return bool(
        (set(config.exclude_categories) & KNOWN_ENTITY_CATEGORIES)
        or config.exclude_hidden
        or config.deny_entity_ids
        or config.exclude_areas
        or config.exclude_labels
        or config.allow_entity_ids
        or config.allow_areas
        or config.allow_labels
        or config.respect_assist_exposure
    )


async def visibility_filter_active() -> bool:
    """Load the visibility config off-loop; report whether the filter can hide.

    ``ha_search`` routing gates the component fast path on this: a query search
    must NOT route through the ha_mcp_tools component while the filter is active,
    because the component does not apply the server's opt-in visibility filter
    and would leak entities the legacy path hides. Reuses the same loader the
    legacy filter uses (``load_visibility_config`` over ``get_data_dir()``), so a
    test that redirects ``resolver.get_data_dir`` steers both.

    Fails **closed** to ``True`` (keep the legacy, filter-applying path) on a load
    error: a missing config file returns a disabled default (not an error, → the
    component), but a *malformed* enabled config raising here would otherwise
    silently route to the unfiltered component — so on any exception keep legacy,
    where ``load_hidden_set`` surfaces the load-failure warning.
    """
    try:
        config = await asyncio.to_thread(load_visibility_config, get_data_dir())
    except Exception:
        return True
    return config_has_active_hide_dimensions(config)


async def load_visibility_wire() -> dict[str, Any] | None:
    """Serialize the visibility config for the component ``search`` fast path.

    Loads the same memoized config ``visibility_filter_active`` reads and returns
    its hide dimensions via ``VisibilityConfig.to_wire`` — the ``visibility``
    param the ha_search consumer hands the ha_mcp_tools component when it
    advertises the ``search_visibility`` capability, letting the component apply
    the hide dimensions in-process instead of the server dropping to the legacy
    path. Returns ``None`` on a load error so the caller keeps the legacy path
    (there is no config to push into the component), matching the fail-closed-to-
    legacy policy ``visibility_filter_active`` uses for the same gate. Reuses
    ``get_data_dir`` so a test redirecting ``resolver.get_data_dir`` steers this
    too.
    """
    try:
        config = await asyncio.to_thread(load_visibility_config, get_data_dir())
    except Exception:
        return None
    return config.to_wire()


async def visibility_state_and_wire() -> tuple[bool, dict[str, Any] | None]:
    """Load the visibility config once and report both ``(active, wire)``.

    Single-load counterpart to calling ``visibility_filter_active`` and
    ``load_visibility_wire`` back to back for the same gate — the config is
    memoized, so the pair only costs an extra ``os.stat``, but the component
    ``search_visibility`` gate is the one caller that always needs both, so
    this collapses it to one load. Fails **closed** to ``(True, None)`` on a
    load error, mirroring ``visibility_filter_active``'s fail-closed-to-legacy
    policy: keep the legacy path, and there is no config to serialize.
    """
    try:
        config = await asyncio.to_thread(load_visibility_config, get_data_dir())
    except Exception:
        return True, None
    return config_has_active_hide_dimensions(config), config.to_wire()


async def device_registry_needed_for_visibility() -> bool:
    """Load the visibility config off-loop and report whether the device-registry
    fetch is needed by any active area/label dimension.

    Fail-open to ``False``: a default install (no config file) or an unloadable
    config resolves to disabled, which needs no device registry. The
    operator-facing load-failure warning is owned by the ``load_hidden_set`` call
    that runs right after — this gate only decides whether to spend the fetch.
    """
    try:
        config = await asyncio.to_thread(load_visibility_config, get_data_dir())
    except Exception:
        return False
    return config_needs_device_registry(config)


async def load_hidden_set(
    registry_result: object,
    states_result: object | None = None,
    client: Any | None = None,
    device_registry_result: object | None = None,
) -> tuple[set[str], list[str]]:
    """Load the visibility config off-loop and resolve ``(hidden, warnings)``.

    Fail-open: any error (missing/corrupt config, unexpected exception) yields an
    empty hidden set so a config problem never blanks the instance from the
    agent; a genuine load failure is surfaced as a warning. ``states_result`` is
    passed through to widen the allowlist/Assist dimensions to states-only
    entities and supply device_class; ``device_registry_result``
    (``config/device_registry/list``) lets the area/label dimensions match the
    area/labels an entity inherits from its device. When the config enables
    ``respect_assist_exposure`` and a ``client`` is given, the two Assist
    exposure websocket reads are fetched here (once per call, in parallel) and
    fed to the pure resolver; the fetch fails soft (only that dimension drops).
    """
    try:
        config = await asyncio.to_thread(load_visibility_config, get_data_dir())
        assist_overrides: dict[str, bool] | None = None
        expose_new = False
        if config.enabled and config.respect_assist_exposure and client is not None:
            assist_overrides, expose_new = await _fetch_assist_exposure(client)
        return hidden_entity_ids(
            registry_result,
            config,
            states_result,
            assist_overrides,
            expose_new,
            device_registry_result,
        )
    except Exception:
        logger.warning(
            "entity visibility config load failed; filter disabled", exc_info=True
        )
        return set(), [
            "Entity visibility config could not be loaded; the filter is disabled."
        ]
