"""ha_config_list_helpers(describe=True): helper fields read from HA (#2632).

Every form, menu, stored item and help text below is Core's: the server's
client (``core_client``) talks to a real ``hass``. Where no helper of Core's
own has the shape a test needs (a secret field, an options flow that opens
with a menu), a helper integration registered with Core's ``mock_integration``
provides it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.components import websocket_api
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)
from homeassistant.helpers.translation import async_get_translations
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
from ha_mcp.read_only import read_only_request
from ha_mcp.tools.config_entry_flow_introspect import abort_probe_flow
from ha_mcp.tools.config_helpers.describe import MAX_OPTIONS, describe_helper
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._core_setup import with_config_component
from ._helper_tool_core import create_item, set_up
from ._template_helper_core import create_template_sensor, state_template


def _fields(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in result["fields"]}


@pytest.fixture
def tools(hass: HomeAssistant, core_client: HomeAssistantClient) -> HelperConfigTools:
    """The tools on an install without the component. Core's translations
    command is registered on its own: the frontend cannot be set up here."""
    websocket_api.async_register_command(hass, websocket_get_translations)
    return HelperConfigTools(core_client)


@pytest.fixture
async def flow_tools(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> HelperConfigTools:
    """``tools`` on a ``hass`` whose ``config`` serves the flow and
    config-entry endpoints."""
    await with_config_component(hass, core_client)
    return tools


async def _describe(tools: HelperConfigTools, **kwargs: Any) -> dict[str, Any]:
    return await tools.ha_config_list_helpers(describe=True, **kwargs)


# --- storage helpers ---------------------------------------------------------


async def test_storage_helper_fields_come_from_core_when_component_present(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """With the component, the field list is Core's; a field whose validator
    Core cannot serialize is named as untyped, not typed by hand."""
    await set_up(hass, "schedule")

    result = await _describe(
        HelperConfigTools(component_core_client), helper_type="schedule"
    )

    assert result["source"] == "core_schema"
    fields = _fields(result)
    assert fields["name"]["required"] is True
    # Core's schedule-day validator does not serialize: named, not typed by hand.
    assert "type" not in fields["monday"]
    assert "monday" in result["untyped_fields"]


async def test_storage_helper_without_component_gets_no_made_up_field_list(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """Core has no command that lists a storage helper's fields: without the
    component the answer says so instead of offering a list that can go stale."""
    await set_up(hass, "input_number")

    result = await _describe(tools, helper_type="input_number")

    assert result["source"] == "unavailable"
    assert "fields" not in result
    assert "ha_mcp_tools component" in result["note"]


async def _pool(hass: HomeAssistant, client: HomeAssistantClient) -> str:
    await set_up(hass, "input_number")
    return await create_item(client, "input_number", {"min": 0, "max": 40})


async def test_existing_storage_helper_reports_current_values_without_component(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """Editing an existing helper starts from its stored values: without a
    field list from Core, the item is returned as Core stores it."""
    item_id = await _pool(hass, core_client)

    result = await _describe(tools, helper_type="input_number", helper_id=item_id)

    assert result["stored"]["max"] == 40


async def test_helper_named_by_entity_id_reports_current_values_without_component(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    tools: HelperConfigTools,
) -> None:
    """The entity_id ha_config_set_helper returns finds the stored item too,
    also after the entity was renamed away from the storage id."""
    item_id = await _pool(hass, core_client)
    entity_id = entity_registry.async_get_entity_id(
        "input_number", "input_number", item_id
    )
    assert entity_id is not None
    entity_registry.async_update_entity(
        entity_id, new_entity_id="input_number.pool_temperature"
    )

    result = await _describe(
        tools, helper_type="input_number", helper_id="input_number.pool_temperature"
    )

    assert result["stored"]["max"] == 40


async def test_existing_person_reports_current_values_without_component(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """person/list nests the editable persons under "storage"."""
    await set_up(hass, "person")
    item_id = await create_item(
        core_client, "person", {"device_trackers": ["device_tracker.pat_phone"]}
    )

    result = await _describe(tools, helper_type="person", helper_id=item_id)

    assert result["stored"]["device_trackers"] == ["device_tracker.pat_phone"]


async def test_unknown_storage_helper_is_an_error_not_a_field_list(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    """As the flow path refuses a wrong entry, a storage id that matches nothing
    is refused rather than described without current values."""
    await set_up(hass, "input_number")

    with pytest.raises(ToolError, match="RESOURCE_NOT_FOUND"):
        await _describe(tools, helper_type="input_number", helper_id="gone")


async def test_read_only_still_describes_a_storage_helper_type(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    await set_up(hass, "input_number")

    with read_only_request():
        result = await _describe(tools, helper_type="input_number")

    assert result["source"] == "unavailable"


async def test_describe_only_parameters_are_refused_without_describe(
    tools: HelperConfigTools,
) -> None:
    with pytest.raises(ToolError, match="apply only with describe=True"):
        await tools.ha_config_list_helpers(helper_type="counter", helper_id="c1")


# --- menu_choice -------------------------------------------------------------


@pytest.mark.parametrize(
    ("helper_type", "existing"),
    [
        ("input_number", False),  # a storage helper has no menu
        ("template", True),  # an existing helper: its options
        ("derivative", False),  # a flow helper that opens with a form
    ],
)
async def test_menu_choice_without_a_menu_is_refused_not_ignored(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: HelperConfigTools,
    helper_type: str,
    existing: bool,
) -> None:
    kwargs = {}
    if existing:
        kwargs["helper_id"] = (await create_template_sensor(hass, "1")).entry_id

    with (
        patch.object(
            core_client, "start_options_flow", wraps=core_client.start_options_flow
        ) as options_flow,
        pytest.raises(ToolError, match="menu_choice 'sensor' does not apply"),
    ):
        await _describe(
            flow_tools, helper_type=helper_type, menu_choice="sensor", **kwargs
        )

    options_flow.assert_not_awaited()


@pytest.mark.parametrize(
    ("helper_type", "suggestion"),
    [
        ("counter", "Drop menu_choice"),
        ("template", "Drop menu_choice, or pass one of menu_options"),
    ],
    ids=["storage-helper", "choice-not-offered"],
)
async def test_a_refused_menu_choice_points_at_menu_options_only_with_a_menu(
    flow_tools: HelperConfigTools, helper_type: str, suggestion: str
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await _describe(flow_tools, helper_type=helper_type, menu_choice="sensors")

    assert json.loads(str(exc_info.value))["error"]["suggestion"] == suggestion


async def test_flow_helper_menu_lists_sub_types_until_one_is_chosen(
    hass: HomeAssistant, flow_tools: HelperConfigTools
) -> None:
    """A menu-rooted helper names its sub-types instead of failing, and the
    flow opened to read the menu is not left open."""
    result = await _describe(flow_tools, helper_type="template")

    assert {"sensor", "binary_sensor"} <= set(result["menu_options"])
    assert "fields" not in result
    assert result["note"] == "Pass menu_choice to describe one of these sub-types."
    assert hass.config_entries.flow.async_progress() == []


@pytest.mark.parametrize(
    ("helper_type", "menu_choice", "failing", "match"),
    [
        ("template", "sensor", "submit_config_flow_step", "with menu_choice 'sensor'"),
        ("derivative", None, "start_config_flow", "no setup form for derivative"),
    ],
    ids=["branch-rejected", "flow-not-started"],
)
async def test_flow_that_cannot_be_read_is_an_error_not_an_empty_field_list(
    core_client: HomeAssistantClient,
    flow_tools: HelperConfigTools,
    helper_type: str,
    menu_choice: str | None,
    failing: str,
    match: str,
) -> None:
    """A flow HA does not open or answer is an error: the agent must not be
    told the helper has no fields."""
    lost = AsyncMock(side_effect=HomeAssistantConnectionError("connection lost"))
    with (
        patch.object(core_client, failing, lost),
        pytest.raises(ToolError, match=match),
    ):
        await _describe(flow_tools, helper_type=helper_type, menu_choice=menu_choice)


# --- forms of Core's own helper flows -----------------------------------------


@pytest.mark.usefixtures("recorder_mock")  # filter needs the recorder
@pytest.mark.parametrize(
    ("helper_type", "more_forms"),
    [
        ("statistics", "yes"),  # Core declares the next form
        ("filter", "depends_on_answers"),  # Core picks it from the answers
        ("derivative", None),  # a single form
    ],
)
async def test_flow_helper_says_when_more_forms_follow(
    flow_tools: HelperConfigTools, helper_type: str, more_forms: str | None
) -> None:
    """A statistics helper asks for its characteristic on a second form; the
    agent learns that instead of taking the first form for all of it."""
    result = await _describe(flow_tools, helper_type=helper_type)

    assert result.get("more_forms") == more_forms
    assert ("note" in result) is (more_forms is not None)
    if more_forms is not None:
        assert ("did not say whether more forms follow" in result["note"]) is (
            more_forms == "depends_on_answers"
        )
        assert ("asks for more fields" in result["note"]) is (more_forms == "yes")


async def _thermostat(hass: HomeAssistant) -> str:
    """A generic_thermostat entry, made by Core's flow."""
    result = await hass.config_entries.flow.async_init(
        "generic_thermostat", context={"source": "user"}
    )
    for answer in (
        {
            "name": "Office",
            "ac_mode": False,
            "target_sensor": "sensor.office_temperature",
            "heater": "switch.office_heater",
        },
        {},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], answer
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return str(result["result"].entry_id)


