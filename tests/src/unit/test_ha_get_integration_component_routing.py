"""Routing tests for ``ha_get_integration`` over the ``ha_mcp_tools`` component gate.

When the component advertises ``config_entries``, one frame serves entry identity,
options and subentries: no REST list, no OptionsFlow dance. Every degradation
falls back to the legacy REST read; a WS-down ConnectionError falls back too
(the legacy read is pure REST) instead of propagating.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.redaction import REDACTED_SET
from ha_mcp.tools import component_api, component_config_entries
from ha_mcp.tools.component_config_entries import fetch_config_entries
from ha_mcp.tools.tools_integrations import register_integration_tools

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

WS_CONFIG_ENTRIES = "ha_mcp_tools/config_entries"

_CAPSLESS = HomeAssistantCommandError("no info", "unknown_command")
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
        integration_types: dict[str, str] | None = None,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._rest_entries = list(rest_entries or [])
        self._single_entry = single_entry
        self._options_flow = options_flow
        self._subentries = list(subentries or [])
        # domain -> the manifest's integration_type, for Core's ``?type=`` filter.
        self._integration_types = dict(integration_types or {})
        self.rest_list_calls = 0
        self.rest_list_params: dict[str, Any] | None = None
        self.get_config_entry_calls = 0
        self.start_options_flow_calls = 0
        self.abort_options_flow_calls = 0
        self.list_config_subentries_calls = 0

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        # Entry-list REST fetch; Core filters ``?domain=`` and ``?type=`` itself.
        # Like Core, ``?type=`` keeps an entry whose integration it cannot
        # find, except for ``helper``.
        self.rest_list_calls += 1
        self.rest_list_params = kwargs.get("params")
        params = self.rest_list_params or {}
        domain = params.get("domain")
        return [
            e
            for e in self._rest_entries
            if (not domain or e["domain"] == domain)
            and (
                "type" not in params
                or self._integration_types.get(e["domain"]) == params["type"]
                or (
                    e["domain"] not in self._integration_types
                    and params["type"] != "helper"
                )
            )
        ]

    async def get_config_entry(self, entry_id: str) -> dict[str, Any]:
        # Legacy single-entry read (config_entries/get_single in real life).
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

    async def submit_options_flow_step(
        self, flow_id: str, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        raise AssertionError("the schema read never submits a form")

    async def list_config_subentries(self, entry_id: str) -> dict[str, Any]:
        self.list_config_subentries_calls += 1
        return {"success": True, "result": list(self._subentries)}

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        # logger/log_info (get_logger_levels enrichment) fails soft, so an empty
        # result yields log_level DEFAULT on both paths; a schema read asks for
        # Core's flow strings, and an empty catalog describes the form without
        # help text.
        if msg.get("type") == "logger/log_info":
            return {"success": True, "result": []}
        if msg.get("type") == "frontend/get_translations":
            return {"success": True, "result": {"resources": {}}}
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


async def _get(ws: Any, client: Any, **call: Any) -> Any:
    with patch_ws(ws, component_config_entries):
        return await _build_get_integration(client)(**call)


def _ce_calls(ws: Any) -> list[Any]:
    return [c for c in ws.send_command.call_args_list if c.args[0] == WS_CONFIG_ENTRIES]


# --- single-entry routing ------------------------------------------------------
@pytest.mark.asyncio
async def test_single_entry_served_by_component_no_options_dance() -> None:
    """One config_entries frame serves the entry; no REST get, no OptionsFlow."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("cfg1", options={"discovery": True})]},
    )
    client = RoutingClient()
    resp = await _get(ws, client, entry_id="cfg1")

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
    """include_subentries comes from the component row, not the legacy WS call."""
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
    resp = await _get(ws, client, entry_id="cfg1", include_subentries=True)

    assert resp["subentry_count"] == 1
    assert resp["subentries"] == subs
    assert client.list_config_subentries_calls == 0
    assert client.start_options_flow_calls == 0


