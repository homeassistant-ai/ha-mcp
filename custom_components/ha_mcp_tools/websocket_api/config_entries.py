"""The ``config_entries`` read command."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes

from .registry import _iter_config_entries, _mapping_values, _plainify
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
    """Return config entries as Core's ``config_entries/get`` rows, extended.

    Each row is Core's ``ConfigEntry.as_json_fragment`` plus ``unique_id``,
    ``options`` and ``subentries`` (see :func:`_config_entry_row`). Filtered by
    ``domain`` when given, or the single entry by ``entry_id``
    (``hass.config_entries.async_get_entry``). Only a WHOLLY ABSENT ``entry_id``
    key (``None``) selects list mode; an empty-string ``entry_id`` is a
    single-entry lookup for a nonexistent id, so it returns ``entries: []``
    (mirroring ``async_get_entry("")``) rather than falling through to list mode
    and returning the first entry.

    Data minimization: ``entry.data`` (integration credentials) is NEVER read.
    ``options`` is the only credential-bearing surface emitted, so it is passed
    through a BEST-EFFORT resolved-``!secret`` scrub — an options leaf (dict value,
    list item, or scalar whose ``str()`` form) that exactly equals a ``secrets.yaml``
    value becomes ``"**redacted**"`` (``secret_values`` loaded off the loop by
    :func:`_config_entries_prep`). ``subentries`` carries identity fields only
    (``subentry_id`` / ``subentry_type`` / ``title`` / ``unique_id``) and a
    subentry's ``data`` only on request (``include_subentry_data``, for backups),
    under the same scrub as ``options``; a core version without subentries
    degrades to ``[]``.

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
        "entries": [
            _config_entry_row(
                e, secret_values, params.get("include_subentry_data", False)
            )
            for e in entries
        ]
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


def _config_entry_row(
    entry: Any, secret_values: frozenset[str], include_subentry_data: bool = False
) -> dict[str, Any]:
    """One config entry: Core's API row plus ``unique_id``, ``options``, ``subentries``.

    The base is ``entry.as_json_fragment``, the same bytes Core's
    ``config_entries/get`` and REST entry list send, so every field Core adds
    reaches the caller unchanged; a ``supports_*`` property that raises fails
    Core's own ``config_entries/get`` the same way. Core withholds ``unique_id``
    from every config-entry endpoint, and the reconfigure identity anchors need it.
    """
    # An ``orjson.Fragment`` exposes no bytes; serializing it yields them.
    row: dict[str, Any] = json.loads(json_bytes(entry.as_json_fragment))
    raw_options = getattr(entry, "options", None)
    options = _plainify(dict(raw_options)) if isinstance(raw_options, Mapping) else {}
    row["unique_id"] = getattr(entry, "unique_id", None)
    row["options"] = _scrub_secret_values(options, secret_values)
    row["subentries"] = _config_subentries(
        entry, secret_values if include_subentry_data else None
    )
    return row


def _config_subentries(
    entry: Any, data_secret_values: frozenset[str] | None = None
) -> list[dict[str, Any]]:
    """Identity fields of each config subentry; its ``data`` only on request.

    ``data_secret_values`` (the backup capture's opt-in) adds each subentry's
    ``data`` under the same resolved-``!secret`` scrub as ``options``.
    ``entry.subentries`` is a ``MappingProxyType`` keyed by subentry_id in modern
    core; a version without it (``getattr`` -> ``None``) degrades to ``[]``.
    """
    rows = []
    for sub in _mapping_values(getattr(entry, "subentries", None)):
        row = {
            "subentry_id": getattr(sub, "subentry_id", None),
            "subentry_type": getattr(sub, "subentry_type", None),
            "title": getattr(sub, "title", None),
            "unique_id": getattr(sub, "unique_id", None),
        }
        if data_secret_values is not None:
            raw = getattr(sub, "data", None)
            data = _plainify(dict(raw)) if isinstance(raw, Mapping) else {}
            row["data"] = _scrub_secret_values(data, data_secret_values)
        rows.append(row)
    return rows
