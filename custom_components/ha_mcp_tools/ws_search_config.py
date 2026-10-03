"""Config-body and helper search surfaces for ``ha_mcp_tools/search``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .ws_constants import (
    COLLECTION_HELPER_DOMAINS,
    ENTITY_COMPONENTS_KEY,
    FLOW_HELPER_DOMAINS,
    MAX_BODY_BYTES,
    SEARCH_TYPE_SCENE,
)
from .ws_registry import _iter_config_entries, _iter_states, _plainify, _RegistryView
from .ws_search_score import _config_score


# --- Config surfaces (automation/script/scene) -------------------------------
def _search_config_surface(
    hass: HomeAssistant,
    view: _RegistryView,
    domain: str,
    query_lower: str,
    *,
    match_all: bool,
    exact: bool,
    include_config: bool,
    partial_reasons: list[str],
    diagnostics: dict[str, int],
    secret_values: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Score one config domain's loaded entities (raw_config indexed, not emitted for YAML)."""
    component = hass.data.get(domain) if getattr(hass, "data", None) else None
    entities = getattr(component, "entities", None)
    if entities is None:
        diagnostics["config_components_inaccessible"] = (
            diagnostics.get("config_components_inaccessible", 0) + 1
        )
        return []

    results: list[dict[str, Any]] = []
    for entity in entities:
        entity_id = getattr(entity, "entity_id", None)
        if not entity_id:
            continue
        name, item_id, config_dict = _extract_config(domain, entity)
        source = _classify_source(item_id)

        if match_all:
            score: int | None = 100
            match_in_name = False
            match_in_config = False
        else:
            scored = _config_score(
                query_lower,
                entity_id,
                name,
                config_dict,
                exact=exact,
                secret_values=secret_values,
            )
            if scored is None:
                continue
            score, match_in_name, match_in_config = scored

        # Scenes never emit a component-served body: a HomeAssistantScene holds no
        # raw storage dict (its states are runtime State objects), so config stays
        # None and config_dict is used only as the faithful match corpus.
        config_out: dict[str, Any] | None = None
        if (
            domain != SEARCH_TYPE_SCENE
            and include_config
            and source == "storage"
            and config_dict is not None
        ):
            if _too_large(config_dict):
                partial_reasons.append(f"{domain} {entity_id} body omitted (too large)")
            else:
                config_out = config_dict

        rec: dict[str, Any] = {
            "id": item_id,
            "entity_id": entity_id,
            "source": source,
            "score": score,
            "match_in_name": match_in_name,
            "match_in_config": match_in_config,
            "config": config_out,
        }
        # Scenes carry a "name"; automations/scripts carry an "alias".
        if domain == SEARCH_TYPE_SCENE:
            rec["name"] = name
        else:
            rec["alias"] = name
        results.append(rec)
    return results


