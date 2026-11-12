"""The HA-MCP component's fixable repairs, run through Core's repairs flows.

Each repair is filed in Core's issue registry and its fix flow is started and
answered through Core's repairs flow manager, the way the Repairs dashboard
does it. A restart goes through Core's own ``homeassistant.restart`` service,
which checks the configuration first; only what it finally calls to stop the
process is replaced, through Core's ``async_set_stop_handler``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, User
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.components.homeassistant import async_set_stop_handler
from homeassistant.components.repairs import RepairsFlowManager, repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.const import (
    DATA_ADMIN_TOKEN,
    DATA_ISSUED_TOKEN_ID,
    DATA_REINSTALL_REQUESTED,
    DOMAIN,
    ISSUE_LEGACY_OAUTH_RESTART,
    ISSUE_PACKAGE_FAILED,
    ISSUE_STORE_RECORDS_CONFLICT,
    ISSUE_TOKEN_NEEDED,
)
from custom_components.ha_mcp_tools.server_credentials import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
)
from custom_components.ha_mcp_tools.store_records import (
    MAIN_REPOSITORY,
    MIRROR_REPOSITORY,
)

from ._marketplace_records import loaded_marketplace, stored_record
from ._server_entry import add_server_entry, replace_server, set_up

CATALOGS = [
    "custom_components/ha_mcp_tools/strings.json",
    "custom_components/ha_mcp_tools/translations/en.json",
]


def _catalog(catalog_path: str) -> dict[str, Any]:
    root = Path(__file__).parents[3]
    catalog: dict[str, Any] = json.loads((root / catalog_path).read_text())
    return catalog


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Core's config directory, whose configuration a restart checks first."""
    return hass_tmp_config_dir


@pytest.fixture
async def flows(hass: HomeAssistant) -> RepairsFlowManager:
    """Core's repairs flow manager, which starts and answers a fix flow.

    The component is loaded, as it is whenever it has filed a repair; Core
    finds its fix flows only on a loaded integration.
    """
    assert await async_setup_component(hass, DOMAIN, {})
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return manager


@pytest.fixture
async def stop(hass: HomeAssistant) -> AsyncMock:
    """What Core's restart service finally calls to stop the process.

    The restart service is real; it checks a valid configuration first.
    """
    await hass.async_add_executor_job(
        Path(hass.config.path("configuration.yaml")).write_text, "homeassistant:\n"
    )
    assert await async_setup_component(hass, "homeassistant", {})
    handler = AsyncMock()
    async_set_stop_handler(hass, handler)
    return handler


async def _break_configuration(hass: HomeAssistant) -> None:
    """A configuration Core's restart service refuses to restart with."""
    await hass.async_add_executor_job(
        Path(hass.config.path("configuration.yaml")).write_text, "homeassistant: [\n"
    )


def _file(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=issue_id,
        data=data,
    )


