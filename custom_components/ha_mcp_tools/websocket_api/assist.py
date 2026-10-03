"""Assist exposure lookups shared by the visibility filter and exposure."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(__package__)


def _async_get_entity_settings(hass: HomeAssistant, entity_id: str) -> Any:
    """core's ``async_get_entity_settings(hass, entity_id)``; test seam.

    Imported lazily so the fake-hass unit suite (which MagicMock-stubs
    ``homeassistant.*`` at import time) can monkeypatch this whole function rather
    than the deep core module. Returns ``{assistant: settings_mapping}`` and raises
    ``HomeAssistantError("Unknown entity")`` for an id in neither the registry nor
    the exposed-entities store — caught by :func:`_entity_exposed_to`.
    """
    from homeassistant.components.homeassistant.exposed_entities import (
        async_get_entity_settings,
    )

    return async_get_entity_settings(hass, entity_id)


def _is_unknown_entity_error(exc: Exception) -> bool:
    """True for core's ``HomeAssistantError('Unknown entity')`` from the settings helper.

    Keyed off the exception type NAME (not an ``isinstance`` against the imported
    class) so the fake-hass suite — which stubs ``homeassistant.exceptions`` — can
    raise a stand-in ``HomeAssistantError`` without importing the real class. The
    type name alone is too wide: core raises a plain ``HomeAssistantError`` for
    other faults too, so the message is also required to carry ``unknown entity``
    (case-insensitive). A store-read failure that raises a bare
    ``HomeAssistantError`` therefore propagates instead of being silently reported
    as not-exposed; the audit guardrail (junk id → not-exposed default) still
    matches because that raise carries the ``Unknown entity`` message.
    """
    return (
        type(exc).__name__ == "HomeAssistantError"
        and "unknown entity" in str(exc).lower()
    )


def _assist_should_expose(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether ``entity_id`` is exposed to the ``conversation`` assistant — READ-ONLY.

    A read-only replication of core's ``async_should_expose`` for the conversation
    assistant. core's real function is NOT called because it has a WRITE side effect:
    for an entity with no explicit stored exposure it computes the default and
    persists it back (``entity_registry.async_update_entity_options`` for a registry
    entity, or the exposed-entities store for a legacy one — see core
    ``exposed_entities.py``). Consulting it once per candidate entity from a
    ``readOnlyHint`` search would stamp exposure onto the whole entity universe and
    pin those defaults, which violates this module's pure-read contract. So this
    reconstructs the SAME precedence (matching the server resolver's
    ``_is_assist_exposed``) with no write: an explicit per-entity ``should_expose``
    wins; otherwise the "expose new entities" flag gates the default-exposure check.

    Composed from three read-only, individually monkeypatchable seams. Fails OPEN
    (returns ``True`` — do not hide) on any error, matching the resolver's "skip the
    Assist dimension when its data is unavailable" behaviour.
    """
    try:
        explicit = _explicit_assist_exposure(hass, entity_id)
        if explicit is not None:
            return explicit
        if not _assist_expose_new_entities(hass):
            return False
        return _assist_default_exposed(hass, entity_id)
    except Exception:  # noqa: BLE001  # fail open (do not hide) on any error, mirroring the resolver
        return True


def _explicit_assist_exposure(hass: HomeAssistant, entity_id: str) -> bool | None:
    """The entity's explicit ``conversation`` ``should_expose`` (True/False), else None.

    Reads the SAME read-only surface :func:`_do_exposure`'s list mode mirrors — core's
    ``async_get_entity_settings`` (registry ``options`` for a registry entity, else
    the legacy exposed-entities store), which never writes. Returns ``None`` when there
    is no explicit override (an id in neither the registry nor the store, or no
    ``conversation.should_expose`` key), so the caller falls through to the
    expose-new default. A non-``Unknown entity`` raise propagates (fails open upstream).
    """
    try:
        settings = _async_get_entity_settings(hass, entity_id)
    except Exception as exc:
        if _is_unknown_entity_error(exc):
            return None
        raise
    conv = settings.get("conversation") if isinstance(settings, Mapping) else None
    if isinstance(conv, Mapping) and "should_expose" in conv:
        return bool(conv["should_expose"])
    return None


def _assist_expose_new_entities(hass: HomeAssistant) -> bool:
    """core's ``ExposedEntities.async_get_expose_new_entities("conversation")`` — read-only.

    Function-local import + a standalone seam so the fake-hass suite can monkeypatch
    it. A ``@callback`` that only reads the assistant preferences (no store write).
    """
    from homeassistant.components.homeassistant.exposed_entities import (
        DATA_EXPOSED_ENTITIES,
    )

    exposed = hass.data[DATA_EXPOSED_ENTITIES]
    return bool(exposed.async_get_expose_new_entities("conversation"))


def _assist_default_exposed(hass: HomeAssistant, entity_id: str) -> bool:
    """Read-only mirror of ``ExposedEntities._is_default_exposed`` for ``conversation``.

    core's ``async_should_expose`` calls the private ``_is_default_exposed`` and then
    WRITES the result back; this recomputes it WITHOUT the write. Imports core's own
    default-exposure constants (no drift — the running core defines them) and uses
    core's ``get_device_class``, so the domain / device-class verdict matches core
    exactly. entity_category / hidden_by entities are never a default exposure. A
    standalone seam so the fake-hass suite can monkeypatch it.
    """
    from homeassistant.components.homeassistant.exposed_entities import (
        DEFAULT_EXPOSED_BINARY_SENSOR_DEVICE_CLASSES,
        DEFAULT_EXPOSED_DOMAINS,
        DEFAULT_EXPOSED_SENSOR_DEVICE_CLASSES,
    )
    from homeassistant.helpers import entity_registry as er

    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and (
        getattr(entry, "entity_category", None) is not None
        or getattr(entry, "hidden_by", None) is not None
    ):
        return False
    domain = entity_id.split(".", maxsplit=1)[0] if "." in entity_id else entity_id
    if domain in DEFAULT_EXPOSED_DOMAINS:
        return True
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.entity import get_device_class

    try:
        device_class = get_device_class(hass, entity_id)
    except HomeAssistantError:  # the entity no longer exists — matches core
        return False
    if domain == "binary_sensor":
        return device_class in DEFAULT_EXPOSED_BINARY_SENSOR_DEVICE_CLASSES
    if domain == "sensor":
        return device_class in DEFAULT_EXPOSED_SENSOR_DEVICE_CLASSES
    return False


def _assist_exposure_available(hass: HomeAssistant) -> bool:
    """Whether core's Assist exposure machinery can be consulted for this request.

    The resolver emits ``_ASSIST_UNAVAILABLE_WARNING`` when its expose-list fetch
    fails wholesale; the component's analog is core's ``async_should_expose`` being
    unavailable — it reads ``hass.data[DATA_EXPOSED_ENTITIES]`` and raises when the
    exposed_entities store isn't set up (``_assist_should_expose`` then fails open
    per entity, hiding nothing but warning about nothing either). Probing the store
    once lets the caller skip the Assist dimension AND surface the resolver-parity
    degradation warning instead of degrading silently. Imported lazily and a test
    seam (monkeypatched alongside ``_assist_should_expose``).
    """
    try:
        from homeassistant.components.homeassistant.exposed_entities import (
            DATA_EXPOSED_ENTITIES,
        )
    except Exception:
        _LOGGER.warning("Assist exposure support import failed", exc_info=True)
        return False
    data = getattr(hass, "data", None)
    return isinstance(data, Mapping) and DATA_EXPOSED_ENTITIES in data
