"""Routing tests for ``ha_get_integration`` over the ``ha_mcp_tools`` component gate.

``ha_get_integration`` resolved a single entry by listing ALL config entries over
REST and then paying an OptionsFlow start/abort round-trip per entry just to read
``options`` (and a separate WS call for subentries). When the component advertises
``config_entries``, one ``ha_mcp_tools/config_entries`` frame returns the entry
identity, its already-materialized ``options`` (raw persisted, secret-scrubbed),
and its ``subentries`` identity rows — no REST list, no OptionsFlow dance, no
subentries WS call. These tests pin that: the component-served single lookup and
list send ZERO ``start_options_flow`` calls (the key no-dance assertion), schema
requests still open the legacy live flow while KEEPING the component's options,
and every backend degradation (no caps, ``unknown_command`` → invalidate + fall
back, a command error) behaves per the routing taxonomy. A connection error is a
deliberate DEVIATION: this tool's legacy path is pure REST, not the shared pooled
WS, so a WS-down error ALSO falls back (returns None → legacy REST get) rather
than propagating. An empty-string ``entry_id`` is a single-entry lookup for a
nonexistent id (not-found, never the first entry).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, tools_integrations
from ha_mcp.tools.tools_integrations import (
    _fetch_entries_via_component,
    register_integration_tools,
)

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

WS_CONFIG_ENTRIES = "ha_mcp_tools/config_entries"

_CAPS_CONFIG_ENTRIES = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["config_entries"],
    "limits": {},
}


def _row(
    entry_id: str = "cfg1",
    domain: str = "mqtt",
    *,
    options: dict[str, Any] | None = None,
    subentries: list[dict[str, Any]] | None = None,
    supports_options: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    """A ``config_entries/get``-shaped row as the component's ``entries`` element."""
    base: dict[str, Any] = {
        "entry_id": entry_id,
        "domain": domain,
        "title": f"Title {entry_id}",
        "state": "loaded",
        "source": "user",
        "supports_options": supports_options,
        "supports_remove_device": False,
        "supports_unload": True,
        "supports_reconfigure": False,
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "disabled_by": None,
        "reason": None,
        "options": {"discovery": True} if options is None else options,
        "subentries": subentries if subentries is not None else [],
    }
    base.update(overrides)
    return base


