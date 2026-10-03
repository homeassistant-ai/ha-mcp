"""Single-object lookups: blueprint, device, enrichment, exposure and registry."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

import yaml  # type: ignore[import-untyped]
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .ws_assist import _async_get_entity_settings, _is_unknown_entity_error
from .ws_registry import (
    _all_device_entries,
    _all_entity_entries,
    _device_dict_repr,
    _effective_device_area_id,
    _entity_partial_dict,
    _enum_value,
    _plainify,
    _reg_entity,
    _RegistryView,
    _resolve_registries,
    _state_get,
    _substrate_unavailable,
    _unambiguous_device_entries,
)
from .ws_search import _registry_enrichment

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(f"{__package__}.websocket_api")


# =============================================================================
# ha_mcp_tools/blueprint_get
# =============================================================================
def _do_blueprint_get(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    body: dict[str, Any] | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Return one installed blueprint as ``{metadata, config, yaml}``.

    core's ``blueprint/list`` returns only ``{metadata}`` (no triggers /
    conditions / actions / sequence, and never the file text), so the server can
    otherwise serve metadata only. This reads the on-disk blueprint file once and
    returns both views of it: ``config`` is the parsed file (the server merges it
    additively over the ``blueprint/list`` metadata), ``metadata`` is its
    ``blueprint:`` section, and ``yaml`` is the raw text the server hands back for
    a round trip through ``blueprint/save``. Each is ``None`` when it could not be
    produced — a file that reads but does not parse still yields its ``yaml`` —
    and all three are ``None`` when the file is missing or the requested path
    escapes the jail (see :func:`_read_blueprint_file`).

    Pure: the blocking jail-resolve + file read + YAML parse run in the executor
    via :func:`_blueprint_get_prep`, which passes both views in.
    """
    if not isinstance(body, dict):
        return {"metadata": None, "config": None, "yaml": text}
    metadata = body.get("blueprint")
    return {
        "metadata": _plainify(metadata) if isinstance(metadata, dict) else None,
        "config": _plainify(body),
        "yaml": text,
    }


