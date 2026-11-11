"""``ha_config_set_helper`` flow helpers and subentries on a real ``hass``.

Each call answers one step of Core's own flow through the server's client
(``_core_client``), so every form, refusal and stored option below is Core's.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

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
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.config_entry_flow import (
    CreationFlowError,
    recreate_flow_helper,
    restore_config_entry_options,
)
from ha_mcp.tools.config_helpers import describe
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

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
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[HelperConfigTools]:
    # ``config`` serves the registry and flow endpoints.
    assert await async_setup_component(hass, "config", {})
    yield HelperConfigTools(core_client)
    # An entry's setup or reload outlives the call that caused it.
    await hass.async_block_till_done()


@pytest.fixture
def component_tools(
    component_core_client: HomeAssistantClient, tools: HelperConfigTools
) -> HelperConfigTools:
    """``tools`` on an install with the ``ha_mcp_tools`` component: its client
    is the shared one, on a ``hass`` that now answers the component's commands."""
    return tools


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


async def test_an_empty_menu_choice_and_flow_id_count_as_not_given(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """A client that fills unused parameters with "" still creates a helper
    whose flow has no menu, instead of being refused for a menu choice."""
    created = await tools.ha_config_set_helper(
        helper_type="min_max", menu_choice="", flow_id="", config=dict(_MIN_MAX)
    )

    assert _options(hass, created["entry_id"])["entity_ids"] == _SOURCES


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


async def test_a_name_given_on_a_later_call_is_checked_for_a_duplicate(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """A flow opened without config has no name to check yet. The call that
    supplies one is refused like a first call would be, instead of creating
    ``..._2`` beside the helper the caller meant."""
    await tools.ha_config_set_helper(helper_type="min_max", config=dict(_MIN_MAX))
    shown = await tools.ha_config_set_helper(helper_type="min_max")

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(
            helper_type="min_max", flow_id=shown["flow_id"], config=dict(_MIN_MAX)
        )

    assert "already exists" in str(exc_info.value)
    assert len(hass.config_entries.async_entries("min_max")) == 1


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


async def test_describe_shows_the_later_forms_core_declares(
    component_tools: HelperConfigTools,
) -> None:
    """The forms after the first are read from Core's own declaration of the
    flow, so they are known before any of them is answered. A form Core
    builds from the earlier answers is named, not guessed."""
    thermostat = await component_tools.ha_config_list_helpers(
        helper_type="generic_thermostat", describe=True
    )
    statistics = await component_tools.ha_config_list_helpers(
        helper_type="statistics", describe=True
    )

    (presets,) = thermostat["later_forms"]
    assert presets["step_id"] == "presets"
    assert "away_temp" in {field["name"] for field in presets["fields"]}
    characteristic, options = statistics["later_forms"]
    assert characteristic == {
        "step_id": "state_characteristic",
        "fields_depend_on_answers": True,
    }
    assert "sampling_size" in {field["name"] for field in options["fields"]}


async def test_steps_core_picks_from_the_answers_are_listed_as_candidates(
    recorder_mock: Any,  # filter depends on recorder; requested before hass
    component_tools: HelperConfigTools,
) -> None:
    """filter's second form depends on the filter chosen on the first. Core's
    declaration does not say which form an answer leads to, so each is a
    candidate and none is presented as certain."""
    described = await component_tools.ha_config_list_helpers(
        helper_type="filter", describe=True
    )

    later = described["later_forms"]
    assert {"lowpass", "outlier"} <= {form["step_id"] for form in later}
    assert all(form.get("may_follow") for form in later)
    assert "unverified candidate" in described["note"]


async def test_a_connection_lost_reading_the_later_forms_still_returns_the_form(
    component_tools: HelperConfigTools,
) -> None:
    """The flow is open by the time its later forms are read. A connection
    lost on that read costs the later forms, not the form and its flow_id."""
    lost = AsyncMock(side_effect=HomeAssistantConnectionError("connection lost"))
    with patch.object(describe, "flow_outline", lost):
        shown = await component_tools.ha_config_set_helper(
            helper_type="generic_thermostat", config=dict(_THERMOSTAT)
        )

    assert shown["step_id"] == "presets"
    assert shown["flow_id"]
    assert any("later forms" in warning for warning in shown["warnings"])


async def test_an_open_form_names_the_later_forms_and_the_last_one_none(
    component_tools: HelperConfigTools,
) -> None:
    """Each form the flow returns says what is still to come from there on."""
    first = await component_tools.ha_config_set_helper(helper_type="generic_thermostat")
    last = await component_tools.ha_config_set_helper(
        helper_type="generic_thermostat",
        flow_id=first["flow_id"],
        config=dict(_THERMOSTAT),
    )

    assert [form["step_id"] for form in first["later_forms"]] == ["presets"]
    assert "config list" in first["note"]
    assert last["step_id"] == "presets"
    assert "later_forms" not in last
    assert "more_forms" not in last


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


async def test_a_null_for_a_key_the_edit_form_lacks_is_cores_to_refuse(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """A null empties a field the form declares. For any other key it is sent
    as written, so a mistyped field name is refused instead of ignored."""
    entry_id = await _template_sensor(tools, unit_of_measurement="W")

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(
            helper_type="template",
            helper_id=entry_id,
            config={"unit_of_measurment": None},
        )

    assert "unit_of_measurment" in json.loads(str(exc_info.value))["error"]["message"]
    assert _options(hass, entry_id)["unit_of_measurement"] == "W"


def _says_duplicates_unchecked(response: dict[str, Any]) -> bool:
    return any("ha_mcp_tools component" in w for w in response.get("warnings", []))


async def test_without_the_component_a_flow_create_says_what_went_unchecked(
    tools: HelperConfigTools,
) -> None:
    """Near-duplicate names are found with Core's slugify, through the
    component. Without it only an exact name is refused."""
    created = await tools.ha_config_set_helper(
        helper_type="min_max", config=dict(_MIN_MAX)
    )

    assert _says_duplicates_unchecked(created)


async def test_with_the_component_a_flow_create_carries_no_duplicate_warning(
    component_tools: HelperConfigTools,
) -> None:
    created = await component_tools.ha_config_set_helper(
        helper_type="min_max", config=dict(_MIN_MAX)
    )

    assert not _says_duplicates_unchecked(created)


async def test_a_refusal_on_a_later_form_shows_that_form(
    tools: HelperConfigTools,
) -> None:
    """generic_thermostat is refused on its second form: the error shows
    that form's fields, not the first form's again (#2632)."""
    shown = await tools.ha_config_set_helper(
        helper_type="generic_thermostat", config=dict(_THERMOSTAT)
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(
            helper_type="generic_thermostat",
            flow_id=shown["flow_id"],
            config={"away_temp": "warm"},
        )

    error = json.loads(str(exc_info.value))
    assert error["step_id"] == "presets"
    names = {field["name"] for field in error["data_schema"]}
    assert "away_temp" in names and "heater" not in names


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
    hass: HomeAssistant, tools: HelperConfigTools, core_client: HomeAssistantClient
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
        core_client,
        entry_id,
        snapshot,
        expected_domain="template",
        fixed_options=current,
    )

    assert _options(hass, entry_id) == snapshot


async def test_a_deleted_menu_helper_is_recreated_from_its_options(
    hass: HomeAssistant, tools: HelperConfigTools, core_client: HomeAssistantClient
) -> None:
    """The snapshot's template_type picks the creation menu's branch."""
    entry_id = await _template_sensor(tools, unit_of_measurement="W")
    snapshot = _options(hass, entry_id)
    await hass.config_entries.async_remove(entry_id)

    recreated = await recreate_flow_helper(core_client, "template", snapshot)

    assert recreated["entry_id"] != entry_id
    assert _options(hass, recreated["entry_id"]) == snapshot


_HALL_LIGHT = {"name": "Hall Light", "state": "{{ true }}", "device_class": "light"}


async def _deleted_hall_light(
    hass: HomeAssistant, tools: HelperConfigTools
) -> dict[str, Any]:
    """The options of a deleted template binary_sensor whose device_class is
    also the name of another template sub-type."""
    created = await tools.ha_config_set_helper(
        helper_type="template", menu_choice="binary_sensor", config=dict(_HALL_LIGHT)
    )
    snapshot = _options(hass, created["entry_id"])
    await hass.config_entries.async_remove(created["entry_id"])
    return snapshot


async def test_a_snapshot_naming_two_menu_branches_is_recreated_on_the_right_one(
    hass: HomeAssistant,
    component_tools: HelperConfigTools,
    component_core_client: HomeAssistantClient,
) -> None:
    """template_type "binary_sensor" and device_class "light" both name a
    template sub-type. Core's declaration of the flow says which is the
    branch: the one whose form takes the snapshot's other options."""
    snapshot = await _deleted_hall_light(hass, component_tools)

    recreated = await recreate_flow_helper(component_core_client, "template", snapshot)

    assert _options(hass, recreated["entry_id"]) == snapshot


async def test_without_the_component_two_possible_branches_are_refused(
    hass: HomeAssistant, tools: HelperConfigTools, core_client: HomeAssistantClient
) -> None:
    """Nothing but Core's declaration tells the two apart, so no branch is
    guessed and no helper of the wrong sub-type is created."""
    snapshot = await _deleted_hall_light(hass, tools)

    with pytest.raises(CreationFlowError):
        await recreate_flow_helper(core_client, "template", snapshot)

    assert hass.config_entries.async_entries("template") == []


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
