"""Routing of ``ha_get_integration`` over the ``ha_mcp_tools`` component.

When the component advertises ``config_entries``, one frame serves an entry's
identity, options and subentries: no REST read and no options flow. Every miss
of the component falls back to Core's REST list or single-entry read; a socket
that cannot be opened falls back too, since those reads do not use it. A read
never opens an options flow unless ``include_schema`` asks for the form (#2987),
and never one of an ignored entry (#2927).

The server's own client runs on a real ``hass``. The entries are
``MockConfigEntry`` rows of mock integrations whose config and options flows are
Core's; Core answers the component's command, the REST reads and the flows.
Spies on the client's calls record which of them the tool made.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple, cast
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryDataWithId,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from custom_components.ha_mcp_tools import websocket_api as component_ws_api
from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.redaction import REDACTED_SET
from ha_mcp.tools import component_api, component_config_entries
from ha_mcp.tools.component_config_entries import (
    WS_CONFIG_ENTRIES,
    fetch_config_entries,
)
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._client_spies import sent_commands
from ._mock_flow_integration import answer_translations
from ._routing_core import held_command, invalidations, pooled_socket, sent_frames
from ._tool_errors import error_of

OPTIONS = "flow_opts"  # an options flow that shows the stored options
STALE = "flow_stale"  # an options flow whose form suggests discovery=False
PLAIN = "flow_plain"  # no options flow
SERVICE = "flow_service"  # a "service" integration without an options flow
_ENTRY_LIST = ("GET", "/config/config_entries/entry")

_FIELDS = vol.Schema(
    {
        vol.Optional("discovery"): bool,
        vol.Optional("host"): TextSelector(),
        vol.Optional("api_key"): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class _StoredOptions(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _FIELDS, self.config_entry.options
            ),
        )


class _StaleOptions(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Optional("discovery"): bool}), {"discovery": False}
            ),
        )


class _OptionsFlow(ConfigFlow):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _StoredOptions()


class _StaleFlow(ConfigFlow):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _StaleOptions()


class _PlainFlow(ConfigFlow):
    VERSION = 1


class Spies(NamedTuple):
    rest: AsyncMock
    single: AsyncMock
    start_flow: AsyncMock
    abort_flow: AsyncMock
    subentries: AsyncMock

    def rest_lists(self) -> list[dict[str, Any] | None]:
        """The ``params`` of every REST entry-list read."""
        return [
            c.kwargs.get("params")
            for c in self.rest.call_args_list
            if tuple(c.args[:2]) == _ENTRY_LIST
        ]


@pytest.fixture
def integrations(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """The mock integrations and their flows; Core reads their manifests."""
    monkeypatch.chdir(tmp_path)
    for domain, integration_type in (
        (OPTIONS, "hub"),
        (STALE, "hub"),
        (PLAIN, "hub"),
        (SERVICE, "service"),
    ):
        mock_integration(
            hass,
            MockModule(domain, partial_manifest={"integration_type": integration_type}),
        )
        mock_platform(hass, f"{domain}.config_flow", None)
    with (
        mock_config_flow(OPTIONS, _OptionsFlow),
        mock_config_flow(STALE, _StaleFlow),
        mock_config_flow(PLAIN, _PlainFlow),
        mock_config_flow(SERVICE, _PlainFlow),
    ):
        yield


@pytest.fixture
async def core(
    hass: HomeAssistant, core_client: HomeAssistantClient, integrations: None
) -> None:
    """Core's config API, log levels and flow strings, without the component."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "logger", {})
    answer_translations(hass)


