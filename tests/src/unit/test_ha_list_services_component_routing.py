"""Routing tests for ``ha_list_services`` over the ``ha_mcp_tools`` gate.

The legacy path pays for a REST ``get_services()`` catalog fetch plus a WS
``frontend/get_translations`` round-trip on every call, regardless of the
``domain``/``query`` filters. When the component advertises ``services_list``
one ``ha_mcp_tools/services_list`` frame returns both (coarse-filtered) in a
single round-trip; the server still runs its own ``_process_services`` filter
+ pagination unchanged over that payload. These tests pin: the
component-preferred path (both legacy calls skipped), capability miss falls
back, ``unknown_command`` invalidates caps and falls back, a command
error/timeout falls back, and a connection error ALSO falls back to the REST
legacy — a deliberate deviation from the uniform taxonomy, because this tool's
legacy path is REST + a per-request WS bridge, NOT the shared pooled WS, so a WS
outage must not kill the tool.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, tools_services
from ha_mcp.tools.tools_services import register_services_tools

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

_CAPS_SERVICES_LIST = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["services_list"],
    "limits": {},
}
_CAPS_NONE = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": [],
    "limits": {},
}

_COMPONENT_SERVICES = [
    {
        "domain": "light",
        "services": {
            "turn_on": {"description": "Turn a light on"},
            "turn_off": {"description": "Turn a light off"},
        },
    },
]
_COMPONENT_TRANSLATIONS = {
    "component.light.services.turn_on.name": "Turn on",
}

_LEGACY_REST_SERVICES = [
    {
        "domain": "light",
        "services": {
            "turn_on": {"description": "Turn a light on"},
            "turn_off": {"description": "Turn a light off"},
        },
    },
]


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy REST + WS translations fetch."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.legacy_rest_calls = 0
        self.legacy_ws_calls = 0

    async def get_services(self) -> list[dict[str, Any]]:
        self.legacy_rest_calls += 1
        return [dict(e) for e in _LEGACY_REST_SERVICES]

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "frontend/get_translations":
            self.legacy_ws_calls += 1
            return {"success": True, "result": {"resources": {}}}
        raise AssertionError(f"unexpected ws message {msg.get('type')!r}")


def _build_list_services(client: Any) -> Any:
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp = MagicMock()
    mcp.add_tool = capture_add_tool
    register_services_tools(mcp, client)
    return registered["ha_list_services"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _services_list_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/services_list"
    ]


@pytest.mark.asyncio
async def test_component_preferred_skips_legacy_fetches() -> None:
    """Capability present → one component frame, both legacy calls skipped."""
    ws = make_ws(
        "ha_mcp_tools/services_list",
        info_result=_CAPS_SERVICES_LIST,
        cmd_result={
            "services": _COMPONENT_SERVICES,
            "translations": _COMPONENT_TRANSLATIONS,
        },
    )
    client = RoutingClient()
    list_services = _build_list_services(client)

    with patch_ws(ws, tools_services):
        resp = await list_services(domain="light")

    assert resp["success"] is True
    assert "light.turn_on" in resp["services"]
    # ``_build_service_entry``'s translation lookup keys by the bare
    # ``component.<domain>.services.<service>`` prefix, which never matches the
    # flat ``...name``/``...description``-suffixed keys this fixture (and real
    # HA translations) actually use — so the name always falls back to the
    # title-cased service name, identically on both paths.
    assert resp["services"]["light.turn_on"]["name"] == "Turn On"
    assert client.legacy_rest_calls == 0
    assert client.legacy_ws_calls == 0
    assert len(_services_list_calls(ws)) == 1
    call = _services_list_calls(ws)[0]
    assert call.kwargs["domain"] == "light"
    assert call.kwargs["language"] == "en"
    assert "query" not in call.kwargs


@pytest.mark.asyncio
async def test_query_is_never_forwarded_but_still_filters() -> None:
    """``query`` is filtered server-side, never sent to the component's frame.

    Forwarding ``query`` to the component would let its coarse per-domain pass
    drop a whole domain from the payload when nothing under it matches — but
    ``_process_services``'s ``domains`` field is populated purely from
    ``domain_filter``, independent of ``query``, so a query-trimmed component
    payload would silently omit domains legacy still lists. Only ``domain`` is
    threaded; ``_process_services`` still applies the exact ``query`` filter
    itself over the (domain-scoped, not query-scoped) component payload.
    """
    ws = make_ws(
        "ha_mcp_tools/services_list",
        info_result=_CAPS_SERVICES_LIST,
        cmd_result={
            "services": _COMPONENT_SERVICES,
            "translations": _COMPONENT_TRANSLATIONS,
        },
    )
    client = RoutingClient()
    list_services = _build_list_services(client)

    with patch_ws(ws, tools_services):
        resp = await list_services(query="turn_off")

    # The exact server-side filter still narrows to the one matching service.
    assert set(resp["services"]) == {"light.turn_off"}
    assert client.legacy_rest_calls == 0
    assert client.legacy_ws_calls == 0
    call = _services_list_calls(ws)[0]
    assert "query" not in call.kwargs


@pytest.mark.asyncio
async def test_no_capability_uses_legacy_fetches() -> None:
    """Component without ``services_list`` → legacy REST + WS translations."""
    ws = make_ws("ha_mcp_tools/services_list", info_result=_CAPS_NONE)
    client = RoutingClient()
    list_services = _build_list_services(client)

    with patch_ws(ws, tools_services):
        resp = await list_services()

    assert resp["success"] is True
    assert "light.turn_on" in resp["services"]
    assert client.legacy_rest_calls == 1
    assert client.legacy_ws_calls == 1
    assert not _services_list_calls(ws)


@pytest.mark.asyncio
async def test_unknown_command_invalidates_and_falls_back() -> None:
    """``unknown_command`` on the services_list frame → invalidate caps + legacy."""
    ws = make_ws(
        "ha_mcp_tools/services_list",
        info_result=_CAPS_SERVICES_LIST,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient()
    list_services = _build_list_services(client)

    with patch_ws(ws, tools_services):
        resp = await list_services()

    assert resp["success"] is True
    assert client.legacy_rest_calls == 1
    assert client.legacy_ws_calls == 1
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_command_error_falls_back_to_legacy() -> None:
    """A non-unknown command error/timeout → legacy fetches, caps stay cached."""
    ws = make_ws(
        "ha_mcp_tools/services_list",
        info_result=_CAPS_SERVICES_LIST,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient()
    list_services = _build_list_services(client)

    with patch_ws(ws, tools_services):
        resp = await list_services()

    assert resp["success"] is True
    assert client.legacy_rest_calls == 1
    assert client.legacy_ws_calls == 1
    assert client in component_api._CAPS_CACHE


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param("not-a-dict", id="non_dict_result"),
        pytest.param({"services": "nope", "translations": {}}, id="services_not_list"),
        pytest.param(
            {"services": [], "translations": "nope"}, id="translations_not_dict"
        ),
    ],
)
@pytest.mark.asyncio
async def test_malformed_component_payload_falls_back_to_legacy(
    malformed: Any,
) -> None:
    """A shape-drift component reply (non-dict result, ``services`` not a list, or
    ``translations`` not a dict) routes to the legacy REST + WS fetches
    (tools_services.py:314-320); caps stay cached (drift is not unknown_command)."""
    ws = make_ws(
        "ha_mcp_tools/services_list",
        info_result=_CAPS_SERVICES_LIST,
        cmd_result=malformed,
    )
    client = RoutingClient()
    list_services = _build_list_services(client)

    with patch_ws(ws, tools_services):
        resp = await list_services()

    assert resp["success"] is True
    assert "light.turn_on" in resp["services"]
    assert client.legacy_rest_calls == 1
    assert client.legacy_ws_calls == 1
    # The component WAS asked (one frame); its malformed reply fell back.
    assert len(_services_list_calls(ws)) == 1
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_connection_error_falls_back_to_legacy() -> None:
    """A WS-down error on the component frame falls back to the REST legacy.

    DEVIATION from the uniform taxonomy: this tool's legacy path is the REST
    ``get_services()`` catalog + a per-request WS bridge for translations, NOT the
    shared pooled WS, so ``_fetch_services_list_via_component`` catches
    ``HomeAssistantConnectionError`` and returns ``None`` — the REST fallback then
    serves the catalog rather than the tool erroring out.
    """
    ws = make_ws(
        "ha_mcp_tools/services_list",
        info_result=_CAPS_SERVICES_LIST,
        cmd_exc=HomeAssistantConnectionError("down"),
    )
    client = RoutingClient()
    list_services = _build_list_services(client)

    with patch_ws(ws, tools_services):
        resp = await list_services()

    assert resp["success"] is True
    assert "light.turn_on" in resp["services"]
    # The REST catalog + WS translations legacy fetch served the result.
    assert client.legacy_rest_calls == 1
    assert client.legacy_ws_calls == 1
    # A transient connection error keeps the (positive) caps entry cached.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_ws_establish_failure_falls_back_to_legacy() -> None:
    """``get_websocket_client()`` raising a plain ``Exception`` falls back to REST.

    After caps are cached, ``WebSocketManager`` can raise a plain ``Exception`` (not
    ``HomeAssistantConnectionError``) when it cannot (re)establish the pooled socket
    for the read command. ``_fetch_services_list_via_component`` catches it broadly
    and returns ``None`` so the REST catalog + WS translations legacy fetch serves
    the result rather than the tool erroring out.
    """
    caps_ws = make_ws("ha_mcp_tools/services_list", info_result=_CAPS_SERVICES_LIST)
    client = RoutingClient()
    list_services = _build_list_services(client)

    with patch_ws_establish_failure(
        caps_ws,
        tools_services,
        Exception("Failed to connect to Home Assistant WebSocket"),
    ):
        resp = await list_services()

    assert resp["success"] is True
    assert "light.turn_on" in resp["services"]
    assert client.legacy_rest_calls == 1
    assert client.legacy_ws_calls == 1
    # A transient establish failure is not a downgrade — caps stay cached.
    assert client in component_api._CAPS_CACHE