async def _open(
    flows: RepairsFlowManager,
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """File the issue and open its fix flow, as the Repairs dashboard does."""
    _file(hass, issue_id, data)
    result = await flows.async_init(DOMAIN, context={"issue_id": issue_id})
    return dict(result)


async def _answer(
    flows: RepairsFlowManager, opened: dict[str, Any], user_input: dict[str, Any]
) -> dict[str, Any]:
    result = await flows.async_configure(opened["flow_id"], user_input)
    return dict(result)


def _issue(issue_registry: ir.IssueRegistry, issue_id: str) -> ir.IssueEntry | None:
    return issue_registry.async_get_issue(DOMAIN, issue_id)


# --- legacy_oauth_restart (#2210) ----------------------------------------------


async def test_legacy_oauth_fix_flow_prompts_before_restart(
    hass: HomeAssistant, flows: RepairsFlowManager, stop: AsyncMock
) -> None:
    """Opening the repair must show confirmation without restarting HA."""
    result = await _open(flows, hass, ISSUE_LEGACY_OAUTH_RESTART)

    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    stop.assert_not_awaited()


async def test_legacy_oauth_fix_flow_restarts_home_assistant(
    hass: HomeAssistant,
    flows: RepairsFlowManager,
    stop: AsyncMock,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A missing restart would leave the OAuth change pending."""
    opened = await _open(flows, hass, ISSUE_LEGACY_OAUTH_RESTART)

    result = await _answer(flows, opened, {})

    assert result["type"] == "create_entry"
    stop.assert_awaited_once_with(hass, True)
    assert _issue(issue_registry, ISSUE_LEGACY_OAUTH_RESTART) is None


async def test_legacy_oauth_fix_flow_does_not_complete_rejected_restart(
    hass: HomeAssistant,
    flows: RepairsFlowManager,
    stop: AsyncMock,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A rejected restart must leave the repair flow, and the issue, open."""
    opened = await _open(flows, hass, ISSUE_LEGACY_OAUTH_RESTART)
    await _break_configuration(hass)

    with pytest.raises(HomeAssistantError, match="configuration is not valid"):
        await _answer(flows, opened, {})

    stop.assert_not_awaited()
    assert _issue(issue_registry, ISSUE_LEGACY_OAUTH_RESTART) is not None


@pytest.mark.parametrize("catalog_path", CATALOGS)
def test_legacy_oauth_repair_catalog_has_fix_flow(catalog_path: str) -> None:
    """Both HA English catalogs must render the actionable confirmation flow."""
    issue = _catalog(catalog_path)["issues"]["legacy_oauth_restart"]
    assert "description" not in issue
    confirm = issue["fix_flow"]["step"]["confirm"]
    assert confirm["title"]
    assert confirm["description"]


# --- server_token_needed (#2427): replace the server's credential --------------


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return replace_server(monkeypatch)


@pytest.fixture
async def server_entry(hass: HomeAssistant, server: SimpleNamespace) -> MockConfigEntry:
    """A loaded server entry; its server is replaced."""
    entry = add_server_entry(hass)
    assert await set_up(hass, entry)
    return entry


async def _long_lived_token(hass: HomeAssistant, user: User) -> str:
    refresh_token = await hass.auth.async_create_refresh_token(
        user, client_name="repair test", token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    )
    return hass.auth.async_create_access_token(refresh_token)


async def test_token_repair_starts_the_server_with_the_new_token(
    hass: HomeAssistant,
    flows: RepairsFlowManager,
    server_entry: MockConfigEntry,
    server: SimpleNamespace,
    hass_admin_user: User,
) -> None:
    token = await _long_lived_token(hass, hass_admin_user)
    before = dict(server_entry.data)
    started = server.bring_up.await_count
    opened = await _open(
        flows, hass, ISSUE_TOKEN_NEEDED, {"entry_id": server_entry.entry_id}
    )

    result = await _answer(flows, opened, {DATA_ADMIN_TOKEN: f"  {token} "})
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert server_entry.data == {**before, DATA_ADMIN_TOKEN: token}
    # The entry reloaded, which starts the server again with the new token.
    assert server.bring_up.await_count == started + 1


async def test_token_repair_takes_an_accounts_login(
    hass: HomeAssistant,
    flows: RepairsFlowManager,
    server_entry: MockConfigEntry,
    local_auth: HassAuthProvider,
) -> None:
    # The kind of account the form asks for: a local-only administrator.
    await local_auth.async_add_auth("ha-mcp", "pw")
    credentials = await local_auth.async_get_or_create_credentials(
        {"username": "ha-mcp"}
    )
    account = await hass.auth.async_create_user(
        "HA-MCP", group_ids=[GROUP_ID_ADMIN], local_only=True
    )
    await hass.auth.async_link_user(account, credentials)
    opened = await _open(
        flows, hass, ISSUE_TOKEN_NEEDED, {"entry_id": server_entry.entry_id}
    )

    result = await _answer(
        flows, opened, {ADMIN_USERNAME: "ha-mcp", ADMIN_PASSWORD: "pw"}
    )

    assert result["type"] == "create_entry"
    issued = hass.auth.async_validate_access_token(server_entry.data[DATA_ADMIN_TOKEN])
    assert issued is not None
    assert issued.user.id == account.id
    assert issued.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    assert server_entry.data[DATA_ISSUED_TOKEN_ID] == issued.id


async def test_token_repair_keeps_asking_until_the_token_works(
    hass: HomeAssistant,
    flows: RepairsFlowManager,
    server_entry: MockConfigEntry,
    hass_read_only_user: User,
) -> None:
    token = await _long_lived_token(hass, hass_read_only_user)
    before = dict(server_entry.data)
    opened = await _open(
        flows, hass, ISSUE_TOKEN_NEEDED, {"entry_id": server_entry.entry_id}
    )

    result = await _answer(flows, opened, {DATA_ADMIN_TOKEN: token})

    assert result["type"] == "form"
    assert result["errors"] == {DATA_ADMIN_TOKEN: "token_not_admin"}
    assert server_entry.data == before


@pytest.mark.parametrize(
    ("reason", "errors"),
    [
        ("token_not_admin", {DATA_ADMIN_TOKEN: "token_not_admin"}),
        ("invalid_token", {DATA_ADMIN_TOKEN: "invalid_token"}),
        ("missing_token", {}),
    ],
)
async def test_token_repair_says_why_the_stored_token_stopped_working(
    hass: HomeAssistant,
    flows: RepairsFlowManager,
    server_entry: MockConfigEntry,
    reason: str,
    errors: dict[str, str],
) -> None:
    result = await _open(
        flows,
        hass,
        ISSUE_TOKEN_NEEDED,
        {"entry_id": server_entry.entry_id, "reason": reason},
    )

    assert result["errors"] == errors


async def test_token_repair_aborts_when_the_entry_is_gone(
    hass: HomeAssistant, flows: RepairsFlowManager, server_entry: MockConfigEntry
) -> None:
    opened = await _open(
        flows, hass, ISSUE_TOKEN_NEEDED, {"entry_id": server_entry.entry_id}
    )
    assert await hass.config_entries.async_remove(server_entry.entry_id)

    result = await _answer(flows, opened, {DATA_ADMIN_TOKEN: "tok"})

    assert (result["type"], result["reason"]) == ("abort", "entry_removed")


async def test_an_unknown_repair_issue_has_no_fix_flow(
    hass: HomeAssistant, flows: RepairsFlowManager
) -> None:
    with pytest.raises(ValueError, match="no fix flow"):
        await _open(flows, hass, "not_an_issue")


@pytest.mark.parametrize("catalog_path", CATALOGS)
def test_token_repair_catalog_explains_every_refusal(catalog_path: str) -> None:
    step = _catalog(catalog_path)["issues"]["server_token_needed"]["fix_flow"]
    for reason in (
        "invalid_token",
        "token_not_long_lived",
        "token_not_admin",
        "invalid_login",
        "credentials_needed",
    ):
        assert step["error"][reason]


# --- server_package_install_failed (#2427): reinstall the server and its ------
# dependencies, the only fix for a dependency another integration downgraded


@pytest.fixture
def pip_allowed(hass: HomeAssistant) -> None:
    """Home Assistant started without ``--skip-pip``, so it installs packages."""
    hass.config.skip_pip = False


async def _open_package_repair(
    flows: RepairsFlowManager, hass: HomeAssistant, entry_id: str
) -> dict[str, Any]:
    return await _open(
        flows,
        hass,
        ISSUE_PACKAGE_FAILED,
        {"entry_id": entry_id, "detail": "No module named 'pydantic_core'"},
    )


@pytest.mark.usefixtures("pip_allowed")
async def test_package_repair_shows_the_failure_before_reinstalling(
    hass: HomeAssistant, flows: RepairsFlowManager, stop: AsyncMock
) -> None:
    entry = add_server_entry(hass)

    result = await _open_package_repair(flows, hass, entry.entry_id)

    assert result["type"] == "form"
    assert "pydantic_core" in result["description_placeholders"]["detail"]
    stop.assert_not_awaited()


@pytest.mark.usefixtures("pip_allowed")
async def test_package_repair_reinstalls_the_server_on_a_fresh_start(
    hass: HomeAssistant, flows: RepairsFlowManager, stop: AsyncMock
) -> None:
    """A failed import leaves the broken dependency loaded in Home Assistant's
    process, so the reinstall must run at the next start, before any import."""
    entry = add_server_entry(hass)
    before = dict(entry.data)
    opened = await _open_package_repair(flows, hass, entry.entry_id)

    result = await _answer(flows, opened, {})

    assert result["type"] == "create_entry"
    assert entry.data == {**before, DATA_REINSTALL_REQUESTED: True}
    stop.assert_awaited_once_with(hass, True)


@pytest.mark.usefixtures("pip_allowed")
async def test_a_refused_restart_keeps_the_repair_and_the_reinstall_request(
    hass: HomeAssistant,
    flows: RepairsFlowManager,
    stop: AsyncMock,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The repair stays open, and the next start still reinstalls."""
    entry = add_server_entry(hass)
    opened = await _open_package_repair(flows, hass, entry.entry_id)
    await _break_configuration(hass)

    with pytest.raises(HomeAssistantError):
        await _answer(flows, opened, {})

    assert entry.data[DATA_REINSTALL_REQUESTED] is True
    assert _issue(issue_registry, ISSUE_PACKAGE_FAILED) is not None
    stop.assert_not_awaited()


@pytest.mark.usefixtures("pip_allowed")
async def test_package_repair_explains_when_the_entry_is_gone(
    hass: HomeAssistant, flows: RepairsFlowManager, stop: AsyncMock
) -> None:
    result = await _open_package_repair(flows, hass, "removed-entry")

    assert (result["type"], result["reason"]) == ("abort", "entry_removed")
    stop.assert_not_awaited()


async def test_package_repair_explains_when_home_assistant_installs_nothing(
    hass: HomeAssistant, flows: RepairsFlowManager, stop: AsyncMock
) -> None:
    # Home Assistant started with --skip-pip: its packages are managed outside.
    hass.config.skip_pip = True
    entry = add_server_entry(hass)

    result = await _open_package_repair(flows, hass, entry.entry_id)

    assert (result["type"], result["reason"]) == ("abort", "externally_managed")
    stop.assert_not_awaited()


@pytest.mark.parametrize("catalog_path", CATALOGS)
def test_package_repair_catalog_has_its_fix_flow(catalog_path: str) -> None:
    issues = _catalog(catalog_path)["issues"]
    fix_flow = issues["server_package_install_failed"]["fix_flow"]
    assert "{detail}" in fix_flow["step"]["confirm"]["description"]
    for reason in ("entry_removed", "externally_managed"):
        assert fix_flow["abort"][reason]


# --- Two store listings claim the integration (#2993) -------------------------


async def test_store_repair_waits_when_no_store_is_loaded(
    hass: HomeAssistant,
    flows: RepairsFlowManager,
    issue_registry: ir.IssueRegistry,
) -> None:
    """HACS disabled or the Marketplace entry still retrying: the card cannot
    tell, so it says so instead of claiming the problem is gone."""
    result = await _open(flows, hass, ISSUE_STORE_RECORDS_CONFLICT)

    assert (result["type"], result["reason"]) == ("abort", "store_unavailable")
    assert _issue(issue_registry, ISSUE_STORE_RECORDS_CONFLICT) is not None


async def test_store_repair_clears_a_stale_card_when_the_store_reads_clean(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    flows: RepairsFlowManager,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A fix made by hand, or a store that loaded after the retries stopped:
    opening the card is what removes it."""
    await loaded_marketplace(hass, hass_storage, main=True, mirror=False)

    result = await _open(flows, hass, ISSUE_STORE_RECORDS_CONFLICT)

    assert (result["type"], result["reason"]) == ("abort", "already_resolved")
    assert _issue(issue_registry, ISSUE_STORE_RECORDS_CONFLICT) is None


async def test_store_repair_names_both_listings_then_keeps_the_main_one(
    hass: HomeAssistant, hass_storage: dict[str, Any], flows: RepairsFlowManager
) -> None:
    await loaded_marketplace(hass, hass_storage, main=True, mirror=True)

    shown = await _open(flows, hass, ISSUE_STORE_RECORDS_CONFLICT)
    assert shown["type"] == "form" and shown["step_id"] == "confirm"
    assert shown["description_placeholders"] == {
        "records": f"{MAIN_REPOSITORY} and {MIRROR_REPOSITORY}",
        "keep": MAIN_REPOSITORY,
    }

    done = await _answer(flows, shown, {})
    assert done["type"] == "create_entry"
    assert "installed" not in stored_record(hass_storage, MIRROR_REPOSITORY)
    assert stored_record(hass_storage, MAIN_REPOSITORY)["installed"] is True


@pytest.mark.parametrize("catalog_path", CATALOGS)
def test_store_repair_catalog_explains_every_abort(catalog_path: str) -> None:
    fix_flow = _catalog(catalog_path)["issues"]["store_records_conflict"]["fix_flow"]
    for placeholder in ("{records}", "{keep}"):
        assert placeholder in fix_flow["step"]["confirm"]["description"]
    for reason in ("already_resolved", "store_unavailable"):
        assert fix_flow["abort"][reason]