async def _blueprint_get_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Async pre-step for ``blueprint_get``: jail + read + parse off the loop.

    The path jail (symlink-safe ``Path.resolve`` containment), the ``open()`` and
    the YAML parse are all blocking filesystem work, so they run in the executor
    via :meth:`hass.async_add_executor_job` — keeping :func:`_do_blueprint_get` a
    pure assembler over the raw text and parsed body this returns (both ``None``
    on a failed read).
    """
    domain = msg["domain"]
    path = msg["path"]
    read = await hass.async_add_executor_job(_read_blueprint_file, hass, domain, path)
    return {"body": read.body, "text": read.text}


class _BlueprintFile(NamedTuple):
    """One blueprint file read once: its raw ``text`` and its parsed ``body``.

    ``text`` is ``None`` when the file could not be read at all; ``body`` is
    additionally ``None`` when it read but did not parse into a mapping.
    """

    text: str | None
    body: dict[str, Any] | None


_UNREADABLE_BLUEPRINT = _BlueprintFile(None, None)


def _read_blueprint_file(hass: HomeAssistant, domain: str, path: str) -> _BlueprintFile:
    """Resolve + jail + read + parse one blueprint YAML file, reading it once.

    Blueprint files live under ``<config>/blueprints/<domain>/``. The requested
    ``path`` is joined under that root and resolved symlink-safe (mirrors the
    file-tool jail's ``_resolves_within`` — resolve the RAW input, following
    symlinks, THEN check containment, so ``<root>/<symlink>/..`` cannot escape). A
    path escaping the root — via ``..``, an absolute path, or a symlink — yields
    an empty result (rejected, never opened), as does a missing file, a non-file
    target, or a read error. A file that reads but does not parse into a mapping
    keeps its ``text`` and drops its ``body``, so the caller can still round-trip
    the exact bytes it holds.

    Parsed with :class:`_BlueprintLoader`: ``!input`` markers are preserved and
    every other custom tag (``!secret`` / ``!include`` / …) is neutralized to
    ``None``, so no resolved secret plaintext can ever enter the returned body
    (defense in depth — blueprints use ``!input``, not ``!secret``).
    """
    config = getattr(hass, "config", None)
    path_fn = getattr(config, "path", None)
    if not callable(path_fn):
        return _UNREADABLE_BLUEPRINT
    try:
        base = Path(path_fn("blueprints", domain))
        candidate = Path(path) if path.startswith("/") else base / path
        real = candidate.resolve()
        base_real = base.resolve()
    except (OSError, ValueError):
        return _UNREADABLE_BLUEPRINT
    if not (real == base_real or real.is_relative_to(base_real)):
        return _UNREADABLE_BLUEPRINT
    try:
        text = real.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return _UNREADABLE_BLUEPRINT
    try:
        # Instance form (not yaml.load) mirrors the component's existing
        # _PackagesDirLoader usage; _BlueprintLoader is a SafeLoader subclass,
        # so no !!python/object can construct arbitrary types.
        loader = _BlueprintLoader(text)
        try:
            parsed = loader.get_single_data()
        finally:
            loader.dispose()
    except (ValueError, yaml.YAMLError):
        return _BlueprintFile(text, None)
    return _BlueprintFile(text, parsed if isinstance(parsed, dict) else None)


def _construct_blueprint_input(loader: Any, node: Any) -> dict[str, str]:
    """Represent ``!input <name>`` as ``{"__input__": <name>}``.

    A JSON-safe, unambiguous marker of a blueprint input substitution point (the
    body is a display artifact, not a runnable config), so a consumer can see
    which fields an input fills without the tag crashing a plain safe-load.
    """
    return {"__input__": str(getattr(node, "value", ""))}


def _drop_blueprint_tag(loader: Any, tag_suffix: Any, node: Any) -> None:
    """Neutralize every non-``!input`` custom tag to ``None`` (never resolve it).

    ``!secret`` must never resolve to plaintext; ``!include`` / ``!env_var`` /
    unknown tags are irrelevant to a read-only body view. Mirrors the component's
    ``_ignore_unknown_tag`` pattern in ``__init__.py``.
    """
    return None


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader for blueprint files: keep ``!input``, neutralize all other tags."""


_BlueprintLoader.add_constructor("!input", _construct_blueprint_input)
_BlueprintLoader.add_multi_constructor("!", _drop_blueprint_tag)


