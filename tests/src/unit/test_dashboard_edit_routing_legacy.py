"""Dashboard edits without the component: Core's ``lovelace/config/save``.

Without the component the server reads the dashboard from Core, applies the
edit itself and saves through Core's own command, once. A save Core may have
applied is never sent again and is reported as an unknown outcome; only
Core's definite refusals are reported as unwritten.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.utils.config_hash import compute_config_hash

from ._client_spies import sent_commands
from ._command_faults import dropped_on, pooled_socket, received
from ._config_dir_yaml import write_config_files
from ._dashboard_edit_routing_core import (
    DOCUMENT,
    DOCUMENT_HASH,
    EDIT,
    EDITS,
    READ,
    SAVE,
    URL,
    advice,
    failing,
    lovelace,
    refused,
    refused_on_save,
    registry_row,
    saved_over,
    set_dashboard,
    stored,
)
from ._held_command import held_command
from ._routing_core import sent_types
from ._tool_errors import error_body

ICON = "/views/0/cards/0/icon"


async def test_without_the_component_a_patch_is_saved_once_through_core(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await lovelace(hass, core_client)
    socket = await pooled_socket(core_client)
    patch = [{"op": "replace", "path": ICON, "value": "mdi:music-box-multiple"}]

    with sent_commands(socket) as spy, received(hass, SAVE) as seen:
        result = await set_dashboard(
            core_client, patch=patch, config_hash=DOCUMENT_HASH
        )

    after = await stored(core_client)
    assert after["views"][0]["cards"][0]["icon"] == "mdi:music-box-multiple"
    assert result["config_hash"] == compute_config_hash(after)
    assert (result["write_committed"], result["post_write_verified"]) == (True, True)
    assert len(seen[SAVE]) == 1
    assert EDIT not in sent_types(spy)


@pytest.mark.parametrize("as_json", [False, True])
async def test_patch_values_are_stored_literally(
    hass: HomeAssistant, core_client: HomeAssistantClient, as_json: bool
) -> None:
    """A template or JSON-looking string is a value, never rendered or parsed;
    the patch itself may arrive as a JSON string."""
    await lovelace(hass, core_client)
    value = '{"literal": "{{ do_not_render }}"}'
    patch = [{"op": "add", "path": "/title", "value": value}]

    result = await set_dashboard(
        core_client,
        patch=json.dumps(patch) if as_json else patch,
        config_hash=DOCUMENT_HASH,
    )

    assert result["action"] == "patch"
    assert (await stored(core_client))["title"] == value


async def test_a_patch_that_changes_nothing_is_not_saved(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await lovelace(hass, core_client)
    same = [{"op": "replace", "path": ICON, "value": "mdi:lamp"}]

    with received(hass, READ, SAVE) as seen:
        result = await set_dashboard(core_client, patch=same, config_hash=DOCUMENT_HASH)

    assert result["unchanged"] is True
    assert (result["write_committed"], result["post_write_verified"]) == (False, True)
    assert result["config_hash"] == DOCUMENT_HASH
    assert "render_paths" in result
    assert (len(seen[READ]), seen[SAVE]) == (1, [])


async def test_true_replaced_by_one_is_a_change_that_is_saved(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Python holds ``True == 1``; the dashboard's JSON does not."""
    config = {**DOCUMENT, "counter": True}
    await lovelace(hass, core_client, config)

    with received(hass, SAVE) as seen:
        result = await set_dashboard(
            core_client,
            patch=[{"op": "replace", "path": "/counter", "value": 1}],
            config_hash=compute_config_hash(config),
        )

    counter = (await stored(core_client))["counter"]
    assert counter == 1
    assert counter is not True
    assert result["write_committed"] is True
    assert "unchanged" not in result
    assert len(seen[SAVE]) == 1


@pytest.mark.parametrize(
    ("mode", "fault"),
    [
        ("patch", "dropped"),
        ("patch", "cancelled"),
        ("patch", "core_error"),
        ("python_transform", "core_error"),
    ],
)
async def test_a_save_core_may_have_applied_is_unknown_and_never_resent(
    hass: HomeAssistant, core_client: HomeAssistantClient, mode: str, fault: str
) -> None:
    """The connection drops once Core has the save; the call is cancelled
    while Core holds it; or Core applies it and then fails."""
    await lovelace(hass, core_client)

    body, saves = await refused_on_save(
        hass, core_client, fault, config_hash=DOCUMENT_HASH, **{mode: EDITS[mode]}
    )

    assert body["action"] == mode
    assert body["reason"] == "write_outcome_unknown"
    assert (body["write_committed"], body["post_write_verified"]) == (None, False)
    assert saves == 1
    applied = fault != "dropped"
    assert (await stored(core_client) == {"views": []}) is applied


@pytest.mark.parametrize(
    ("prior", "failure", "config_committed"),
    [
        ("metadata", "conflict", False),
        ("create", "invalid", False),
        ("create", "dropped", None),
        ("metadata", "cancelled", None),
    ],
)
async def test_a_failed_config_write_still_reports_the_registry_write_before_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    prior: str,
    failure: str,
    config_committed: bool | None,
) -> None:
    """A new title or a new dashboard is in Core's registry before the config
    is written; the error says so whatever became of the config. A stale
    hash and a config that is no object stop it before any save."""
    await lovelace(hass, core_client)
    url_path = URL if prior == "metadata" else "new-dashboard"

    body, saves = await refused_on_save(
        hass,
        core_client,
        failure if failure in ("dropped", "cancelled") else None,
        url_path,
        title="After",
        config="[]" if failure == "invalid" else {"views": []},
        config_hash="stale" if failure == "conflict" else None,
    )

    assert body["write_committed"] is True
    assert body["config_write_committed"] is config_committed
    assert body["dashboard_created"] is (prior == "create")
    assert body["metadata_updated"] is (prior == "metadata")
    assert (await registry_row(core_client, url_path) or {}).get("title") == "After"
    assert saves == (0 if config_committed is False else 1)
    if failure == "conflict":
        assert await stored(core_client) == DOCUMENT


