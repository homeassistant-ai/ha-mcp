"""Restore a reusable sidebar backup against the destination's HA entries."""

from __future__ import annotations

import copy

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import settings_transfer
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
            "webhook_id": "mirror-hook",
            "secret_path": "/mirror-path",
            "admin_token": "mirror-only",
            "dcr_signing_key": "ab" * 32,
        },
        options={
            "enable_webhook": True,
            "webhook_auth": "ha_auth",
            "pip_spec": "mirror-package",
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
        data={
            "entry_type": "server",
            "webhook_id": "old",
            "secret_path": "/old",
            "admin_token": "main-only",
        },
        options={
            "pip_spec": "main-package",
            "server_url": "https://local.example",
            "server_port": 1234,
        },
    )

    await settings_transfer.async_import(hass, bundle)
    assert server.data["webhook_id"] == "mirror-hook"
    assert server.data["secret_path"] == "/mirror-path"
    assert server.data["dcr_signing_key"] == "ab" * 32
    assert server.data["admin_token"] == "main-only"
    assert server.options == {
        "enable_webhook": True,
        "webhook_auth": "ha_auth",
        "pip_spec": "main-package",
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