@pytest.mark.asyncio
async def test_single_entry_include_schema_keeps_component_options() -> None:
    """include_schema opens a live flow only for the schema; component options win."""
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
    resp = await _get(ws, client, entry_id="cfg1", include_schema=True)

    assert resp["entry"]["options"] == {"discovery": True}  # component won
    assert resp["options_schema"]["flow_type"] == "form"
    assert resp["options_schema"]["fields"] == [{"name": "discovery", "current": False}]
    # The schema needed a live flow (start + abort), but the entry did not come
    # from the REST get.
    assert client.start_options_flow_calls == 1
    assert client.abort_options_flow_calls == 1
    assert client.get_config_entry_calls == 0


@pytest.mark.asyncio
async def test_single_entry_not_found_via_component_raises() -> None:
    """An empty component ``entries`` is authoritative not-found; no REST get."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": []},
    )
    client = RoutingClient()
    with pytest.raises(ToolError) as excinfo:
        await _get(ws, client, entry_id="ghost")

    assert "ghost" in str(excinfo.value)
    assert client.get_config_entry_calls == 0


@pytest.mark.asyncio
async def test_single_entry_capsless_falls_back_to_legacy() -> None:
    """Old component -> legacy REST get; include_schema reads options from the form."""
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
    resp = await _get(ws, client, entry_id="cfg1", include_schema=True)

    assert resp["entry"]["entry_id"] == "cfg1"
    assert client.get_config_entry_calls == 1
    assert resp["entry"]["options"] == {"discovery": True}
    assert "warnings" not in resp
    assert not _ce_calls(ws)


# unknown_command invalidates the caps cache; transient failures keep it.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "caps_cached"),
    [
        (HomeAssistantCommandError("gone", "unknown_command"), False),
        (HomeAssistantCommandTimeout("timeout"), True),
        (HomeAssistantConnectionError("ws down"), True),
    ],
    ids=["unknown_command_invalidates", "timeout_keeps_caps", "connection_error"],
)
async def test_single_entry_command_error_falls_back(
    exc: Exception, caps_cached: bool
) -> None:
    """A config_entries error falls back to the legacy REST get."""
    ws = make_ws(WS_CONFIG_ENTRIES, info_result=_CAPS_CONFIG_ENTRIES, cmd_exc=exc)
    client = RoutingClient(single_entry=_rest_entry("cfg1", supports_options=False))
    resp = await _get(ws, client, entry_id="cfg1")

    assert resp["entry"]["entry_id"] == "cfg1"
    assert client.get_config_entry_calls == 1
    assert len(_ce_calls(ws)) == 1
    assert (client in component_api._CAPS_CACHE) is caps_cached


# --- list routing --------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_served_by_component_with_options() -> None:
    """include_options list is served by component rows; no REST list or probes."""
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
    resp = await _get(ws, client, include_options=True)

    by_id = {e["entry_id"]: e for e in resp["entries"]}
    assert by_id["c1"]["options"] == {"discovery": True}
    assert by_id["c2"]["options"] == {"bridge": "x"}
    assert client.rest_list_calls == 0
    assert client.start_options_flow_calls == 0
    # No domain filter → the component lists all (no entry_id/domain kwargs).
    assert _ce_calls(ws)[0].kwargs == {}


@pytest.mark.asyncio
async def test_list_without_options_uses_component_and_omits_options() -> None:
    """Without include_options the component replaces the REST list; no options."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={
            "entries": [
                _row("c1", subentries=[{"subentry_id": "s1", "subentry_type": "x"}])
            ]
        },
    )
    client = RoutingClient()
    resp = await _get(ws, client)

    assert [e["entry_id"] for e in resp["entries"]] == ["c1"]
    assert "options" not in resp["entries"][0]
    assert "subentries" not in resp["entries"][0]
    assert client.rest_list_calls == 0