# =============================================================================
# ha_mcp_tools/device_get + ha_mcp_tools/device_list
# =============================================================================
def _do_device_get(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return one device registry entry by id, optionally with its entities.

    ``{device: <DeviceEntry.dict_repr> | None}`` — a request-local snapshot over
    Core's supported main and child collections rejects conflicting identities,
    and the emitted body is core's ``DeviceEntry.dict_repr`` returned UNMODIFIED —
    exactly the shape
    ``config/device_registry/list`` serializes (it sends
    ``json_bytes(entry.dict_repr)``), so a component-served record is byte-identical
    to one legacy list element by construction (the WS transport JSON-encodes the
    same dict with the same encoder). The body is never ``_plainify``'d: that would
    ``str()`` the ``disabled_by`` / ``entry_type`` enums to their repr instead of the
    wire value core's encoder emits, breaking parity. ``device`` is ``None`` when no
    such device exists — the server maps that onto its own not-found contract.

    When ``include_entities`` is set, a SIBLING ``entities`` key carries the device's
    entity-registry rows (``[<RegistryEntry.as_partial_dict>, ...]`` — the same shape
    and serialization ``config/entity_registry/list`` emits), so a single-device
    lookup no longer pulls the WHOLE entity registry to list one device's entities.
    ``er.async_entries_for_device`` is called with ``include_disabled_entities=True``
    to match what ``config/entity_registry/list`` returns (it lists disabled entities
    too). The DeviceEntry dict itself stays exactly the raw shape — the join is a
    sibling, so consumers keep their own transforms. The ``entities`` key is present
    only when requested. A child-device result may also carry a sibling
    ``effective_area_id`` computed from its direct-or-parent placement; that
    transport-only field is absent from the raw ``device`` mapping.
    """
    device_id = params.get("device_id")
    include_entities = params.get("include_entities", False)
    view = _resolve_registries(hass)
    entry = _unambiguous_device_entries(view).get(device_id) if device_id else None
    entry_row = _device_dict_repr(entry) if entry is not None else None
    result: dict[str, Any] = {"device": entry_row}
    if (
        entry is not None
        and isinstance(entry_row, dict)
        and isinstance(entry_row.get("parent_device_id"), str)
    ):
        # Additive internal transport metadata. The raw child ``dict_repr`` stays
        # byte-identical to Core while the server can expose its existing area_id
        # field using Core's effective placement without a whole-registry read.
        result["effective_area_id"] = _effective_device_area_id(view, entry)
    if include_entities:
        result["entities"] = _device_entities(view, device_id) if device_id else []
    return result


def _do_device_list(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return every device registry entry as ``{devices: [dict_repr, ...]}``.

    The in-process equivalent of ``config/device_registry/list``: each element is
    core's ``DeviceEntry.dict_repr`` returned VERBATIM (same byte-parity rationale
    as :func:`_do_device_get`), so the server's existing device transforms consume
    it unchanged. An entry whose ``dict_repr`` is unavailable is skipped rather
    than emitted as a partial record.
    """
    view = _resolve_registries(hass)
    out: list[dict[str, Any]] = []
    for dev in _all_device_entries(view):
        repr_dict = _device_dict_repr(dev)
        if repr_dict is not None:
            out.append(repr_dict)
        else:
            _LOGGER.warning(
                "device_list: skipping device %r with unavailable dict_repr",
                getattr(dev, "id", None),
            )
    return {"devices": out}


def _device_entities(view: _RegistryView, device_id: str) -> list[dict[str, Any]]:
    """The device's entity-registry rows as ``config/entity_registry/list`` elements.

    Each row is core's ``RegistryEntry.as_partial_dict`` returned VERBATIM (the same
    shape + serialization ``config/entity_registry/list`` emits — it sends
    ``json_bytes(entry.partial_json_repr)`` over ``as_partial_dict``), so the
    server's device<->entity map builds identically off the join or the legacy list.
    A row whose ``as_partial_dict`` is unavailable is skipped.
    """
    out: list[dict[str, Any]] = []
    for entry in _entries_for_device(view, device_id):
        partial = _entity_partial_dict(entry)
        if partial is not None:
            out.append(partial)
    return out


def _entries_for_device(view: _RegistryView, device_id: str) -> list[Any]:
    """Entity-registry entries bound to ``device_id``, disabled ones INCLUDED.

    Delegates to core's ``er.async_entries_for_device`` (its device_id index) with
    ``include_disabled_entities=True`` so the result matches what
    ``config/entity_registry/list`` returns — that command lists disabled entities
    too, and dropping them would diverge the join from the legacy shape. Guarded
    against a missing registry / core drift (returns ``[]``).
    """
    reg = view.entity
    if reg is None or not device_id:
        return []
    try:
        entries = er.async_entries_for_device(
            reg, device_id, include_disabled_entities=True
        )
    except Exception:  # pragma: no cover - defensive; core drift  # noqa: BLE001
        return []
    return list(entries)


# =============================================================================
# ha_mcp_tools/entity_enrich
# =============================================================================
def _do_entity_enrich(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the area/floor/labels/aliases join for each requested entity_id.

    ``{entities: {id: {area, floor, labels, aliases}}}`` — each id runs through the
    SAME :func:`_registry_enrichment` join the search path uses (device-inherited
    area/labels, resolved NAMES), so ``ha_get_entity`` adds the resolved-name
    fields the raw registry entry lacks without the caller fanning out its own
    area/floor/label registry reads. Pure O(id) in-memory registry lookups; a
    registry-only (stateless) entity is enriched too (the join keys off the
    registry, not the state machine). An id with no registry entry yields empty /
    ``None`` fields rather than being dropped, so the caller can pair the result
    back to its request by key.
    """
    entity_ids = params.get("entity_ids") or []
    view = _resolve_registries(hass)
    entities: dict[str, Any] = {}
    for entity_id in entity_ids:
        join = _registry_enrichment(view, entity_id)
        entities[entity_id] = {
            "area": join["area"],
            "floor": join["floor"],
            "labels": join["labels"],
            "aliases": join["aliases"],
        }
    return {"entities": entities}


# =============================================================================
# ha_mcp_tools/exposure
# =============================================================================
def _do_exposure(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return voice-assistant exposure with names/areas attached.

    ``{exposed_entities: {id: {assistant: True}}, entity_info: {id: {...}}}``.
    ``exposed_entities`` is byte-identical to core's ``ws_list_exposed_entities``
    result (``homeassistant/expose_entity/list``): only ``should_expose``-true
    assistants appear, and an entity with none is omitted from the map — so the
    server's existing exposure shaping consumes it unchanged. ``entity_info`` is
    the additive half: each relevant id enriched through :func:`_registry_enrichment`
    (friendly_name/domain/area/floor/labels), closing the "one call gives a bare
    ``{id: {assistant: bool}}`` map with no names/areas" gap.

    Modes:

    * single-entity (``entity_id`` set) — reads core's module-level
      ``async_get_entity_settings`` for that id and enriches it (whether exposed or
      not — the caller asked about that specific entity).
    * list (``entity_id`` omitted) — mirrors ``ws_list_exposed_entities``: walks
      the exposed-entities store ids + the entity registry, keeps the exposed ones,
      and enriches each.

    Parity guardrails (mirroring the legacy shape, pinned in tests):

    1. only ``should_expose``-true assistants are reported (the raw helper returns
       every assistant that has *any* stored option, not just exposed ones);
    2. core's ``HomeAssistantError("Unknown entity")`` on a junk id is caught and
       degrades to the not-exposed default (the legacy ``expose_entity/list`` never
       raises on a junk id);
    3. a missing ``hass.states.get(id)`` omits the live-state fields
       (friendly_name / state) from ``entity_info`` rather than crashing.
    """
    entity_id = params.get("entity_id")
    view = _resolve_registries(hass)

    if entity_id:
        exposed_to = _entity_exposed_to(hass, entity_id)
        return {
            "exposed_entities": {entity_id: exposed_to} if exposed_to else {},
            "entity_info": {entity_id: _exposure_enrichment(hass, view, entity_id)},
        }

    exposed_entities: dict[str, Any] = {}
    entity_info: dict[str, Any] = {}
    for eid in _all_exposable_entity_ids(hass, view):
        exposed_to = _entity_exposed_to(hass, eid)
        if not exposed_to:
            continue
        exposed_entities[eid] = exposed_to
        entity_info[eid] = _exposure_enrichment(hass, view, eid)
    return {"exposed_entities": exposed_entities, "entity_info": entity_info}


def _entity_exposed_to(hass: HomeAssistant, entity_id: str) -> dict[str, bool]:
    """``{assistant: True}`` for the entity's ``should_expose``-true assistants.

    Reads core's ``async_get_entity_settings`` (via the local
    :func:`_async_get_entity_settings` test-seam wrapper) and keeps only assistants
    whose settings carry a truthy ``should_expose`` (guardrail 1 — the raw helper is
    not pre-filtered like ``ws_list_exposed_entities``). A junk id whose helper raises
    ``HomeAssistantError("Unknown entity")`` degrades to ``{}`` (guardrail 2), the
    same not-exposed default the legacy path returns for an id it never listed.
    """
    try:
        settings = _async_get_entity_settings(hass, entity_id)
    except Exception as exc:
        if _is_unknown_entity_error(exc):
            return {}
        raise
    out: dict[str, bool] = {}
    if isinstance(settings, Mapping):
        for assistant, opts in settings.items():
            if isinstance(opts, Mapping) and opts.get("should_expose"):
                out[str(assistant)] = True
    return out


def _exposure_enrichment(
    hass: HomeAssistant, view: _RegistryView, entity_id: str
) -> dict[str, Any]:
    """area/floor/labels + domain for an id, plus live-state fields when present.

    Runs the id through :func:`_registry_enrichment` for area/floor/labels
    (device-inherited names). ``domain`` comes from the id itself (no state
    needed). ``friendly_name`` and ``state`` are LIVE-STATE fields: included only
    when ``hass.states.get(id)`` exists, omitted otherwise (guardrail 3 — a
    disabled / legacy-only entity has no state, so those keys are simply absent
    rather than crashing the join).
    """
    join = _registry_enrichment(view, entity_id)
    domain = entity_id.split(".", maxsplit=1)[0] if "." in entity_id else ""
    info: dict[str, Any] = {
        "domain": domain,
        "area": join["area"],
        "floor": join["floor"],
        "labels": join["labels"],
    }
    state = _state_get(hass, entity_id)
    if state is not None:
        attrs = getattr(state, "attributes", None) or {}
        friendly = (
            attrs.get("friendly_name", entity_id)
            if isinstance(attrs, Mapping)
            else entity_id
        )
        info["friendly_name"] = str(friendly)
        info["state"] = getattr(state, "state", "unknown")
    return info


def _all_exposable_entity_ids(hass: HomeAssistant, view: _RegistryView) -> list[str]:
    """Every id ``ws_list_exposed_entities`` walks: store ids plus registry ids.

    Core iterates ``chain(exposed_entities.entities, entity_registry.entities)`` —
    the legacy store (entities WITHOUT a unique_id, exposed manually) plus every
    registry entity. This reproduces that union, de-duplicated with store-first
    order, so an exposed YAML entity that lives only in the store is not missed.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for eid in _legacy_exposed_entity_ids(hass):
        if eid and eid not in seen:
            seen.add(eid)
            ordered.append(eid)
    for entry in _all_entity_entries(view):
        entry_eid = getattr(entry, "entity_id", None)
        if entry_eid and entry_eid not in seen:
            seen.add(entry_eid)
            ordered.append(entry_eid)
    return ordered


def _legacy_exposed_entity_ids(hass: HomeAssistant) -> list[str]:
    """Entity ids in the exposed-entities store (entities without a unique_id).

    The ``exposed_entities.entities`` half of core's ``ws_list_exposed_entities``
    iteration. Imported lazily (test seam); a missing store / core drift yields
    ``[]`` so list mode still enumerates the registry half.
    """
    try:
        from homeassistant.components.homeassistant.const import (
            DATA_EXPOSED_ENTITIES,
        )

        data = getattr(hass, "data", None)
        store = data.get(DATA_EXPOSED_ENTITIES) if isinstance(data, Mapping) else None
    except Exception:  # pragma: no cover - defensive; core drift  # noqa: BLE001
        return []
    entities = getattr(store, "entities", None)
    if isinstance(entities, Mapping):
        return [str(eid) for eid in entities]
    return []


# =============================================================================
# ha_mcp_tools/registry_lookup
# =============================================================================
def _do_registry_lookup(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return entity-registry rows for a set of ids or one config entry.

    Rows are core's ``RegistryEntry.as_partial_dict`` VERBATIM (the
    ``config/entity_registry/list`` shape, disabled entities included), so the
    consumers that parse that exact shape today read the component-served rows
    unchanged.

    * ``config_entry_id`` — scans the whole entity registry and returns EVERY
      entity bound to that entry (``{entities: [...]}``). It deliberately does
      NOT reuse :func:`_entities_by_config_entry` (single-valued — first entity
      only), so a multi-entity flow helper (a utility_meter and its tariff
      sub-entities) does not silently lose members.
    * ``entity_ids`` — looks each up (``{entities: [...], missing: [...]}``); an
      id with no registry entry lands in ``missing`` rather than being dropped.

    Pure O(n)/O(id) in-memory registry reads. Exactly one of the two params is
    meaningful — the schema rejects both being present; neither present (or only an
    empty-string ``config_entry_id`` / empty ``entity_ids`` list — no usable target)
    raises ``HomeAssistantError`` (see :func:`_registry_lookup_missing_target`)
    rather than silently returning an empty result the caller could mistake for "no
    matches".
    """
    config_entry_id = params.get("config_entry_id")
    entity_ids = params.get("entity_ids") or []
    if not config_entry_id and not entity_ids:
        raise _registry_lookup_missing_target()

    view = _resolve_registries(hass)
    if config_entry_id:
        rows = [
            _entity_partial_dict(entry)
            for entry in _all_entity_entries(view)
            if getattr(entry, "config_entry_id", None) == config_entry_id
        ]
        return {"entities": [row for row in rows if row is not None]}

    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for entity_id in entity_ids:
        entry = _reg_entity(view, entity_id)
        row = _entity_partial_dict(entry) if entry is not None else None
        if row is None:
            missing.append(entity_id)
        else:
            found.append(row)
    return {"entities": found, "missing": missing}


def _registry_lookup_missing_target() -> Exception:
    """Build a ``HomeAssistantError`` for a target-less ``registry_lookup`` request.

    Mirrors :func:`_backup_unavailable`: imported function-locally (test-stubbable)
    so a request with no usable target — neither ``entity_ids`` nor
    ``config_entry_id``, OR only an empty-string / empty-list one — raises instead of
    returning ``{entities: [], missing: []}``, a shape indistinguishable from a
    genuine "nothing matched" result, letting the server's command-error fallback
    fire for the degenerate case. (This is a deliberate divergence from
    ``config_entries``, whose empty-string ``entry_id`` is an authoritative empty
    result mirroring ``async_get_entry("")`` — a different, intentional semantic.)
    """
    from homeassistant.exceptions import HomeAssistantError

    err: Exception = HomeAssistantError(
        "ha_mcp_tools/registry_lookup requires a non-empty entity_ids or "
        "config_entry_id"
    )
    return err


# =============================================================================
# ha_mcp_tools/entity_lookup
# =============================================================================
def _do_entity_lookup(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return registry entries whose ``unique_id`` matches (domain/platform narrow).

    ``{matches: [{entity_id, unique_id, platform, domain, config_entry_id,
    categories, disabled_by, hidden_by}]}``. Scans the entity registry for every
    entry whose ``unique_id`` equals the requested one, optionally narrowed by
    ``domain`` (the entity's own domain, from its entity_id) and ``platform``
    (the owning integration). Multiple matches across platforms are all returned
    — the server picks. ``categories`` mirrors ``as_partial_dict``'s
    ``dict(entry.categories)``; ``disabled_by`` / ``hidden_by`` are unwrapped to
    their wire strings. In-process, so the read is authoritative immediately (no
    registry-write settle retry).

    A drifted entity registry (``er.async_get`` raised / renamed → ``None``) RAISES
    ``HomeAssistantError`` (→ server command-error fallback to the legacy scan)
    rather than returning ``{matches: []}`` — a well-formed empty the server can't
    tell from a genuine "no entry with that unique_id". A present-but-empty registry
    still returns ``{matches: []}`` (correct: no match).
    """
    unique_id = params.get("unique_id")
    domain = params.get("domain")
    platform = params.get("platform")
    view = _resolve_registries(hass)
    if view.entity is None:
        raise _substrate_unavailable("entity registry")
    matches: list[dict[str, Any]] = []
    for entry in _all_entity_entries(view):
        if getattr(entry, "unique_id", None) != unique_id:
            continue
        entity_id = getattr(entry, "entity_id", "") or ""
        ent_domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain and ent_domain != domain:
            continue
        if platform and getattr(entry, "platform", None) != platform:
            continue
        matches.append(
            {
                "entity_id": entity_id,
                "unique_id": getattr(entry, "unique_id", None),
                "platform": getattr(entry, "platform", None),
                "domain": ent_domain,
                "config_entry_id": getattr(entry, "config_entry_id", None),
                "categories": _plainify(getattr(entry, "categories", None) or {}),
                "disabled_by": _enum_value(getattr(entry, "disabled_by", None)),
                "hidden_by": _enum_value(getattr(entry, "hidden_by", None)),
            }
        )
    return {"matches": matches}
