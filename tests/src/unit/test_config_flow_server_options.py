"""What Configure edits on the server entry: callbacks, the token and what it shows.

The callback allowlist and the administrator token were added for the HACS
review (#2427); the hints say what the server serves right now. Every step runs
through Core's options flow manager, with a real entry set up by Core.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.auth.models import User
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.const import (
    DATA_ADMIN_TOKEN,
    DATA_ISSUED_TOKEN_ID,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_WEBHOOK_ID,
    OPT_ADMIN_TOKEN_REPLACEMENT,
    OPT_OAUTH_ALLOW_ANY_CALLBACK,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)
from custom_components.ha_mcp_tools.oauth_redirect_allowlist import (
    DEFAULT_OAUTH_REDIRECT_ALLOWLIST,
    MAX_OAUTH_CALLBACKS,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
)
from custom_components.ha_mcp_tools.server_credentials import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
)

from ._config_flow_core import (
    STRINGS_COMMON,
    WEBHOOK_ID,
    FlowResultDict,
    add_local_account,
    add_options_entry,
    bind_legacy_credentials,
    long_lived_token,
    open_options_form,
    placeholders,
    schema_markers,
    schema_validators,
    shown_value,
    start_options_flow,
    submit_options_step,
)
from ._server_entry import add_server_entry, replace_server, set_up

CALLBACK = "https://chatgpt.example/cb"


@pytest.fixture(autouse=True)
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A reload sets the entry up again; no server starts."""
    return replace_server(monkeypatch)


async def _save(
    hass: HomeAssistant, entry: MockConfigEntry, user_input: dict[str, Any]
) -> FlowResultDict:
    """Open the entry's options form and submit ``user_input``."""
    form = await start_options_flow(hass, entry)
    return await submit_options_step(hass, form["flow_id"], user_input)


async def _loaded_entry(
    hass: HomeAssistant, server: SimpleNamespace, **data: Any
) -> MockConfigEntry:
    """A server entry Core has set up and saved once, so its options are settled."""
    entry = add_server_entry(hass, data=data)
    assert await set_up(hass, entry)
    await _save(hass, entry, {})
    await hass.async_block_till_done()
    return entry


async def test_an_unedited_entry_shows_the_default_callback_list(
    hass: HomeAssistant,
) -> None:
    """Until edited, the form lists the callbacks the server ships with."""
    form = await open_options_form(hass)

    shown = shown_value(schema_markers(form)[OPT_OAUTH_REDIRECT_ALLOWLIST])
    assert shown == list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)


async def test_saving_stores_the_entered_callbacks(hass: HomeAssistant) -> None:
    """Callbacks are saved without the whitespace a paste brings."""
    entry = add_options_entry(hass)

    result = await _save(hass, entry, {OPT_OAUTH_REDIRECT_ALLOWLIST: [f" {CALLBACK} "]})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == [CALLBACK]


async def test_an_unusable_callback_blocks_the_save(hass: HomeAssistant) -> None:
    """A callback that is not loopback or https is refused and named."""
    entry = add_options_entry(hass)

    result = await _save(
        hass, entry, {OPT_OAUTH_REDIRECT_ALLOWLIST: ["http://not-loopback/cb"]}
    )

    assert result["errors"] == {OPT_OAUTH_REDIRECT_ALLOWLIST: "invalid_oauth_callback"}
    assert placeholders(result)["invalid_callbacks"] == "http://not-loopback/cb"
    assert OPT_OAUTH_REDIRECT_ALLOWLIST not in entry.options


async def test_more_callbacks_than_the_cap_block_the_save(hass: HomeAssistant) -> None:
    """The list is capped; one over the cap is refused."""
    entry = add_options_entry(hass)
    many = [f"{CALLBACK}{n}" for n in range(MAX_OAUTH_CALLBACKS + 1)]

    result = await _save(hass, entry, {OPT_OAUTH_REDIRECT_ALLOWLIST: many})

    assert result["errors"] == {
        OPT_OAUTH_REDIRECT_ALLOWLIST: "too_many_oauth_callbacks"
    }


