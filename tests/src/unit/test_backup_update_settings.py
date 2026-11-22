"""``ha_manage_backup`` reads and changes Home Assistant's backup-before-update
settings as the Backups settings page does, through Core's
``hassio/update/config/info`` and ``hassio/update/config/update``.

Core's ``hassio`` WebSocket API answers every call: in front of the stand-in
Supervisor (``_tools_addons_core.core_supervisor``) for a settings change, and
on the Supervisor install of ``_backup_snapshot_core`` for the snapshot list.
The settings live in the ``hassio`` config entry's options, as on a Supervisor
install.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.helpers import core_error_message

from ._backup_core import backup_settings as backup_settings
from ._backup_core import manage_backup
from ._backup_snapshot_core import BackupInstall
from ._backup_snapshot_core import backup_install as backup_install
from ._client_spies import sent_messages
from ._command_faults import (
    before_handling,
    dropped_on,
    pooled_socket,
    registry_lookup_failing,
)
from ._tool_errors import error_body, error_of
from ._tools_addons_core import Supervisor
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

INFO = "hassio/update/config/info"
UPDATE = "hassio/update/config/update"

pytestmark = pytest.mark.usefixtures("backup_settings")


def hassio_entry(hass: HomeAssistant, **options: Any) -> ConfigEntry:
    """The ``hassio`` config entry a Supervisor install has, holding ``options``."""
    entry = MockConfigEntry(domain="hassio", options=options)
    entry.add_to_hass(hass)
    return entry


async def core_settings(client: HomeAssistantClient) -> Any:
    """The settings Core's ``info`` answers now."""
    reply = await client.send_websocket_message({"type": INFO})
    assert reply["success"], reply
    return reply["result"]


async def configure(client: HomeAssistantClient, settings: Any) -> Any:
    return await manage_backup(
        client,
        scope="snapshot",
        action="configure",
        update_backup_settings=settings,
    )


async def test_configure_stores_the_setting_and_returns_cores_readback(
    hass: HomeAssistant,
    core_supervisor: Supervisor,
    core_client: HomeAssistantClient,
) -> None:
    """The change is what the Backups page would store, and the answer is what
    Home Assistant holds afterwards, not an echo of the request."""
    entry = hassio_entry(hass, core_backup_before_update=True)

    result = await configure(core_client, {"add_on_backup_retain_copies": 3})

    assert entry.options["add_on_backup_retain_copies"] == 3
    assert entry.options["core_backup_before_update"] is True
    assert result["update_backup_settings"] == await core_settings(core_client)
    assert "warnings" not in result


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({"add_on_backup_retain_copies": 0}, id="retain-zero"),
        pytest.param({"backup_everything": True}, id="unknown-key"),
    ],
)
async def test_core_refusal_comes_back_with_its_text_and_stores_nothing(
    hass: HomeAssistant,
    core_supervisor: Supervisor,
    core_client: HomeAssistantClient,
    settings: dict[str, Any],
) -> None:
    """A value Core would refuse must not be judged or reworded by the tool."""
    entry = hassio_entry(hass)

    with pytest.raises(ToolError) as exc_info:
        await configure(core_client, settings)

    reply = await core_client.send_websocket_message({"type": UPDATE, **settings})
    assert error_body(exc_info)["ha_error_code"] == reply["error_code"]
    assert core_error_message(reply["error"]) in error_of(exc_info)["message"]
    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert entry.options == {}