@pytest.mark.asyncio
async def test_list_domain_filter_normalized_to_component() -> None:
    """A domain filter is lowercased, sent to the component, enables options."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("c1", domain="template", options={"x": 1})]},
    )
    client = RoutingClient()
    resp = await _get(ws, client, domain="Template")

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
    resp = await _get(ws, client)

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
    with patch_ws(ws, component_config_entries):
        assert await fetch_config_entries(client, entry_id="cfg1") is None
    assert not _ce_calls(ws)


# unknown_command invalidates the caps cache; transient failures keep it. A
# ConnectionError returns None too: the legacy path is pure REST.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "caps_cached"),
    [
        (HomeAssistantCommandError("gone", "unknown_command"), False),
        (HomeAssistantCommandTimeout("timeout"), True),
        (HomeAssistantConnectionError("ws down"), True),
    ],
    ids=["unknown_command_invalidates", "timeout_keeps_caps", "connection_error"],
)
async def test_fetch_command_error_returns_none(
    exc: Exception, caps_cached: bool
) -> None:
    ws = make_ws(WS_CONFIG_ENTRIES, info_result=_CAPS_CONFIG_ENTRIES, cmd_exc=exc)
    client = RoutingClient()
    with patch_ws(ws, component_config_entries):
        assert await fetch_config_entries(client) is None
    assert (client in component_api._CAPS_CACHE) is caps_cached


@pytest.mark.asyncio
async def test_fetch_malformed_shape_falls_back() -> None:
    """A non-list ``entries`` (shape drift) -> None (legacy)."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": "not-a-list"},
    )
    client = RoutingClient()
    with patch_ws(ws, component_config_entries):
        assert await fetch_config_entries(client) is None


@pytest.mark.asyncio
async def test_fetch_ws_establish_failure_falls_back_to_none() -> None:
    """A failed pooled-WS establish returns None (legacy) and keeps the caps."""
    caps_ws = make_ws(WS_CONFIG_ENTRIES, info_result=_CAPS_CONFIG_ENTRIES)
    client = RoutingClient()
    with patch_ws_establish_failure(
        caps_ws,
        component_config_entries,
        HomeAssistantConnectionError("Failed to connect to Home Assistant WebSocket"),
    ):
        assert await fetch_config_entries(client) is None
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_empty_entry_id_is_not_found_never_first_entry() -> None:
    """An empty ``entry_id`` is a single-entry lookup that misses, never list mode."""
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": []},  # component: async_get_entry("") missed
    )
    client = RoutingClient()
    with pytest.raises(ToolError):
        await _get(ws, client, entry_id="")

    # Routed as a single-entry lookup for the empty id, not list mode.
    assert len(_ce_calls(ws)) == 1
    assert _ce_calls(ws)[0].kwargs == {"entry_id": ""}
    assert client.get_config_entry_calls == 0


# --- ignored entries (#2927) ----------------------------------------------------
# Core never sets up an ignored entry (``source == "ignore"``, created with empty
# ``data``), so its options flow has nothing to read and several integrations'
# flows raise. Core's ``supports_options`` asks only the domain's flow handler,
# never ``source``, so the tool must key on ``source``.
_IGNORED = {"source": "ignore", "state": "not_loaded", "supports_options": True}


@pytest.mark.asyncio
async def test_ignored_entry_legacy_single_is_not_probed() -> None:
    ws = make_ws(WS_CONFIG_ENTRIES, info_exc=_CAPSLESS)
    client = RoutingClient(single_entry=_rest_entry("ign1", "unifi", **_IGNORED))
    resp = await _get(ws, client, entry_id="ign1")

    assert client.start_options_flow_calls == 0
    assert resp["entry"]["supports_options"] is False
    assert resp["entry"]["options"] == {}
    assert "warnings" not in resp


@pytest.mark.asyncio
async def test_not_loaded_entry_that_is_not_ignored_keeps_its_schema_read() -> None:
    # Control for the ignored-entry tests: the gate keys on ``source``, not on
    # the entry state, so a not-loaded user entry's options form is still read.
    ws = make_ws(WS_CONFIG_ENTRIES, info_exc=_CAPSLESS)
    entry = _rest_entry("nl1", "unifi", state="not_loaded")
    client = RoutingClient(single_entry=entry)
    resp = await _get(ws, client, entry_id="nl1", include_schema=True)

    assert client.start_options_flow_calls == 1
    assert resp["entry"]["supports_options"] is True