async def test_saving_the_untouched_default_keeps_following_it(
    hass: HomeAssistant,
) -> None:
    """Pinning the default would hide callbacks a later release adds to it."""
    entry = add_options_entry(hass)

    await _save(
        hass,
        entry,
        {OPT_OAUTH_REDIRECT_ALLOWLIST: list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)},
    )

    assert OPT_OAUTH_REDIRECT_ALLOWLIST not in entry.options


async def test_removing_every_callback_sticks(hass: HomeAssistant) -> None:
    """Home Assistant's frontend omits an emptied optional field; that is a clear."""
    entry = add_options_entry(hass, options={OPT_OAUTH_REDIRECT_ALLOWLIST: [CALLBACK]})

    await _save(hass, entry, {})

    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == []


async def test_a_save_that_changes_only_the_callbacks_needs_no_reload(
    hass: HomeAssistant, server: SimpleNamespace
) -> None:
    """#2427: the list is read per request, so the server keeps running. A change
    to a setting read at start-up does restart it."""
    entry = await _loaded_entry(hass, server, **{DATA_WEBHOOK_ID: WEBHOOK_ID})
    started = server.bring_up.await_count

    await _save(hass, entry, {OPT_OAUTH_REDIRECT_ALLOWLIST: [CALLBACK]})
    await hass.async_block_till_done()
    assert server.bring_up.await_count == started
    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == [CALLBACK]

    await _save(hass, entry, {OPT_SERVER_PORT: 9999})
    await hass.async_block_till_done()
    assert server.bring_up.await_count == started + 1


async def test_the_allow_any_switch_saves_without_a_reload(
    hass: HomeAssistant, server: SimpleNamespace
) -> None:
    """The switch is read per request like the list: turning it on needs no restart."""
    entry = await _loaded_entry(hass, server, **{DATA_WEBHOOK_ID: WEBHOOK_ID})
    started = server.bring_up.await_count

    await _save(hass, entry, {OPT_OAUTH_ALLOW_ANY_CALLBACK: True})
    await hass.async_block_till_done()

    assert entry.options[OPT_OAUTH_ALLOW_ANY_CALLBACK] is True
    assert server.bring_up.await_count == started


# --- the administrator token (#2427) ---------------------------------------------


async def test_the_login_fields_never_offer_a_saved_login(hass: HomeAssistant) -> None:
    """A browser would autofill the administrator's own saved login, which must
    never go there; a filled login also switches the server's account."""
    form = await open_options_form(hass)

    fields = schema_validators(form)
    assert fields[ADMIN_USERNAME].config["autocomplete"] == "off"
    assert fields[ADMIN_PASSWORD].config["autocomplete"] == "new-password"


async def test_the_stored_token_is_never_shown(hass: HomeAssistant) -> None:
    """The replacement field opens empty whatever the entry holds."""
    form = await open_options_form(
        hass,
        data={DATA_ADMIN_TOKEN: "secret"},
        options={OPT_ADMIN_TOKEN_REPLACEMENT: "good"},
    )

    marker = schema_markers(form)[OPT_ADMIN_TOKEN_REPLACEMENT]
    assert not (marker.description or {}).get("suggested_value")
    assert "secret" not in str([m.description for m in schema_markers(form).values()])


async def test_a_working_token_goes_into_the_entry_data_not_the_options(
    hass: HomeAssistant, hass_admin_user: User
) -> None:
    """``config_entries/get`` returns the options, so the token never passes through them."""
    entry = add_options_entry(hass, data={DATA_WEBHOOK_ID: "w"})
    token = await long_lived_token(hass, hass_admin_user)

    result = await _save(hass, entry, {OPT_ADMIN_TOKEN_REPLACEMENT: f" {token} "})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert OPT_ADMIN_TOKEN_REPLACEMENT not in result["data"]
    assert OPT_ADMIN_TOKEN_REPLACEMENT not in entry.options
    assert entry.data[DATA_ADMIN_TOKEN] == token
    assert entry.data[DATA_WEBHOOK_ID] == "w"


