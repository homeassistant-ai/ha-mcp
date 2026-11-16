"""The server entry's setup flow (#2427) and the options flow each entry type gets.

Every step runs through Core's flow managers, with real accounts and long-lived
tokens in Core's auth. The in-process server itself is replaced, since a
created entry is set up the moment the flow finishes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.auth.models import User
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.config_flow import (
    REMOTE_ACCESS_DISABLED,
    SETUP_ADMIN_TOKEN,
    SETUP_REMOTE_ACCESS,
)
from custom_components.ha_mcp_tools.const import (
    BIND_HOST_ALL,
    BIND_HOST_LOOPBACK,
    CONF_ENTRY_TYPE,
    DATA_ADMIN_TOKEN,
    DATA_ISSUED_TOKEN_ID,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    OPT_BIND_HOST,
    OPT_ENABLE_WEBHOOK,
    OPT_WEBHOOK_AUTH,
    SERVER_ENTRY_TITLE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)
from custom_components.ha_mcp_tools.server_credentials import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
)

from ._config_flow_core import (
    FlowResultDict,
    add_local_account,
    add_tools_entry,
    long_lived_token,
    schema_markers,
    start_entry_type_flow,
    start_options_flow,
    submit_config_step,
)
from ._server_entry import add_server_entry, replace_server


@pytest.fixture(autouse=True)
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A finished setup flow sets the entry up; no server starts."""
    return replace_server(monkeypatch)


@pytest.fixture
async def admin_token(hass: HomeAssistant, hass_admin_user: User) -> str:
    """A long-lived token of an administrator: what the form asks to be pasted."""
    return await long_lived_token(hass, hass_admin_user)


