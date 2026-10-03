"""The ``services_list`` and ``reference_data`` read commands."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .overview import _overview_services
from .registry import _call_no_arg, _iter_states, _substrate_unavailable

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(__package__)


# =============================================================================
# ha_mcp_tools/services_list
# =============================================================================
def _do_services_list(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    descriptions: Mapping[str, Any] | None = None,
    translations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reshape the service catalog to the REST ``/api/services`` list, ``domain``-filtered.

    ``descriptions`` (``async_get_all_descriptions`` — ``{domain: {service:
    desc}}``) and ``translations`` (``async_get_translations`` for the ``services``
    category) are loaded off the loop by :func:`_services_list_prep`.

    ``domain`` is the only filter — an exact match, same semantics both paths.
    There is deliberately NO ``query`` coarse-filter: the server's exact filter
    matches against the CONCATENATION ``f"{domain}.{service} {name} {description}"``
    (with a title-cased ``service.replace("_"," ").title()`` name fallback), and a
    cheap per-field component pass is NOT a superset of that — it misses queries
    spanning the ``domain.service`` / name / description boundaries and the
    title-cased fallback, so forwarding ``query`` would silently drop matching
    services. No consumer forwards ``query`` (the server re-runs its exact filter +
    pagination over the full payload), so the component simply doesn't accept it.
    Translations are scoped to the kept domains' key prefixes.
    """
    descriptions = descriptions or {}
    translations = translations or {}
    domain_filter = params.get("domain")

    services: list[dict[str, Any]] = []
    kept_domains: set[str] = set()
    for domain, domain_services in descriptions.items():
        if domain_filter and domain != domain_filter:
            continue
        services_map = (
            dict(domain_services) if isinstance(domain_services, Mapping) else {}
        )
        services.append({"domain": domain, "services": services_map})
        kept_domains.add(str(domain))

    return {
        "services": services,
        "translations": _filter_service_translations(translations, kept_domains),
    }


async def _services_list_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Async pre-step for ``services_list``: load descriptions + translations off-loop.

    Both ``async_get_all_descriptions`` and ``async_get_translations`` await
    (translation loads touch the filesystem / integration setup), so they run in
    the prep via the seam wrappers below and :func:`_do_services_list` stays a pure
    reshape/filter.
    """
    language = msg.get("language") or "en"
    descriptions = await _fetch_service_descriptions(hass)
    translations = await _fetch_service_translations(hass, language)
    return {"descriptions": descriptions, "translations": translations}


async def _fetch_service_descriptions(hass: HomeAssistant) -> Mapping[str, Any]:
    """core ``async_get_all_descriptions(hass)``; function-local import test seam.

    A non-``Mapping`` return is core drift, NOT an empty catalog: RAISE
    ``HomeAssistantError`` (→ server command-error fallback to the legacy REST
    ``/api/services`` read) rather than serving a well-formed empty catalog the
    server would trust as authoritative. A raising ``async_get_all_descriptions``
    already propagates the same way.
    """
    from homeassistant.helpers.service import async_get_all_descriptions

    result = await async_get_all_descriptions(hass)
    if not isinstance(result, Mapping):
        raise _substrate_unavailable("service descriptions")
    return result


async def _fetch_service_translations(
    hass: HomeAssistant, language: str
) -> Mapping[str, Any]:
    """core ``async_get_translations(hass, language, "services")``; test seam.

    Fails soft (empty map) so a translation-load failure degrades to the untranslated
    service list rather than failing the whole command.
    """
    from homeassistant.helpers.translation import async_get_translations

    try:
        result = await async_get_translations(hass, language, "services")
    except Exception:  # translations are additive; degrade to none on any error
        _LOGGER.warning(
            "services_list: could not load service translations; "
            "continuing without them",
            exc_info=True,
        )
        return {}
    if isinstance(result, Mapping):
        return result
    # Same visibility as the failure above: discarding the catalog leaves the
    # service list untranslated, and the type is the only useful clue. Kept in
    # step with the mirrored seam in config_flow.
    _LOGGER.warning(
        "services_list: ignoring the %s service translations: expected a "
        "Mapping, got %s",
        language,
        type(result).__name__,
    )
    return {}


def _filter_service_translations(
    translations: Mapping[str, Any], kept_domains: set[str]
) -> dict[str, Any]:
    """Keep only translation keys whose domain segment is in ``kept_domains``.

    Backend ``services``-category keys are ``component.<domain>.services.<service>.…``
    so the domain is the second dotted segment.
    """
    return {
        key: value
        for key, value in translations.items()
        if _translation_key_domain(key) in kept_domains
    }


def _translation_key_domain(key: Any) -> str | None:
    """The ``<domain>`` segment of a ``component.<domain>.services.…`` translation key."""
    if not isinstance(key, str):
        return None
    parts = key.split(".")
    return parts[1] if len(parts) > 1 else None


# =============================================================================
# ha_mcp_tools/reference_data
# =============================================================================
def _do_reference_data(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """Return the service index + entity-id universe the reference validator reads.

    ``services`` is the REST ``/api/services`` list shape ``build_service_index``
    consumes — reusing :func:`_overview_services`, whose per-service bodies are
    EMPTY dicts (the index only reads service-name keys). ``entity_ids`` is every
    ``hass.states.async_all()`` id (the ``build_entity_set`` universe). Pure,
    synchronous, no prep — both are in-memory reads.

    A drifted service registry (``hass.services.async_services()`` raised / renamed
    → non-``Mapping``) or state machine (``hass.states.async_all`` absent / renamed)
    RAISES ``HomeAssistantError`` (→ server command-error fallback to the legacy
    REST ``get_services()`` / ``get_states()`` pair) rather than returning empty
    catalogs — which would make EVERY reference emit a false "not found" warning,
    where the legacy failure mode is skip-validation. A genuinely-empty (but
    present) substrate still returns its empty result.
    """
    include_states = params.get("include_states", True)
    if not isinstance(
        _call_no_arg(getattr(hass, "services", None), "async_services"), Mapping
    ):
        raise _substrate_unavailable("service registry")
    entity_ids: list[str] = []
    if include_states:
        states_obj = getattr(hass, "states", None)
        if not callable(getattr(states_obj, "async_all", None)):
            raise _substrate_unavailable("state machine")
        for state in _iter_states(hass):
            entity_id = getattr(state, "entity_id", None)
            if entity_id:
                entity_ids.append(entity_id)
    return {"services": _overview_services(hass), "entity_ids": entity_ids}
