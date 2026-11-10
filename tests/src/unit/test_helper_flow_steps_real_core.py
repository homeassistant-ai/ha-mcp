"""``ha_config_set_helper`` flow helpers and subentries on a real ``hass``.

Each call answers one step of Core's own flow through the server's client
(``_core_client``), so every form, refusal and stored option below is Core's.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import probatio
import pytest
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.config_entry_flow import (
    recreate_flow_helper,
    restore_config_entry_options,
)
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._core_client import core_backed_client

_SOURCES = ["sensor.a", "sensor.b"]
_MIN_MAX = {"name": "Lowest", "entity_ids": _SOURCES, "type": "min"}
_THERMOSTAT = {
    "name": "Office",
    "ac_mode": False,
    "target_sensor": "sensor.office_temperature",
    "heater": "switch.office_heater",
}


@pytest.fixture
async def tools(
    hass: HomeAssistant, hass_ws_client: Any, hass_client: Any
) -> AsyncIterator[HelperConfigTools]:
    async with core_backed_client(hass, hass_ws_client, hass_client) as client:
        yield HelperConfigTools(client)
        # An entry's setup or reload outlives the call that caused it.
        await hass.async_block_till_done()


@pytest.fixture
async def component_tools(
    hass: HomeAssistant, hass_ws_client: Any, hass_client: Any
) -> AsyncIterator[HelperConfigTools]:
    """``tools`` on an install with the ``ha_mcp_tools`` component."""
    async with core_backed_client(
        hass, hass_ws_client, hass_client, component=True
    ) as client:
        yield HelperConfigTools(client)
        await hass.async_block_till_done()


def _options(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    entry = hass.config_entries.async_get_entry(entry_id)
    assert entry is not None
    return dict(entry.options)


async def _template_sensor(tools: HelperConfigTools, **fields: Any) -> str:
    created = await tools.ha_config_set_helper(
        helper_type="template",
        menu_choice="sensor",
        config={"name": "Flow Probe", "state": "{{ 1 }}", **fields},
    )
    entry_id: str = created["entry_id"]
    return entry_id


async def test_a_menu_helper_is_created_in_one_call(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """menu_choice answers the opening menu and config the form behind it."""
    created = await tools.ha_config_set_helper(
        helper_type="template",
        menu_choice="sensor",
        config={"name": "Flow Probe", "state": "{{ 1 }}"},
    )

    assert created["action"] == "create"
    assert created["entity_ids"] == ["sensor.flow_probe"]
    options = _options(hass, created["entry_id"])
    assert options["template_type"] == "sensor"
    assert options["state"] == "{{ 1 }}"


async def test_the_top_level_name_goes_only_to_a_form_that_asks_for_one(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """switch_as_x names its entity after the switch and has no name field:
    a name passed with it must not be sent for Core to refuse."""
    source = er.async_get(hass).async_get_or_create("switch", "test", "outlet")

    named = await tools.ha_config_set_helper(
        helper_type="min_max",
        name="Lowest",
        config={"entity_ids": _SOURCES, "type": "min"},
    )
    unnamed = await tools.ha_config_set_helper(
        helper_type="switch_as_x",
        name="Desk lamp",
        config={"entity_id": source.entity_id, "target_domain": "light"},
    )

    assert _options(hass, named["entry_id"])["name"] == "Lowest"
    assert _options(hass, unnamed["entry_id"])["entity_id"] == source.entity_id


async def test_a_call_without_config_returns_cores_form_and_saves_nothing(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    shown = await tools.ha_config_set_helper(helper_type="min_max")

    assert shown["flow_type"] == "form"
    assert {"name", "entity_ids"} <= {field["name"] for field in shown["fields"]}
    assert hass.config_entries.async_entries("min_max") == []

    created = await tools.ha_config_set_helper(
        helper_type="min_max",
        flow_id=shown["flow_id"],
        config=dict(_MIN_MAX),
    )

    assert _options(hass, created["entry_id"])["entity_ids"] == _SOURCES


async def test_a_later_form_is_answered_in_its_own_call(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """A helper with two forms is not saved until the second is answered."""
    presets = await tools.ha_config_set_helper(
        helper_type="generic_thermostat", config=dict(_THERMOSTAT)
    )

    assert presets["flow_type"] == "form"
    assert presets["step_id"] == "presets"
    assert hass.config_entries.async_entries("generic_thermostat") == []

    created = await tools.ha_config_set_helper(
        helper_type="generic_thermostat",
        flow_id=presets["flow_id"],
        config={"away_temp": 16},
    )

    options = _options(hass, created["entry_id"])
    assert options["heater"] == _THERMOSTAT["heater"]
    assert options["away_temp"] == 16


async def test_a_list_answers_every_form_in_one_call(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """Each object of the list is the answer to the form the flow is then at."""
    created = await tools.ha_config_set_helper(
        helper_type="generic_thermostat",
        config=[dict(_THERMOSTAT), {"away_temp": 16}],
    )

    options = _options(hass, created["entry_id"])
    assert options["heater"] == _THERMOSTAT["heater"]
    assert options["away_temp"] == 16


async def test_a_refused_answer_of_a_list_leaves_the_flow_at_its_form(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """The answers before the refused one stand: the flow is continued from
    the form Core refused, not started over."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(
            helper_type="generic_thermostat",
            config=[dict(_THERMOSTAT), {"bogus": 1}],
        )

    error = json.loads(str(exc_info.value))
    assert "bogus" in error["error"]["message"]
    assert hass.config_entries.async_entries("generic_thermostat") == []

    created = await tools.ha_config_set_helper(
        helper_type="generic_thermostat",
        flow_id=error["flow_id"],
        config={"away_temp": 16},
    )
    assert _options(hass, created["entry_id"])["heater"] == _THERMOSTAT["heater"]