async def test_existing_flow_helper_with_a_later_options_form_says_so(
    hass: HomeAssistant, flow_tools: HelperConfigTools
) -> None:
    entry_id = await _thermostat(hass)

    result = await _describe(
        flow_tools, helper_type="generic_thermostat", helper_id=entry_id
    )

    assert result["more_forms"] == "yes"


@pytest.mark.parametrize(
    ("menu_choice", "existing"),
    [("sensor", False), (None, False), (None, True)],
    ids=["setup-menu", "setup", "existing"],
)
async def test_read_only_still_describes_a_flow_based_helper(
    hass: HomeAssistant,
    flow_tools: HelperConfigTools,
    menu_choice: str | None,
    existing: bool,
) -> None:
    helper_id = (await create_template_sensor(hass, "1")).entry_id if existing else None

    with read_only_request():
        result = await _describe(
            flow_tools,
            helper_type="template",
            menu_choice=menu_choice,
            helper_id=helper_id,
        )

    if menu_choice or existing:
        assert "state" in _fields(result)
    else:
        assert "sensor" in result["menu_options"]
    assert result["source"] == ("options_flow" if existing else "config_flow")


async def test_entry_of_another_helper_type_is_refused_without_opening_its_flow(
    hass: HomeAssistant, core_client: HomeAssistantClient, flow_tools: HelperConfigTools
) -> None:
    """Describing a template's entry as a utility meter must not show its options."""
    entry = await create_template_sensor(hass, "1")

    with (
        patch.object(
            core_client, "start_options_flow", wraps=core_client.start_options_flow
        ) as options_flow,
        pytest.raises(ToolError, match="belongs to domain 'template'"),
    ):
        await _describe(
            flow_tools, helper_type="utility_meter", helper_id=entry.entry_id
        )

    options_flow.assert_not_awaited()