async def test_a_token_only_save_restarts_the_server_with_it(
    hass: HomeAssistant, server: SimpleNamespace, hass_admin_user: User
) -> None:
    """Unchanged options do not reload the entry by themselves."""
    entry = await _loaded_entry(hass, server)
    started = server.bring_up.await_count
    token = await long_lived_token(hass, hass_admin_user)

    await _save(hass, entry, {OPT_ADMIN_TOKEN_REPLACEMENT: token})
    await hass.async_block_till_done()

    assert entry.data[DATA_ADMIN_TOKEN] == token
    assert server.bring_up.await_count == started + 1


async def test_a_save_that_changes_options_restarts_the_server_once(
    hass: HomeAssistant, server: SimpleNamespace, hass_admin_user: User
) -> None:
    """The options change reloads the entry through the listener; the token adds
    no second reload."""
    entry = await _loaded_entry(hass, server)
    started = server.bring_up.await_count
    token = await long_lived_token(hass, hass_admin_user)

    await _save(
        hass,
        entry,
        {OPT_ADMIN_TOKEN_REPLACEMENT: token, OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_LEGACY},
    )
    await hass.async_block_till_done()

    assert server.bring_up.await_count == started + 1


async def test_an_unusable_token_blocks_the_save(
    hass: HomeAssistant, hass_read_only_user: User
) -> None:
    """A token without administrator rights is refused and nothing is saved."""
    entry = add_options_entry(hass, options={OPT_PIP_SPEC: "ha-mcp==1.0"})
    before = dict(entry.data)
    token = await long_lived_token(hass, hass_read_only_user)

    result = await _save(
        hass, entry, {OPT_ADMIN_TOKEN_REPLACEMENT: token, OPT_PIP_SPEC: ""}
    )

    assert result["errors"] == {OPT_ADMIN_TOKEN_REPLACEMENT: "token_not_admin"}
    assert dict(entry.data) == before
    assert entry.options[OPT_PIP_SPEC] == "ha-mcp==1.0"


