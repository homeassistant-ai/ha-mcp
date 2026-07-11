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


# Weak-keyed by client so the negotiated caps live for the client's lifetime
# (the component version only changes on a HACS update + HA restart, which drops
# the WS connection and yields a fresh pooled client → fresh probe) and
# self-evict when the client is collected. ``None`` is a cached *negative*
# ("probed, no usable WS surface"); absence means "not yet probed".
_CAPS_CACHE: weakref.WeakKeyDictionary[Any, ComponentCaps | None] = (
    weakref.WeakKeyDictionary()
)
_CAPS_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()


def _get_caps_lock(client: Any) -> asyncio.Lock:
    """Per-client lock so concurrent first-callers probe ``info`` exactly once."""
    lock = _CAPS_LOCKS.get(client)
    if lock is None:
        lock = asyncio.Lock()
        _CAPS_LOCKS[client] = lock
    return lock


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

    One ``ha_mcp_tools/info`` probe per client lifetime. ``None`` means "no
    usable component WS surface" — the caller falls back to its legacy path.

    Cache-on-failure semantics follow the error taxonomy:

    - ``HomeAssistantCommandError`` (``info`` is ``unknown_command`` on an old
      component, or the handler raised): cache ``None``. The negative is stable
      until the connection is replaced, so we probe exactly once.
    - ``HomeAssistantConnectionError`` / ``HomeAssistantCommandTimeout`` (WS
      down or slow): do **not** cache — re-probe on the next call once the
      connection recovers. The consuming tool's legacy path will surface the
      transport failure on its own.
    - No credentials on the client (a bare test double with no ``base_url`` /
      ``token``): nothing to probe; return ``None`` without caching.
    """
    if client in _CAPS_CACHE:
        return _CAPS_CACHE[client]

    async with _get_caps_lock(client):
        if client in _CAPS_CACHE:
            return _CAPS_CACHE[client]

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
            _CAPS_CACHE[client] = None
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
        _CAPS_CACHE[client] = caps
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
