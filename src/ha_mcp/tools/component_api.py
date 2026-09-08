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

# Additive semantic capability shared by every component path whose result is
# enriched from the device registry.  The pre-2.1.3 ``device_list``/``device_get``
# commands (and search/overview/entity enrichment built on the same registry)
# predate Core 2026.9 child devices.  Because component and server releases update
# independently, command presence alone cannot authorize the newer semantics.
DEVICE_REGISTRY_CHILD_SEMANTICS = "device_registry_child_semantics"

# Negative (None) caps entries expire after this many seconds of monotonic
# time, so a component installed / upgraded mid-session is re-probed and
# adopted instead of being pinned to "absent" for the whole process lifetime
# (the cache key is the process-lifetime REST client — see ``_CAPS_CACHE``).
# Valid positive entries never expire on this timer; they are dropped only by
# ``invalidate_caps`` (a supposedly-supported command coming back
# ``unknown_command``). This ABSENT-negative window covers a definitive
# "component responded, no usable surface" verdict (``unknown_command`` /
# unsupported schema_version). Inconclusive failures use the short window below.
_NEGATIVE_CACHE_TTL_S = 300.0

# A separate, SHORTER window for inconclusive discovery: transport failures,
# info handler errors, unexpected faults, and malformed replies. Caching these
# briefly avoids repeating a broken probe on every tool call (issue #1813 Phase 2,
# review-5 M8), while allowing strict writers to recover promptly after discovery
# works again. These failures do not establish that a capability is absent.
_TRANSIENT_NEGATIVE_CACHE_TTL_S = 30.0


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
    ``limits`` are advisory (display / body-size caps). ``timezone`` is the
    instance's ``hass.config.time_zone`` (an additive ``info`` field — ``None``
    on a component too old to report it, or when unset); it is cached here for
    the consumers that localize timestamps (issue #1813 Phase 2), but nothing
    routes on it — capability negotiation, not a timezone floor.
    """

    schema_version: int
    component_version: str
    capabilities: frozenset[str]
    limits: dict[str, Any]
    timezone: str | None = None
    # Whether the tools entry's filesystem/YAML HA services are registered.
    # Additive ``info`` field (2.1.0, #2292): since the server entry also
    # registers the WS surface (#2289), caps-present no longer implies those
    # services exist; ``None`` means a component too old to report it (where
    # the old implication still holds — only the tools entry registered
    # ``info`` there).
    tools_services: bool | None = None


# Weak-keyed by client so the negotiated caps self-evict when the client is
# garbage-collected. The key is the process-lifetime REST client, NOT the WS
# connection: an HA restart drops the socket but reuses the same pooled client,
# so a valid positive entry persists for the whole process (dropped only by
# ``invalidate_caps`` on an ``unknown_command``). A ``None`` value is a cached
# *negative* ("probed, no usable WS surface"); absence means "not yet probed".
# A negative carries an expiry in EXACTLY ONE of the two timestamp maps below —
# ``_NEGATIVE_CACHE_TS`` (definitive absent, long) or ``_TRANSIENT_NEGATIVE_TS``
# (inconclusive failure, short) — so a component installed/upgraded mid-session, or one
# that just became reachable again, is eventually re-probed instead of pinned.
_CAPS_CACHE: weakref.WeakKeyDictionary[Any, ComponentCaps | None] = (
    weakref.WeakKeyDictionary()
)
# Monotonic timestamp a definitive ABSENT-negative was stored, keyed by the same
# client. Inconclusive failures never use this window.
_NEGATIVE_CACHE_TS: weakref.WeakKeyDictionary[Any, float] = weakref.WeakKeyDictionary()
# Monotonic timestamp an inconclusive probe failure was stored. Also expires
# malformed snapshots retained for permissive readers. Kept
# separate from ``_NEGATIVE_CACHE_TS`` so the two negative kinds carry different
# TTLs; a client is stamped in at most one of the two at a time (the store helpers
# clear the other).
_TRANSIENT_NEGATIVE_TS: weakref.WeakKeyDictionary[Any, float] = (
    weakref.WeakKeyDictionary()
)
# Preserve failed discovery separately so write callers can refuse a downgrade
# even when a best-effort read populated the negative cache first. Store only
# text, not exceptions whose traceback would retain clients through weak keys.
_PROBE_FAILURES: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
_CAPS_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()


class ComponentDiscoveryError(RuntimeError):
    """Discovery failed without establishing which component commands are usable."""


def _checked_caps(
    client: Any, caps: ComponentCaps | None, strict: bool
) -> ComponentCaps | None:
    """Keep read fallbacks while exposing failed discovery to write callers."""
    if strict and (failure := _PROBE_FAILURES.get(client)):
        raise ComponentDiscoveryError(failure)
    return caps


def _get_caps_lock(client: Any) -> asyncio.Lock:
    """Per-client lock so concurrent first-callers probe ``info`` exactly once."""
    lock = _CAPS_LOCKS.get(client)
    if lock is None:
        lock = asyncio.Lock()
        _CAPS_LOCKS[client] = lock
    return lock


def _live_cache_entry(client: Any) -> tuple[bool, ComponentCaps | None]:
    """Return ``(hit, caps)`` for a still-valid cache entry, else ``(False, None)``.

    A valid positive entry is a hit for the process lifetime. Malformed replies
    retained for permissive readers expire like transient failures. A negative (``None``)
    entry is a hit only within its window of when it was stored (monotonic clock):
    ``_NEGATIVE_CACHE_TTL_S`` for a definitive absent-negative, the shorter
    ``_TRANSIENT_NEGATIVE_CACHE_TTL_S`` for an inconclusive failure. Once the
    relevant window lapses it reports a miss so the caller re-probes and can adopt a
    component that appeared — or became reachable — mid-session.
    """
    if client not in _CAPS_CACHE:
        return False, None
    cached = _CAPS_CACHE[client]
    if cached is not None and client not in _PROBE_FAILURES:
        return True, cached
    now = _monotonic()
    stored_at = _NEGATIVE_CACHE_TS.get(client)
    if stored_at is not None and (now - stored_at) < _NEGATIVE_CACHE_TTL_S:
        return True, cached
    transient_at = _TRANSIENT_NEGATIVE_TS.get(client)
    if (
        transient_at is not None
        and (now - transient_at) < _TRANSIENT_NEGATIVE_CACHE_TTL_S
    ):
        return True, cached
    return False, None


def _store_caps(client: Any, caps: ComponentCaps | None) -> None:
    """Cache a positive result or a definitive ABSENT-negative for ``client``.

    A ``None`` here is the long-window absent-negative (``info`` responded
    ``unknown_command`` / unsupported schema). Clears any prior
    transient-negative stamp so a client never carries both negative kinds.
    """
    _PROBE_FAILURES.pop(client, None)
    _CAPS_CACHE[client] = caps
    _TRANSIENT_NEGATIVE_TS.pop(client, None)
    if caps is None:
        _NEGATIVE_CACHE_TS[client] = _monotonic()
    else:
        _NEGATIVE_CACHE_TS.pop(client, None)


def _store_transient_negative(client: Any, caps: ComponentCaps | None = None) -> None:
    """Cache an inconclusive probe for the short recovery window.

    Read callers retain legacy fallback or a permissively parsed snapshot.
    Strict writers reject the accompanying probe failure until the next probe
    succeeds. Preserve parsed capabilities from malformed replies for readers,
    while expiring them on the same short timer as failed probes. Clear any
    prior absent-negative stamp so a client never carries both cache windows.
    """
    _CAPS_CACHE[client] = caps
    _NEGATIVE_CACHE_TS.pop(client, None)
    _TRANSIENT_NEGATIVE_TS[client] = _monotonic()


def _parse_caps(response: Any) -> ComponentCaps | None:
    """Map an ``ha_mcp_tools/info`` response into ``ComponentCaps``.

    Parse permissively for existing read callers. A missing result returns
    ``None``; strict callers separately reject malformed discovery.
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
    raw_timezone = result.get("timezone")
    raw_tools_services = result.get("tools_services")
    return ComponentCaps(
        schema_version=schema_version,
        component_version=str(result.get("component_version", "")),
        capabilities=capabilities,
        limits=raw_limits if isinstance(raw_limits, dict) else {},
        # Additive info field: a component too old to report it (or an unset
        # time_zone) leaves this None; a non-string value is ignored likewise.
        timezone=raw_timezone if isinstance(raw_timezone, str) else None,
        # Additive info field (#2292): non-bool (including absent, on a
        # pre-2.1.0 component) stays None.
        tools_services=raw_tools_services
        if isinstance(raw_tools_services, bool)
        else None,
    )


async def get_component_caps(
    client: Any, *, strict: bool = False
) -> ComponentCaps | None:
    """Return the cached (or freshly probed) capabilities of the component.

    One ``ha_mcp_tools/info`` probe per client, cached. ``None`` means "no
    usable component WS surface" — the caller falls back to its legacy path.

    With ``strict=True``, failed or malformed discovery raises
    ``ComponentDiscoveryError``, including failures cached by earlier read tools.
    A definitive unknown command, unsupported schema, or valid capability list
    without the requested command still permits legacy routing. The default
    retains best-effort read behavior. Inconclusive failures expire after 30
    seconds for both readers and writers.

    Cache-on-failure semantics follow the error taxonomy:

    - ``HomeAssistantCommandError`` with ``unknown_command`` (old or absent
      component): cache ``None`` with an expiry. The negative is re-probed after ``_NEGATIVE_CACHE_TTL_S`` so a component
      installed / upgraded mid-session (the REST client — the cache key — is not
      recreated on an HA restart) is eventually adopted instead of pinned absent.
    - ``HomeAssistantConnectionError`` / ``HomeAssistantCommandTimeout`` (WS
      down or slow, including the failed-connect raise from
      ``WebSocketManager``): cache a SHORT transient
      negative (``_TRANSIENT_NEGATIVE_CACHE_TTL_S``) so repeated calls on a
      WS-broken install skip the slow connect and go straight to legacy, then
      re-probe once the window lapses (self-healing). The consuming tool's legacy
      path serves read requests meanwhile; strict writers refuse the edit.
    - No credentials on the client (a bare test double with no ``base_url`` /
      ``token``): nothing to probe; return ``None`` without caching.
    - Other command errors, malformed replies, or unexpected exceptions: cache
      for the same short transient window. None establishes capability absence;
      strict writers refuse the edit until a subsequent probe succeeds.

    A probe whose ``schema_version`` is not ``SUPPORTED_SCHEMA_VERSION`` is
    treated as no-caps (cached negative, logged once): the server can't trust
    command payloads shaped for a different wire-format generation.
    """
    hit, caps = _live_cache_entry(client)
    if hit:
        return _checked_caps(client, caps, strict)

    async with _get_caps_lock(client):
        hit, caps = _live_cache_entry(client)
        if hit:
            return _checked_caps(client, caps, strict)

        base_url = getattr(client, "base_url", None)
        token = getattr(client, "token", None)
        if not base_url or not token:
            # Not a credentialed HA client — no WS connection to negotiate over.
            return None
        # Thread verify_ssl so a verify_ssl=False client keys (and can establish)
        # its own pooled connection rather than failing the probe or colliding with
        # a default-verification entry — otherwise EVERY component capability is
        # silently unreachable on an HTTPS + verify_ssl=False install. ``None`` is
        # the pool's settings default, so a client without the attr is unchanged.
        verify_ssl = getattr(client, "verify_ssl", None)

        try:
            ws = await get_websocket_client(
                url=base_url, token=token, verify_ssl=verify_ssl
            )
            response = await ws.send_command(INFO_COMMAND)
        except HomeAssistantCommandError as exc:
            if is_unknown_command(exc):
                _store_caps(client, None)
            else:
                _store_transient_negative(client)
                _PROBE_FAILURES[client] = f"{INFO_COMMAND} failed: {exc}"
            return _checked_caps(client, None, strict)
        except (HomeAssistantConnectionError, HomeAssistantCommandTimeout):
            logger.debug(
                "%s probe skipped: WS unavailable", INFO_COMMAND, exc_info=True
            )
            _store_transient_negative(client)
            _PROBE_FAILURES[client] = (
                f"{INFO_COMMAND} probe could not reach Home Assistant"
            )
            return _checked_caps(client, None, strict)
        except Exception:
            # Read callers retain best-effort fallback; strict write callers
            # must not confuse an unexpected fault with capability absence.
            logger.debug("%s probe failed unexpectedly", INFO_COMMAND, exc_info=True)
            _store_transient_negative(client)
            _PROBE_FAILURES[client] = f"{INFO_COMMAND} probe failed unexpectedly"
            return _checked_caps(client, None, strict)

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
        result = response.get("result") if isinstance(response, dict) else None
        if (
            not isinstance(response, dict)
            or response.get("success") is not True
            or not isinstance(result, dict)
            or type(result.get("schema_version")) is not int
            or not isinstance(result.get("capabilities"), list)
            or not all(isinstance(cap, str) for cap in result["capabilities"])
        ):
            # Keep permissive parsing for existing read callers, but expire
            # malformed positive snapshots too so strict callers can recover.
            _store_transient_negative(client, caps)
            _PROBE_FAILURES[client] = f"Malformed {INFO_COMMAND} response"
        return _checked_caps(client, caps, strict)


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
    _PROBE_FAILURES.pop(client, None)
    _CAPS_CACHE.pop(client, None)
    _NEGATIVE_CACHE_TS.pop(client, None)
    _TRANSIENT_NEGATIVE_TS.pop(client, None)