async def test_existing_flow_helper_reads_current_values_and_aborts_options_flow(
    hass: HomeAssistant, flow_tools: HelperConfigTools
) -> None:
    """An options flow opened only to read values is never left open."""
    entry = await create_template_sensor(hass, "1 + 1")

    result = await _describe(
        flow_tools, helper_type="template", helper_id=entry.entry_id
    )

    assert _fields(result)["state"]["current"] == state_template("1 + 1")
    assert hass.config_entries.options.async_progress() == []


async def test_flow_fields_carry_ha_own_help_text_including_sections(
    hass: HomeAssistant, flow_tools: HelperConfigTools
) -> None:
    """The model sees the same per-field help the HA UI shows, with no doc to
    maintain, also for a field inside a collapsed section."""
    result = await _describe(flow_tools, helper_type="template", menu_choice="sensor")
    fields = _fields(result)

    strings = await async_get_translations(hass, "en", "config", ["template"])
    step = "component.template.config.step.sensor"
    assert (result["step_id"], result["menu_choice"]) == ("sensor", "sensor")
    assert fields["state"]["description"] == strings[f"{step}.data_description.state"]
    (availability,) = fields["additional_options"]["fields"]
    assert availability["name"] == "availability"
    assert (
        availability["description"]
        == strings[f"{step}.sections.additional_options.data_description.availability"]
    )


