"""Value-source registry for the predicate builder UI.

When a user picks a tool + arg-path in the Tool Security Policies tab,
the UI needs to know whether to render a free-text input or a dropdown
of legal values. This module maps `(tool_name, arg_path)` pairs to a
named value source, plus implements the fetchers that read live values
out of Home Assistant.

Read-only tools are explicitly out of scope for gating UX polish, so the
registry covers write/destructive tools only. Anything not in the
registry falls back to free-text JSON entry in the UI.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# (tool_name, arg_path) → value_source key. arg_path is the same dotted
# string the user picks in the predicate "path" dropdown — i.e. with the
# "args." prefix the evaluator uses.
VALUE_SOURCE_REGISTRY: dict[tuple[str, str], str] = {
    # Service-call gating — by far the most common case the user wants
    # to author (gate ha_call_service when domain in [lock, alarm_*]).
    ("ha_call_service", "args.domain"): "ha_domains",
    ("ha_call_service", "args.service"): "ha_services",
    ("ha_call_service", "args.entity_id"): "ha_entities",
    # Bulk-control mirrors call_service's arg shape per item, but the
    # outer wrapper takes a list — the predicate language can't reach
    # into list items today, so no registry entries here. Free-text
    # fallback still works.
    ("ha_set_entity", "args.entity_id"): "ha_entities",
    ("ha_get_state", "args.entity_id"): "ha_entities",
    ("ha_get_entity", "args.entity_id"): "ha_entities",
    ("ha_get_history", "args.entity_ids"): "ha_entities",
    ("ha_remove_entity", "args.entity_id"): "ha_entities",
    ("ha_update_device", "args.entity_id"): "ha_entities",
    ("ha_get_entity_exposure", "args.entity_id"): "ha_entities",
    ("ha_set_integration_enabled", "args.entity_id"): "ha_entities",
}

# Fetched-value TTL cache so the UI can click through path options
# without hammering HA. 30s is long enough to cover normal exploration
# but short enough that newly-added domains/entities appear quickly.
_CACHE_TTL_SECONDS = 30.0
_cache: dict[str, tuple[float, list[str]]] = {}


def value_source_for(tool_name: str, arg_path: str) -> str | None:
    return VALUE_SOURCE_REGISTRY.get((tool_name, arg_path))


def all_value_sources_for(tool_name: str) -> dict[str, str]:
    """Return {arg_path: value_source} for one tool — used by the UI to
    decide which paths render as dropdowns vs free-text."""
    return {
        arg_path: source
        for (tn, arg_path), source in VALUE_SOURCE_REGISTRY.items()
        if tn == tool_name
    }


def _cache_get(key: str) -> list[str] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        return None
    return value


def _cache_set(key: str, value: list[str]) -> None:
    _cache[key] = (time.monotonic(), value)


async def fetch_value_source(
    source: str,
    *,
    client: Any,
    params: dict[str, str] | None = None,
) -> list[str]:
    """Fetch live choices for a known value source.

    Raises ValueError if ``source`` is unknown so the handler can return
    a 400 instead of an empty list (which would look like "no choices
    available" to the user).
    """
    params = params or {}
    fetcher = _FETCHERS.get(source)
    if fetcher is None:
        raise ValueError(f"Unknown value source: {source!r}")
    cache_key = source + "|" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    values = sorted(await fetcher(client, params))
    # Don't cache an empty result — a transient HA glitch (WebSocket
    # reconnect window, auth lapse, etc.) can return [] briefly; caching
    # that for 30s would degrade the dropdown UX for the whole window.
    # Genuine "this source legitimately has no values" callers re-fetch
    # cheap enough.
    if values:
        _cache_set(cache_key, values)
    return values


async def _fetch_ha_domains(client: Any, _params: dict[str, str]) -> list[str]:
    services = await client.get_services()
    # /services returns either {domain: {service: ...}} or
    # [{domain, services}, ...] depending on HA version; handle both.
    if isinstance(services, dict):
        return list(services.keys())
    if isinstance(services, list):
        return [s["domain"] for s in services if isinstance(s, dict) and "domain" in s]
    # Unknown shape — surface it so a future HA API change doesn't silently
    # blank the domain dropdown (looks identical to "HA has no domains").
    logger.warning(
        "ha_domains: get_services returned unexpected shape %s", type(services).__name__
    )
    return []


async def _fetch_ha_services(client: Any, params: dict[str, str]) -> list[str]:
    services = await client.get_services()
    domain_filter = params.get("domain")
    out: set[str] = set()
    if isinstance(services, dict):
        if domain_filter:
            out.update((services.get(domain_filter) or {}).keys())
        else:
            for svcs in services.values():
                if isinstance(svcs, dict):
                    out.update(svcs.keys())
        return list(out)
    if isinstance(services, list):
        for entry in services:
            if not isinstance(entry, dict):
                continue
            if domain_filter and entry.get("domain") != domain_filter:
                continue
            svcs = entry.get("services") or {}
            if isinstance(svcs, dict):
                out.update(svcs.keys())
        return list(out)
    logger.warning(
        "ha_services: get_services returned unexpected shape %s",
        type(services).__name__,
    )
    return []


async def _fetch_ha_entities(client: Any, params: dict[str, str]) -> list[str]:
    states = await client.get_states()
    domain_filter = params.get("domain")
    out: list[str] = []
    for s in states:
        if not isinstance(s, dict):
            continue
        eid = s.get("entity_id")
        if not isinstance(eid, str):
            continue
        if domain_filter and not eid.startswith(domain_filter + "."):
            continue
        out.append(eid)
    return out


_FETCHERS: dict[str, Callable[[Any, dict[str, str]], Awaitable[list[str]]]] = {
    "ha_domains": _fetch_ha_domains,
    "ha_services": _fetch_ha_services,
    "ha_entities": _fetch_ha_entities,
}
