"""The helper types Home Assistant creates through a config flow, read from Core.

``flow_handlers?type=helper`` lists Core's own helper flows plus custom
integrations of ``integration_type: "helper"`` — the list the HA UI offers under
"Create helper". Cached per client for a few minutes so a tool call does not pay
a REST round-trip each time; a type missing from a cached copy is re-read once
before it is refused (``listed_helper_flows``), so a newly installed helper
integration is found.
"""

from __future__ import annotations

import time
import weakref
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from .config_helpers.schemas import SIMPLE_HELPER_TYPES
from .helpers import exception_to_structured_error, raise_tool_error

_TTL_SECONDS = 300.0
_CACHE: weakref.WeakKeyDictionary[Any, tuple[float, frozenset[str]]] = (
    weakref.WeakKeyDictionary()
)


async def _fetch_helper_flow_types(client: Any) -> frozenset[str]:
    domains = await client._request(
        "GET", "/config/config_entries/flow_handlers", params={"type": "helper"}
    )
    # _request answers an unparseable body with {}; an empty set would refuse
    # every flow helper as "not a helper".
    if not isinstance(domains, list):
        raise HomeAssistantConnectionError(
            "flow_handlers returned an unexpected response shape: "
            f"{type(domains).__name__}"
        )
    return frozenset(str(domain) for domain in domains)


def _cached(client: Any) -> frozenset[str] | None:
    try:
        cached = _CACHE.get(client)
    except TypeError:
        return None
    if cached is not None and time.monotonic() - cached[0] < _TTL_SECONDS:
        return cached[1]
    return None


async def helper_flow_types(client: Any, *, refresh: bool = False) -> frozenset[str]:
    """The helper types the connected Home Assistant offers as config flows."""
    cached = None if refresh else _cached(client)
    if cached is not None:
        return cached
    types = await _fetch_helper_flow_types(client)
    try:
        _CACHE[client] = (time.monotonic(), types)
    except TypeError:  # a client that cannot be weakly referenced is not cached
        pass
    return types


async def listed_helper_flows(client: Any, name: str) -> frozenset[str]:
    """The helper flow list, re-read once when ``name`` is missing from a cached
    copy (a fresh read is not repeated)."""
    cached = _cached(client)
    if cached is not None and name in cached:
        return cached
    return await helper_flow_types(client, refresh=cached is not None)


async def require_helper_type(client: Any, helper_type: str, *also: str) -> None:
    """Refuse a helper_type that is neither a storage helper, one of ``also``,
    nor a helper flow this Home Assistant lists. A failed read of that list is
    raised as a structured tool error, never as the raw exception."""
    if helper_type in SIMPLE_HELPER_TYPES or helper_type in also:
        return
    try:
        flow_types = await listed_helper_flows(client, helper_type)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        exception_to_structured_error(
            e,
            context={
                "helper_type": helper_type,
                "operation": "read Home Assistant's helper flow list",
            },
        )
    if helper_type not in flow_types:
        _raise_unknown_helper_type(helper_type, flow_types)


def _raise_unknown_helper_type(
    helper_type: str, flow_types: frozenset[str]
) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Unknown helper_type {helper_type!r}: it is neither a storage helper "
            "nor one of the helper flows this Home Assistant lists.",
            context={"helper_type": helper_type},
            suggestions=[
                "Storage helpers: " + ", ".join(sorted(SIMPLE_HELPER_TYPES)),
                "Helper flows on this instance: " + ", ".join(sorted(flow_types)),
            ],
        )
    )
