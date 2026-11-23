"""The component-owned extra YAML write keys: normalize, read live, apply (#1887)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN, YAML_KEY_DENYLIST
from .tools_settings_store import (
    EXTRA_YAML_KEYS_KEY,
    save_extra_yaml_keys,
    store_writes,
)

# User-configurable extra YAML write keys (#1887). Same store-per-concern
# rationale as the allowed paths in __init__.py: a separate Store, loaded into hass.data
# at setup and updated in place by set_extra_yaml_keys so enforcement picks up
# changes with no HA restart. This is the component-owned half of the setting;
# the ha-mcp server also carries its own HA_MCP_EXTRA_YAML_KEYS, and the
# effective write allowlist is the union of the two (the server reads this store
# via get_extra_yaml_keys). YAML_KEY_DENYLIST members are stripped on the way in
# and re-checked at enforcement, so the store can never widen the deny floor.
_HASS_DATA_EXTRA_YAML_KEYS_KEY = "extra_yaml_keys"


def _normalize_extra_yaml_keys(raw: Any) -> tuple[list[str], list[Any]]:
    """Clean a candidate extra-YAML-keys list into ``(kept, dropped)`` (#1887).

    Mirrors the server's ``parse_extra_yaml_write_keys`` (strip whitespace, drop
    empties, dedup, sort) and additionally drops any ``YAML_KEY_DENYLIST``
    member, so the component store can never widen the deny floor. Non-string
    and blank entries are dropped too. ``dropped`` collects every rejected
    entry for reporting/logging. Case is preserved: HA's own top-level domain
    lookup is exact-case, so a mis-cased key never reaches a real integration.
    """
    kept: list[str] = []
    dropped: list[Any] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, str):
            dropped.append(entry)
            continue
        key = entry.strip()
        if not key or key in YAML_KEY_DENYLIST:
            dropped.append(entry)
            continue
        if key not in seen:
            seen.add(key)
            kept.append(key)
    return sorted(kept), dropped


def _current_extra_yaml_keys(hass: HomeAssistant) -> list[str]:
    """Return the live component-configured extra YAML write keys from hass.data."""
    domain_data = hass.data.get(DOMAIN)
    if isinstance(domain_data, dict):
        keys = domain_data.get(_HASS_DATA_EXTRA_YAML_KEYS_KEY)
        if isinstance(keys, list):
            return keys
    return []


async def _apply_extra_yaml_keys(
    hass: HomeAssistant, raw_keys: Any, only_if: list[list[str]] | None = None
) -> tuple[list[str], list[Any]] | None:
    """Shared by the ``set_extra_yaml_keys`` service and the tools-entry options flow.

    Returns None, without writing, when the current list is not in ``only_if``.
    """
    async with store_writes(hass, EXTRA_YAML_KEYS_KEY).writing():
        if only_if is not None and _current_extra_yaml_keys(hass) not in only_if:
            return None
        return await _apply_extra_yaml_keys_locked(hass, raw_keys)


async def _apply_extra_yaml_keys_locked(
    hass: HomeAssistant, raw_keys: Any
) -> tuple[list[str], list[Any]]:
    """Normalize, persist, and hot-swap the extra YAML write keys (#1887).

    The caller holds ``store_writes(hass, EXTRA_YAML_KEYS_KEY).lock`` and owns
    the revision bump. Delegates validation to :func:`_normalize_extra_yaml_keys` (strip,
    dedup, sort, drop denylist). Persists to .storage and updates hass.data so
    enforcement applies live. Returns ``(kept, rejected)``.
    """
    normalized, rejected = _normalize_extra_yaml_keys(raw_keys)
    await save_extra_yaml_keys(hass, normalized)
    hass.data.setdefault(DOMAIN, {})[_HASS_DATA_EXTRA_YAML_KEYS_KEY] = normalized
    return normalized, rejected