@pytest.fixture
async def client(
    core: None, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return component_core_client


def _entry(
    hass: HomeAssistant, domain: str = OPTIONS, **kwargs: Any
) -> MockConfigEntry:
    kwargs.setdefault("options", {"discovery": True})
    entry = MockConfigEntry(domain=domain, title=f"Title {domain}", **kwargs)
    entry.add_to_hass(hass)
    return entry


@contextmanager
def _spies(client: HomeAssistantClient) -> Iterator[Spies]:
    with (
        patch.object(
            client, "guarded_request", AsyncMock(wraps=client.guarded_request)
        ) as rest,
        patch.object(
            client, "get_config_entry", AsyncMock(wraps=client.get_config_entry)
        ) as single,
        patch.object(
            client, "start_options_flow", AsyncMock(wraps=client.start_options_flow)
        ) as start_flow,
        patch.object(
            client, "abort_options_flow", AsyncMock(wraps=client.abort_options_flow)
        ) as abort_flow,
        patch.object(
            client,
            "list_config_subentries",
            AsyncMock(wraps=client.list_config_subentries),
        ) as subentries,
    ):
        yield Spies(rest, single, start_flow, abort_flow, subentries)


async def _get(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    return cast(
        "dict[str, Any]", await IntegrationTools(client).ha_get_integration(**kwargs)
    )


def _without_capability(monkeypatch: pytest.MonkeyPatch, capability: str) -> None:
    """The component as a build from before ``capability``."""
    monkeypatch.setattr(
        system, "CAPABILITIES", [c for c in system.CAPABILITIES if c != capability]
    )


# --- one entry -------------------------------------------------------------------


async def test_one_entry_is_one_component_frame_and_opens_no_options_flow(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _entry(hass)
    socket = await pooled_socket(client)

    with _spies(client) as spies, sent_commands(socket) as sent:
        resp = await _get(client, entry_id=entry.entry_id)

    assert resp["success"] is True
    assert resp["entry_id"] == entry.entry_id
    assert resp["entry"]["domain"] == OPTIONS
    assert resp["entry"]["options"] == {"discovery": True}
    spies.start_flow.assert_not_awaited()
    spies.abort_flow.assert_not_awaited()
    spies.single.assert_not_awaited()
    # Subentries were not asked for, so they are not returned.
    assert "subentries" not in resp["entry"]
    assert "subentries" not in resp
    assert sent_frames(sent, WS_CONFIG_ENTRIES) == [{"entry_id": entry.entry_id}]


async def test_an_entrys_subentries_come_from_the_component_frame(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _entry(
        hass,
        subentries_data=[
            ConfigSubentryDataWithId(
                data={},
                subentry_id="sub1",
                subentry_type="device",
                title="Sub One",
                unique_id="u1",
            )
        ],
    )

    with _spies(client) as spies:
        resp = await _get(client, entry_id=entry.entry_id, include_subentries=True)

    assert resp["subentry_count"] == 1
    (sub,) = resp["subentries"]
    assert (sub["subentry_id"], sub["subentry_type"], sub["title"]) == (
        "sub1",
        "device",
        "Sub One",
    )
    spies.subentries.assert_not_awaited()
    spies.start_flow.assert_not_awaited()


async def test_a_schema_read_keeps_the_component_options(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The form is only for the schema: its suggested value must not replace
    the stored options the component returned."""
    entry = _entry(hass, domain=STALE)

    with _spies(client) as spies:
        resp = await _get(client, entry_id=entry.entry_id, include_schema=True)

    assert resp["entry"]["options"] == {"discovery": True}
    assert resp["options_schema"]["flow_type"] == "form"
    (field,) = resp["options_schema"]["fields"]
    assert (field["name"], field["current"]) == ("discovery", False)
    assert (spies.start_flow.await_count, spies.abort_flow.await_count) == (1, 1)
    spies.single.assert_not_awaited()


async def test_an_entry_the_component_does_not_have_is_not_found(
    client: HomeAssistantClient,
) -> None:
    with _spies(client) as spies, pytest.raises(ToolError) as exc:
        await _get(client, entry_id="ghost")

    assert "ghost" in str(exc.value)
    spies.single.assert_not_awaited()


async def test_an_empty_entry_id_is_not_found_never_the_first_entry(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    _entry(hass)
    socket = await pooled_socket(client)

    with (
        _spies(client) as spies,
        sent_commands(socket) as sent,
        pytest.raises(ToolError),
    ):
        await _get(client, entry_id="")

    assert sent_frames(sent, WS_CONFIG_ENTRIES) == [{"entry_id": ""}]
    spies.single.assert_not_awaited()


async def test_without_the_component_an_entry_comes_from_core_and_its_form(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    """include_schema reads the options from the options form."""
    entry = _entry(hass)
    socket = await pooled_socket(core_client)

    with _spies(core_client) as spies, sent_commands(socket) as sent:
        resp = await _get(core_client, entry_id=entry.entry_id, include_schema=True)

    assert resp["entry"]["entry_id"] == entry.entry_id
    assert resp["entry"]["options"]["discovery"] is True
    assert spies.single.await_count == 1
    assert "warnings" not in resp
    assert not sent_frames(sent, WS_CONFIG_ENTRIES)


async def test_a_component_without_the_command_is_invalidated_and_core_serves(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry(hass, domain=PLAIN)
    assert await component_api.get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], WS_CONFIG_ENTRIES)

    with _spies(client) as spies, invalidations(component_config_entries) as invalidate:
        resp = await _get(client, entry_id=entry.entry_id)

    assert resp["entry"]["entry_id"] == entry.entry_id
    assert spies.single.await_count == 1
    invalidate.assert_called_once_with(client)


async def test_a_timed_out_component_read_falls_back_and_keeps_the_capability(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _entry(hass, domain=PLAIN)

    async with held_command(hass, client, WS_CONFIG_ENTRIES):
        with (
            _spies(client) as spies,
            invalidations(component_config_entries) as invalidate,
        ):
            resp = await _get(client, entry_id=entry.entry_id)

    assert resp["entry"]["entry_id"] == entry.entry_id
    assert spies.single.await_count == 1
    invalidate.assert_not_called()


async def test_an_unreachable_component_socket_falls_back_to_the_rest_read(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """Core's reads are REST, so a socket that cannot be opened falls back
    instead of failing the tool; the capability stays cached."""
    entry = _entry(hass, domain=PLAIN)
    assert await component_api.get_component_caps(client) is not None

    with (
        patch.object(
            component_config_entries,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
        ),
        _spies(client) as spies,
        invalidations(component_config_entries) as invalidate,
    ):
        resp = await _get(client, entry_id=entry.entry_id)

    assert resp["entry"]["entry_id"] == entry.entry_id
    assert spies.single.await_count == 1
    invalidate.assert_not_called()


# --- the list ----------------------------------------------------------------------


async def test_a_list_with_options_is_one_component_frame(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    first = _entry(hass, options={"discovery": True})
    second = _entry(hass, domain=PLAIN, options={"bridge": "x"})
    socket = await pooled_socket(client)

    with _spies(client) as spies, sent_commands(socket) as sent:
        resp = await _get(client, include_options=True)

    by_id = {e["entry_id"]: e for e in resp["entries"]}
    assert by_id[first.entry_id]["options"] == {"discovery": True}
    assert by_id[second.entry_id]["options"] == {"bridge": "x"}
    assert spies.rest_lists() == []
    spies.start_flow.assert_not_awaited()
    assert sent_frames(sent, WS_CONFIG_ENTRIES) == [{}]


async def test_a_list_without_options_omits_options_and_subentries(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _entry(
        hass,
        subentries_data=[
            ConfigSubentryDataWithId(
                data={}, subentry_id="s1", subentry_type="x", title="S", unique_id=None
            )
        ],
    )

    with _spies(client) as spies:
        resp = await _get(client)

    assert [e["entry_id"] for e in resp["entries"]] == [entry.entry_id]
    assert "options" not in resp["entries"][0]
    assert "subentries" not in resp["entries"][0]
    assert spies.rest_lists() == []


async def test_a_domain_filter_is_lowercased_for_the_component_and_adds_options(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    _entry(hass, domain=PLAIN, options={"x": 1})
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await _get(client, domain="Flow_Plain")

    assert sent_frames(sent, WS_CONFIG_ENTRIES) == [{"domain": PLAIN}]
    assert resp["domain_filter"] == PLAIN
    assert resp["entries"][0]["options"] == {"x": 1}


async def test_without_the_component_core_lists_the_entries(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    entry = _entry(hass, domain=PLAIN)
    socket = await pooled_socket(core_client)

    with _spies(core_client) as spies, sent_commands(socket) as sent:
        resp = await _get(core_client)

    assert [e["entry_id"] for e in resp["entries"]] == [entry.entry_id]
    assert len(spies.rest_lists()) == 1
    assert not sent_frames(sent, WS_CONFIG_ENTRIES)


async def test_a_component_without_the_capability_is_never_sent_the_read(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_capability(monkeypatch, "config_entries")
    entry = _entry(hass)
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        assert await fetch_config_entries(client, entry_id=entry.entry_id) is None

    assert not sent_frames(sent, WS_CONFIG_ENTRIES)


async def test_the_legacy_list_passes_the_domain_filter_to_core(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    _entry(hass, domain=OPTIONS)
    plain = _entry(hass, domain=PLAIN)

    with _spies(core_client) as spies:
        resp = await _get(core_client, domain=" FLOW_PLAIN ")

    assert spies.rest_lists() == [{"domain": PLAIN}]
    assert [e["entry_id"] for e in resp["entries"]] == [plain.entry_id]


async def test_the_legacy_list_keeps_cores_fields(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    _entry(hass, domain=PLAIN)

    resp = await _get(core_client)

    assert resp["entries"][0]["num_subentries"] == 0


@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("param", ["domain", "integration_type"])
@pytest.mark.parametrize("component", [True, False], ids=["component", "legacy"])
async def test_a_blank_filter_is_rejected_before_any_read(
    hass: HomeAssistant,
    core: None,
    core_client: HomeAssistantClient,
    value: str,
    param: str,
    component: bool,
) -> None:
    """A blank domain is no filter to Core, so it would list and probe every
    entry; a blank type matches no integration."""
    if component:
        component_ws_api.async_register_commands(hass)
    _entry(hass)
    socket = await pooled_socket(core_client)

    with (
        _spies(core_client) as spies,
        sent_commands(socket) as sent,
        pytest.raises(ToolError) as exc,
    ):
        await _get(core_client, **{param: value})

    assert error_of(exc)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert not sent_frames(sent, WS_CONFIG_ENTRIES)
    assert spies.rest_lists() == []
    spies.start_flow.assert_not_awaited()


# --- integration_type: Core's REST ?type= filter ------------------------------------


@pytest.mark.parametrize("by_domain", [False, True], ids=["all", "domain"])
async def test_the_legacy_list_passes_the_integration_type_to_core(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient, by_domain: bool
) -> None:
    _entry(hass, domain=PLAIN)
    service = _entry(hass, domain=SERVICE)
    domain = SERVICE if by_domain else None

    with _spies(core_client) as spies:
        resp = await _get(core_client, domain=domain, integration_type="service")

    expected = (
        {"domain": SERVICE, "type": "service"} if by_domain else {"type": "service"}
    )
    assert spies.rest_lists() == [expected]
    assert [e["entry_id"] for e in resp["entries"]] == [service.entry_id]


@pytest.mark.parametrize("by_domain", [False, True], ids=["all", "domain"])
async def test_an_older_component_list_keeps_the_rows_core_matches_for_a_type(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    by_domain: bool,
) -> None:
    """A component without ``config_entries_type_filter`` gets no type; Core's
    REST answer decides which rows stay, and their options are the component's."""
    _without_capability(monkeypatch, "config_entries_type_filter")
    _entry(hass, domain=PLAIN)
    service = _entry(hass, domain=SERVICE, options={"elevation": 1})
    domain = SERVICE if by_domain else None
    socket = await pooled_socket(client)

    with _spies(client) as spies, sent_commands(socket) as sent:
        resp = await _get(
            client, domain=domain, integration_type="service", include_options=True
        )

    expected = (
        {"domain": SERVICE, "type": "service"} if by_domain else {"type": "service"}
    )
    assert spies.rest_lists() == [expected]
    assert "type_filter" not in sent_frames(sent, WS_CONFIG_ENTRIES)[0]
    assert [e["entry_id"] for e in resp["entries"]] == [service.entry_id]
    assert resp["entries"][0]["options"] == {"elevation": 1}
    assert resp["total_count"] == 1
    spies.start_flow.assert_not_awaited()


@pytest.mark.parametrize(
    ("integration_type", "kept"), [("service", True), ("helper", False)]
)
async def test_a_component_list_keeps_what_core_keeps_for_an_unknown_integration(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    integration_type: str,
    kept: bool,
) -> None:
    """Core keeps an entry whose integration it cannot find for every type
    but helper; ha-mcp passes that answer on."""
    _without_capability(monkeypatch, "config_entries_type_filter")
    _entry(hass, domain=PLAIN)
    service = _entry(hass, domain=SERVICE)
    gone = _entry(hass, domain="flow_removed")

    resp = await _get(client, integration_type=integration_type)

    expected = [service.entry_id, gone.entry_id] if kept else []
    assert sorted(e["entry_id"] for e in resp["entries"]) == sorted(expected)


@pytest.mark.parametrize("component", [True, False], ids=["component", "legacy"])
async def test_the_integration_type_is_ignored_with_a_warning_for_one_entry(
    hass: HomeAssistant,
    core: None,
    core_client: HomeAssistantClient,
    component: bool,
) -> None:
    if component:
        component_ws_api.async_register_commands(hass)
    entry = _entry(hass, domain=PLAIN)

    with _spies(core_client) as spies:
        resp = await _get(
            core_client, entry_id=entry.entry_id, integration_type="service"
        )

    assert resp["entry_id"] == entry.entry_id
    assert spies.rest_lists() == []
    assert any("integration_type" in w for w in resp.get("warnings", []))


# --- ignored entries (#2927) ---------------------------------------------------------
# Core never sets up an ignored entry, so its options flow has nothing to read and
# several integrations' flows raise. Core's supports_options asks only the flow
# handler, never the source, so the tool keys on the source.


def _ignored(hass: HomeAssistant) -> MockConfigEntry:
    return _entry(hass, source=SOURCE_IGNORE, options={})


async def test_an_ignored_entry_without_the_component_is_not_probed(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    entry = _ignored(hass)

    with _spies(core_client) as spies:
        resp = await _get(core_client, entry_id=entry.entry_id)

    spies.start_flow.assert_not_awaited()
    assert resp["entry"]["supports_options"] is False
    assert resp["entry"]["options"] == {}
    assert "warnings" not in resp


async def test_a_not_loaded_entry_that_is_not_ignored_keeps_its_schema_read(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    """The gate keys on the source, not the state: a not-loaded user entry's
    options form is still read."""
    entry = _entry(hass)
    assert entry.state.value == "not_loaded"

    with _spies(core_client) as spies:
        resp = await _get(core_client, entry_id=entry.entry_id, include_schema=True)

    assert spies.start_flow.await_count == 1
    assert resp["entry"]["supports_options"] is True


async def test_an_ignored_entrys_schema_read_without_the_component_opens_no_flow(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    entry = _ignored(hass)

    with _spies(core_client) as spies:
        resp = await _get(core_client, entry_id=entry.entry_id, include_schema=True)

    spies.start_flow.assert_not_awaited()
    assert "options_schema" not in resp
    assert "warnings" not in resp


async def test_ignored_entries_in_a_legacy_list_are_not_probed(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    _ignored(hass)
    _ignored(hass)

    with _spies(core_client) as spies:
        resp = await _get(core_client, include_options=True)

    spies.start_flow.assert_not_awaited()
    assert [e["supports_options"] for e in resp["entries"]] == [False, False]
    assert [e["options"] for e in resp["entries"]] == [{}, {}]
    assert "warnings" not in resp


async def test_an_ignored_entrys_schema_read_through_the_component_opens_no_flow(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _ignored(hass)

    with _spies(client) as spies:
        resp = await _get(client, entry_id=entry.entry_id, include_schema=True)

    spies.start_flow.assert_not_awaited()
    assert resp["entry"]["supports_options"] is False
    assert "options_schema" not in resp
    assert "warnings" not in resp


# --- a read opens no options flow (#2987) --------------------------------------------
# An options flow's first step is the integration's own code and may act: Sonoff
# LAN logs in to its cloud there, which ends the session the integration runs on.
# Only include_schema=True, which asks for the form, opens one.


async def test_reading_an_entry_without_the_component_opens_no_options_flow(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    entry = _entry(hass)

    with _spies(core_client) as spies:
        resp = await _get(core_client, entry_id=entry.entry_id)

    spies.start_flow.assert_not_awaited()
    assert "options" not in resp["entry"]
    assert "ha_mcp_tools" in resp["warnings"][0]


async def test_listing_options_without_the_component_opens_no_options_flow(
    hass: HomeAssistant, core: None, core_client: HomeAssistantClient
) -> None:
    _entry(hass)
    _entry(hass)

    with _spies(core_client) as spies:
        resp = await _get(core_client, domain=OPTIONS)

    spies.start_flow.assert_not_awaited()
    assert all("options" not in entry for entry in resp["entries"])
    assert len(resp["warnings"]) == 1
    assert "ha_mcp_tools" in resp["warnings"][0]


@pytest.mark.parametrize("single", [True, False], ids=["single", "list"])
async def test_redact_secrets_withholds_options_without_opening_the_options_flow(
    hass: HomeAssistant, client: HomeAssistantClient, redact_on: None, single: bool
) -> None:
    entry = _entry(hass, options={"api_key": "sk-LIVEFLOWSECRET", "host": "1.2.3.4"})
    call: dict[str, Any] = (
        {"entry_id": entry.entry_id} if single else {"include_options": True}
    )

    with _spies(client) as spies:
        resp = await _get(client, **call)

    shown = resp["entry"] if single else resp["entries"][0]
    spies.start_flow.assert_not_awaited()
    assert shown["options"] == {"api_key": REDACTED_SET, "host": REDACTED_SET}
    assert "redact_secrets" in resp["warnings"][0]


async def test_redact_secrets_leaves_the_options_of_an_entry_without_a_flow(
    hass: HomeAssistant, client: HomeAssistantClient, redact_on: None
) -> None:
    """Only an options flow's form marks password fields, so an entry without
    one has nothing to withhold; the warning counts the entries that had."""
    with_flow = _entry(hass, options={"api_key": "sk-LIVEFLOWSECRET"})
    plain = _entry(hass, domain=PLAIN, options={"discovery": True})

    resp = await _get(client, include_options=True)

    by_id = {e["entry_id"]: e for e in resp["entries"]}
    assert by_id[plain.entry_id]["options"] == {"discovery": True}
    assert by_id[with_flow.entry_id]["options"] == {"api_key": REDACTED_SET}
    assert "1 entry" in resp["warnings"][0]


async def test_a_schema_read_under_redact_secrets_opens_the_options_flow_once(
    hass: HomeAssistant, client: HomeAssistantClient, redact_on: None
) -> None:
    """The flow's first step may act, so the form that is described is also
    the one that names the password fields."""
    entry = _entry(hass, options={"api_key": "sk-LIVEFLOWSECRET", "host": "1.2.3.4"})

    with _spies(client) as spies:
        resp = await _get(client, entry_id=entry.entry_id, include_schema=True)

    assert spies.start_flow.await_count == 1
    assert resp["entry"]["options"] == {"api_key": REDACTED_SET, "host": "1.2.3.4"}


# --- a degraded secrets scrub is surfaced, not passed off as scrubbed ----------------


@pytest.mark.parametrize("single", [True, False], ids=["one", "list"])
async def test_a_degraded_secret_scrub_is_a_warning(
    hass: HomeAssistant, client: HomeAssistantClient, single: bool
) -> None:
    secrets = Path(hass.config.path("secrets.yaml"))
    await hass.async_add_executor_job(secrets.write_text, "token: [unclosed\n")
    entry = _entry(hass, domain=PLAIN)
    call: dict[str, Any] = (
        {"entry_id": entry.entry_id} if single else {"include_options": True}
    )

    resp = await _get(client, **call)

    assert any("secrets.yaml" in w for w in resp.get("warnings", []))


async def test_a_clean_secret_scrub_adds_no_warning(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = _entry(hass, domain=PLAIN)

    resp = await _get(client, entry_id=entry.entry_id)

    assert not any("secrets.yaml" in w for w in resp.get("warnings", []))