async def test_an_accounts_login_switches_the_server_and_is_never_stored(
    hass: HomeAssistant, local_auth: HassAuthProvider
) -> None:
    """The login is exchanged for a token issued for the account; neither the
    username nor the password is kept."""
    password = "never-kept-0f3c9a"
    account = await add_local_account(hass, local_auth, "ha-mcp", password)
    entry = add_options_entry(hass, data={DATA_WEBHOOK_ID: "w"})

    result = await _save(
        hass, entry, {ADMIN_USERNAME: "ha-mcp", ADMIN_PASSWORD: password}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert ADMIN_USERNAME not in result["data"]
    assert ADMIN_PASSWORD not in result["data"]
    issued = hass.auth.async_validate_access_token(entry.data[DATA_ADMIN_TOKEN])
    assert issued is not None
    assert issued.user.id == account.id
    assert entry.data[DATA_ISSUED_TOKEN_ID] == issued.id
    # Searched as text so a nested copy is found too; the password cannot
    # turn up by chance in the generated ids and tokens.
    assert password not in str(entry.data) + str(entry.options)


async def test_leaving_the_token_empty_keeps_the_current_one(
    hass: HomeAssistant,
) -> None:
    """A save that leaves the replacement empty changes no credential."""
    entry = add_options_entry(hass, data={DATA_ADMIN_TOKEN: "current"})

    result = await _save(hass, entry, {})

    assert OPT_ADMIN_TOKEN_REPLACEMENT not in result["data"]
    assert entry.data[DATA_ADMIN_TOKEN] == "current"


# --- the OAuth credentials hint ----------------------------------------------------

SIGNING_KEY = "ab" * 32
LEGACY_OPTIONS = {OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_LEGACY}
MINTED = {
    DATA_WEBHOOK_ID: WEBHOOK_ID,
    DATA_OAUTH_CLIENT_ID: "hamcp-abc",
    DATA_OAUTH_CLIENT_SECRET: "s3cr3t",
    DATA_OAUTH_SIGNING_KEY: SIGNING_KEY,
}
CREDENTIALS = "Client ID: hamcp-abc\nClient Secret: s3cr3t"


async def _creds_hint(hass: HomeAssistant, **options: Any) -> str:
    """What the form shows under the OAuth credentials, for the minted entry."""
    form = await open_options_form(hass, data=MINTED, options=options)
    return placeholders(form)["oauth_creds"]


async def test_other_modes_point_at_the_mode_selector(hass: HomeAssistant) -> None:
    """Credentials left from an earlier legacy period stay hidden in another mode."""
    hint = await _creds_hint(hass, **{OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_NONE})

    assert hint == STRINGS_COMMON["oauth_select_legacy_mode"]


async def test_legacy_mode_before_minting_says_they_appear_after_start(
    hass: HomeAssistant,
) -> None:
    """Legacy was just selected and no start-up has minted credentials yet."""
    form = await open_options_form(
        hass, data={DATA_WEBHOOK_ID: WEBHOOK_ID}, options=LEGACY_OPTIONS
    )

    assert placeholders(form)["oauth_creds"] == STRINGS_COMMON["oauth_creds_pending"]


async def test_credentials_the_views_serve_show_without_a_caveat(
    hass: HomeAssistant,
) -> None:
    """Bound at start-up and unchanged since: the steady state needs no caveat."""
    await bind_legacy_credentials(
        hass,
        client_id="hamcp-abc",
        client_secret="s3cr3t",
        signing_key=SIGNING_KEY,
        at_boot=True,
    )

    assert await _creds_hint(hass, **LEGACY_OPTIONS) == CREDENTIALS


async def test_a_first_enable_mid_session_carries_the_not_serving_caveat(
    hass: HomeAssistant,
) -> None:
    """The views bound mid-session serve these credentials but not before a restart."""
    await bind_legacy_credentials(
        hass,
        client_id="hamcp-abc",
        client_secret="s3cr3t",
        signing_key=SIGNING_KEY,
        at_boot=False,
    )

    hint = await _creds_hint(hass, **LEGACY_OPTIONS)

    assert hint == f"{CREDENTIALS}\n{STRINGS_COMMON['oauth_not_serving']}"


async def test_a_rotation_waits_for_the_restart_and_says_so(
    hass: HomeAssistant,
) -> None:
    """After a rotation the entry holds the new credentials but the views keep
    the previous ones, so the shown values do not work yet."""
    await bind_legacy_credentials(
        hass,
        client_id="hamcp-old",
        client_secret="old-secret",
        signing_key=SIGNING_KEY,
        at_boot=True,
    )

    hint = await _creds_hint(hass, **LEGACY_OPTIONS)

    assert hint == f"{CREDENTIALS}\n{STRINGS_COMMON['oauth_not_serving']}"
    assert "restart" in STRINGS_COMMON["oauth_not_serving"]


# --- the version line -----------------------------------------------------------


def _write_server_dist(site: Path, name: str, version: str) -> None:
    """A real ``.dist-info`` for a server distribution under another name."""
    dist_info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    (dist_info / "top_level.txt").write_text("ha_mcp\n", encoding="utf-8")


async def test_the_version_line_reads_the_override_not_the_server_beside_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#2787: with a package override the form reads the version of the package
    that override installs, as bring-up does, so the paired ha-mcp left beside it
    cannot mask it."""
    _write_server_dist(tmp_path, "acme-ha-mcp", "5.5.5")
    monkeypatch.syspath_prepend(str(tmp_path))

    overridden = await open_options_form(
        hass, options={OPT_PIP_SPEC: "acme-ha-mcp==5.5.5"}
    )
    paired = await open_options_form(hass)

    assert (
        "Server ha-mcp 5.5.5 (pip requirement override)"
        in placeholders(overridden)["versions"]
    )
    assert "5.5.5" not in placeholders(paired)["versions"]
