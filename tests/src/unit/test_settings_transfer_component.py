"""Restore a reusable sidebar backup against the destination's HA entries."""

from __future__ import annotations

import contextlib
import copy
import importlib.abc
import importlib.resources
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components import webhook
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import (
    CALLER_TOKEN_FIELD,
    SERVICE_GET_ALLOWED_PATHS,
    SERVICE_GET_CALLER_TOKEN,
    SERVICE_SET_ALLOWED_PATHS,
    settings_transfer,
)
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, SERVER_CONFIG_SUBDIR
from ha_mcp import settings_transfer as server_transfer
from ha_mcp.policy.persistence import load_policy, save_policy
from ha_mcp.utils.config_write_lock import config_file_lock

from ._config_flow_core import (
    add_options_entry,
    add_tools_entry,
    start_options_flow,
    submit_options_step,
)
from ._file_services import make_file_service
from ._server_entry import add_server_entry, replace_server, set_up


async def test_mirror_backup_restores_connection_and_permissions_on_main(
    hass: HomeAssistant,
) -> None:
    server = add_options_entry(
        hass,
        data={
            "access_enabled": True,
            "webhook_id": "mirror-hook",
            "secret_path": "/mirror-path",
            "admin_token": "mirror-only",
            "dcr_signing_key": "ab" * 32,
            "oauth_client_id": "legacy-client",
            "oauth_client_secret": "legacy-secret",
            "oauth_signing_key": "cd" * 32,
        },
        options={
            "enable_webhook": True,
            "webhook_auth": "legacy",
            "pip_spec": "ha-mcp-dev==9.2.1.dev1",
            # Configure stores the field as typed, scheme or not.
            "external_url": "mcp.example.org",
        },
    )
    tools_entry = await add_tools_entry(hass, data={})
    flow = await start_options_flow(hass, tools_entry)
    await submit_options_step(
        hass,
        flow["flow_id"],
        {"allowed_dirs": ["blueprints"], "extra_yaml_keys": ["mqtt"]},
    )
    bundle = await settings_transfer.async_export(hass)
    assert "admin_token" not in bundle["entries"]["server"]["data"]
    assert "access_enabled" not in bundle["entries"]["server"]["data"]
    assert "pip_spec" not in bundle["entries"]["server"]["options"]
    assert bundle["contains_credentials"] is True

    assert bundle["entries"]["tools"] == {
        "allowed_dirs": ["blueprints"],
        "extra_yaml_keys": ["mqtt"],
    }
    flow = await start_options_flow(hass, tools_entry)
    await submit_options_step(
        hass, flow["flow_id"], {"allowed_dirs": [], "extra_yaml_keys": []}
    )
    await hass.config_entries.async_remove(tools_entry.entry_id)
    hass.config_entries.async_update_entry(
        server,
        # A destination configured through the sidebar has already completed
        # the one-time 1.2 migration that cleared pre-upgrade pip overrides.
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={
            "access_enabled": False,
            "entry_type": "server",
            "webhook_id": "old",
            "secret_path": "/old",
            "admin_token": "main-only",
        },
        options={
            "pip_spec": "ha-mcp-dev==9.2.1.dev2",
            "server_url": "https://local.example",
            "server_port": 1234,
            # A pending rotation would replace the imported secrets on reload.
            "regenerate_secrets": True,
        },
    )

    assert await settings_transfer.async_import(hass, bundle)
    assert server.data["webhook_id"] == "mirror-hook"
    assert server.data["secret_path"] == "/mirror-path"
    assert server.data["dcr_signing_key"] == "ab" * 32
    assert server.data["oauth_client_secret"] == "legacy-secret"
    assert server.data["admin_token"] == "main-only"
    # The Access switch stays as the destination's administrator left it.
    assert server.data["access_enabled"] is False
    assert server.options == {
        "enable_webhook": True,
        "webhook_auth": "legacy",
        "external_url": "mcp.example.org",
        "pip_spec": "ha-mcp-dev==9.2.1.dev2",
        "server_url": "https://local.example",
    }
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    assert (await settings_transfer.async_export(hass))["entries"]["tools"] == bundle[
        "entries"
    ]["tools"]
    # The recreated entry enforces the imported directories without a restart.
    token = await hass.services.async_call(
        DOMAIN, SERVICE_GET_CALLER_TOKEN, {}, blocking=True, return_response=True
    )
    assert token is not None
    live = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_ALLOWED_PATHS,
        {CALLER_TOKEN_FIELD: token["token"]},
        blocking=True,
        return_response=True,
    )
    assert live is not None
    assert live["paths"] == ["blueprints"]
    assert await settings_transfer.async_import(hass, bundle) is False
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    assert await settings_transfer.async_export(hass) == bundle


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("data", "admin_token", "foreign"),
        ("options", "pip_spec", "foreign"),
        ("options", "server_url", "https://foreign.invalid"),
        ("options", "external_url", "https://example.invalid/\nnext"),
        ("tools", "allowed_dirs", ["../outside"]),
        ("tools", "extra_yaml_keys", ["homeassistant"]),
        ("data", "secret_path", "/"),
        ("options", "enable_webhook", "false"),
        ("options", "webhook_auth", "legacy"),
    ],
)
async def test_invalid_backup_cannot_change_even_an_earlier_valid_field(
    hass: HomeAssistant,
    section: str,
    field: str,
    value: object,
) -> None:
    add_options_entry(hass, data={"webhook_id": "main-hook", "secret_path": "/main"})
    original = await settings_transfer.async_export(hass)
    invalid = copy.deepcopy(original)
    invalid["entries"]["server"]["data"]["webhook_id"] = "mirror-hook"
    if section == "tools":
        invalid["entries"]["tools"] = {"allowed_dirs": [], "extra_yaml_keys": []}
        invalid["entries"]["tools"][field] = value
    else:
        invalid["entries"]["server"][section][field] = value
    with pytest.raises(settings_transfer.TransferRefused):
        await settings_transfer.async_import(hass, invalid)
    assert await settings_transfer.async_export(hass) == original


