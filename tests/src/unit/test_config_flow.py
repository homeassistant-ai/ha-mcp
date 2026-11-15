"""The config flow: setup menu, tools branch and both entries' options flows.

One config flow serves two entry types: the ``user`` step is a menu, ``tools``
creates the File & YAML Tools entry, and the options flow follows the entry's
type. Every step runs through Core's flow managers, so submitted input passes
the schema Core holds for the shown form, as it does for a user. The second
half covers the server entry's options form: what it shows and what it saves.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import TextSelector, TextSelectorType

from custom_components.ha_mcp_tools import (
    SERVICE_GET_ALLOWED_PATHS,
    SERVICE_GET_EXTRA_YAML_KEYS,
    SERVICE_SET_ALLOWED_PATHS,
    SERVICE_SET_EXTRA_YAML_KEYS,
    config_flow,
    server_credentials,
)
from custom_components.ha_mcp_tools.const import (
    BIND_HOST_ALL,
    BIND_HOST_LOOPBACK,
    CONF_ENTRY_TYPE,
    DEFAULT_LOOPBACK_URL,
    DEFAULT_PIP_SPEC,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    EXPOSURE_FULL,
    OPT_ACCESS_OFF_AFTER_RESTART,
    OPT_ADMIN_TOKEN_REPLACEMENT,
    OPT_BIND_HOST,
    OPT_ENABLE_LLM_API,
    OPT_ENABLE_SIDEBAR_PANEL,
    OPT_ENABLE_STARTUP_NOTIFICATION,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_LLM_API_EXPOSURE,
    OPT_OAUTH_ALLOW_ANY_CALLBACK,
    OPT_OAUTH_CLIENT_ID,
    OPT_OAUTH_CLIENT_SECRET,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
    OPT_OAUTH_REGENERATE,
    OPT_PIP_SPEC,
    OPT_REGENERATE_SECRETS,
    OPT_SECRET_PATH_OVERRIDE,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    OPT_WEBHOOK_AUTH,
    OPT_WEBHOOK_ID_OVERRIDE,
    TOOLS_ENTRY_TITLE,
    WEBHOOK_AUTH_HA,
)

from ._config_flow_core import (
    FlowResultDict,
    add_options_entry,
    open_options_form,
    schema_markers,
    schema_validators,
    shown_value,
    start_config_flow,
    start_entry_type_flow,
    start_options_flow,
    submit_config_step,
    submit_options_step,
)
from ._file_services import make_file_service

CONNECT_PATH_FIELDS = [OPT_SECRET_PATH_OVERRIDE, OPT_WEBHOOK_ID_OVERRIDE]
CLEARABLE = {
    OPT_PIP_SPEC: "ha-mcp @ https://example/x.tgz",
    OPT_SERVER_URL: "https://ha.example:8123",
    OPT_EXTERNAL_URL: "https://ha.example.com",
    OPT_WEBHOOK_ID_OVERRIDE: "my_custom_hook",
    OPT_SECRET_PATH_OVERRIDE: "/custom_path",
}


async def _save(hass: HomeAssistant, user_input: dict[str, Any]) -> FlowResultDict:
    """Open a fresh server options form and submit ``user_input``."""
    form = await open_options_form(hass)
    return await submit_options_step(hass, form["flow_id"], user_input)


# -- setup menu and tools entry -------------------------------------------------


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


# -- server options form --------------------------------------------------------


@pytest.mark.parametrize("field", [OPT_SECRET_PATH_OVERRIDE, OPT_OAUTH_CLIENT_SECRET])
async def test_secret_fields_are_plain_text_not_password_inputs(
    hass: HomeAssistant, field: str
) -> None:
    """The direct-access path and the legacy OAuth secret show in clear text.

    The administrator copies them into clients, so a masked input would hide
    the value they came to read.
    """
    form = await open_options_form(hass)

    selector = schema_validators(form)[field]

    assert isinstance(selector, TextSelector)
    assert selector.config["type"] == TextSelectorType.TEXT


@pytest.mark.parametrize("field", CONNECT_PATH_FIELDS)
@pytest.mark.parametrize("invalid_character", ["#", "?", "%", " ", "\t"])
async def test_connect_path_override_rejects_url_breaking_characters(
    hass: HomeAssistant, field: str, invalid_character: str
) -> None:
    """A path or id that cannot sit in a URL is refused and the form keeps the typed value."""
    invalid_value = f"safe{invalid_character}value"

    result = await _save(hass, {field: invalid_value})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {field: "invalid_connect_path"}
    assert shown_value(schema_markers(result)[field]) == invalid_value


async def test_connect_path_overrides_accept_url_safe_values(
    hass: HomeAssistant,
) -> None:
    """URL-safe overrides are saved as typed."""
    user_input = {
        OPT_SECRET_PATH_OVERRIDE: "/mcp_safe-token.123",
        OPT_WEBHOOK_ID_OVERRIDE: "mcp_safe-token.123",
    }

    result = await _save(hass, user_input)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {key: result["data"][key] for key in user_input} == user_input


async def test_webhook_id_override_rejects_slash(hass: HomeAssistant) -> None:
    """A slash would split the webhook id into two URL segments."""
    result = await _save(hass, {OPT_WEBHOOK_ID_OVERRIDE: "mcp/abc"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {OPT_WEBHOOK_ID_OVERRIDE: "invalid_webhook_id"}


async def test_connect_path_overrides_accept_surrounding_whitespace(
    hass: HomeAssistant,
) -> None:
    """Whitespace around a pasted value is dropped; the value itself is kept."""
    result = await _save(
        hass,
        {
            OPT_SECRET_PATH_OVERRIDE: " \t/nested/direct/path\r\n",
            OPT_WEBHOOK_ID_OVERRIDE: " \tmcp_abc\r\n",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][OPT_SECRET_PATH_OVERRIDE] == "/nested/direct/path"
    assert result["data"][OPT_WEBHOOK_ID_OVERRIDE] == "mcp_abc"


async def test_webhook_auth_is_the_first_field(hass: HomeAssistant) -> None:
    """Authentication mode sits at the top of the form, under the connect URLs."""
    form = await open_options_form(hass)

    assert next(iter(schema_markers(form))) == OPT_WEBHOOK_AUTH


async def test_form_prefills_every_field_from_saved_options(
    hass: HomeAssistant,
) -> None:
    """The form shows the saved value of every field, never the default.

    A regression here silently reverts a user's configuration on the next
    save. Optional text fields pre-fill by suggestion so they stay clearable;
    every other field pre-fills through the schema default.
    """
    saved = {
        OPT_SERVER_PORT: 12345,
        OPT_BIND_HOST: BIND_HOST_LOOPBACK,
        OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_HA,
        OPT_PIP_SPEC: "ha-mcp==0.0.1",
        OPT_SERVER_URL: "https://ha.example:8123",
        OPT_ENABLE_LLM_API: False,
        OPT_ACCESS_OFF_AFTER_RESTART: True,
        OPT_LLM_API_EXPOSURE: EXPOSURE_FULL,
        OPT_EXTERNAL_URL: "https://ha.example.com",
        OPT_WEBHOOK_ID_OVERRIDE: "my_custom_hook",
        OPT_SECRET_PATH_OVERRIDE: "/custom_path",
        OPT_OAUTH_CLIENT_ID: "hamcp-deadbeef",
        OPT_OAUTH_CLIENT_SECRET: "super-secret-value",
        OPT_OAUTH_REDIRECT_ALLOWLIST: ["https://chatgpt.example/cb"],
    }
    text_fields = (
        OPT_PIP_SPEC,
        OPT_SERVER_URL,
        OPT_EXTERNAL_URL,
        OPT_WEBHOOK_ID_OVERRIDE,
        OPT_SECRET_PATH_OVERRIDE,
        OPT_OAUTH_CLIENT_ID,
        OPT_OAUTH_CLIENT_SECRET,
        OPT_OAUTH_REDIRECT_ALLOWLIST,
    )
    form = await open_options_form(hass, options=saved)
    markers = schema_markers(form)

    for key in text_fields:
        assert markers[key].description is not None
        assert markers[key].description["suggested_value"] == saved[key]

    # One-shot actions are never pre-filled on, and the toggles the entry never
    # stored take their defaults.
    unsaved = {
        OPT_REGENERATE_SECRETS: False,
        OPT_OAUTH_REGENERATE: False,
        OPT_ENABLE_WEBHOOK: True,
        OPT_ENABLE_STARTUP_NOTIFICATION: True,
        OPT_ENABLE_SIDEBAR_PANEL: True,
        OPT_OAUTH_ALLOW_ANY_CALLBACK: False,
    }
    never_prefilled = (
        OPT_ADMIN_TOKEN_REPLACEMENT,
        server_credentials.ADMIN_USERNAME,
        server_credentials.ADMIN_PASSWORD,
    )
    defaults = {
        key: marker.default()
        for key, marker in markers.items()
        if key not in (*text_fields, *never_prefilled)
    }
    assert defaults == {
        **{k: v for k, v in saved.items() if k not in text_fields},
        **unsaved,
    }


@pytest.mark.parametrize("field", [OPT_PIP_SPEC, OPT_SERVER_URL])
async def test_override_fields_are_empty_when_nothing_is_overridden(
    hass: HomeAssistant, field: str
) -> None:
    """With no override saved the field is blank, not filled with the default.

    A pre-filled default made every save store it as an explicit override.
    """
    form = await open_options_form(hass)

    assert shown_value(schema_markers(form)[field]) == ""


@pytest.mark.parametrize("field", list(CLEARABLE))
async def test_clearing_an_override_field_sticks(
    hass: HomeAssistant, field: str
) -> None:
    """Emptying an optional text field saves it as cleared and leaves the others.

    The frontend drops an emptied field from the submit. A schema default equal
    to the saved value would be put back by Core's validation, so the old
    override would come back; a suggested value is not.
    """
    entry = add_options_entry(hass, options=dict(CLEARABLE))
    form = await start_options_flow(hass, entry)

    result = await submit_options_step(
        hass, form["flow_id"], {k: v for k, v in CLEARABLE.items() if k != field}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options.get(field, "") == ""
    assert {k: entry.options[k] for k in CLEARABLE if k != field} == {
        k: v for k, v in CLEARABLE.items() if k != field
    }


async def test_submit_stores_what_was_entered(hass: HomeAssistant) -> None:
    """A genuine override is stored verbatim and the URL fields are always present."""
    entry = add_options_entry(hass)
    form = await start_options_flow(hass, entry)
    user_input = {
        OPT_SERVER_PORT: 9999,
        OPT_BIND_HOST: BIND_HOST_ALL,
        OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_HA,
        OPT_PIP_SPEC: "ha-mcp @ https://example/x.tgz",
        OPT_SERVER_URL: "http://ha.internal:8123",
    }

    result = await submit_options_step(hass, form["flow_id"], user_input)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ""
    assert {key: entry.options[key] for key in user_input} == user_input
    assert {
        key: entry.options[key]
        for key in (
            OPT_EXTERNAL_URL,
            OPT_WEBHOOK_ID_OVERRIDE,
            OPT_SECRET_PATH_OVERRIDE,
            OPT_OAUTH_CLIENT_ID,
            OPT_OAUTH_CLIENT_SECRET,
        )
    } == {
        OPT_EXTERNAL_URL: "",
        OPT_WEBHOOK_ID_OVERRIDE: "",
        OPT_SECRET_PATH_OVERRIDE: "",
        OPT_OAUTH_CLIENT_ID: "",
        OPT_OAUTH_CLIENT_SECRET: "",
    }
    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == []


async def test_default_pip_spec_is_saved_as_no_override(hass: HomeAssistant) -> None:
    """The pinned default moves with each release, so it is saved as "use the paired server"."""
    result = await _save(hass, {OPT_PIP_SPEC: DEFAULT_PIP_SPEC})

    assert result["data"][OPT_PIP_SPEC] == ""


async def test_whitespace_server_url_is_dropped(hass: HomeAssistant) -> None:
    """A blank URL is truthy; stored, it would bypass the loopback fallback."""
    result = await _save(hass, {OPT_SERVER_URL: "   "})

    assert OPT_SERVER_URL not in result["data"]


async def test_server_url_trailing_slash_is_stripped(hass: HomeAssistant) -> None:
    """A real URL is kept without its trailing slash."""
    result = await _save(hass, {OPT_SERVER_URL: "http://ha.local:8123/"})

    assert result["data"][OPT_SERVER_URL] == "http://ha.local:8123"


async def test_default_loopback_url_is_not_saved_as_an_override(
    hass: HomeAssistant,
) -> None:
    """Older forms pre-filled the loopback URL; saving it would pin its scheme and port."""
    result = await _save(hass, {OPT_SERVER_URL: DEFAULT_LOOPBACK_URL + "/"})

    assert OPT_SERVER_URL not in result["data"]


async def test_new_toggles_default_for_an_entry_that_never_stored_them(
    hass: HomeAssistant,
) -> None:
    """Notification and panel default on, restart access off, for an existing install."""
    markers = schema_markers(await open_options_form(hass))

    assert markers[OPT_ENABLE_STARTUP_NOTIFICATION].default() is True
    assert markers[OPT_ENABLE_SIDEBAR_PANEL].default() is True
    assert markers[OPT_ACCESS_OFF_AFTER_RESTART].default() is False


async def test_new_toggles_sit_right_after_enable_webhook(hass: HomeAssistant) -> None:
    """Both toggles follow the enable-webhook field in the form."""
    keys = list(schema_markers(await open_options_form(hass)))

    start = keys.index(OPT_ENABLE_WEBHOOK) + 1
    assert set(keys[start : start + 2]) == {
        OPT_ENABLE_STARTUP_NOTIFICATION,
        OPT_ENABLE_SIDEBAR_PANEL,
    }


async def test_opted_out_toggles_reopen_as_off(hass: HomeAssistant) -> None:
    """Reopening after saving off shows off, not the default on."""
    form = await open_options_form(
        hass,
        options={
            OPT_ENABLE_STARTUP_NOTIFICATION: False,
            OPT_ENABLE_SIDEBAR_PANEL: False,
        },
    )
    markers = schema_markers(form)

    assert markers[OPT_ENABLE_STARTUP_NOTIFICATION].default() is False
    assert markers[OPT_ENABLE_SIDEBAR_PANEL].default() is False


async def test_submitting_toggles_off_saves_them_off(hass: HomeAssistant) -> None:
    """Unchecking both toggles persists False for both."""
    result = await _save(
        hass,
        {OPT_ENABLE_STARTUP_NOTIFICATION: False, OPT_ENABLE_SIDEBAR_PANEL: False},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][OPT_ENABLE_STARTUP_NOTIFICATION] is False
    assert result["data"][OPT_ENABLE_SIDEBAR_PANEL] is False