async def test_answers_the_flow_never_asked_for_are_reported(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """A one-form helper is created by the first answer: the caller is told
    the second went nowhere instead of taking it as applied."""
    created = await tools.ha_config_set_helper(
        helper_type="min_max", config=[dict(_MIN_MAX), {"type": "max"}]
    )

    assert _options(hass, created["entry_id"])["type"] == "min"
    assert any("before the last 1" in warning for warning in created["warnings"])


async def test_a_flow_is_continued_after_the_server_forgot_it(
    hass: HomeAssistant, component_tools: HelperConfigTools, opened_flows: Any
) -> None:
    """An ha-mcp restart empties its own list of open flows; Home Assistant
    still holds the flow, and its record says what the flow is for."""
    entry_id = await _template_sensor(component_tools)
    create = await component_tools.ha_config_set_helper(helper_type="min_max")
    update = await component_tools.ha_config_set_helper(
        helper_type="template", helper_id=entry_id
    )
    opened_flows.clear()

    created = await component_tools.ha_config_set_helper(
        helper_type="min_max", flow_id=create["flow_id"], config=dict(_MIN_MAX)
    )
    await component_tools.ha_config_set_helper(
        helper_type="template",
        helper_id=entry_id,
        flow_id=update["flow_id"],
        config={"state": "{{ 2 }}"},
    )

    assert _options(hass, created["entry_id"])["entity_ids"] == _SOURCES
    assert _options(hass, entry_id)["state"] == "{{ 2 }}"


async def test_a_forgotten_flow_is_not_continued_for_another_target(
    hass: HomeAssistant, component_tools: HelperConfigTools, opened_flows: Any
) -> None:
    """Core's record binds the flow as the server's own list did: a create
    flow is not answered as the options flow of an existing helper."""
    entry_id = await _template_sensor(component_tools)
    shown = await component_tools.ha_config_set_helper(
        helper_type="template", menu_choice="sensor"
    )
    opened_flows.clear()

    with pytest.raises(ToolError):
        await component_tools.ha_config_set_helper(
            helper_type="template",
            helper_id=entry_id,
            flow_id=shown["flow_id"],
            config={"name": "Other", "state": "{{ 3 }}"},
        )
    with pytest.raises(ToolError):
        await component_tools.ha_config_set_helper(
            helper_type="min_max", flow_id=shown["flow_id"], config=dict(_MIN_MAX)
        )

    assert len(hass.config_entries.async_entries("template")) == 1
    assert hass.config_entries.async_entries("min_max") == []


async def test_without_the_component_a_forgotten_flow_is_refused_unanswered(
    hass: HomeAssistant, tools: HelperConfigTools, opened_flows: Any
) -> None:
    """Core's REST API does not say what a flow is for, so the answer is not
    sent to a flow the server cannot bind to the call's target."""
    shown = await tools.ha_config_set_helper(helper_type="min_max")
    opened_flows.clear()

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(
            helper_type="min_max", flow_id=shown["flow_id"], config=dict(_MIN_MAX)
        )

    assert "ha_mcp_tools component" in str(exc_info.value)
    assert hass.config_entries.async_entries("min_max") == []


async def test_an_option_patch_keeps_the_options_it_leaves_out(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """Core drops a stored optional value an options submit leaves out, so a
    one-field patch goes over the values the form shows (#2254)."""
    entry_id = await _template_sensor(tools, unit_of_measurement="W")

    updated = await tools.ha_config_set_helper(
        helper_type="template", helper_id=entry_id, config={"state": "{{ 2 }}"}
    )

    assert updated["action"] == "update"
    assert updated["title"] == "Flow Probe"
    options = _options(hass, entry_id)
    assert options["state"] == "{{ 2 }}"
    assert options["unit_of_measurement"] == "W"


async def test_a_null_empties_an_option(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    entry_id = await _template_sensor(tools, unit_of_measurement="W")

    await tools.ha_config_set_helper(
        helper_type="template",
        helper_id=entry_id,
        config={"unit_of_measurement": None},
    )

    options = _options(hass, entry_id)
    assert "unit_of_measurement" not in options
    assert options["state"] == "{{ 1 }}"


async def test_a_key_the_form_lacks_is_refused_by_core_and_the_flow_stays_open(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(
            helper_type="min_max", config={**_MIN_MAX, "bogus": 1}
        )

    error = json.loads(str(exc_info.value))
    assert "bogus" in error["error"]["message"]
    assert hass.config_entries.async_entries("min_max") == []

    created = await tools.ha_config_set_helper(
        helper_type="min_max", flow_id=error["flow_id"], config=dict(_MIN_MAX)
    )
    assert _options(hass, created["entry_id"])["entity_ids"] == _SOURCES


async def test_a_form_core_rejects_is_an_error_that_saves_nothing(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(
            helper_type="generic_thermostat",
            config={**_THERMOSTAT, "min_temp": 30, "max_temp": 10},
        )

    error = json.loads(str(exc_info.value))
    assert "base" in error["errors"]
    assert error["flow_id"]
    assert hass.config_entries.async_entries("generic_thermostat") == []


async def test_an_aborted_flow_is_gone_from_core_and_cannot_be_continued(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    shown = await tools.ha_config_set_helper(helper_type="min_max")

    aborted = await tools.ha_config_set_helper(
        helper_type="min_max", flow_id=shown["flow_id"], abort=True
    )

    assert aborted["aborted"] is True
    assert hass.config_entries.flow.async_progress_by_handler("min_max") == []
    with pytest.raises(ToolError):
        await tools.ha_config_set_helper(
            helper_type="min_max", flow_id=shown["flow_id"], config={"name": "x"}
        )


async def test_a_create_flow_is_not_continued_as_another_helpers_update(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """A flow_id continues only the flow it was returned for: answering a
    create flow with a helper_id would write the form to that flow unchecked."""
    entry_id = await _template_sensor(tools)
    shown = await tools.ha_config_set_helper(
        helper_type="template", menu_choice="sensor"
    )

    with pytest.raises(ToolError):
        await tools.ha_config_set_helper(
            helper_type="template",
            helper_id=entry_id,
            flow_id=shown["flow_id"],
            config={"name": "Other", "state": "{{ 3 }}"},
        )

    assert len(hass.config_entries.async_entries("template")) == 1
    assert _options(hass, entry_id)["state"] == "{{ 1 }}"


async def test_the_area_is_applied_by_the_call_that_finishes_the_flow(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    area = ar.async_get(hass).async_create("Lab")

    presets = await tools.ha_config_set_helper(
        helper_type="generic_thermostat", config=dict(_THERMOSTAT), area_id=area.id
    )
    assert any("area_id" in warning for warning in presets["warnings"])

    created = await tools.ha_config_set_helper(
        helper_type="generic_thermostat",
        flow_id=presets["flow_id"],
        config={},
        area_id=area.id,
    )

    (entity_id,) = created["entity_ids"]
    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None
    assert entity.area_id == area.id


async def test_an_options_snapshot_is_restored_through_cores_options_flow(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """A backup restore puts back what the snapshot holds and clears what it
    lacks, as the options form does."""
    entry_id = await _template_sensor(tools)
    snapshot = _options(hass, entry_id)
    await tools.ha_config_set_helper(
        helper_type="template",
        helper_id=entry_id,
        config={"state": "{{ 2 }}", "unit_of_measurement": "W"},
    )
    current = _options(hass, entry_id)

    await restore_config_entry_options(
        tools._client,
        entry_id,
        snapshot,
        expected_domain="template",
        fixed_options=current,
    )

    assert _options(hass, entry_id) == snapshot


async def test_a_deleted_menu_helper_is_recreated_from_its_options(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """The snapshot's template_type picks the creation menu's branch."""
    entry_id = await _template_sensor(tools, unit_of_measurement="W")
    snapshot = _options(hass, entry_id)
    await hass.config_entries.async_remove(entry_id)

    recreated = await recreate_flow_helper(tools._client, "template", snapshot)

    assert recreated["entry_id"] != entry_id
    assert _options(hass, recreated["entry_id"]) == snapshot


_SUBENTRY_DOMAIN = "flow_steps_test"


class _SensorSubentryFlow(ConfigSubentryFlow):
    """A subentry whose reconfigure form shows its stored values."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.pop("name"), data=user_input
            )
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required("name"): str,
                    probatio.Required("state"): int,
                    probatio.Optional("unit"): str,
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(), subentry, data=user_input
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(
                    {
                        probatio.Optional("state"): int,
                        probatio.Optional("unit"): str,
                    }
                ),
                subentry.data,
            ),
        )


class _SubentryParentFlow(ConfigFlow):
    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"sensor": _SensorSubentryFlow}


@pytest.fixture
def parent_entry(hass: HomeAssistant) -> Iterator[MockConfigEntry]:
    """A config entry of an integration with a ``sensor`` subentry type."""
    mock_integration(hass, MockModule(_SUBENTRY_DOMAIN))
    mock_platform(hass, f"{_SUBENTRY_DOMAIN}.config_flow", None)
    entry = MockConfigEntry(domain=_SUBENTRY_DOMAIN)
    entry.add_to_hass(hass)
    with mock_config_flow(_SUBENTRY_DOMAIN, _SubentryParentFlow):
        yield entry


async def test_a_subentry_is_created_and_then_reconfigured_as_a_patch(
    tools: HelperConfigTools, parent_entry: MockConfigEntry
) -> None:
    """The created subentry is named by its id, and reconfiguring it with one
    field keeps the other the form shows."""
    created = await tools.ha_config_set_helper(
        helper_type="config_subentry",
        entry_id=parent_entry.entry_id,
        subentry_type="sensor",
        config={"name": "Probe", "state": 15, "unit": "W"},
    )

    assert created["operation"] == "created"
    subentry_id = created["subentry_id"]
    assert parent_entry.subentries[subentry_id].data == {"state": 15, "unit": "W"}

    updated = await tools.ha_config_set_helper(
        helper_type="config_subentry",
        entry_id=parent_entry.entry_id,
        subentry_type="sensor",
        subentry_id=subentry_id,
        config={"state": 20},
    )

    assert updated["operation"] == "reconfigured"
    assert parent_entry.subentries[subentry_id].data == {"state": 20, "unit": "W"}


async def test_a_forgotten_subentry_flow_is_continued_for_its_own_subentry_only(
    component_tools: HelperConfigTools,
    parent_entry: MockConfigEntry,
    opened_flows: Any,
) -> None:
    """A subentry's reconfigure flow is bound to that subentry by Core's
    record: it is not answered as the flow that creates a new one."""
    subentry = {
        "helper_type": "config_subentry",
        "entry_id": parent_entry.entry_id,
        "subentry_type": "sensor",
    }
    created = await component_tools.ha_config_set_helper(
        **subentry, config={"name": "Probe", "state": 15, "unit": "W"}
    )
    subentry_id = created["subentry_id"]
    shown = await component_tools.ha_config_set_helper(
        **subentry, subentry_id=subentry_id
    )
    opened_flows.clear()

    with pytest.raises(ToolError):
        await component_tools.ha_config_set_helper(
            **subentry, flow_id=shown["flow_id"], config={"state": 99}
        )
    await component_tools.ha_config_set_helper(
        **subentry,
        subentry_id=subentry_id,
        flow_id=shown["flow_id"],
        config={"state": 20},
    )

    assert parent_entry.subentries[subentry_id].data == {"state": 20, "unit": "W"}