@pytest.mark.parametrize(
    ("state", "locked"),
    [
        (ConfigEntryState.LOADED, True),
        (ConfigEntryState.SETUP_IN_PROGRESS, False),
        (ConfigEntryState.UNLOAD_IN_PROGRESS, False),
    ],
)
async def test_import_rejects_a_transitioning_tools_entry_before_writing(
    hass: HomeAssistant, state: ConfigEntryState, locked: bool
) -> None:
    add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
    target = MockConfigEntry(domain=DOMAIN, data={"entry_type": "tools"}, state=state)
    target.add_to_hass(hass)
    original = await settings_transfer.async_export(hass)
    candidate = copy.deepcopy(original)
    candidate["server_settings"]["backup"] = {"auto_backup_retain_per_entity": 18}
    async with target.setup_lock if locked else contextlib.nullcontext():
        with pytest.raises(settings_transfer.TransferRefused, match="tools_entry_busy"):
            await settings_transfer.async_import(hass, candidate)
    assert await settings_transfer.async_export(hass) == original


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
    ],
)
async def test_import_requires_enabled_tools_to_be_loaded(
    hass: HomeAssistant, state: ConfigEntryState
) -> None:
    add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
    MockConfigEntry(
        domain=DOMAIN, data={"entry_type": "tools"}, state=state
    ).add_to_hass(hass)
    original = await settings_transfer.async_export(hass)
    candidate = copy.deepcopy(original)
    candidate["server_settings"]["backup"] = {"auto_backup_retain_per_entity": 18}
    with pytest.raises(
        settings_transfer.TransferRefused, match="tools_entry_unavailable"
    ):
        await settings_transfer.async_import(hass, candidate)
    assert await settings_transfer.async_export(hass) == original


