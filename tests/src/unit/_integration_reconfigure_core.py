"""A test integration whose reconfigure flow Core runs, for the
``ha_set_integration(reconfigure=True)`` tests.

The flow is the integration's own ``async_step_reconfigure``, run by Core's
flow manager; the server's client drives it over Core's flow endpoints, reads
the entry's identity from Core's registries, and watches the reload Core runs
after the change.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._mock_flow_integration import answer_translations

# Reconfigurable; an entry's ``opens`` data picks how its flow opens.
HUB = "reconfigure_hub"
# Has a config flow without a reconfigure step.
PLAIN = "reconfigure_plain"
# Another integration that may share a hub's device.
OTHER = "reconfigure_other"

MAC = "AA:BB:CC:00:11:22"
UNIQUE_ID = "hub-serial-1"
NEW_HOST = "192.0.2.10"
# The host the flow answers with a second form instead of applying.
SECOND_FORM_HOST = "192.0.2.99"

_FORM = vol.Schema({vol.Required("host"): str, vol.Required("reload"): bool})


@dataclass
class HubWorld:
    """What the test integration does: its entry setup raises ``setup_error``,
    and a submit applies only once ``applied`` is set."""

    setup_error: Exception | None = None
    applied: asyncio.Event = field(default_factory=asyncio.Event)

    async def setup_entry(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        if self.setup_error is not None:
            raise self.setup_error

    async def unload_entry(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        return None


class _HubFlow(ConfigFlow):
    VERSION = 1
    _answer: dict[str, Any]
    _work: asyncio.Task[Any] | None = None

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opens = self._get_reconfigure_entry().data.get("opens")
        if opens == "external":
            return self.async_external_step(
                step_id="reconfigure", url="https://example.com/authorize"
            )
        if opens == "abort":
            return self.async_abort(reason="cannot_connect")
        if user_input is None:
            return self.async_show_form(step_id="reconfigure", data_schema=_FORM)
        self._answer = user_input
        if user_input["host"] == SECOND_FORM_HOST:
            return await self.async_step_confirm()
        return await self.async_step_apply()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="confirm", data_schema=vol.Schema({vol.Required("pin"): str})
            )
        return await self.async_step_apply()

    async def async_step_apply(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        world: HubWorld = self.hass.data[HUB]
        if self._work is None:
            if world.applied.is_set():
                return await self.async_step_finish()
            self._work = self.hass.async_create_task(world.applied.wait())
        if not self._work.done():
            return self.async_show_progress(
                step_id="apply", progress_action="apply", progress_task=self._work
            )
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        data = {**entry.data, "host": self._answer["host"]}
        if self._answer["reload"]:
            return self.async_update_reload_and_abort(entry, data=data)
        return self.async_update_and_abort(entry, data=data)


class _PlainFlow(ConfigFlow):
    VERSION = 1


@pytest.fixture
async def world(hass: HomeAssistant) -> AsyncIterator[HubWorld]:
    """The test integrations on a ``hass`` with Core's ``config`` integration."""
    assert await async_setup_component(hass, "config", {})
    answer_translations(hass)
    hub_world = HubWorld()
    hub_world.applied.set()
    hass.data[HUB] = hub_world
    mock_integration(
        hass,
        MockModule(
            HUB,
            async_setup_entry=hub_world.setup_entry,
            async_unload_entry=hub_world.unload_entry,
        ),
    )
    for domain in (HUB, PLAIN):
        mock_platform(hass, f"{domain}.config_flow")
    mock_integration(hass, MockModule(OTHER))
    with mock_config_flow(HUB, _HubFlow), mock_config_flow(PLAIN, _PlainFlow):
        yield hub_world


@pytest.fixture
def tools(core_client: HomeAssistantClient, world: HubWorld) -> IntegrationTools:
    """``ha_set_integration`` on a Core without the ha_mcp_tools component."""
    return IntegrationTools(core_client)


def add_hub(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    """A hub entry holding ``data`` over its stored host, not set up."""
    entry = MockConfigEntry(
        domain=HUB, title="Hub", unique_id=UNIQUE_ID, data={"host": "old", **data}
    )
    entry.add_to_hass(hass)
    return entry


@dataclass
class Hub:
    """A hub entry, set up by Core, with its device and one entity."""

    entry: MockConfigEntry
    device_id: str
    entity_id: str


@pytest.fixture
async def hub(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    world: HubWorld,
) -> Hub:
    entry = add_hub(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )
    entity = entity_registry.async_get_or_create(
        "switch", HUB, "relay", config_entry=entry, device_id=device.id
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return Hub(entry, device.id, entity.entity_id)


def share_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    domain: str,
) -> MockConfigEntry:
    """An entry of ``domain`` that Core attaches to the hub's device."""
    entry = MockConfigEntry(domain=domain, title=f"{domain} on the hub")
    entry.add_to_hass(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )
    return entry


@contextmanager
def spies(client: HomeAssistantClient, *methods: str) -> Iterator[dict[str, Any]]:
    """Call-through spies on ``methods`` of the client; Core answers each call."""
    with ExitStack() as stack:
        yield {
            name: stack.enter_context(
                patch.object(client, name, wraps=getattr(client, name))
            )
            for name in methods
        }


def config_flows(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The config (setup and reconfigure) flows open in Core."""
    return [dict(flow) for flow in hass.config_entries.flow.async_progress()]


@contextmanager
def before_answering(
    hass: HomeAssistant, command: str, act: Callable[[], Awaitable[Any]]
) -> Iterator[None]:
    """Core runs ``act`` when ``command`` arrives, then answers it with its
    own handler."""
    handler, schema = hass.data[websocket_api.DOMAIN][command]

    async def run_first(core: HomeAssistant, connection: Any, msg: Any) -> None:
        await act()
        handler(core, connection, msg)

    websocket_api.async_register_command(
        hass, command, websocket_api.async_response(run_first), schema
    )
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, handler, schema)
