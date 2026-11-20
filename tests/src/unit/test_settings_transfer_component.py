"""Restore a reusable sidebar backup against the destination's HA entries."""

from __future__ import annotations

import copy

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.ha_mcp_tools import settings_transfer
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN

from ._config_flow_core import (
    add_options_entry,
    add_tools_entry,
    start_options_flow,
    submit_options_step,
)


async def test_mirror_backup_restores_connection_and_permissions_on_main(
    hass: HomeAssistant,
) -> None:
    server = add_options_entry(
        hass,
        data={
            "access_enabled": False,
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
        },
    )

    assert await settings_transfer.async_import(hass, bundle)
    assert server.data["webhook_id"] == "mirror-hook"
    assert server.data["secret_path"] == "/mirror-path"
    assert server.data["dcr_signing_key"] == "ab" * 32
    assert server.data["oauth_client_secret"] == "legacy-secret"
    assert server.data["admin_token"] == "main-only"
    assert server.options == {
        "enable_webhook": True,
        "webhook_auth": "legacy",
        "pip_spec": "ha-mcp-dev==9.2.1.dev2",
        "server_url": "https://local.example",
    }
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    assert (await settings_transfer.async_export(hass))["entries"]["tools"] == bundle[
        "entries"
    ]["tools"]
    await settings_transfer.async_import(hass, bundle)
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    assert await settings_transfer.async_export(hass) == bundle


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("data", "admin_token", "foreign"),
        ("options", "pip_spec", "foreign"),
        ("options", "server_url", "https://foreign.invalid"),
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
    invalid["entries"]["server"][section][field] = value
    with pytest.raises(settings_transfer.InvalidBackup):
        await settings_transfer.async_import(hass, invalid)
    assert await settings_transfer.async_export(hass) == original


async def test_failed_entry_update_restores_prior_tools_and_server_file(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = add_options_entry(hass, data={"webhook_id": "old", "secret_path": "/old"})
    await add_tools_entry(hass)
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