@pytest.mark.asyncio
async def test_ignored_entry_legacy_single_schema_opens_no_flow() -> None:
    ws = make_ws(WS_CONFIG_ENTRIES, info_exc=_CAPSLESS)
    client = RoutingClient(single_entry=_rest_entry("ign1", "zha", **_IGNORED))
    resp = await _get(ws, client, entry_id="ign1", include_schema=True)

    assert client.start_options_flow_calls == 0
    assert "options_schema" not in resp
    assert "warnings" not in resp


@pytest.mark.asyncio
async def test_ignored_entries_legacy_list_are_not_probed() -> None:
    ws = make_ws(WS_CONFIG_ENTRIES, info_exc=_CAPSLESS)
    client = RoutingClient(
        rest_entries=[
            _rest_entry("ign1", "homeassistant_sky_connect", **_IGNORED),
            _rest_entry("ign2", "homeassistant_sky_connect", **_IGNORED),
        ]
    )
    resp = await _get(ws, client, include_options=True)

    assert client.start_options_flow_calls == 0
    assert [e["supports_options"] for e in resp["entries"]] == [False, False]
    assert [e["options"] for e in resp["entries"]] == [{}, {}]
    assert "warnings" not in resp


@pytest.mark.asyncio
async def test_ignored_entry_component_single_schema_opens_no_flow() -> None:
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("ign1", "unifi", options={}, **_IGNORED)]},
    )
    client = RoutingClient()
    resp = await _get(ws, client, entry_id="ign1", include_schema=True)

    assert client.start_options_flow_calls == 0
    assert resp["entry"]["supports_options"] is False
    assert "options_schema" not in resp
    assert "warnings" not in resp


# --- a read opens no options flow (#2987) ---------------------------------------
# An options flow's first step is the integration's own code and may act: Sonoff
# LAN logs in to its cloud there, which ends the session the integration runs on.
# Only include_schema=True, which asks for the form, opens one.
@pytest.mark.asyncio
async def test_reading_an_entry_without_the_component_opens_no_options_flow() -> None:
    ws = make_ws(WS_CONFIG_ENTRIES, info_exc=_CAPSLESS)
    client = RoutingClient(single_entry=_rest_entry("cfg1", "sonoff"))
    resp = await _get(ws, client, entry_id="cfg1")

    assert client.start_options_flow_calls == 0
    assert "options" not in resp["entry"]
    assert "ha_mcp_tools" in resp["warnings"][0]


@pytest.mark.asyncio
async def test_listing_options_without_the_component_opens_no_options_flow() -> None:
    ws = make_ws(WS_CONFIG_ENTRIES, info_exc=_CAPSLESS)
    client = RoutingClient(
        rest_entries=[_rest_entry("c1", "sonoff"), _rest_entry("c2", "sonoff")]
    )
    resp = await _get(ws, client, domain="sonoff")

    assert client.start_options_flow_calls == 0
    assert all("options" not in entry for entry in resp["entries"])
    assert len(resp["warnings"]) == 1
    assert "ha_mcp_tools" in resp["warnings"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call", [{"entry_id": "cfg1"}, {"include_options": True}], ids=["single", "list"]
)
async def test_redact_secrets_withholds_options_without_opening_the_options_flow(
    call: dict[str, Any], redact_on: None
) -> None:
    options = {"api_key": "sk-LIVEFLOWSECRET", "host": "1.2.3.4"}
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("cfg1", "sonoff", options=options)]},
    )
    client = RoutingClient()
    resp = await _get(ws, client, **call)

    entry = resp["entry"] if "entry" in resp else resp["entries"][0]
    assert client.start_options_flow_calls == 0
    assert entry["options"] == {"api_key": REDACTED_SET, "host": REDACTED_SET}
    assert "redact_secrets" in resp["warnings"][0]