async def _setup(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[str, FlowResultDict]:
    """Open the server form and submit ``user_input``; the form's id and the result."""
    form = await start_entry_type_flow(hass, ENTRY_TYPE_SERVER)
    return form["flow_id"], await submit_config_step(hass, form["flow_id"], user_input)


def saved_options(hass: HomeAssistant) -> dict[str, Any]:
    """The options of the one entry the setup flow created."""
    [entry] = hass.config_entries.async_entries(DOMAIN)
    return dict(entry.options)


async def test_server_branch_shows_the_setup_form(hass: HomeAssistant) -> None:
    """Choosing the server asks for the account, remote access and network access."""
    form = await start_entry_type_flow(hass, ENTRY_TYPE_SERVER)

    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "server"
    assert set(schema_markers(form)) == {
        SETUP_ADMIN_TOKEN,
        ADMIN_USERNAME,
        ADMIN_PASSWORD,
        SETUP_REMOTE_ACCESS,
        OPT_BIND_HOST,
    }


async def test_new_install_defaults_to_no_webhook_and_loopback_only(
    hass: HomeAssistant, admin_token: str
) -> None:
    """#2427: a fresh install exposes nothing beyond the Home Assistant machine."""
    _, created = await _setup(hass, {SETUP_ADMIN_TOKEN: admin_token})

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["title"] == SERVER_ENTRY_TITLE
    assert created["data"][CONF_ENTRY_TYPE] == ENTRY_TYPE_SERVER
    options = saved_options(hass)
    assert options[OPT_ENABLE_WEBHOOK] is False
    assert options[OPT_BIND_HOST] == BIND_HOST_LOOPBACK


async def test_new_install_options_are_saved_not_inherited(
    hass: HomeAssistant, admin_token: str
) -> None:
    """Saved choices keep later default changes from moving this entry."""
    await _setup(hass, {SETUP_ADMIN_TOKEN: admin_token})

    assert {OPT_ENABLE_WEBHOOK, OPT_WEBHOOK_AUTH, OPT_BIND_HOST} <= set(
        saved_options(hass)
    )


@pytest.mark.parametrize(
    "mode", [WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_HA, WEBHOOK_AUTH_LEGACY]
)
async def test_choosing_remote_access_enables_the_webhook_with_that_auth(
    hass: HomeAssistant, admin_token: str, mode: str
) -> None:
    """Each remote-access choice turns the webhook on with that sign-in mode."""
    await _setup(
        hass,
        {
            SETUP_REMOTE_ACCESS: mode,
            OPT_BIND_HOST: BIND_HOST_ALL,
            SETUP_ADMIN_TOKEN: admin_token,
        },
    )

    options = saved_options(hass)
    assert options[OPT_ENABLE_WEBHOOK] is True
    assert options[OPT_WEBHOOK_AUTH] == mode
    assert options[OPT_BIND_HOST] == BIND_HOST_ALL


async def test_disabled_remote_access_keeps_ha_sign_in_for_a_later_enable(
    hass: HomeAssistant, admin_token: str
) -> None:
    """Turning the webhook on later in Configure must not land on the secret-URL mode."""
    await _setup(
        hass,
        {SETUP_REMOTE_ACCESS: REMOTE_ACCESS_DISABLED, SETUP_ADMIN_TOKEN: admin_token},
    )

    options = saved_options(hass)
    assert options[OPT_ENABLE_WEBHOOK] is False
    assert options[OPT_WEBHOOK_AUTH] == WEBHOOK_AUTH_HA


async def test_the_server_runs_with_the_administrators_token(
    hass: HomeAssistant, admin_token: str
) -> None:
    """#2427: setup makes no account for itself; the pasted token is what it keeps."""
    _, created = await _setup(hass, {SETUP_ADMIN_TOKEN: f" {admin_token} "})

    assert created["data"][DATA_ADMIN_TOKEN] == admin_token


@pytest.mark.parametrize("who", ["unknown", "non-admin"])
async def test_an_unusable_token_keeps_the_form_open(
    hass: HomeAssistant, hass_read_only_user: User, who: str
) -> None:
    """A token Core does not accept, or one without administrator rights, is refused."""
    token = (
        await long_lived_token(hass, hass_read_only_user)
        if who == "non-admin"
        else "wrong"
    )

    _, result = await _setup(hass, {SETUP_ADMIN_TOKEN: token})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        SETUP_ADMIN_TOKEN: "token_not_admin" if who == "non-admin" else "invalid_token"
    }
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_an_accounts_login_sets_up_the_server_without_a_pasted_token(
    hass: HomeAssistant, local_auth: HassAuthProvider
) -> None:
    """#2427: the token is issued for the account; its id is kept so a switch or
    the entry's removal revokes it."""
    account = await add_local_account(hass, local_auth, "ha-mcp", "pw")

    _, created = await _setup(hass, {ADMIN_USERNAME: " ha-mcp ", ADMIN_PASSWORD: "pw"})

    assert created["type"] is FlowResultType.CREATE_ENTRY
    issued = hass.auth.async_validate_access_token(created["data"][DATA_ADMIN_TOKEN])
    assert issued is not None
    assert issued.user.id == account.id
    assert created["data"][DATA_ISSUED_TOKEN_ID] == issued.id
    assert ADMIN_PASSWORD not in saved_options(hass)


@pytest.mark.parametrize("fault", ["wrong-password", "not-local-only"])
async def test_a_refused_login_keeps_the_form_open_and_issues_nothing(
    hass: HomeAssistant, local_auth: HassAuthProvider, fault: str
) -> None:
    """A bad password or an account reachable from outside the network is refused."""
    account = await add_local_account(
        hass, local_auth, "ha-mcp", "pw", local_only=fault != "not-local-only"
    )

    _, result = await _setup(
        hass,
        {
            ADMIN_USERNAME: "ha-mcp",
            ADMIN_PASSWORD: "bad" if fault == "wrong-password" else "pw",
        },
    )

    assert result["errors"] == {"base": "invalid_login"}
    assert account.refresh_tokens == {}