def _rest_entry(
    entry_id: str = "cfg1", domain: str = "mqtt", **over: Any
) -> dict[str, Any]:
    """A raw REST ``/config/config_entries/entry`` element (legacy fallback shape)."""
    base: dict[str, Any] = {
        "entry_id": entry_id,
        "domain": domain,
        "title": f"Title {entry_id}",
        "state": "loaded",
        "source": "user",
        "supports_options": True,
        "supports_unload": True,
        "disabled_by": None,
    }
    base.update(over)
    return base


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy REST / OptionsFlow fetches."""

    def __init__(
        self,
        *,
        rest_entries: list[dict[str, Any]] | None = None,
        single_entry: dict[str, Any] | None = None,
        options_flow: dict[str, Any] | None = None,
        subentries: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._rest_entries = list(rest_entries or [])
        self._single_entry = single_entry
        self._options_flow = options_flow
        self._subentries = list(subentries or [])
        self.rest_list_calls = 0
        self.get_config_entry_calls = 0
        self.start_options_flow_calls = 0
        self.abort_options_flow_calls = 0
        self.list_config_subentries_calls = 0

    async def _request(self, method: str, path: str) -> Any:
        # Legacy list-all-entries REST fetch.
        self.rest_list_calls += 1
        return list(self._rest_entries)

    async def get_config_entry(self, entry_id: str) -> dict[str, Any]:
        # Legacy single-entry read (lists all + filters client-side in real life).
        self.get_config_entry_calls += 1
        if self._single_entry is None:
            raise HomeAssistantAPIError(
                f"Config entry not found: {entry_id}", status_code=404
            )
        return dict(self._single_entry)

    async def start_options_flow(self, entry_id: str) -> dict[str, Any]:
        self.start_options_flow_calls += 1
        return dict(
            self._options_flow
            or {"type": "form", "flow_id": "f1", "step_id": "init", "data_schema": []}
        )

    async def abort_options_flow(self, flow_id: str) -> dict[str, Any]:
        self.abort_options_flow_calls += 1
        return {"success": True}

    async def list_config_subentries(self, entry_id: str) -> dict[str, Any]:
        self.list_config_subentries_calls += 1
        return {"success": True, "result": list(self._subentries)}

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        # Only logger/log_info is reached (get_logger_levels enrichment); it fails
        # soft, so an empty result yields log_level DEFAULT on both paths.
        if msg.get("type") == "logger/log_info":
            return {"success": True, "result": []}
        raise AssertionError(f"unexpected ws message {msg.get('type')!r}")


def _build_get_integration(client: Any) -> Any:
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
    register_integration_tools(mcp, client)
    return registered["ha_get_integration"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()


def _ce_calls(ws: Any) -> list[Any]:
    return [c for c in ws.send_command.call_args_list if c.args[0] == WS_CONFIG_ENTRIES]


# --- single-entry routing ------------------------------------------------------
@pytest.mark.asyncio
async def test_single_entry_served_by_component_no_options_dance() -> None:
    """A single-entry read is served by one config_entries frame carrying identity
    + options; the REST get and the OptionsFlow start/abort dance never run."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("cfg1", options={"discovery": True})]},
    )
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1")

    assert resp["success"] is True
    assert resp["entry_id"] == "cfg1"
    assert resp["entry"]["domain"] == "mqtt"
    # Options rode the same frame — no per-entry OptionsFlow probe.
    assert resp["entry"]["options"] == {"discovery": True}
    assert client.start_options_flow_calls == 0
    assert client.abort_options_flow_calls == 0
    assert client.get_config_entry_calls == 0
    # ``subentries`` is lifted off ``entry`` so it never leaks unrequested.
    assert "subentries" not in resp["entry"]
    assert "subentries" not in resp
    assert len(_ce_calls(ws)) == 1
    assert _ce_calls(ws)[0].kwargs == {"entry_id": "cfg1"}


@pytest.mark.asyncio
async def test_single_entry_include_subentries_from_component() -> None:
    """include_subentries surfaces the component row's subentries at the top level;
    the legacy config_entries/subentries/list WS call never runs."""
    subs = [
        {
            "subentry_id": "sub1",
            "subentry_type": "device",
            "title": "Sub One",
            "unique_id": "u1",
        }
    ]
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("cfg1", subentries=subs)]},
    )
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1", include_subentries=True)

    assert resp["subentry_count"] == 1
    assert resp["subentries"] == subs
    assert client.list_config_subentries_calls == 0
    assert client.start_options_flow_calls == 0


@pytest.mark.asyncio
async def test_single_entry_include_schema_keeps_component_options() -> None:
    """include_schema opens the legacy live options flow ONLY for the schema; the
    component's raw options are kept (not overwritten by the flow's suggested
    values). The entry itself still comes from the component (no REST get)."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("cfg1", options={"discovery": True})]},
    )
    # The flow would report discovery=False; if options were (wrongly) taken from
    # it, the assertion below would flip.
    client = RoutingClient(
        options_flow={
            "type": "form",
            "flow_id": "f1",
            "step_id": "init",
            "data_schema": [
                {"name": "discovery", "description": {"suggested_value": False}}
            ],
        }
    )
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1", include_schema=True)

    assert resp["entry"]["options"] == {"discovery": True}  # component won
    assert resp["options_schema"]["flow_type"] == "form"
    assert resp["options_schema"]["data_schema"]
    # The schema needed a live flow (start + abort), but the entry did not come
    # from the REST get.
    assert client.start_options_flow_calls == 1
    assert client.abort_options_flow_calls == 1
    assert client.get_config_entry_calls == 0


@pytest.mark.asyncio
async def test_single_entry_not_found_via_component_raises() -> None:
    """An empty component ``entries`` for a given entry_id is authoritative
    not-found — the same error the legacy REST get raises, and the REST get is
    never consulted."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": []},
    )
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations), pytest.raises(ToolError) as excinfo:
        await get_integration(entry_id="ghost")

    assert "ghost" in str(excinfo.value)
    assert client.get_config_entry_calls == 0


