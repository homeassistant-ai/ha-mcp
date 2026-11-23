"""``ha_dev_manage_server``: info, update_source and restart, on a real ``hass``.

The component's server entry is a real config entry (``_server_entry``) and
its options flow is the component's own, opened and submitted over Core's REST
API. ``test_ha_dev_manage_server_component_routing.py`` covers which route
update_source takes; ``test_ha_dev_server_entry_component_routing.py`` how the
entry is found.
"""

import json
import logging
from collections.abc import Iterator
from inspect import signature
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.http import current_request
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, async_capture_events

from custom_components.ha_mcp_tools.const import DATA_LAST_PIP_SPEC, DOMAIN
from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import tools_dev
from ha_mcp.tools.tools_dev import DevTools

from ._core_setup import with_config_component
from ._dev_tool_env import _isolated_env as _isolated_env
from ._server_entry import add_server_entry, replace_server, set_up
from ._server_update import (
    EMBEDDED_TARGET,
    PIP_SPEC,
    SERVER_URL,
    server_entry,
    update_source,
)
from ._tools_addons_core import outwait, timers
from ._tools_dev_core import Supervisor
from ._tools_dev_core import supervisor as supervisor
from ._ws_transport import until

# Stored overrides a partial options submit would blank unless resent.
OVERRIDES = {
    "server_url": SERVER_URL,
    "external_url": "https://ext.example.com",
    "webhook_id_override": "hook123",
    "secret_path_override": "/secret",
}
OLD_PIN = "ha-mcp==9.9.9"
DEV_LOGGER = "ha_mcp.tools.tools_dev"


@pytest.fixture
async def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The server's client with the component; ``config`` serves the flows."""
    return await with_config_component(hass, component_core_client)


@pytest.fixture
async def entries(hass: HomeAssistant) -> tuple[str, str]:
    """(services entry id, server entry id); ``config`` serves their flows."""
    assert await async_setup_component(hass, "config", {})
    services = MockConfigEntry(domain=DOMAIN, data={}, options={})
    services.add_to_hass(hass)
    return services.entry_id, add_server_entry(hass).entry_id


@pytest.fixture
def embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool runs in the component's in-process server, with no flush wait."""
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    monkeypatch.setattr(tools_dev, "SELF_ACTION_FLUSH_DELAY_S", 0)


def _stored_overrides(entry: MockConfigEntry) -> dict[str, Any]:
    return {key: entry.options[key] for key in OVERRIDES}


