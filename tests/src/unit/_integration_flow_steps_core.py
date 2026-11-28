"""Test integrations whose setup, options, reconfigure and reauth flows Core
runs, for the ``ha_set_integration`` flow-step tests.

Every flow is a mock integration's own ``ConfigFlow``, run by Core's flow
managers; the server's client (``flow_client``: without the component, and
with it, where Home Assistant 2026.12 formats the texts) drives them over
Core's flow endpoints, and Core's ``frontend/get_translations`` reads their
strings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    FlowType,
    OptionsFlow,
)
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, mock_config_flow

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._mock_flow_integration import answer_translations, mock_flow_integration

# A form, then a routing form that opens with an advisory error (as KNX's
# does), or a menu; the routing form refuses REFUSED_ADDRESS.
STEPS = "flow_steps"
# The same flow, opening on a menu.
MENU = "flow_steps_menu"
# One form; a host that is already set up aborts it.
SINGLE = "flow_steps_single"
# Finishes when it starts.
INSTANT = "flow_steps_instant"
# Opens on a browser (OAuth) step.
EXTERNAL = "flow_steps_external"
# Updates the existing entry and aborts, as async_update_reload_and_abort does.
REJOIN = "flow_steps_rejoin"
# Its form fails when Core runs the step again.
REREAD = "flow_steps_reread"
# Aborts with HANDOVER_REASON, handing over to a setup flow of SINGLE it opens
# (next_flow), as improv_ble hands a provisioned device to its integration.
HANDOVER = "flow_steps_handover"
HANDOVER_REASON = "provision_successful"
# Creates its entry at once and opens that entry's options flow (next_flow),
# as satel_integra does; scrape, ntfy and smtp open a subentry flow the same way.
FOLLOWUP = "flow_steps_followup"

MENU_OPTIONS = ["routing", "tunneling"]
# STEPS' strings for its pick menu.
MENU_LABELS = {"routing": "Routing", "tunneling": "Tunneling"}
# Its menu labels its own options, as elmax labels its panels by name.
LABELLED = "flow_steps_labelled"
OWN_LABELS = {"routing": "Kitchen panel", "tunneling": "Garage panel"}
REFUSED_ADDRESS = "0.0.0"
# The host STEPS' reauth form refuses.
REFUSED_HOST = "refused.invalid"
MAC = "AA:BB:CC:DD:EE:01"
# The user form's description_placeholders value for {router}.
ROUTER = "KNX/IP"
ERROR_TEXTS = {
    "no_router": "No router answered.",
    "no_router_discovered": "No router was discovered.",
    "invalid_auth": "The router refused the credentials.",
}
# STEPS' strings for its user form (no submit label) and its pick menu.
STEP_STRINGS = {
    "user": {
        "title": "Connect",
        "description": "Reach the {router} bus.",
        "data_description": {"connection_type": "How {router} is reached."},
    },
    "pick": {"title": "Connection", "menu_options": MENU_LABELS},
}

_ADDRESS = vol.Schema({vol.Required("individual_address"): str})
_HOST = vol.Schema({vol.Required("host"): str})


class _Options(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema({vol.Optional("string"): str, vol.Optional("int"): int})
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )


class _StepsFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({vol.Required("connection_type"): str}),
                description_placeholders={"router": ROUTER},
            )
        if user_input["connection_type"] == "menu":
            return await self.async_step_pick()
        return await self.async_step_routing()

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="pick", menu_options=MENU_OPTIONS)

    async def async_step_routing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="routing",
                data_schema=_ADDRESS,
                errors={"base": "no_router_discovered"},
            )
        address = user_input["individual_address"]
        if address == REFUSED_ADDRESS:
            return self.async_show_form(
                step_id="routing", data_schema=_ADDRESS, errors={"base": "no_router"}
            )
        return self.async_create_entry(title=f"Routing as {address}", data=user_input)

    async def async_step_tunneling(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="tunneling", data_schema=_HOST)
        return self.async_create_entry(title="Tunnel", data=user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(step_id="reconfigure", data_schema=_HOST)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = None
        if user_input is not None:
            if user_input["host"] != REFUSED_HOST:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=user_input
                )
            errors = {"host": "invalid_auth"}
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_HOST, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _Options()


class _MenuFlow(_StepsFlow):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=MENU_OPTIONS)


class _LabelledMenuFlow(_StepsFlow):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=OWN_LABELS)


class _SingleFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required("host"): str,
                    vol.Optional("name", description={"suggested_value": "Hint"}): str,
                }
            )
            return self.async_show_form(step_id="user", data_schema=schema)
        await self.async_set_unique_id(user_input["host"])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=user_input["host"], data=user_input)


class _InstantFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_create_entry(title="Instant", data={})


class _ExternalFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_auth()

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_external_step(
            step_id="auth", url="https://example.com/authorize"
        )


class _RejoinFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_HOST)
        (entry,) = self._async_current_entries()
        self.hass.config_entries.async_update_entry(entry, data=user_input)
        return self.async_abort(reason="reauth_successful")


class _RereadFlow(ConfigFlow):
    VERSION = 1
    shown = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self.shown:
            raise RuntimeError("the integration's step fails when run again")
        self.shown = True
        return self.async_show_form(step_id="user", data_schema=_HOST)


class _HandoverFlow(ConfigFlow):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        setup = await self.hass.config_entries.flow.async_init(
            SINGLE, context={"source": SOURCE_USER}
        )
        return self.async_abort(
            reason=HANDOVER_REASON, next_flow=(FlowType.CONFIG_FLOW, setup["flow_id"])
        )


class _FollowUpFlow(_StepsFlow):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_create_entry(title="Follow-up", data={})

    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        options = await self.hass.config_entries.options.async_init(
            result["result"].entry_id
        )
        result["next_flow"] = (FlowType.OPTIONS_FLOW, options["flow_id"])
        return result


_FLOWS: dict[str, type[ConfigFlow]] = {
    STEPS: _StepsFlow,
    MENU: _MenuFlow,
    LABELLED: _LabelledMenuFlow,
    SINGLE: _SingleFlow,
    INSTANT: _InstantFlow,
    EXTERNAL: _ExternalFlow,
    REJOIN: _RejoinFlow,
    REREAD: _RereadFlow,
    HANDOVER: _HandoverFlow,
    FOLLOWUP: _FollowUpFlow,
}


@pytest.fixture
async def flow_tools(
    hass: HomeAssistant,
    flow_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[IntegrationTools]:
    """``ha_set_integration``'s tools on a ``hass`` with the test integrations."""
    assert await async_setup_component(hass, "config", {})
    answer_translations(hass)
    for domain in _FLOWS:
        strings = (
            {"config": {"error": ERROR_TEXTS, "step": STEP_STRINGS}}
            if domain == STEPS
            else None
        )
        mock_flow_integration(hass, domain, tmp_path, monkeypatch, strings)
    with ExitStack() as stack:
        for domain, flow in _FLOWS.items():
            stack.enter_context(mock_config_flow(domain, flow))
        yield IntegrationTools(flow_client)