async def test_configure_without_a_supervisor_says_it_needs_one(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await configure(core_client, {"core_backup_before_update": True})

    assert error_of(exc_info)["code"] == "RESOURCE_NOT_FOUND"
    assert "Supervisor installation" in error_of(exc_info)["message"]
    assert error_body(exc_info)["ha_error_code"] == "unknown_command"


async def test_a_user_core_does_not_answer_is_told_who_may(
    hass: HomeAssistant,
    core_supervisor: Supervisor,
    core_client: HomeAssistantClient,
    read_only_client: HomeAssistantClient,
) -> None:
    """Core answers these settings only to an administrator. Its refusal carries
    the code Supervisor also refuses an app's ``hassio/`` commands with, so the
    tool names both causes instead of a bare "Unauthorized"."""
    entry = hassio_entry(hass)

    with pytest.raises(ToolError) as exc_info:
        await configure(read_only_client, {"core_backup_before_update": True})

    reply = await read_only_client.send_websocket_message(
        {"type": UPDATE, "core_backup_before_update": True}
    )
    assert error_body(exc_info)["ha_error_code"] == reply["error_code"]
    assert core_error_message(reply["error"]) in error_of(exc_info)["message"]
    assert error_of(exc_info)["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
    assert "Home Assistant app" in error_of(exc_info)["message"]
    assert entry.options == {}


async def test_list_from_the_app_keeps_the_snapshots_and_says_why_settings_are_missing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    backup_install: BackupInstall,
) -> None:
    """Supervisor's proxy answers an app's ``hassio/`` command with Core's own
    ``unauthorized`` refusal; Core gives it here as it would to any caller it
    refuses."""
    await backup_install.supervisor()
    hassio_entry(hass)

    def refuse(msg: dict[str, Any]) -> None:
        raise Unauthorized

    with before_handling(hass, INFO, refuse):
        result = await manage_backup(core_client, scope="snapshot", action="list")

    assert "backups" in result
    assert "update_backup_settings" not in result
    assert any("Home Assistant app" in w for w in result["warnings"])


@pytest.mark.parametrize("settings", [None, {}], ids=["missing", "empty"])
async def test_configure_with_nothing_to_change_sends_nothing(
    core_client: HomeAssistantClient, settings: dict[str, Any] | None
) -> None:
    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await configure(core_client, settings)

    assert error_of(exc_info)["code"] == "VALIDATION_MISSING_PARAMETER"
    sent.assert_not_awaited()


async def test_settings_cannot_carry_another_command(
    hass: HomeAssistant,
    core_supervisor: Supervisor,
    core_client: HomeAssistantClient,
) -> None:
    """A ``type`` inside the settings would send some other command past the
    backup controls."""
    hassio_entry(hass)

    with sent_messages(core_client) as sent, pytest.raises(ToolError):
        await configure(core_client, {"type": "backup/generate"})

    sent.assert_not_awaited()


async def test_a_change_that_got_no_answer_may_have_been_applied(
    hass: HomeAssistant,
    core_supervisor: Supervisor,
    core_client: HomeAssistantClient,
) -> None:
    """Called a failure without that, it would invite a blind retry."""
    hassio_entry(hass)

    with (
        dropped_on(hass, UPDATE, await pooled_socket(core_client)),
        pytest.raises(ToolError) as exc_info,
    ):
        await configure(core_client, {"core_backup_before_update": True})

    assert error_of(exc_info)["code"] == "CONNECTION_FAILED"
    assert "may have been applied" in error_of(exc_info)["message"]


async def test_a_stored_change_whose_readback_fails_is_still_a_success(
    hass: HomeAssistant,
    core_supervisor: Supervisor,
    core_client: HomeAssistantClient,
) -> None:
    """Home Assistant took the change; reporting it as failed would hide that."""
    entry = hassio_entry(hass)

    with registry_lookup_failing(hass, INFO):
        result = await configure(core_client, {"core_backup_before_update": True})

    assert entry.options["core_backup_before_update"] is True
    assert result["success"] is True
    assert "update_backup_settings" not in result
    assert result["warnings"]


async def test_list_on_a_supervisor_install_carries_cores_settings(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    backup_install: BackupInstall,
) -> None:
    await backup_install.supervisor()
    hassio_entry(hass, add_on_backup_before_update=True)

    result = await manage_backup(core_client, scope="snapshot", action="list")

    assert result["update_backup_settings"] == await core_settings(core_client)
    assert result["update_backup_settings"]["add_on_backup_before_update"] is True


async def test_list_without_a_supervisor_has_no_settings_and_no_warning(
    core_client: HomeAssistantClient, backup_install: BackupInstall
) -> None:
    """A Core install has no such settings; that is not a failure to report."""
    await backup_install.core()

    result = await manage_backup(core_client, scope="snapshot", action="list")

    assert "update_backup_settings" not in result
    assert "warnings" not in result


@pytest.mark.parametrize("failure", ["refused", "dropped"])
async def test_a_settings_read_that_fails_keeps_the_list_and_says_so(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    backup_install: BackupInstall,
    failure: str,
) -> None:
    """The snapshot list is what a caller checks before a restore; losing it to
    a side read would leave them blind."""
    await backup_install.supervisor()
    hassio_entry(hass)
    if failure == "refused":
        fault = registry_lookup_failing(hass, INFO)
    else:
        fault = dropped_on(hass, INFO, await pooled_socket(core_client))

    with fault:
        result = await manage_backup(core_client, scope="snapshot", action="list")

    assert result["success"] is True
    assert "backups" in result
    assert "update_backup_settings" not in result
    assert any("backup-before-update" in w for w in result["warnings"])