class TestInfo:
    async def test_a_standalone_server_reports_itself_and_home_assistant(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        assert await async_setup_component(hass, "config", {})
        result = await DevTools(core_client).ha_dev_manage_server(action="info")
        data = result["data"]
        assert data["deployment_mode"] == "standalone"
        assert data["ha_version"] == HA_VERSION
        assert data["component_server_entry"] is None
        assert data["server_version"]

    async def test_info_reports_the_server_entry_and_closes_every_probe_flow(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entries: tuple[str, str],
    ) -> None:
        """The services entry's informational form and the server entry's
        options form are both opened to tell them apart; neither may be
        left open, or every info call leaks a flow."""
        result = await DevTools(core_client).ha_dev_manage_server(action="info")
        entry = result["data"]["component_server_entry"]
        assert entry["entry_id"] == entries[1]
        assert entry["pip_spec"] == ""
        assert "channel" not in entry
        assert hass.config_entries.options.async_progress() == []

    async def test_info_says_the_entry_is_not_the_server_answering(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """Its pip_spec must not be read as the version of the serving server."""
        server_entry(hass, pip_spec="")
        result = await DevTools(client).ha_dev_manage_server(action="info")
        role = result["data"]["component_server_entry"]["role"]
        assert "separate" in role
        assert "update_source" in role

    @pytest.mark.parametrize("pending", [True, False])
    async def test_info_says_when_the_entry_spec_is_not_installed_yet(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        pending: bool,
    ) -> None:
        """A deferred install shows the new pip_spec while the old build
        serves; info must not read as a finished switch."""
        monkeypatch.setattr(hass.config, "skip_pip", False)
        add_server_entry(
            hass,
            data={DATA_LAST_PIP_SPEC: OLD_PIN if pending else PIP_SPEC},
            options={"pip_spec": PIP_SPEC},
        )
        result = await DevTools(client).ha_dev_manage_server(action="info")
        assert result["data"]["component_server_entry"]["install_pending"] is pending
        assert bool(result.get("warnings")) is pending

    async def test_info_asks_for_a_component_that_can_tell(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A component without the flag cannot say whether the spec is
        installed; info says unknown and how to get the answer."""
        monkeypatch.setattr(
            system,
            "CAPABILITIES",
            [c for c in system.CAPABILITIES if c != "server_entry_install_pending"],
        )
        server_entry(hass, pip_spec=PIP_SPEC)
        result = await DevTools(client).ha_dev_manage_server(action="info")
        assert result["data"]["component_server_entry"]["install_pending"] is None
        assert result.get("warnings")

    async def test_info_warns_when_home_assistant_does_not_answer(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert await async_setup_component(hass, "config", {})
        monkeypatch.setattr(
            core_client,
            "get_config",
            AsyncMock(side_effect=HomeAssistantConnectionError("api down")),
        )
        result = await DevTools(core_client).ha_dev_manage_server(action="info")
        assert result["success"] is True
        assert any("HA version" in w for w in result["warnings"])

    async def test_info_warns_when_the_entry_cannot_be_looked_up(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Without Core's ``config`` integration the entries cannot be listed:
        a warning, not a failed report."""
        result = await DevTools(core_client).ha_dev_manage_server(action="info")
        assert result["success"] is True
        assert any("component server entry" in w for w in result["warnings"])
        assert result["data"]["server_version"]

    async def test_an_entry_whose_flow_will_not_open_is_skipped(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entries: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core refuses the services entry's flow; the probe keeps going."""
        services_id, server_id = entries
        open_flow = config_entries.OptionsFlowManager.async_create_flow

        async def refuse_services(
            manager: config_entries.OptionsFlowManager, handler_key: str, **kw: Any
        ) -> Any:
            if handler_key == services_id:
                raise data_entry_flow.UnknownHandler
            return await open_flow(manager, handler_key, **kw)

        monkeypatch.setattr(
            config_entries.OptionsFlowManager, "async_create_flow", refuse_services
        )
        result = await DevTools(core_client).ha_dev_manage_server(action="info")
        assert result["data"]["component_server_entry"]["entry_id"] == server_id


class TestUpdateSource:
    async def test_a_pip_spec_is_required(
        self, core_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError, match="pip_spec"):
            await DevTools(core_client).ha_dev_manage_server(action="update_source")

    def test_there_is_no_channel_to_choose(self) -> None:
        """Each component release pins its server; the channel selector is gone."""
        assert "channel" not in signature(DevTools.ha_dev_manage_server).parameters

    async def test_a_multiline_pip_spec_is_refused(
        self, core_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError, match="single-line"):
            await DevTools(core_client).ha_dev_manage_server(
                action="update_source", pip_spec="a\nb"
            )

    async def test_without_a_server_entry_it_says_one_is_needed(
        self, client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError, match="server entry"):
            await update_source(client, PIP_SPEC)

    async def test_the_result_reports_the_pin_it_replaced(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        entry = server_entry(hass, pip_spec=OLD_PIN)
        result = await update_source(client, PIP_SPEC)
        assert result["data"]["applied"] == {"pip_spec": PIP_SPEC}
        assert result["data"]["previous"] == {"pip_spec": OLD_PIN}
        assert entry.options["pip_spec"] == PIP_SPEC

    async def test_the_result_says_this_connection_keeps_its_version(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """Regression: an agent on the ADD-ON read success plus a generic
        "reloading" note and concluded its own server would change version.
        Only the component's separate entry changes, and success means its
        options were applied, not that the background install finished."""
        server_entry(hass, pip_spec="")
        data = (await update_source(client, PIP_SPEC))["data"]
        assert data["target"] == "ha_mcp_tools in-process server entry"
        assert "NOT" in data["note"]
        assert "this connection" in data["note"]
        assert "background" in data["note"]
        assert "fail" in data["note"]

    @pytest.mark.parametrize("spelling", ["Clear", "   "])
    async def test_clearing_drops_the_pin_and_keeps_the_overrides(
        self, hass: HomeAssistant, client: HomeAssistantClient, spelling: str
    ) -> None:
        """'clear' in any case, or blank, clears: some MCP clients mangle an
        empty-string argument in transit."""
        entry = server_entry(hass, pip_spec=OLD_PIN, **OVERRIDES)
        await update_source(client, spelling)
        assert entry.options["pip_spec"] == ""
        assert _stored_overrides(entry) == OVERRIDES

    async def test_a_spec_merely_containing_clear_is_kept(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        entry = server_entry(hass, pip_spec="")
        await update_source(client, "clearly==1.0")
        assert entry.options["pip_spec"] == "clearly==1.0"

    async def test_a_new_pin_replaces_the_old_and_keeps_the_overrides(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """The options form pre-fills its optional fields; a field left out of
        the submit reads as cleared, so the stored overrides are resent."""
        entry = server_entry(hass, pip_spec=OLD_PIN, **OVERRIDES)
        await update_source(client, PIP_SPEC)
        assert entry.options["pip_spec"] == PIP_SPEC
        assert _stored_overrides(entry) == OVERRIDES

    async def test_an_override_the_tool_does_not_name_is_kept_too(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """What is resent comes from the form itself, so a field the
        component's options grow later survives without this tool knowing it."""
        entry = server_entry(hass, pip_spec="", oauth_client_id_override="kept-id")
        await update_source(client, PIP_SPEC)
        assert entry.options["oauth_client_id_override"] == "kept-id"

    async def test_a_resent_secret_is_never_echoed_back(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """The form pre-fills the OAuth client secret; it is resent so the
        update does not clear it, but the reply carries only what the caller
        asked to change (Codex review, #2256)."""
        secret = "super-secret-value"
        entry = server_entry(hass, pip_spec="", oauth_client_secret_override=secret)
        result = await update_source(client, PIP_SPEC)
        assert entry.options["oauth_client_secret_override"] == secret
        assert result["data"]["applied"] == {"pip_spec": PIP_SPEC}
        assert secret not in json.dumps(result)

    @pytest.mark.usefixtures("embedded")
    async def test_embedded_the_result_says_this_server_restarts(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        assert await async_setup_component(hass, "config", {})
        entry = server_entry(hass, pip_spec="")
        result = await update_source(core_client, PIP_SPEC)
        assert result["data"]["target"] == EMBEDDED_TARGET
        await until(lambda: entry.options.get("pip_spec") == PIP_SPEC)

    async def test_entries_core_cannot_list_are_a_failure_not_a_missing_entry(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Without Core's ``config`` integration ``config_entries/get`` is
        refused; that must not read as "install the component"."""
        with pytest.raises(ToolError, match="config_entries/get failed"):
            await update_source(core_client, PIP_SPEC)

    async def test_a_dead_connection_does_not_read_as_a_missing_entry(
        self,
        core_client: HomeAssistantClient,
        entries: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """That told users to reinstall a component that was running."""
        monkeypatch.setattr(
            core_client,
            "start_options_flow",
            AsyncMock(side_effect=HomeAssistantConnectionError("conn refused")),
        )
        with pytest.raises(ToolError) as raised:
            await update_source(core_client, PIP_SPEC)
        assert "server entry" not in str(raised.value)
        assert "conn refused" in str(raised.value)


@pytest.fixture
def service_calls(hass: HomeAssistant) -> list[Event]:
    """Every service call Core receives."""
    return async_capture_events(hass, EVENT_CALL_SERVICE)


def _reloads(calls: list[Event]) -> list[dict[str, Any]]:
    """What each ``homeassistant.reload_config_entry`` call asked for."""
    return [
        call.data["service_data"]
        for call in calls
        if (call.data["domain"], call.data["service"])
        == ("homeassistant", "reload_config_entry")
    ]


@pytest.fixture
async def running_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> MockConfigEntry:
    """A loaded server entry; ``homeassistant`` provides the reload service."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "config", {})
    replace_server(monkeypatch)
    entry = add_server_entry(hass)
    assert await set_up(hass, entry)
    return entry


@pytest.fixture
def dev_log(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    with caplog.at_level(logging.INFO, logger=DEV_LOGGER):
        yield caplog


def _dev_records(
    log: pytest.LogCaptureFixture, level: int = logging.DEBUG
) -> list[logging.LogRecord]:
    return [r for r in log.records if r.name == DEV_LOGGER and r.levelno >= level]


def _logged(log: pytest.LogCaptureFixture, text: str) -> bool:
    return any(text in r.getMessage() for r in _dev_records(log))


class TestRestart:
    async def test_a_standalone_server_must_be_restarted_from_outside(
        self, core_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError, match="standalone"):
            await DevTools(core_client).ha_dev_manage_server(action="restart")

    @pytest.mark.usefixtures("embedded")
    async def test_embedded_without_a_server_entry_says_so(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        assert await async_setup_component(hass, "config", {})
        with pytest.raises(ToolError, match="server entry"):
            await DevTools(core_client).ha_dev_manage_server(action="restart")

    @pytest.mark.usefixtures("embedded")
    async def test_embedded_restart_reloads_through_the_shielded_service(
        self,
        core_client: HomeAssistantClient,
        running_entry: MockConfigEntry,
        service_calls: list[Event],
        dev_log: pytest.LogCaptureFixture,
    ) -> None:
        """Regression: POSTing ``/config/config_entries/entry/{id}/reload``
        wedged the entry. That handler is unshielded and aiohttp cancels it
        when the unload kills the client sending it, leaving the entry in
        ``UNLOAD_IN_PROGRESS`` until Home Assistant restarts. Only the
        ``homeassistant.reload_config_entry`` service handler is shielded."""
        result = await DevTools(core_client).ha_dev_manage_server(action="restart")
        assert result["data"]["scheduled"] is True
        assert result["data"]["mode"] == "embedded"

        await until(lambda: _logged(dev_log, "requested"))
        assert _reloads(service_calls) == [{"entry_id": running_entry.entry_id}]

    @pytest.mark.usefixtures("embedded")
    async def test_a_reload_that_tears_down_its_own_connection_is_no_error(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        running_entry: MockConfigEntry,
        dev_log: pytest.LogCaptureFixture,
    ) -> None:
        """Losing the reply is how a successful self-restart ends: the reload
        stops the worker owning the client, so the answer never arrives.
        Logged at WARNING, which Home Assistant shows for this package."""

        @callback
        def drop_the_connection(event: Event) -> None:
            if event.data["service"] == "reload_config_entry":
                request = current_request.get()
                assert request is not None
                assert request.transport is not None
                request.transport.abort()

        hass.bus.async_listen(EVENT_CALL_SERVICE, drop_the_connection)
        await DevTools(core_client).ha_dev_manage_server(action="restart")

        await until(lambda: _logged(dev_log, "tore down this"))
        await hass.async_block_till_done()
        assert [r.levelno for r in _dev_records(dev_log, logging.WARNING)] == [
            logging.WARNING
        ]

    @pytest.mark.usefixtures("embedded")
    async def test_a_reload_that_never_reached_home_assistant_is_an_error(
        self,
        core_client: HomeAssistantClient,
        running_entry: MockConfigEntry,
        service_calls: list[Event],
        dev_log: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The entry may be left un-reloaded, so this stays loud."""
        await DevTools(core_client).ha_dev_manage_server(action="restart")
        monkeypatch.setattr(
            core_client,
            "call_service",
            AsyncMock(side_effect=HomeAssistantConnectionError("refused")),
        )

        await until(lambda: bool(_dev_records(dev_log, logging.ERROR)))
        assert _reloads(service_calls) == []

    async def test_an_add_on_asks_the_supervisor_to_restart_it(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        supervisor: Supervisor,
    ) -> None:
        armed_before = timers(hass)
        result = await DevTools(core_client).ha_dev_manage_server(action="restart")
        assert result["data"]["mode"] == "addon"
        assert result["data"]["scheduled"] is True
        # The restart waits for this response to flush: skip that on Core's clock.
        await outwait(hass, 0, 1, armed_before)
        await until(lambda: supervisor.restarts == 1)