@pytest.mark.parametrize("readback", ["another_writer", "core_error", "disconnected"])
async def test_a_replacement_reports_the_config_core_holds_after_it(
    hass: HomeAssistant, core_client: HomeAssistantClient, readback: str
) -> None:
    """The saved config is read back: what another writer saved meanwhile is
    what the hash describes. A failed read-back leaves the save committed
    but unverified, with no render paths for a config nobody read."""
    await lovelace(hass, core_client)
    later = {"title": "Later writer", "views": []}
    url_path = URL
    socket = await pooled_socket(core_client)

    with received(hass, SAVE) as seen:
        if readback == "core_error":
            # The first read is the hash check before the replacement.
            with failing(hass, "async_json", from_call=2):
                result = await set_dashboard(core_client, config={"views": []})
        elif readback == "disconnected":
            # A new dashboard is not read before its first save.
            url_path = "new-dashboard"
            with dropped_on(hass, READ, socket):
                result = await set_dashboard(
                    core_client, url_path, title="New", config={"views": []}
                )
        else:
            with saved_over(hass, later):
                result = await set_dashboard(core_client, config={"views": []})

    assert result["write_committed"] is True
    assert result["post_write_verified"] is (readback == "another_writer")
    assert len(seen[SAVE]) == 1
    if readback == "another_writer":
        assert result["config_hash"] == compute_config_hash(later)
    else:
        assert result["config_hash"] is None
        assert any("render paths unavailable" in w for w in result["warnings"])
        assert "render_paths" not in result
    assert await stored(core_client, url_path) in ({"views": []}, later)


@pytest.mark.parametrize("rejection", ["deleted", "not_admin", "recovery_mode"])
async def test_only_a_save_core_refused_outright_is_reported_unwritten(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    read_only_client: HomeAssistantClient,
    rejection: str,
) -> None:
    """Core refuses the save of a dashboard deleted while it was on its way,
    from a user who is not an admin, and in recovery mode (where Core reads
    no config, so only a replacement reaches the save)."""
    dashboard_id = await lovelace(hass, core_client)
    client = read_only_client if rejection == "not_admin" else core_client
    edit = (
        {"config": {"views": []}}
        if rejection == "recovery_mode"
        else {"patch": EDITS["patch"], "config_hash": DOCUMENT_HASH}
    )
    if rejection == "recovery_mode":
        hass.config.recovery_mode = True

    with received(hass, SAVE) as seen:
        if rejection == "deleted":
            async with held_command(hass, SAVE) as held:
                task = asyncio.create_task(set_dashboard(client, **edit))
                await held.reached_by(task)
                deleted = await client.send_websocket_message(
                    {"type": "lovelace/dashboards/delete", "dashboard_id": dashboard_id}
                )
                assert deleted["success"], deleted
                held.release.set()
                with pytest.raises(ToolError) as caught:
                    await asyncio.gather(task)
            body = error_body(caught)
        else:
            body = await refused(client, **edit)

    code, reason = {
        "deleted": ("RESOURCE_NOT_FOUND", "not_found"),
        "not_admin": ("AUTH_INSUFFICIENT_PERMISSIONS", "unauthorized"),
        "recovery_mode": ("SERVICE_CALL_FAILED", "recovery_mode"),
    }[rejection]
    assert (body["error"]["code"], body["reason"]) == (code, reason)
    assert (body["write_committed"], body["post_write_verified"]) == (False, False)
    assert len(seen[SAVE]) == 1
    if rejection == "recovery_mode":
        assert "recovery mode" in advice(body)
        hass.config.recovery_mode = False
    if rejection != "deleted":
        assert await stored(core_client) == DOCUMENT


async def test_a_dashboard_core_cannot_read_is_not_reported_as_missing(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Its YAML file holds no dashboard: creating it with ``config`` is no fix."""
    await write_config_files(hass, {"broken.yaml": "- not a dashboard\n"})
    dashboards = {URL: {"mode": "yaml", "filename": "broken.yaml", "title": "Broken"}}
    assert await async_setup_component(
        hass, "lovelace", {"lovelace": {"dashboards": dashboards}}
    )

    with received(hass, SAVE) as seen:
        body = await refused(
            core_client, patch=EDITS["patch"], config_hash=DOCUMENT_HASH
        )

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert (body["reason"], body["write_committed"]) == ("load_failed", False)
    assert "'config'" not in advice(body)
    assert seen[SAVE] == []


async def test_a_dashboard_core_does_not_know_is_reported_as_missing(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's config_not_found is the one answer that means the dashboard is
    absent, so the edit says so instead of reporting a failed read."""
    assert await async_setup_component(hass, "lovelace", {})

    with received(hass, SAVE) as seen:
        body = await refused(
            core_client, patch=EDITS["patch"], config_hash=DOCUMENT_HASH
        )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert (body["reason"], body["write_committed"]) == ("not_found", False)
    assert seen[SAVE] == []