@pytest.fixture
def hub(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> MockConfigEntry:
    """An entry of ``STEPS`` with stored options, whose device reports ``MAC``."""
    entry = MockConfigEntry(
        domain=STEPS, title="Hub", options={"string": "Default", "int": 10}
    )
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
    )
    entity_registry.async_get_or_create(
        "switch", STEPS, "relay", config_entry=entry, device_id=device.id
    )
    return entry


@pytest.fixture
def annex(hass: HomeAssistant) -> MockConfigEntry:
    """A second entry of ``STEPS``."""
    entry = MockConfigEntry(domain=STEPS, title="Annex")
    entry.add_to_hass(hass)
    return entry


_FLOW_METHODS = (
    "start_config_flow",
    "submit_config_flow_step",
    "abort_config_flow",
    "start_options_flow",
    "submit_options_flow_step",
    "abort_options_flow",
)


@contextmanager
def flow_calls(client: HomeAssistantClient) -> Iterator[dict[str, AsyncMock]]:
    """Spy on the client's setup and options flow calls; Core still answers
    each one."""
    spies = {name: AsyncMock(wraps=getattr(client, name)) for name in _FLOW_METHODS}
    with ExitStack() as stack:
        for name, spy in spies.items():
            stack.enter_context(patch.object(client, name, spy))
        yield spies


async def start_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Start ``entry``'s reauth flow as Core does when its credentials fail,
    and return the flow_id from the Repairs issue Core raises for it."""
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(
        HOMEASSISTANT_DOMAIN, reauth_issue(entry)
    )
    assert issue is not None
    assert issue.data is not None
    return str(issue.data["flow_id"])


def reauth_issue(entry: MockConfigEntry) -> str:
    """The issue_id of the Repairs issue Core raises for ``entry``'s reauth."""
    return f"config_entry_reauth_{entry.domain}_{entry.entry_id}"


def config_flow_ids(hass: HomeAssistant) -> list[str]:
    """The setup and reconfigure flows open in Core."""
    return [flow["flow_id"] for flow in hass.config_entries.flow.async_progress()]


def snapshot_count(directory: Path) -> int:
    """The auto-backup snapshots written to ``directory``."""
    return len(list(directory.glob("*.yaml")))