async def test_a_setup_that_lost_the_race_revokes_the_token_it_issued(
    hass: HomeAssistant,
    local_auth: HassAuthProvider,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two setups, two accounts: the other finished while this login was checked,
    so the token issued for this one must not outlive its refusal."""
    account = await add_local_account(hass, local_auth, "ha-mcp", "pw")
    form = await start_entry_type_flow(hass, ENTRY_TYPE_SERVER)
    other = await start_entry_type_flow(hass, ENTRY_TYPE_SERVER)
    check_login = local_auth.async_validate_login

    async def other_setup_finishes_first(username: str, password: str) -> None:
        await submit_config_step(
            hass, other["flow_id"], {SETUP_ADMIN_TOKEN: admin_token}
        )
        await check_login(username, password)

    monkeypatch.setattr(local_auth, "async_validate_login", other_setup_finishes_first)

    result = await submit_config_step(
        hass, form["flow_id"], {ADMIN_USERNAME: "ha-mcp", ADMIN_PASSWORD: "pw"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert account.refresh_tokens == {}


async def test_an_abandoned_setup_does_not_block_a_new_one(
    hass: HomeAssistant, admin_token: str
) -> None:
    """A dialog closed mid-setup stays open until restart; a new setup still opens
    and finishes."""
    await start_entry_type_flow(hass, ENTRY_TYPE_SERVER)

    _, created = await _setup(hass, {SETUP_ADMIN_TOKEN: admin_token})

    assert created["type"] is FlowResultType.CREATE_ENTRY


async def test_a_second_server_entry_is_refused(
    hass: HomeAssistant, admin_token: str
) -> None:
    """The server entry is single-instance: a setup begun after one exists aborts,
    and the dialog left open beside the finished one is closed by Core."""
    first = await start_entry_type_flow(hass, ENTRY_TYPE_SERVER)
    beside = await start_entry_type_flow(hass, ENTRY_TYPE_SERVER)
    await submit_config_step(hass, first["flow_id"], {SETUP_ADMIN_TOKEN: admin_token})

    later = await start_entry_type_flow(hass, ENTRY_TYPE_SERVER)

    assert later["type"] is FlowResultType.ABORT
    assert later["reason"] == "already_configured"
    with pytest.raises(UnknownFlow):
        await submit_config_step(hass, beside["flow_id"], {})
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_the_server_and_tools_entries_coexist(
    hass: HomeAssistant, admin_token: str
) -> None:
    """Their unique ids differ, so adding one never aborts on the other."""
    tools = await start_entry_type_flow(hass, ENTRY_TYPE_TOOLS)
    await submit_config_step(hass, tools["flow_id"], {})

    _, created = await _setup(hass, {SETUP_ADMIN_TOKEN: admin_token})

    assert created["type"] is FlowResultType.CREATE_ENTRY
    entries = hass.config_entries.async_entries(DOMAIN)
    assert {entry.data[CONF_ENTRY_TYPE] for entry in entries} == {
        ENTRY_TYPE_SERVER,
        ENTRY_TYPE_TOOLS,
    }
    assert len({entry.unique_id for entry in entries}) == 2


@pytest.mark.parametrize(
    ("data", "step_id"),
    [
        ({CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER}, "init"),
        ({CONF_ENTRY_TYPE: ENTRY_TYPE_TOOLS}, "tools_info"),
        ({}, "tools_info"),
    ],
    ids=["server", "tools", "tools-before-entry-types"],
)
async def test_each_entry_type_opens_its_own_options_form(
    hass: HomeAssistant, data: dict[str, Any], step_id: str
) -> None:
    """An entry from before entry types existed carries no marker and is a tools
    entry: it must never get the server's options."""
    if data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SERVER:
        entry: MockConfigEntry = add_server_entry(hass)
    else:
        entry = await add_tools_entry(hass, data=data, loaded=True)

    form = await start_options_flow(hass, entry)

    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == step_id