@pytest.mark.asyncio
async def test_single_entry_capsless_falls_back_to_legacy() -> None:
    """Old component (info unknown_command) → legacy REST get + OptionsFlow probe."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient(
        single_entry=_rest_entry("cfg1", supports_options=True),
        options_flow={
            "type": "form",
            "flow_id": "f1",
            "step_id": "init",
            "data_schema": [
                {"name": "discovery", "description": {"suggested_value": True}}
            ],
        },
    )
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1")

    assert resp["entry"]["entry_id"] == "cfg1"
    # The legacy path served it: REST get + one OptionsFlow probe.
    assert client.get_config_entry_calls == 1
    assert client.start_options_flow_calls == 1
    assert resp["entry"]["options"] == {"discovery": True}
    assert not _ce_calls(ws)


@pytest.mark.asyncio
async def test_single_entry_unknown_command_falls_back_and_invalidates() -> None:
    """unknown_command on config_entries → invalidate caps + legacy REST get."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(single_entry=_rest_entry("cfg1", supports_options=False))
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1")

    assert resp["entry"]["entry_id"] == "cfg1"
    assert client.get_config_entry_calls == 1
    # Caps dropped from the cache so the next call re-probes.
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_single_entry_command_error_falls_back_keeps_caps() -> None:
    """A non-unknown config_entries error (timeout) falls back to the legacy REST
    get WITHOUT invalidating the still-advertised capability."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient(single_entry=_rest_entry("cfg1", supports_options=False))
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1")

    assert resp["entry"]["entry_id"] == "cfg1"
    assert client.get_config_entry_calls == 1
    # Transient failure is not a downgrade — caps stay cached.
    assert client in component_api._CAPS_CACHE


# --- list routing --------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_served_by_component_with_options() -> None:
    """List with include_options is served by the component rows (options on each
    row); the REST list and per-entry OptionsFlow probes never run."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={
            "entries": [
                _row("c1", domain="mqtt", options={"discovery": True}),
                _row("c2", domain="hue", options={"bridge": "x"}),
            ]
        },
    )
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(include_options=True)

    by_id = {e["entry_id"]: e for e in resp["entries"]}
    assert by_id["c1"]["options"] == {"discovery": True}
    assert by_id["c2"]["options"] == {"bridge": "x"}
    assert client.rest_list_calls == 0
    assert client.start_options_flow_calls == 0
    # No domain filter → the component lists all (no entry_id/domain kwargs).
    assert _ce_calls(ws)[0].kwargs == {}


@pytest.mark.asyncio
async def test_list_without_options_uses_component_and_omits_options() -> None:
    """Without include_options a plain component list read replaces the REST list;
    options are not surfaced (mirroring the legacy summary shape)."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("c1")]},
    )
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration()

    assert [e["entry_id"] for e in resp["entries"]] == ["c1"]
    assert "options" not in resp["entries"][0]
    assert client.rest_list_calls == 0


@pytest.mark.asyncio
async def test_list_domain_filter_normalized_to_component() -> None:
    """A domain filter is lowercased and passed to the component's server-side
    filter (auto-enabling options), and echoed as domain_filter."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("c1", domain="template", options={"x": 1})]},
    )
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(domain="Template")

    assert _ce_calls(ws)[0].kwargs == {"domain": "template"}
    assert resp["domain_filter"] == "template"
    # domain auto-enables options.
    assert resp["entries"][0]["options"] == {"x": 1}


@pytest.mark.asyncio
async def test_list_capsless_falls_back_to_rest() -> None:
    """Old component → legacy REST list."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient(rest_entries=[_rest_entry("c1", supports_options=False)])
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration()

    assert [e["entry_id"] for e in resp["entries"]] == ["c1"]
    assert client.rest_list_calls == 1
    assert not _ce_calls(ws)


# --- fetch-helper seam taxonomy (direct calls) --------------------------------
@pytest.mark.asyncio
async def test_fetch_caps_miss_returns_none_no_frame() -> None:
    """No config_entries capability → None and no command frame is sent."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result={
            "schema_version": 1,
            "component_version": "1.2.0",
            "capabilities": ["search"],
            "limits": {},
        },
    )
    client = RoutingClient()
    with patch_ws(ws, tools_integrations):
        assert await _fetch_entries_via_component(client, entry_id="cfg1") is None
    assert not _ce_calls(ws)