@pytest.mark.asyncio
async def test_redact_secrets_leaves_the_options_of_an_entry_without_a_flow(
    redact_on: None,
) -> None:
    # Only an options flow's form marks password fields, so an entry without
    # one has nothing to withhold; the warning counts the entries that had.
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={
            "entries": [
                _row("flow", "sonoff", options={"api_key": "sk-LIVEFLOWSECRET"}),
                _row(
                    "plain", "mqtt", options={"discovery": True}, supports_options=False
                ),
            ]
        },
    )
    client = RoutingClient()
    resp = await _get(ws, client, include_options=True)

    by_id = {e["entry_id"]: e for e in resp["entries"]}
    assert by_id["plain"]["options"] == {"discovery": True}
    assert by_id["flow"]["options"] == {"api_key": REDACTED_SET}
    assert "1 entry" in resp["warnings"][0]


@pytest.mark.asyncio
async def test_a_schema_read_under_redact_secrets_opens_the_options_flow_once(
    redact_on: None,
) -> None:
    # The flow's first step may act, so the form that is described is also the
    # one that names the password fields.
    options = {"api_key": "sk-LIVEFLOWSECRET", "host": "1.2.3.4"}
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("cfg1", "sonoff", options=options)]},
    )
    client = RoutingClient(
        options_flow={
            "type": "form",
            "flow_id": "f1",
            "step_id": "init",
            "data_schema": [
                {"name": "host", "selector": {"text": {}}},
                {"name": "api_key", "selector": {"text": {"type": "password"}}},
            ],
        }
    )
    resp = await _get(ws, client, entry_id="cfg1", include_schema=True)

    assert client.start_options_flow_calls == 1
    assert resp["entry"]["options"] == {"api_key": REDACTED_SET, "host": "1.2.3.4"}


@pytest.mark.asyncio
async def test_legacy_list_passes_domain_filter_to_core() -> None:
    ws = make_ws(WS_CONFIG_ENTRIES, info_exc=_CAPSLESS)
    client = RoutingClient(
        rest_entries=[
            _rest_entry("m1", "mqtt", supports_options=False),
            _rest_entry("h1", "hue", supports_options=False),
        ]
    )
    resp = await _get(ws, client, domain=" MQTT ")

    assert client.rest_list_params == {"domain": "mqtt"}
    assert [e["entry_id"] for e in resp["entries"]] == ["m1"]