def _extract_config(
    domain: str, entity: Any
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Return (display_name, item_id, config_dict) for a config entity.

    Uses defensive getattr because the exact accessor can drift across core
    versions: automation/script expose ``raw_config``; scenes expose
    ``scene_config`` (name/icon/id/states) rather than ``raw_config``. For a
    scene the returned ``config_dict`` is the faithful MATCH corpus only (see
    :func:`_scene_match_corpus`), never an emittable body.
    """
    entity_id = getattr(entity, "entity_id", "") or ""
    name = getattr(entity, "name", None) or entity_id
    unique_id = getattr(entity, "unique_id", None)

    if domain == SEARCH_TYPE_SCENE:
        scene_config = getattr(entity, "scene_config", None)
        config_dict = _scene_match_corpus(scene_config)
        item_id = unique_id
        if item_id is None and config_dict is not None:
            item_id = config_dict.get("id")
        if config_dict is not None:
            cfg_name = config_dict.get("name")
            if cfg_name:
                name = str(cfg_name)
        return str(name), (str(item_id) if item_id is not None else None), config_dict

    raw = getattr(entity, "raw_config", None)
    config_dict = dict(raw) if isinstance(raw, dict) else None
    item_id = unique_id
    if item_id is None and config_dict is not None:
        item_id = config_dict.get("id")
    return str(name), (str(item_id) if item_id is not None else None), config_dict


def _scene_match_corpus(scene_config: Any) -> dict[str, Any] | None:
    """Faithful, minimal MATCH corpus for a scene — never an emittable body.

    A ``HomeAssistantScene`` holds no raw storage dict: ``scene_config.states`` is
    a ``{entity_id: State}`` map of RUNTIME ``State`` objects. Scoring/emitting
    those (each stringifying to ``<state light.x=on; ...>``) was garbage and
    diverged the component's scoring from any real body. Index only the faithful,
    non-runtime facts instead: ``id`` / ``name`` / ``icon`` plus the entity-id
    KEYS of ``states`` (so "which scenes touch ``light.x``" still matches) — no
    State values, no timestamps or contexts. Used for MATCHING only; the scene
    record never emits a ``config`` body (see :func:`_search_config_surface`).
    """
    if scene_config is None:
        return None
    if isinstance(scene_config, Mapping):
        src: Mapping[str, Any] = scene_config
    else:
        collected: dict[str, Any] = {}
        for attr in ("id", "name", "icon", "states", "entities"):
            val = getattr(scene_config, attr, None)
            if val is not None:
                collected[attr] = val
        src = collected
    out: dict[str, Any] = {}
    for key in ("id", "name", "icon"):
        val = src.get(key)
        if val is not None:
            out[key] = str(val)
    entity_ids: set[str] = set()
    for key in ("states", "entities"):
        mapping = src.get(key)
        if isinstance(mapping, Mapping):
            entity_ids.update(str(k) for k in mapping)
    if entity_ids:
        out["entities"] = sorted(entity_ids)
    return out or None


def _classify_source(item_id: str | None) -> str:
    """Classify an automation/script/scene as storage- or YAML-backed.

    HA addresses editor-managed items by their ``id`` (the entity's
    ``unique_id``); the config editor's ``/config/<domain>/config/<id>`` REST
    path — and its edit link — key off exactly that id, and 404 for items with
    no id. So an id-bearing item is treated as ``storage`` (body emittable under
    ``include_config``); an id-less item is ``yaml`` and its body is never
    emitted from search (its ``raw_config`` may carry resolved ``!secret``
    plaintext). This is the conservative rule: the safe error is toward
    withholding a body, not leaking one.
    """
    return "storage" if item_id else "yaml"


def _too_large(config_dict: dict[str, Any]) -> bool:
    """Rough guard so a huge body never balloons a single WS frame."""
    try:
        return len(repr(config_dict)) > MAX_BODY_BYTES
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return False


# --- Helpers surface ---------------------------------------------------------
def _collection_storage_index(
    hass: HomeAssistant, domains: frozenset[str]
) -> dict[str, tuple[dict[str, Any], str | None]]:
    """Map collection-helper ``entity_id`` -> ``(storage body, storage id)``.

    Collection helpers keep their full storage config on the ``CollectionEntity``
    as ``_config`` — a schedule's weekday blocks, an input_datetime's
    ``has_date``/``has_time``, an input_boolean's ``initial`` — fields the live
    state attributes do NOT carry. The ``StorageCollection`` that loaded them is a
    setup-local (``helpers/collection.py`` writes nothing to ``hass.data``), so
    the reachable in-process source is the domain's ``EntityComponent``
    (``hass.data['entity_components'][domain]``, or ``hass.data[domain]`` for the
    automation/script/scene pattern) and each entity's ``_config``.

    Domains that decompose config into ``_attr_*`` instead of keeping ``_config``
    (input_number / input_text / input_select) have no entry here; the caller
    falls back to the state-attributes body for them (and for any YAML-defined
    helper whose entity is absent). All access is getattr-guarded against drift.
    """
    index: dict[str, tuple[dict[str, Any], str | None]] = {}
    data = getattr(hass, "data", None)
    if not isinstance(data, Mapping):
        return index
    instances = data.get(ENTITY_COMPONENTS_KEY)
    for domain in domains:
        component = (
            instances.get(domain) if isinstance(instances, Mapping) else None
        ) or data.get(domain)
        entities = getattr(component, "entities", None)
        if entities is None:
            continue
        try:
            entity_list = list(entities)
        except Exception:  # pragma: no cover - defensive  # noqa: BLE001
            continue
        for entity in entity_list:
            entity_id = getattr(entity, "entity_id", None)
            if not entity_id:
                continue
            raw = getattr(entity, "_config", None)
            if not isinstance(raw, dict):
                continue
            storage_id = getattr(entity, "unique_id", None) or raw.get("id")
            index[entity_id] = (
                dict(raw),
                str(storage_id) if storage_id is not None else None,
            )
    return index


def _search_helpers(
    hass: HomeAssistant,
    query_lower: str,
    *,
    match_all: bool,
    exact: bool,
    include_config: bool,
    secret_values: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Index collection helpers (states) + flow helpers (config-entry options)."""
    results: list[dict[str, Any]] = []

    # Collection helpers: entities in the state machine, matched on entity_id /
    # friendly_name AND the searchable config body. That body is the entity's real
    # storage ``_config`` (a schedule's weekday blocks, an input_select's
    # ``options`` + ``initial``, …) when reachable, falling back to the live state
    # attributes otherwise (see _collection_storage_index). The name still comes
    # from the CURRENT friendly_name, not the creation-time storage name.
    storage = _collection_storage_index(hass, COLLECTION_HELPER_DOMAINS)
    for state in _iter_states(hass):
        entity_id = getattr(state, "entity_id", "") or ""
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if domain not in COLLECTION_HELPER_DOMAINS:
            continue
        attrs = getattr(state, "attributes", None) or {}
        name = attrs.get("friendly_name", entity_id)
        object_id = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
        stored = storage.get(entity_id)
        if stored is not None:
            body = _plainify(stored[0])
        else:
            body = dict(attrs) if isinstance(attrs, Mapping) else {}
        if match_all:
            score: int | None = 100
            match_in_name = False
            match_in_config = False
        else:
            scored = _config_score(
                query_lower,
                entity_id,
                name,
                body,
                exact=exact,
                secret_values=secret_values,
            )
            if scored is None:
                continue
            score, match_in_name, match_in_config = scored
        results.append(
            {
                "entity_id": entity_id,
                "helper_type": domain,
                "object_id": object_id,
                "name": name,
                "kind": "collection",
                "score": score,
                "match_in_name": match_in_name,
                "match_in_config": match_in_config,
                "config": body if include_config else None,
            }
        )

    # Flow helpers: config entries — options + title ONLY, never data.
    for entry in _iter_config_entries(hass):
        domain = getattr(entry, "domain", None)
        if domain not in FLOW_HELPER_DOMAINS:
            continue
        title = getattr(entry, "title", None) or ""
        # ``ConfigEntry.options`` is a ``MappingProxyType`` in live HA, not a
        # ``dict``; the old ``isinstance(..., dict)`` guard silently dropped it to
        # ``{}``, so a flow helper's body (a template's ``state``, a group's
        # members, …) was never indexed and ``match_in_config`` could never fire.
        # Accept any ``Mapping`` so the persisted options are searchable and
        # emittable under ``include_config``.
        raw_options = getattr(entry, "options", None)
        options = dict(raw_options) if isinstance(raw_options, Mapping) else {}
        entry_id = getattr(entry, "entry_id", None)
        if match_all:
            score = 100
            match_in_name = False
            match_in_config = False
        else:
            scored = _config_score(
                query_lower,
                title,
                title,
                options,
                exact=exact,
                secret_values=secret_values,
            )
            if scored is None:
                continue
            score, match_in_name, match_in_config = scored
        results.append(
            {
                "entity_id": None,
                "helper_type": domain,
                "entry_id": entry_id,
                "name": title,
                "kind": "flow",
                "score": score,
                "match_in_name": match_in_name,
                "match_in_config": match_in_config,
                # Data minimization: options only, never entry.data.
                "options": options if include_config else None,
            }
        )
    return results