@pytest.mark.asyncio
async def test_fetch_unknown_command_invalidates_caps() -> None:
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient()
    with patch_ws(ws, tools_integrations):
        assert await _fetch_entries_via_component(client) is None
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_fetch_non_unknown_error_keeps_caps() -> None:
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient()
    with patch_ws(ws, tools_integrations):
        assert await _fetch_entries_via_component(client) is None
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_fetch_malformed_shape_falls_back() -> None:
    """An ``entries`` value that is not a list (shape drift) → None (→ legacy)."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": "not-a-list"},
    )
    client = RoutingClient()
    with patch_ws(ws, tools_integrations):
        assert await _fetch_entries_via_component(client) is None


@pytest.mark.asyncio
async def test_fetch_connection_error_falls_back_to_none() -> None:
    """A WS-down ConnectionError on the command returns None → legacy REST.

    DEVIATION from the uniform taxonomy: this tool's legacy path is a pure REST
    read (not the shared pooled WS), so ``_fetch_entries_via_component`` catches
    ``HomeAssistantConnectionError`` and returns ``None`` instead of propagating,
    keeping the (still-advertised) capability cached.
    """
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_exc=HomeAssistantConnectionError("ws down"),
    )
    client = RoutingClient()
    with patch_ws(ws, tools_integrations):
        assert await _fetch_entries_via_component(client) is None
    # A transient connection error is not a downgrade — caps stay cached.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_fetch_ws_establish_failure_falls_back_to_none() -> None:
    """``get_websocket_client()`` raising a plain ``Exception`` returns None → legacy.

    After caps are cached, ``WebSocketManager`` can raise a plain ``Exception`` (not
    ``HomeAssistantConnectionError``) when it cannot (re)establish the pooled socket.
    ``_fetch_entries_via_component`` catches it broadly and returns ``None`` so the
    legacy pure-REST get serves the entry, keeping the (still-advertised) caps.
    """
    caps_ws = make_ws(WS_CONFIG_ENTRIES, info_result=_CAPS_CONFIG_ENTRIES)
    client = RoutingClient()
    with patch_ws_establish_failure(
        caps_ws,
        tools_integrations,
        Exception("Failed to connect to Home Assistant WebSocket"),
    ):
        assert await _fetch_entries_via_component(client) is None
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_single_entry_connection_error_falls_back_to_rest() -> None:
    """End-to-end: a WS-down component read falls back to the legacy REST get.

    The component ``config_entries`` frame raises ``HomeAssistantConnectionError``
    mid-call, but the legacy REST ``get_config_entry`` still serves the entry, so
    the tool returns a result instead of erroring out.
    """
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_exc=HomeAssistantConnectionError("ws down"),
    )
    client = RoutingClient(single_entry=_rest_entry("cfg1", supports_options=False))
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1")

    assert resp["entry"]["entry_id"] == "cfg1"
    # The REST get served the fallback; the component frame was attempted once.
    assert client.get_config_entry_calls == 1
    assert len(_ce_calls(ws)) == 1


@pytest.mark.asyncio
async def test_empty_entry_id_is_not_found_never_first_entry() -> None:
    """An empty-string ``entry_id`` routes as a single-entry (nonexistent) lookup.

    The server forwards ``entry_id=""`` to the component as a single-entry frame
    (kwargs ``{"entry_id": ""}``), and an empty ``entries`` reply is authoritative
    not-found — the tool must raise, never fall through to list mode and return
    the first entry.
    """
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": []},  # component: async_get_entry("") missed
    )
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations), pytest.raises(ToolError):
        await get_integration(entry_id="")

    # Routed as a single-entry lookup for the empty id, not list mode.
    assert len(_ce_calls(ws)) == 1
    assert _ce_calls(ws)[0].kwargs == {"entry_id": ""}
    assert client.get_config_entry_calls == 0