@pytest.mark.asyncio
async def test_legacy_list_keeps_core_fields() -> None:
    ws = make_ws(WS_CONFIG_ENTRIES, info_exc=_CAPSLESS)
    client = RoutingClient(
        rest_entries=[
            _rest_entry("m1", "mqtt", supports_options=False, num_subentries=0)
        ]
    )
    resp = await _get(ws, client)

    assert resp["entries"][0]["num_subentries"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("param", ["domain", "integration_type"])
@pytest.mark.parametrize("component", [True, False], ids=["component", "legacy"])
async def test_blank_filter_is_rejected_before_any_read(
    value: str, param: str, component: bool
) -> None:
    # A blank domain is no filter to Core, so it would list and probe every
    # entry; a blank type matches no integration, so the list comes back empty
    # but for entries whose integration Core cannot find.
    info = (
        {"info_result": _CAPS_CONFIG_ENTRIES} if component else {"info_exc": _CAPSLESS}
    )
    ws = make_ws(WS_CONFIG_ENTRIES, cmd_result={"entries": [_row()]}, **info)
    client = RoutingClient(rest_entries=[_rest_entry("m1", "mqtt")])
    with pytest.raises(ToolError) as exc:
        await _get(ws, client, **{param: value})

    assert "VALIDATION_INVALID_PARAMETER" in str(exc.value)
    assert not _ce_calls(ws)
    assert client.rest_list_calls == 0
    assert client.start_options_flow_calls == 0


# --- integration_type (Core's REST ``?type=`` filter) --------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domain", "params"),
    [(None, {"type": "service"}), ("sun", {"domain": "sun", "type": "service"})],
)
async def test_legacy_list_passes_integration_type_to_core(
    domain: str | None, params: dict[str, str]
) -> None:
    ws = make_ws(WS_CONFIG_ENTRIES, info_exc=_CAPSLESS)
    client = RoutingClient(
        rest_entries=[
            _rest_entry("m1", "mqtt", supports_options=False),
            _rest_entry("s1", "sun", supports_options=False),
        ],
        integration_types={"mqtt": "hub", "sun": "service"},
    )
    resp = await _get(ws, client, domain=domain, integration_type="service")

    assert client.rest_list_params == params
    assert [e["entry_id"] for e in resp["entries"]] == ["s1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domain", "params"),
    [(None, {"type": "service"}), ("sun", {"domain": "sun", "type": "service"})],
)
async def test_component_list_keeps_rows_core_matches_for_integration_type(
    domain: str | None, params: dict[str, str]
) -> None:
    # A component without config_entries_type_filter gets no type, so Core's
    # REST answer decides which component rows stay; the rows themselves
    # (options) still come from the component.
    rows = [_row("m1", "mqtt"), _row("s1", "sun", options={"elevation": 1})]
    if domain:
        rows = [r for r in rows if r["domain"] == domain]
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": rows},
    )
    client = RoutingClient(
        rest_entries=[_rest_entry("m1", "mqtt"), _rest_entry("s1", "sun")],
        integration_types={"mqtt": "hub", "sun": "service"},
    )
    resp = await _get(
        ws, client, domain=domain, integration_type="service", include_options=True
    )

    assert client.rest_list_params == params
    assert "type_filter" not in _ce_calls(ws)[0].kwargs
    assert [e["entry_id"] for e in resp["entries"]] == ["s1"]
    assert resp["entries"][0]["options"] == {"elevation": 1}
    assert resp["total_count"] == 1
    assert client.start_options_flow_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("integration_type", "expected"),
    [("service", ["s1", "o1"]), ("helper", [])],
)
async def test_component_list_keeps_what_core_keeps_for_an_unknown_integration(
    integration_type: str, expected: list[str]
) -> None:
    # Core keeps an entry whose integration it cannot find for every type
    # but helper; ha-mcp passes that answer on.
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={
            "entries": [_row("m1", "mqtt"), _row("s1", "sun"), _row("o1", "gone")]
        },
    )
    client = RoutingClient(
        rest_entries=[
            _rest_entry("m1", "mqtt"),
            _rest_entry("s1", "sun"),
            _rest_entry("o1", "gone"),
        ],
        integration_types={"mqtt": "hub", "sun": "service"},
    )
    resp = await _get(ws, client, integration_type=integration_type)

    assert [e["entry_id"] for e in resp["entries"]] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("component", [True, False], ids=["component", "legacy"])
async def test_integration_type_is_ignored_with_a_warning_for_one_entry(
    component: bool,
) -> None:
    info = (
        {"info_result": _CAPS_CONFIG_ENTRIES} if component else {"info_exc": _CAPSLESS}
    )
    ws = make_ws(WS_CONFIG_ENTRIES, cmd_result={"entries": [_row("cfg1")]}, **info)
    client = RoutingClient(single_entry=_rest_entry("cfg1", supports_options=False))
    resp = await _get(ws, client, entry_id="cfg1", integration_type="service")

    assert resp["entry_id"] == "cfg1"
    assert client.rest_list_calls == 0
    assert any("integration_type" in w for w in resp.get("warnings", []))


# --- a degraded secrets scrub is surfaced, not passed off as scrubbed ---------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call", [{"entry_id": "cfg1"}, {"include_options": True}], ids=["one", "list"]
)
async def test_degraded_secret_scrub_is_a_warning(call: dict[str, Any]) -> None:
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("cfg1")], "secret_scrub_degraded": True},
    )
    resp = await _get(ws, RoutingClient(), **call)

    assert any("secrets.yaml" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_a_clean_secret_scrub_adds_no_warning() -> None:
    ws = make_ws(
        WS_CONFIG_ENTRIES,
        info_result=_CAPS_CONFIG_ENTRIES,
        cmd_result={"entries": [_row("cfg1")]},
    )
    resp = await _get(ws, RoutingClient(), entry_id="cfg1")

    assert not any("secrets.yaml" in w for w in resp.get("warnings", []))