async def test_original_import_error_survives_undo_failure(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
    candidate = await settings_transfer.async_export(hass)
    candidate["server_settings"]["backup"] = {"auto_backup_retain_per_entity": 18}
    primary = FileNotFoundError("primary")

    def fail_entry(*args, **kwargs):
        raise primary

    def fail_undo(*args):
        raise TimeoutError("private undo detail")

    monkeypatch.setattr(type(hass.config_entries), "async_update_entry", fail_entry)
    monkeypatch.setattr(server_transfer, "rollback_server_settings", fail_undo)
    with pytest.raises(FileNotFoundError) as caught:
        await settings_transfer.async_import(hass, candidate)
    assert caught.value is primary
    assert "Settings import rollback incomplete" in caught.value.__notes__
    assert "could not undo server settings (TimeoutError)" in caplog.text
    assert "private undo detail" not in caplog.text


async def test_failed_entry_update_restores_prior_tools_and_server_file(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
    await add_tools_entry(hass, loaded=True)
    original = await settings_transfer.async_export(hass)
    candidate = copy.deepcopy(original)
    candidate["entries"]["server"]["data"]["webhook_id"] = "mirror"
    candidate["entries"]["tools"] = {
        "allowed_dirs": ["blueprints"],
        "extra_yaml_keys": ["mqtt"],
    }
    candidate["server_settings"]["tools"] = {"tools": {}}

    manager_type = type(hass.config_entries)
    update = manager_type.async_update_entry

    def fail_final_update(manager, entry, **changes):
        if entry is server:
            raise OSError("storage failure")
        return update(manager, entry, **changes)

    monkeypatch.setattr(manager_type, "async_update_entry", fail_final_update)
    with pytest.raises(OSError, match="storage failure"):
        await settings_transfer.async_import(hass, candidate)
    assert await settings_transfer.async_export(hass) == original


async def test_tools_failure_keeps_policy_and_permission_saves_made_after_import(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    call = await make_file_service(hass)
    add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
    candidate = await settings_transfer.async_export(hass)
    candidate["server_settings"]["policy"] = {"wait_seconds": 12}
    candidate["server_settings"]["tools"] = {"tools": {}}
    candidate["entries"]["tools"] = {
        "allowed_dirs": ["blueprints"],
        "extra_yaml_keys": ["mqtt"],
    }
    directory = Path(hass.config.path(SERVER_CONFIG_SUBDIR))

    def edit_policy() -> None:
        with config_file_lock(directory):
            policy = load_policy(directory)
            assert policy.wait_seconds == 12
            save_policy(directory, policy.model_copy(update={"wait_seconds": 43}))

    save = Store.async_save

    async def fail_last_write(store, data):
        if data == {"keys": ["mqtt"]}:
            await hass.async_add_executor_job(edit_policy)
            # An ordinary writer explicitly kept this imported value. Its
            # save still wins even though a content-only check would match.
            assert (await call(SERVICE_SET_ALLOWED_PATHS, {"paths": ["blueprints"]}))[
                "paths"
            ] == ["blueprints"]
            raise OSError("tools store unavailable")
        await save(store, data)

    monkeypatch.setattr(Store, "async_save", fail_last_write)
    with pytest.raises(OSError, match="tools store unavailable"):
        await settings_transfer.async_import(hass, candidate)
    restored = await settings_transfer.async_export(hass)
    assert restored["server_settings"]["policy"]["wait_seconds"] == 43
    assert restored["entries"]["tools"]["allowed_dirs"] == ["blueprints"]
    assert restored["server_settings"]["tools"] is None
    assert restored["entries"]["tools"]["extra_yaml_keys"] == []
    assert "Kept allowed_dirs because it changed after settings import" in caplog.text


async def test_importing_a_changed_search_timeout_requests_a_home_assistant_restart(
    hass: HomeAssistant,
) -> None:
    add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
    backup = await settings_transfer.async_export(hass)
    backup["server_settings"]["features"] = {"individual_config_timeout": 27.0}
    assert await settings_transfer.async_import(hass, backup) is True
    assert await settings_transfer.async_import(hass, backup) is False


async def test_unconfirmed_tools_store_write_restores_the_other_setting(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
    await add_tools_entry(hass, loaded=True)
    original = await settings_transfer.async_export(hass)
    candidate = copy.deepcopy(original)
    candidate["entries"]["tools"] = {
        "allowed_dirs": ["blueprints"],
        "extra_yaml_keys": ["mqtt"],
    }

    save = Store.async_save

    async def skip_write(store, data):
        if data != {"keys": ["mqtt"]}:
            await save(store, data)

    monkeypatch.setattr(Store, "async_save", skip_write)
    with pytest.raises(OSError, match="Could not persist"):
        await settings_transfer.async_import(hass, candidate)
    assert await settings_transfer.async_export(hass) == original


@pytest.mark.parametrize(
    ("source", "target", "error"),
    [
        ("absent", "enabled", "extra_tools_entry"),
        ("disabled", "absent", "tools_must_be_disabled"),
        ("disabled", "enabled", "tools_must_be_disabled"),
        ("enabled", "disabled", "tools_must_be_enabled"),
    ],
)
async def test_conflicting_tools_permissions_cannot_change_the_destination(
    hass: HomeAssistant,
    source: str,
    target: str,
    error: str,
) -> None:
    add_options_entry(hass, data={"webhook_id": "destination", "secret_path": "/local"})
    if target != "absent":
        MockConfigEntry(
            domain=DOMAIN,
            data={"entry_type": "tools"},
            disabled_by=ConfigEntryDisabler.USER if target == "disabled" else None,
        ).add_to_hass(hass)
    original = await settings_transfer.async_export(hass)
    backup = copy.deepcopy(original)
    backup["entries"]["server"]["data"]["webhook_id"] = "source"
    backup["server_settings"]["tools"] = {"tools": {}}
    if source == "absent":
        backup["entries"].pop("tools")
    else:
        backup["entries"]["tools"] = {
            "disabled": source == "disabled",
            "allowed_dirs": [],
            "extra_yaml_keys": [],
        }

    with pytest.raises(settings_transfer.TransferRefused) as failure:
        await settings_transfer.async_import(hass, backup)
    assert failure.value.code == error
    assert await settings_transfer.async_export(hass) == original


async def test_disabled_tools_round_trip_without_activating_privileged_services(
    hass: HomeAssistant,
) -> None:
    add_options_entry(hass, data={"webhook_id": "source", "secret_path": "/private"})
    tools = MockConfigEntry(
        domain=DOMAIN,
        data={"entry_type": "tools"},
        disabled_by=ConfigEntryDisabler.USER,
    )
    tools.add_to_hass(hass)
    backup = await settings_transfer.async_export(hass)
    assert backup["entries"]["tools"]["disabled"] is True
    backup["entries"]["tools"]["extra_yaml_keys"] = ["mqtt"]
    await settings_transfer.async_import(hass, backup)
    assert tools.disabled_by is ConfigEntryDisabler.USER
    assert not hass.services.has_service(DOMAIN, "write_file")
    assert (await settings_transfer.async_export(hass)) == backup


async def test_ignored_discovery_is_not_exported_as_a_tools_installation(
    hass: HomeAssistant,
) -> None:
    add_options_entry(hass, data={"webhook_id": "source", "secret_path": "/private"})
    MockConfigEntry(domain=DOMAIN, source=SOURCE_IGNORE, data={}).add_to_hass(hass)
    backup = await settings_transfer.async_export(hass)
    assert set(backup["entries"]) == {"server"}


async def test_failed_import_removes_the_tools_entry_it_created(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
    original = await settings_transfer.async_export(hass)
    candidate = copy.deepcopy(original)
    candidate["entries"]["server"]["data"]["webhook_id"] = "mirror"
    candidate["entries"]["tools"] = {
        "allowed_dirs": ["blueprints"],
        "extra_yaml_keys": [],
    }

    manager_type = type(hass.config_entries)
    update = manager_type.async_update_entry

    def fail_final_update(manager, entry, **changes):
        if entry is server:
            raise OSError("storage failure")
        return update(manager, entry, **changes)

    monkeypatch.setattr(manager_type, "async_update_entry", fail_final_update)
    with pytest.raises(OSError, match="storage failure"):
        await settings_transfer.async_import(hass, candidate)
    assert hass.config_entries.async_entries(DOMAIN) == [server]
    assert not hass.services.has_service(DOMAIN, "write_file")
    assert await settings_transfer.async_export(hass) == original


async def test_backup_whose_webhook_another_integration_owns_changes_nothing(
    hass: HomeAssistant,
) -> None:
    assert await async_setup_component(hass, "webhook", {})
    add_options_entry(hass, data={"webhook_id": "main-hook", "secret_path": "/main"})
    original = await settings_transfer.async_export(hass)
    webhook.async_register(hass, "other", "Other", "taken-hook", AsyncMock())
    candidate = copy.deepcopy(original)
    candidate["entries"]["server"]["data"]["webhook_id"] = "taken-hook"
    with pytest.raises(settings_transfer.TransferRefused, match="webhook_in_use"):
        await settings_transfer.async_import(hass, candidate)
    assert await settings_transfer.async_export(hass) == original


@pytest.mark.parametrize(
    ("data", "options", "changed"),
    [
        (
            {
                "oauth_client_id": "client",
                "oauth_client_secret": "secret",
                "oauth_signing_key": "cd" * 32,
            },
            {"webhook_auth": "legacy"},
            {"data": {"oauth_signing_key": "ef" * 32}},
        ),
        ({}, {"enable_webhook": False}, {"options": {"enable_webhook": True}}),
    ],
)
async def test_import_that_needs_new_http_routes_asks_for_a_restart(
    hass: HomeAssistant,
    data: dict[str, str],
    options: dict[str, object],
    changed: dict[str, dict[str, object]],
) -> None:
    add_options_entry(
        hass,
        data={"webhook_id": "hook", "secret_path": "/path", **data},
        options=options,
    )
    backup = await settings_transfer.async_export(hass)
    for section, values in changed.items():
        backup["entries"]["server"][section].update(values)
    assert await settings_transfer.async_import(hass, backup) is True


async def test_import_reloads_a_running_server_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    server: SimpleNamespace = replace_server(monkeypatch)
    entry = add_server_entry(hass)
    assert await set_up(hass, entry)
    own = await settings_transfer.async_export(hass)
    started = server.bring_up.await_count

    # The running server's own webhook is not a conflict.
    assert await settings_transfer.async_import(hass, own) is False
    await hass.async_block_till_done()
    assert server.bring_up.await_count == started

    restricted = copy.deepcopy(own)
    restricted["server_settings"]["tools"] = {"tools": {"ha_get_history": "disabled"}}
    assert await settings_transfer.async_import(hass, restricted) is False
    await hass.async_block_till_done()
    assert server.bring_up.await_count == started + 1


def _store_features(hass: HomeAssistant, contents: str) -> None:
    directory = Path(hass.config.path(SERVER_CONFIG_SUBDIR))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "feature_flags.json").write_text(contents, encoding="utf-8")


async def test_unreadable_destination_file_is_not_blamed_on_the_backup(
    hass: HomeAssistant,
) -> None:
    add_options_entry(hass, data={"webhook_id": "hook", "secret_path": "/path"})
    backup = await settings_transfer.async_export(hass)
    await hass.async_add_executor_job(_store_features, hass, "{")
    for transfer in (
        settings_transfer.async_export(hass),
        settings_transfer.async_import(hass, backup),
    ):
        with pytest.raises(settings_transfer.TransferRefused) as failure:
            await transfer
        assert failure.value.code == "invalid_server_settings"


async def test_undo_restores_stored_directories_startup_would_drop(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
    await add_tools_entry(hass, loaded=True)
    # A directory a later release refuses stays in the raw store until saved.
    store: Store[dict[str, list[str]]] = Store(hass, 1, "ha_mcp_tools_allowed_paths")
    await store.async_save({"paths": ["../outside"]})
    candidate = await settings_transfer.async_export(hass)
    candidate["entries"]["tools"]["allowed_dirs"] = ["blueprints"]
    candidate["entries"]["server"]["data"]["webhook_id"] = "mirror"
    primary = OSError("storage failure")
    manager_type = type(hass.config_entries)
    update = manager_type.async_update_entry

    def fail_final_update(manager, entry, **changes):
        if entry is server:
            raise primary
        return update(manager, entry, **changes)

    monkeypatch.setattr(manager_type, "async_update_entry", fail_final_update)
    with pytest.raises(OSError) as caught:
        await settings_transfer.async_import(hass, candidate)
    assert caught.value is primary
    assert "Settings import rollback incomplete" not in getattr(
        caught.value, "__notes__", ()
    )
    assert (await settings_transfer.async_export(hass))["entries"]["tools"][
        "allowed_dirs"
    ] == []


async def test_import_onto_an_ignored_tools_discovery_says_why_it_stopped(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    add_options_entry(hass, data={"webhook_id": "hook", "secret_path": "/path"})
    MockConfigEntry(
        domain=DOMAIN, source=SOURCE_IGNORE, unique_id=DOMAIN, data={}
    ).add_to_hass(hass)
    original = await settings_transfer.async_export(hass)
    backup = copy.deepcopy(original)
    backup["entries"]["tools"] = {"allowed_dirs": [], "extra_yaml_keys": []}
    with pytest.raises(settings_transfer.TransferRefused, match="restore_failed"):
        await settings_transfer.async_import(hass, backup)
    assert "could not add the File & YAML Tools entry (already_configured)" in (
        caplog.text
    )
    assert await settings_transfer.async_export(hass) == original


async def test_broken_server_package_is_logged_not_hidden(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add_options_entry(hass, data={"webhook_id": "hook", "secret_path": "/path"})

    class BrokenDependency(importlib.abc.MetaPathFinder):
        def find_spec(self, name: str, path: object, target: object = None) -> None:
            if name == "ha_mcp.settings_transfer":
                raise ImportError("a dependency of the package is broken")

    monkeypatch.delitem(sys.modules, "ha_mcp.settings_transfer")
    monkeypatch.delattr("ha_mcp.settings_transfer")
    monkeypatch.setattr(sys, "meta_path", [BrokenDependency(), *sys.meta_path])
    with pytest.raises(settings_transfer.TransferRefused, match="server_unavailable"):
        await settings_transfer.async_export(hass)
    assert "The HA-MCP server package failed to import" in caplog.text


async def test_another_installations_file_keeps_the_connection_and_tools_entry(
    hass: HomeAssistant,
) -> None:
    server = add_options_entry(hass, data={"webhook_id": "hook", "secret_path": "/p"})
    await add_tools_entry(hass, loaded=True)
    exported = await settings_transfer.async_export(hass)
    standalone = {
        "format": "ha-mcp-settings",
        "version": 1,
        "contains_credentials": False,
        "entries": {},
        "server_settings": {
            **exported["server_settings"],
            "backup": {"auto_backup_retain_per_entity": 21},
        },
    }
    with pytest.raises(settings_transfer.TransferRefused, match="invalid_backup"):
        await settings_transfer.async_import(
            hass, {**standalone, "contains_credentials": True}
        )

    assert await settings_transfer.async_import(hass, standalone) is False
    assert server.data["webhook_id"] == "hook"
    after = await settings_transfer.async_export(hass)
    assert after["server_settings"]["backup"] == {"auto_backup_retain_per_entity": 21}
    assert after["entries"]["tools"] == exported["entries"]["tools"]


async def test_a_file_without_permissions_ignores_an_unloaded_tools_entry(
    hass: HomeAssistant,
) -> None:
    add_options_entry(hass, data={"webhook_id": "hook", "secret_path": "/p"})
    MockConfigEntry(
        domain=DOMAIN, data={"entry_type": "tools"}, state=ConfigEntryState.SETUP_RETRY
    ).add_to_hass(hass)
    exported = await settings_transfer.async_export(hass)
    standalone = {
        "format": "ha-mcp-settings",
        "version": 1,
        "contains_credentials": False,
        "entries": {},
        "server_settings": {
            **exported["server_settings"],
            "backup": {"auto_backup_retain_per_entity": 22},
        },
    }

    await settings_transfer.async_import(hass, standalone)

    after = await settings_transfer.async_export(hass)
    assert after["server_settings"]["backup"] == {"auto_backup_retain_per_entity": 22}