async def test_flow_fields_still_described_when_translations_fail(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Help text is best-effort: a translations read Core refuses leaves it
    out and says so, and the fields are still described."""
    client = await with_config_component(hass, core_client)

    result = await _describe(HelperConfigTools(client), helper_type="derivative")

    assert "source" in _fields(result)
    assert "description" not in _fields(result)["source"]
    assert result["warnings"]


async def test_flow_field_without_help_text_gets_its_ui_label(
    hass: HomeAssistant, flow_tools: HelperConfigTools
) -> None:
    """A field HA documents only by its label still says what it is."""
    result = await _describe(flow_tools, helper_type="derivative")

    strings = await async_get_translations(hass, "en", "config", ["derivative"])
    step = "component.derivative.config.step.user"
    assert f"{step}.data_description.source" not in strings
    assert _fields(result)["source"]["description"] == strings[f"{step}.data.source"]


# --- shapes no Core helper has, from a helper integration of the test's own ----

_DOMAIN = "helper_describe_probe"
_UNITS = [f"u{i}" for i in range(175)]
_CHARACTERISTICS = [f"characteristic_{i}" for i in range(27)]


class _ProbeOptionsFlow(OptionsFlow):
    """Opens with what the entry's ``opens_with`` option names."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self.config_entry.options
        if options["opens_with"] == "abort":
            return self.async_abort(reason="not_loaded")
        if options["opens_with"] == "menu":
            return self.async_show_menu(step_id="init", menu_options=options["menu"])
        if options["opens_with"] == "progress":
            # Never finishes: Core cancels it when the flow is aborted, and
            # Core's stop when it is not, so a flow left open fails the test.
            waiting = self.hass.async_create_background_task(
                asyncio.Event().wait(), "probe progress"
            )
            return self.async_show_progress(
                step_id="init", progress_action="wait", progress_task=waiting
            )
        schema = probatio.Schema(
            {
                probatio.Secret("api_key"): TextSelector(),
                probatio.Secret("password"): str,
                probatio.Optional("name"): str,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, options),
        )


class _ProbeFlow(ConfigFlow):
    """A menu whose ``finish`` saves the helper and whose ``selects`` is a
    form with a long suggestion list and a long fixed list."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["finish", "selects"])

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_create_entry(title="Hall", data={})

    async def async_step_selects(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="selects",
            data_schema=probatio.Schema(
                {
                    probatio.Optional("unit_of_measurement"): SelectSelector(
                        SelectSelectorConfig(options=_UNITS, custom_value=True)
                    ),
                    probatio.Optional("state_characteristic"): SelectSelector(
                        SelectSelectorConfig(options=_CHARACTERISTICS)
                    ),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> _ProbeOptionsFlow:
        return _ProbeOptionsFlow()


async def _set_up_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True


@pytest.fixture
async def probe(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[HomeAssistantClient]:
    """``core_client`` on a ``hass`` with the probe helper integration."""
    await with_config_component(hass, core_client)
    mock_integration(hass, MockModule(_DOMAIN, async_setup_entry=_set_up_entry))
    mock_platform(hass, f"{_DOMAIN}.config_flow", None)
    with mock_config_flow(_DOMAIN, _ProbeFlow):
        yield core_client
        await hass.async_block_till_done()


def _probe_entry(hass: HomeAssistant, **options: Any) -> str:
    entry = MockConfigEntry(domain=_DOMAIN, options=options)
    entry.add_to_hass(hass)
    return entry.entry_id


@pytest.fixture
def secret_entry(hass: HomeAssistant) -> str:
    return _probe_entry(
        hass, opens_with="form", api_key="hunter2", password="swordfish", name="Hall"
    )


@pytest.mark.usefixtures("redact_on")
async def test_current_values_of_a_password_field_are_redacted(
    probe: HomeAssistantClient, secret_entry: str
) -> None:
    result = await describe_helper(probe, _DOMAIN, helper_id=secret_entry)
    fields = _fields(result)

    assert "hunter2" not in str(fields)
    assert fields["api_key"]["current"].startswith("<redacted")
    assert fields["name"]["current"] == "Hall"


@pytest.mark.parametrize(
    "name", ["password", "api_key"], ids=["serialized", "selector"]
)
async def test_core_secret_fields_stay_recognisable_as_secrets(
    probe: HomeAssistantClient, secret_entry: str, name: str
) -> None:
    """A field Core marks secret (#3002) is shown as one, so the model knows a
    redacted value is withheld rather than empty."""
    result = await describe_helper(probe, _DOMAIN, helper_id=secret_entry)

    assert _fields(result)[name]["secret"] is True


async def test_a_menu_choice_that_finishes_the_helper_flow_is_reported_as_saved(
    hass: HomeAssistant, probe: HomeAssistantClient
) -> None:
    result = await describe_helper(probe, _DOMAIN, menu_choice="finish")

    assert result["flow_type"] == "create_entry"
    assert result["menu_choice"] == "finish"
    assert result["title"] == "Hall"
    assert "fields" not in result
    assert [e.title for e in hass.config_entries.async_entries(_DOMAIN)] == ["Hall"]


@pytest.mark.parametrize(
    "menu", [["a", "b"], {"a": "Label A", "b": "Label B"}], ids=["list", "labelled"]
)
async def test_options_flow_menu_lists_its_choices(
    hass: HomeAssistant, probe: HomeAssistantClient, menu: Any
) -> None:
    """Core sends a menu as step ids or as ``{step_id: label}``: both list the
    step ids, and the flow opened to read them is not left open."""
    entry_id = _probe_entry(hass, opens_with="menu", menu=menu)

    result = await describe_helper(probe, _DOMAIN, helper_id=entry_id)

    assert result["menu_options"] == ["a", "b"]
    assert result["note"].startswith("This helper's options flow opens with a menu")
    assert hass.config_entries.options.async_progress() == []


async def test_an_options_flow_abort_is_reported_with_its_reason(
    hass: HomeAssistant, probe: HomeAssistantClient
) -> None:
    entry_id = _probe_entry(hass, opens_with="abort")

    result = await describe_helper(probe, _DOMAIN, helper_id=entry_id)

    assert (result["flow_type"], result["aborted"]) == ("abort", "not_loaded")
    assert "fields" not in result


async def test_an_options_flow_without_a_form_is_an_error(
    hass: HomeAssistant, probe: HomeAssistantClient
) -> None:
    """A progress step has no form: the call fails, and the flow it opened is
    aborted rather than left running."""
    entry_id = _probe_entry(hass, opens_with="progress")

    with pytest.raises(ToolError, match="reached a 'progress' step"):
        await describe_helper(probe, _DOMAIN, helper_id=entry_id)

    assert hass.config_entries.options.async_progress() == []


async def test_long_suggestion_lists_are_truncated_but_say_how_many_remain(
    probe: HomeAssistantClient,
) -> None:
    """A 175-unit dropdown that also takes a custom value must not flood a
    small model's context."""
    result = await describe_helper(probe, _DOMAIN, menu_choice="selects")
    options = _fields(result)["unit_of_measurement"]["options"]

    assert len(options) <= MAX_OPTIONS + 1
    remaining = re.fullmatch(r"\.\.\.(\d+) more", options[-1])
    assert remaining is not None
    assert len(options) - 1 + int(remaining.group(1)) == len(_UNITS)


async def test_a_select_that_must_be_answered_from_its_list_shows_every_choice(
    probe: HomeAssistantClient,
) -> None:
    """A statistics helper's 27 characteristics were cut to 12, which hid
    ``mean`` from the agent choosing one."""
    result = await describe_helper(probe, _DOMAIN, menu_choice="selects")

    assert _fields(result)["state_characteristic"]["options"] == _CHARACTERISTICS


# --- the probe flow's abort ----------------------------------------------------


@pytest.mark.parametrize(("quiet", "level"), [(False, "WARNING"), (True, "DEBUG")])
async def test_a_failed_probe_abort_logs_at_debug_when_quiet(
    quiet: bool, level: str, caplog: pytest.LogCaptureFixture
) -> None:
    abort = AsyncMock(side_effect=HomeAssistantConnectionError("ws down"))

    with caplog.at_level(
        logging.DEBUG, logger="ha_mcp.tools.config_entry_flow_introspect"
    ):
        await abort_probe_flow(abort, "f1", quiet=quiet)

    assert [r.levelname for r in caplog.records if "f1" in r.getMessage()] == [level]
