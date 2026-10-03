"""The ``config_entries`` read command."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .registry import (
    _enum_value,
    _iter_config_entries,
    _mapping_values,
    _plainify,
    _timestamp,
)
from .secrets import _load_secret_scrub, _scrub_secret_values


# =============================================================================
# ha_mcp_tools/config_entries
# =============================================================================
def _do_config_entries(
    hass: HomeAssistant,
    params: dict[str, Any],
    *,
    secret_values: frozenset[str] = frozenset(),
    secret_scrub_degraded: bool = False,
) -> dict[str, Any]:
    """Return config entries in the ``config_entries/get`` WS shape.

    ``{entries: [{created_at, modified_at, entry_id, domain, unique_id, title, state, source,
    supports_options, supports_remove_device, supports_unload, supports_reconfigure,
    supported_subentry_types, pref_disable_new_entities, pref_disable_polling,
    disabled_by, reason, error_reason_translation_key,
    error_reason_translation_placeholders, num_subentries, options, subentries}]}``.
    The FULL ``as_json_fragment`` field set (``created_at`` / ``modified_at`` as
    ``.timestamp()`` floats, ``supported_subentry_types`` as core emits it), so the
    component row carries the same fields the legacy REST row does — no field is
    dropped on the component path — PLUS ``unique_id``, the one deliberate
    superset field (core withholds it everywhere; the reconfigure identity
    anchors need it). Filtered by ``domain`` when
    given, or the single entry by ``entry_id``
    (``hass.config_entries.async_get_entry`` — an id that matches nothing,
    including an empty string, yields an empty list). Only a WHOLLY ABSENT
    ``entry_id`` key (``None``) selects list mode; an empty-string ``entry_id`` is
    a single-entry lookup for a nonexistent id, so it returns ``entries: []``
    (mirroring ``async_get_entry("")``) rather than falling through to list mode
    and returning the first entry. ``state`` is serialized as
    ``ConfigEntryState.value`` (mirroring
    how core's ``config_entries/get`` emits it via ``as_json_fragment``, whose
    ``json_repr`` in ``homeassistant/config_entries.py`` is this row's source
    of truth for every field above except ``options``/``subentries``, which
    this integration re-derives since ``as_json_fragment`` never carries
    credential data).

    Data minimization: ``entry.data`` (integration credentials) is NEVER read.
    ``options`` is the only credential-bearing surface emitted, so it is passed
    through a BEST-EFFORT resolved-``!secret`` scrub — an options leaf (dict value,
    list item, or scalar whose ``str()`` form) that exactly equals a ``secrets.yaml``
    value becomes ``"**redacted**"`` (``secret_values`` loaded off the loop by
    :func:`_config_entries_prep`). ``subentries`` carries identity fields only
    (``subentry_id`` / ``subentry_type`` / ``title`` / ``unique_id``) — never a
    subentry's ``data``; a core version without subentries degrades to ``[]``.

    The scrub is BEST-EFFORT: a present-but-unreadable ``secrets.yaml`` degrades it
    to a no-op (options emitted unredacted). That degradation is signalled to the
    caller as ``secret_scrub_degraded: true`` (present ONLY when degraded), so an
    agent echoing ``options`` onward can tell an unscrubbed response from a clean
    one rather than trusting redaction that did not run.

    Pure over ``hass``: the blocking ``secrets.yaml`` read is offloaded by the
    async prep, so this stays a synchronous in-memory read.
    """
    entry_id = params.get("entry_id")
    domain = params.get("domain")
    if entry_id is not None:
        # Single-entry mode. An empty string is a valid (nonexistent) id — it
        # must NOT fall through to list mode, where a truthiness check would
        # return the first entry for a bogus id.
        entry = _config_entry_by_id(hass, entry_id)
        entries: list[Any] = [entry] if entry is not None else []
    else:
        entries = _iter_config_entries(hass)
        if domain:
            entries = [e for e in entries if getattr(e, "domain", None) == domain]
    result: dict[str, Any] = {
        "entries": [_config_entry_row(e, secret_values) for e in entries]
    }
    if secret_scrub_degraded:
        result["secret_scrub_degraded"] = True
    return result


async def _config_entries_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Async pre-step for ``config_entries``: load the secret-scrub set off the loop.

    Unlike ``search`` (which skips the read for an entity-only query),
    ``config_entries`` ALWAYS emits ``options``, so the ``secrets.yaml`` read is
    unconditional. The blocking ``open()`` + ``yaml.safe_load`` runs in the
    executor via :meth:`hass.async_add_executor_job`, keeping
    :func:`_do_config_entries` a pure in-memory read. The ``degraded`` flag (a
    present-but-unreadable ``secrets.yaml``) rides through so the response can signal
    that ``options`` may be unredacted. See :func:`_load_secret_scrub`.
    """
    values, degraded = await hass.async_add_executor_job(_load_secret_scrub, hass)
    return {"secret_values": values, "secret_scrub_degraded": degraded}


