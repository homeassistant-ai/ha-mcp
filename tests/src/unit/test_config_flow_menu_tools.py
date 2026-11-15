"""The setup menu, the tools branch and the tools entry's options flow.

One config flow serves two entry types: the ``user`` step is a menu, ``tools``
creates the File & YAML Tools entry, and the options flow follows the entry's
type. Every step runs through Core's flow managers.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ha_mcp_tools import (
    SERVICE_GET_ALLOWED_PATHS,
    SERVICE_GET_EXTRA_YAML_KEYS,
    SERVICE_SET_ALLOWED_PATHS,
    SERVICE_SET_EXTRA_YAML_KEYS,
    config_flow,
)
from custom_components.ha_mcp_tools.const import (
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    TOOLS_ENTRY_TITLE,
)

from ._config_flow_core import (
    schema_markers,
    start_config_flow,
    start_entry_type_flow,
    start_options_flow,
    submit_config_step,
    submit_options_step,
)
from ._file_services import make_file_service


async def test_setup_menu_offers_the_server_entry_first(hass: HomeAssistant) -> None:
    """The recommended server entry leads the menu; the optional tools entry follows."""
    menu = await start_config_flow(hass)

    assert menu["type"] is FlowResultType.MENU
    assert menu["step_id"] == "user"
    assert menu["menu_options"] == [ENTRY_TYPE_SERVER, ENTRY_TYPE_TOOLS]


async def test_tools_branch_confirms_then_creates_the_services_entry(
    hass: HomeAssistant,
) -> None:
    """Choosing tools shows a confirm form; submitting it adds the services entry."""
    form = await start_entry_type_flow(hass, ENTRY_TYPE_TOOLS)
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "tools"

    created = await submit_config_step(hass, form["flow_id"], {})

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["title"] == TOOLS_ENTRY_TITLE
    assert created["data"] == {CONF_ENTRY_TYPE: ENTRY_TYPE_TOOLS}
    [entry] = hass.config_entries.async_entries(DOMAIN)
    assert entry.title == TOOLS_ENTRY_TITLE


async def test_a_second_tools_entry_is_refused(hass: HomeAssistant) -> None:
    """The services entry is single-instance: adding it twice aborts the second."""
    first = await start_entry_type_flow(hass, ENTRY_TYPE_TOOLS)
    await submit_config_step(hass, first["flow_id"], {})

    second = await start_entry_type_flow(hass, ENTRY_TYPE_TOOLS)

    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_only_the_server_branch_needs_a_current_home_assistant(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On an older Home Assistant the server entry is refused, the tools entry is not."""
    monkeypatch.setattr(config_flow, "HA_VERSION", "2024.11.0")

    server = await start_entry_type_flow(hass, ENTRY_TYPE_SERVER)
    assert server["type"] is FlowResultType.ABORT
    assert server["reason"] == "unsupported_home_assistant"

    tools = await start_entry_type_flow(hass, ENTRY_TYPE_TOOLS)
    created = await submit_config_step(hass, tools["flow_id"], {})
    assert created["type"] is FlowResultType.CREATE_ENTRY


async def test_tools_options_form_shows_the_stored_directories_and_keys(
    hass: HomeAssistant,
) -> None:
    """The tools entry's options form is editable and pre-filled with what is stored."""
    call = await make_file_service(hass)
    await call(SERVICE_SET_ALLOWED_PATHS, {"paths": ["pyscript"]})
    await call(SERVICE_SET_EXTRA_YAML_KEYS, {"keys": ["alert2"]})
    [entry] = hass.config_entries.async_entries(DOMAIN)

    form = await start_options_flow(hass, entry)

    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "tools_info"
    markers = schema_markers(form)
    assert set(markers) == {"allowed_dirs", "extra_yaml_keys"}
    assert markers["allowed_dirs"].default() == ["pyscript"]
    assert markers["extra_yaml_keys"].default() == ["alert2"]


async def test_tools_options_submit_saves_the_validated_directories_and_keys(
    hass: HomeAssistant,
) -> None:
    """Saving the form stores both lists through the services' validated path."""
    call = await make_file_service(hass)
    [entry] = hass.config_entries.async_entries(DOMAIN)
    form = await start_options_flow(hass, entry)

    result = await submit_options_step(
        hass,
        form["flow_id"],
        {"allowed_dirs": ["pyscript", "../escape"], "extra_yaml_keys": ["alert2"]},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {}
    paths = await call(SERVICE_GET_ALLOWED_PATHS, {})
    keys = await call(SERVICE_GET_EXTRA_YAML_KEYS, {})
    assert paths["paths"] == ["pyscript"]
    assert keys["keys"] == ["alert2"]


async def test_tools_options_submit_without_input_shows_the_form_again(
    hass: HomeAssistant,
) -> None:
    """A submit with no payload re-renders the form and saves nothing."""
    call = await make_file_service(hass)
    await call(SERVICE_SET_ALLOWED_PATHS, {"paths": ["pyscript"]})
    [entry] = hass.config_entries.async_entries(DOMAIN)
    form = await start_options_flow(hass, entry)

    again = await hass.config_entries.options.async_configure(form["flow_id"])

    assert again["type"] is FlowResultType.FORM
    assert again["step_id"] == "tools_info"
    assert (await call(SERVICE_GET_ALLOWED_PATHS, {}))["paths"] == ["pyscript"]
