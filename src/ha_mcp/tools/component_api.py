"""Capability gate for the ``ha_mcp_tools`` custom-component WebSocket API.

The custom component (``custom_components/ha_mcp_tools/``, manifest 1.1.0+)
registers in-process WebSocket commands under the ``ha_mcp_tools/`` namespace
that let the server answer read queries (search, config-get, overview, ...)
from HA's live registries in a single round-trip, instead of the multi-fetch
REST/WS pipelines the server uses today.

Because the component ships over HACS on its own release cadence, a given
install may be at any version — old (no WS surface at all), new (full surface),
or somewhere in between. This module negotiates that with a single cached
``ha_mcp_tools/info`` probe per client:

- ``info`` enumerates the ``capabilities`` the running component actually
  registered. The server checks ``component_supports(caps, "<capability>")``
  before routing a tool through the component, so a capability that a released
  component hasn't shipped yet simply never gets used — no version lockstep.
- If ``info`` itself is ``unknown_command``, the component predates the WS
  surface entirely; caps are cached as ``None`` and every consumer falls back
  to its legacy path.

This mirrors the caller-token cache in ``tools_filesystem.py``: weak-keyed by
client so multi-client / OAuth setups each negotiate independently and the
entry self-evicts when a client is garbage-collected.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from dataclasses import dataclass
from typing import Any

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client

logger = logging.getLogger(__name__)

# The WS command every 1.1.0+ component registers; probing it does double duty
# (existence check + capability enumeration — see module docstring).
INFO_COMMAND = "ha_mcp_tools/info"

# HA's structured error code for a command no handler is registered for. Routing
# keys off this (via the ``code`` attribute threaded onto
# ``HomeAssistantCommandError``) rather than matching the message text.
UNKNOWN_COMMAND_CODE = "unknown_command"

# The wire-format generation this server speaks. A probe advertising any other
# ``schema_version`` is treated as no-caps (see ``get_component_caps``): the
# routing gate can't trust command payloads shaped for a different generation.
SUPPORTED_SCHEMA_VERSION = 1

# Negative (None) caps entries expire after this many seconds of monotonic
# time, so a component installed / upgraded mid-session is re-probed and
# adopted instead of being pinned to "absent" for the whole process lifetime
# (the cache key is the process-lifetime REST client — see ``_CAPS_CACHE``).
# Positive entries never expire on this timer; they are dropped only by
# ``invalidate_caps`` (a supposedly-supported command coming back
# ``unknown_command``).
_NEGATIVE_CACHE_TTL_S = 300.0


def _monotonic() -> float:
    """Monotonic clock read, isolated so tests can advance it deterministically."""
    return time.monotonic()


@dataclass(frozen=True)
class ComponentCaps:
    """Snapshot of what the running ``ha_mcp_tools`` component can serve.

    ``capabilities`` is the routing gate — the set of ``ha_mcp_tools/*``
    commands the component actually registered. ``schema_version`` guards the
    wire-format generation of those commands (a consumer needing a reshaped
    payload checks ``schema_version >= N``). ``component_version`` and
    ``limits`` are advisory (display / body-size caps).
    """

    schema_version: int
    component_version: str
    capabilities: frozenset[str]
    limits: dict[str, Any]


# Weak-keyed by client so the negotiated caps self-evict when the client is
# garbage-collected. The key is the process-lifetime REST client, NOT the WS
# connection: an HA restart drops the socket but reuses the same pooled client,
# so a positive entry persists for the whole process (dropped only by
# ``invalidate_caps`` on an ``unknown_command``). A ``None`` value is a cached
# *negative* ("probed, no usable WS surface"); absence means "not yet probed".
# Negatives carry an expiry in ``_NEGATIVE_CACHE_TS`` so a component installed /
# upgraded mid-session is eventually re-probed instead of pinned absent forever.
_CAPS_CACHE: weakref.WeakKeyDictionary[Any, ComponentCaps | None] = (
    weakref.WeakKeyDictionary()
)
# Monotonic timestamp a negative entry was stored, keyed by the same client.
# Only populated for ``None`` values; positive entries never appear here.
_NEGATIVE_CACHE_TS: weakref.WeakKeyDictionary[Any, float] = weakref.WeakKeyDictionary()
_CAPS_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()


def _get_caps_lock(client: Any) -> asyncio.Lock:
    """Per-client lock so concurrent first-callers probe ``info`` exactly once."""
    lock = _CAPS_LOCKS.get(client)
    if lock is None:
        lock = asyncio.Lock()
        _CAPS_LOCKS[client] = lock
    return lock


def _live_cache_entry(client: Any) -> tuple[bool, ComponentCaps | None]:
    """Return ``(hit, caps)`` for a still-valid cache entry, else ``(False, None)``.

    A positive entry is a hit for the process lifetime. A negative (``None``)
    entry is a hit only within ``_NEGATIVE_CACHE_TTL_S`` of when it was stored
    (monotonic clock); once that window lapses it reports a miss so the caller
    re-probes and can adopt a component that appeared mid-session.
    """
    if client not in _CAPS_CACHE:
        return False, None
    cached = _CAPS_CACHE[client]
    if cached is not None:
        return True, cached
    stored_at = _NEGATIVE_CACHE_TS.get(client)
    if stored_at is not None and (_monotonic() - stored_at) < _NEGATIVE_CACHE_TTL_S:
        return True, None
    return False, None


def _store_caps(client: Any, caps: ComponentCaps | None) -> None:
    """Cache ``caps`` for ``client``, stamping a negative with the current clock."""
    _CAPS_CACHE[client] = caps
    if caps is None:
        _NEGATIVE_CACHE_TS[client] = _monotonic()
    else:
        _NEGATIVE_CACHE_TS.pop(client, None)


def _parse_caps(response: Any) -> ComponentCaps | None:
    """Map an ``ha_mcp_tools/info`` response into ``ComponentCaps``.

    Returns ``None`` for a malformed payload — the command responded, so this
    is still a stable negative worth caching, just not a usable capability set.
    """
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        return None
    raw_caps = result.get("capabilities")
    capabilities = (
        frozenset(c for c in raw_caps if isinstance(c, str))
        if isinstance(raw_caps, list)
        else frozenset()
    )
    raw_limits = result.get("limits")
    try:
        schema_version = int(result.get("schema_version", 0) or 0)
    except (TypeError, ValueError):
        schema_version = 0
    return ComponentCaps(
        schema_version=schema_version,
        component_version=str(result.get("component_version", "")),
        capabilities=capabilities,
        limits=raw_limits if isinstance(raw_limits, dict) else {},
    )


async def get_component_caps(client: Any) -> ComponentCaps | None:
    """Return the cached (or freshly probed) capabilities of the component.

    One ``ha_mcp_tools/info`` probe per client, cached. ``None`` means "no
    usable component WS surface" — the caller falls back to its legacy path.

    Cache-on-failure semantics follow the error taxonomy:

    - ``HomeAssistantCommandError`` (``info`` is ``unknown_command`` on an old
      component, or the handler raised): cache ``None`` with an expiry. The
      negative is re-probed after ``_NEGATIVE_CACHE_TTL_S`` so a component
      installed / upgraded mid-session (the REST client — the cache key — is not
      recreated on an HA restart) is eventually adopted instead of pinned absent.
    - ``HomeAssistantConnectionError`` / ``HomeAssistantCommandTimeout`` (WS
      down or slow): do **not** cache — re-probe on the next call once the
      connection recovers. The consuming tool's legacy path will surface the
      transport failure on its own.
    - No credentials on the client (a bare test double with no ``base_url`` /
      ``token``): nothing to probe; return ``None`` without caching.
    - Any other unexpected exception: logged at debug, returns ``None`` without
      caching (a transient runtime fault re-probes on the next call).

    A probe whose ``schema_version`` is not ``SUPPORTED_SCHEMA_VERSION`` is
    treated as no-caps (cached negative, logged once): the server can't trust
    command payloads shaped for a different wire-format generation.
    """
    hit, caps = _live_cache_entry(client)
    if hit:
        return caps

    async with _get_caps_lock(client):
        hit, caps = _live_cache_entry(client)
        if hit:
            return caps

        base_url = getattr(client, "base_url", None)
        token = getattr(client, "token", None)
        if not base_url or not token:
            # Not a credentialed HA client — no WS connection to negotiate over.
            return None

        try:
            ws = await get_websocket_client(url=base_url, token=token)
            response = await ws.send_command(INFO_COMMAND)
        except HomeAssistantCommandError:
            # Old component (unknown_command) or an info handler bug: the
            # component can't serve WS commands. Cache the negative.
            _store_caps(client, None)
            return None
        except (HomeAssistantConnectionError, HomeAssistantCommandTimeout):
            logger.debug(
                "%s probe skipped: WS unavailable", INFO_COMMAND, exc_info=True
            )
            return None
        except Exception:
            logger.debug("%s probe failed unexpectedly", INFO_COMMAND, exc_info=True)
            return None

        caps = _parse_caps(response)
        if caps is not None and caps.schema_version != SUPPORTED_SCHEMA_VERSION:
            logger.warning(
                "ha_mcp_tools component schema_version %s unsupported "
                "(server supports %s); routing no commands through the component",
                caps.schema_version,
                SUPPORTED_SCHEMA_VERSION,
            )
            caps = None
        _store_caps(client, caps)
        return caps


def component_supports(caps: ComponentCaps | None, capability: str) -> bool:
    """Return True when the component advertised ``capability``."""
    return caps is not None and capability in caps.capabilities


def is_unknown_command(exc: Exception) -> bool:
    """Return True when ``exc`` is HA's ``unknown_command`` rejection.

    Keys off the structured ``code`` threaded onto ``HomeAssistantCommandError``
    (never the message text), so a downgraded component that drops a command
    routes cleanly to the legacy fallback.
    """
    return getattr(exc, "code", None) == UNKNOWN_COMMAND_CODE


def invalidate_caps(client: Any) -> None:
    """Drop the cached caps so the next call re-probes ``ha_mcp_tools/info``.

    Called when a command believed to be supported comes back
    ``unknown_command`` (e.g. the component was downgraded mid-session), so the
    stale positive doesn't keep routing to a dead command.
    """
    _CAPS_CACHE.pop(client, None)
    _NEGATIVE_CACHE_TS.pop(client, None)


async def send_component_config_get(
    client: Any, domain: str, item_id: str
) -> dict[str, Any]:
    """Send one ``ha_mcp_tools/config_get`` command over the per-client WebSocket.

    Returns the raw ``{success, result}`` envelope; the caller shapes
    ``result`` onto the legacy response. Raises ``HomeAssistantCommandError``
    on a ``success:False`` reply (routed by the caller's error taxonomy).

    Shared by the automation and script config-get consumers; both route their
    single-item read through this one WS round-trip.
    """
    ws = await get_websocket_client(url=client.base_url, token=client.token)
    return await ws.send_command(
        "ha_mcp_tools/config_get", domain=domain, item_id=item_id
    )