def _config_entry_by_id(hass: HomeAssistant, entry_id: str) -> Any:
    """``hass.config_entries.async_get_entry(entry_id)`` guarded (``None`` if absent)."""
    config_entries = getattr(hass, "config_entries", None)
    getter = (
        getattr(config_entries, "async_get_entry", None)
        if config_entries is not None
        else None
    )
    if getter is None:
        return None
    try:
        return getter(entry_id)
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        return None


def _config_entry_row(entry: Any, secret_values: frozenset[str]) -> dict[str, Any]:
    """One config entry as the ``config_entries/get`` row (options scrubbed)."""
    raw_options = getattr(entry, "options", None)
    options = _plainify(dict(raw_options)) if isinstance(raw_options, Mapping) else {}
    options = _scrub_secret_values(options, secret_values)
    return {
        # Timestamps as floats via ``.timestamp()``, mirroring core's
        # as_json_fragment (``self.created_at.timestamp()``). Absent on a core old
        # enough to predate them -> None (this row is read-only, never restored, so a
        # None key is harmless here — unlike the area-registry rows).
        "created_at": _timestamp(getattr(entry, "created_at", None)),
        "modified_at": _timestamp(getattr(entry, "modified_at", None)),
        "entry_id": getattr(entry, "entry_id", None),
        "domain": getattr(entry, "domain", None),
        # The ONE field this row adds beyond core's as_json_fragment. Core
        # deliberately withholds unique_id from every config-entry endpoint
        # (REST list, config_entries/get and get_single all serialize that
        # fragment), so a server needing it as an identity anchor has no other
        # source. Additive within schema_version 1: a server reading an older
        # component sees the KEY ABSENT, which is distinguishable from a
        # present-but-None value, so this needs no version gate — the same
        # discipline as device_get's opt-in entities join.
        "unique_id": getattr(entry, "unique_id", None),
        "title": getattr(entry, "title", None),
        "state": _enum_value(getattr(entry, "state", None)),
        "source": getattr(entry, "source", None),
        # supports_* are computed properties (they touch the flow handler) — read
        # through _safe_prop so a domain whose handler is unavailable degrades
        # instead of raising. ``or False`` mirrors core's as_json_fragment, which
        # coerces the optional bools to False.
        "supports_options": bool(_safe_prop(entry, "supports_options")),
        "supports_remove_device": _safe_prop(entry, "supports_remove_device") or False,
        "supports_unload": _safe_prop(entry, "supports_unload") or False,
        "supports_reconfigure": bool(_safe_prop(entry, "supports_reconfigure")),
        # supported_subentry_types is a computed property (it touches the flow
        # handler, same hazard class as supports_*) — _safe_prop-guarded, defaulting
        # to {} like core's ``self._supported_subentry_types or {}``.
        "supported_subentry_types": _safe_prop(entry, "supported_subentry_types", {})
        or {},
        "pref_disable_new_entities": bool(
            getattr(entry, "pref_disable_new_entities", False)
        ),
        "pref_disable_polling": bool(getattr(entry, "pref_disable_polling", False)),
        "disabled_by": _enum_value(getattr(entry, "disabled_by", None)),
        "reason": getattr(entry, "reason", None),
        "error_reason_translation_key": getattr(
            entry, "error_reason_translation_key", None
        ),
        "error_reason_translation_placeholders": getattr(
            entry, "error_reason_translation_placeholders", None
        ),
        "num_subentries": len(_mapping_values(getattr(entry, "subentries", None))),
        "options": options,
        "subentries": _config_subentries(entry),
    }


def _config_subentries(entry: Any) -> list[dict[str, Any]]:
    """Identity fields of each config subentry — NEVER the subentry ``data``.

    ``entry.subentries`` is a ``MappingProxyType`` keyed by subentry_id in modern
    core; a version without it (``getattr`` -> ``None``) degrades to ``[]``.
    """
    return [
        {
            "subentry_id": getattr(sub, "subentry_id", None),
            "subentry_type": getattr(sub, "subentry_type", None),
            "title": getattr(sub, "title", None),
            "unique_id": getattr(sub, "unique_id", None),
        }
        for sub in _mapping_values(getattr(entry, "subentries", None))
    ]


def _safe_prop(obj: Any, name: str, default: Any = None) -> Any:
    """Read a possibly-computed property, degrading to ``default`` if it raises.

    ``getattr`` alone only defaults on ``AttributeError``; a ConfigEntry's
    ``supports_options`` / ``supports_reconfigure`` are computed off the flow
    handler and can raise other errors when it is unavailable, so catch broadly.
    """
    try:
        return getattr(obj, name, default)
    except Exception:  # pragma: no cover - defensive; core drift  # noqa: BLE001
        return default
