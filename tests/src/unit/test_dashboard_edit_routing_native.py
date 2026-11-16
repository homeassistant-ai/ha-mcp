"""Dashboard edits through the component's ``dashboard_edit`` command.

The component answers on a real ``hass`` holding a storage dashboard in Core's
own ``lovelace``. Its reply is the authoritative outcome: the server neither
reads the dashboard again nor falls back to Core's ``lovelace/config/save``
once the command may have run, so an edit is never written twice.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, component_dashboard_edit
from ha_mcp.tools.component_api import get_component_caps
from ha_mcp.utils.config_hash import compute_config_hash

from ._client_spies import sent_commands
from ._command_faults import dropped_on, pooled_socket, received
from ._component_frames import older_component
from ._dashboard_edit_routing_core import (
    DOCUMENT,
    DOCUMENT_HASH,
    EDIT,
    EDITS,
    INFO,
    READ,
    SAVE,
    URL,
    advice,
    after_load,
    failing,
    lovelace,
    refused,
    registry_row,
    set_dashboard,
    stored,
    use_backend,
)
from ._held_command import held_command
from ._routing_core import sent_types
from ._tool_errors import error_body

HOME = {"views": [{"title": "Before", "path": "home"}]}
RENAMED = {"views": [{"title": "New", "path": "home"}]}


@pytest.mark.parametrize("mode", ["patch", "python_transform", "config"])
async def test_an_edit_returns_cores_saved_config_without_reading_it_again(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, mode: str
) -> None:
    """Only a transform reads the dashboard, to run on it; the component sends
    the config it computed, never the transform or the server's action label."""
    client = component_core_client
    await lovelace(hass, client, HOME)
    edits: dict[str, Any] = {
        "patch": [{"op": "replace", "path": "/views/0/title", "value": "New"}],
        "python_transform": "config['views'][0]['title'] = 'New'",
        "config": RENAMED,
    }

    with received(hass, EDIT, READ, SAVE) as seen:
        result = await set_dashboard(
            client, config_hash=compute_config_hash(HOME), **{mode: edits[mode]}
        )

    assert await stored(client) == RENAMED
    assert result["config_hash"] == compute_config_hash(RENAMED)
    assert (result["write_committed"], result["post_write_verified"]) == (True, True)
    assert result["render_paths"][0]["render_path"] == f"{URL}/home"
    assert seen[SAVE] == []
    assert len(seen[READ]) == (1 if mode == "python_transform" else 0)
    (frame,) = seen[EDIT]
    assert frame["expected_hash"] == compute_config_hash(HOME)
    assert "python_transform" not in frame
    assert "action" not in frame
    sent = RENAMED if mode == "python_transform" else edits[mode]
    assert frame["config" if mode != "patch" else "patch"] == sent


async def test_a_patch_that_changes_nothing_is_reported_unchanged_and_not_saved(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await lovelace(hass, component_core_client)

    with received(hass, READ, SAVE) as seen:
        result = await set_dashboard(
            component_core_client, patch=[], config_hash=DOCUMENT_HASH
        )

    assert result["unchanged"] is True
    assert (result["write_committed"], result["post_write_verified"]) == (False, True)
    assert result["config_hash"] == DOCUMENT_HASH
    assert "render_paths" in result
    assert seen == {READ: [], SAVE: []}


async def test_a_save_core_cannot_read_back_keeps_its_warning_without_a_hash(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The component reads its save back; when that read fails, no render
    paths are claimed for a config nobody read, and nothing is read again."""
    await lovelace(hass, component_core_client)

    with (
        failing(hass, "async_load", from_call=2),
        received(hass, READ, SAVE) as seen,
    ):
        result = await set_dashboard(component_core_client, config={"views": []})

    assert (result["write_committed"], result["post_write_verified"]) == (True, False)
    assert result["config_hash"] is None
    assert "render_paths" not in result
    assert any("could not be verified" in w for w in result["warnings"])
    assert await stored(component_core_client) == {"views": []}
    assert seen == {READ: [], SAVE: []}


async def test_replacing_a_large_config_suggests_a_patch_instead(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    large = {"views": [{"title": "x" * 12000}]}
    await lovelace(hass, component_core_client, large)

    result = await set_dashboard(component_core_client, config={"views": []})

    (warning,) = result["warnings"]
    assert warning.startswith("Replaced large config")
    assert "patch for known paths" in warning


@pytest.mark.parametrize("mode", ["patch", "python_transform", "config"])
async def test_a_connection_lost_after_the_edit_was_sent_is_an_unknown_outcome(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, mode: str
) -> None:
    """Core may have run it: the edit is neither retried nor saved again, and
    the error keeps the caller's mode."""
    client = component_core_client
    await lovelace(hass, client)
    socket = await pooled_socket(client)

    with dropped_on(hass, EDIT, socket), received(hass, EDIT, SAVE) as seen:
        body = await refused(client, config_hash=DOCUMENT_HASH, **{mode: EDITS[mode]})

    assert body["reason"] == "write_outcome_unknown"
    assert body["action"] == ("set" if mode == "config" else mode)
    assert body["write_committed"] is None
    assert body["post_write_verified"] is False
    assert "already applied" in advice(body)
    assert (len(seen[EDIT]), seen[SAVE]) == (1, [])


async def test_an_edit_that_was_never_sent_says_so_and_may_be_retried(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    client = component_core_client
    await lovelace(hass, client)
    assert await get_component_caps(client) is not None
    socket = await pooled_socket(client)

    with patch.object(
        socket,
        "send_command",
        AsyncMock(side_effect=HomeAssistantCommandNotSent("not authenticated")),
    ):
        body = await refused(client, patch=EDITS["patch"], config_hash=DOCUMENT_HASH)

    assert body["error"]["code"] == "CONNECTION_FAILED"
    assert (body["reason"], body["write_committed"]) == ("write_not_sent", False)
    assert body["post_write_verified"] is False
    assert "Reconnect to Home Assistant" in advice(body)
    assert "not sent" in advice(body)
    assert "check whether" not in advice(body)
    assert await stored(client) == DOCUMENT


async def test_an_edit_cancelled_after_core_received_it_is_an_unknown_outcome(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Core still applies the edit it received; the caller learns it may have."""
    client = component_core_client
    await lovelace(hass, client)

    with received(hass, SAVE) as seen:
        async with held_command(hass, EDIT) as held:
            task = asyncio.create_task(
                set_dashboard(client, patch=EDITS["patch"], config_hash=DOCUMENT_HASH)
            )
            await held.arrived.wait()
            task.cancel()
            with pytest.raises(ToolError) as caught:
                await asyncio.gather(task)

    body = error_body(caught)
    assert body["reason"] == "write_outcome_unknown"
    assert (body["write_committed"], body["post_write_verified"]) == (None, False)
    assert held.calls == 1
    assert seen[SAVE] == []
    assert await stored(client) == {"views": []}


async def test_an_edit_cancelled_before_it_was_sent_stays_cancelled(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Cancelled while the capabilities are still being read: nothing was sent."""
    client = component_core_client
    await lovelace(hass, client)

    with received(hass, EDIT) as seen:
        async with held_command(hass, INFO) as probe:
            task = asyncio.create_task(
                set_dashboard(client, patch=EDITS["patch"], config_hash=DOCUMENT_HASH)
            )
            await probe.arrived.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.gather(task)

    assert seen[EDIT] == []
    assert await stored(client) == DOCUMENT


@pytest.mark.parametrize(
    ("method", "reason", "committed"),
    [
        ("async_load", "load_failed", False),
        ("async_save", "write_outcome_unknown", None),
    ],
)
async def test_the_components_failure_reason_picks_the_advice(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    method: str,
    reason: str,
    committed: bool | None,
) -> None:
    """Core failing to load the dashboard leaves it unwritten; failing to
    persist a save Core already applied leaves the outcome open."""
    client = component_core_client
    await lovelace(hass, client)

    with failing(hass, method, after_real=True), received(hass, SAVE) as seen:
        body = await refused(client, patch=EDITS["patch"], config_hash=DOCUMENT_HASH)

    assert (body["reason"], body["write_committed"]) == (reason, committed)
    assert body["action"] == "patch"
    assert ("already applied" in advice(body)) is (committed is None)
    assert ("connection" in advice(body)) is (committed is False)
    assert seen[SAVE] == []


async def test_a_component_without_the_command_is_dropped_and_core_saves_once(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_backend(hass, monkeypatch, "unknown_command")
    await lovelace(hass, core_client)
    patched = {"op": "replace", "path": "/views/0/cards/0/icon", "value": "mdi:music"}
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as spy, received(hass, SAVE) as seen:
        result = await set_dashboard(
            core_client, patch=[patched], config_hash=DOCUMENT_HASH
        )

    assert result["success"] is True
    assert (await stored(core_client))["views"][0]["cards"][0]["icon"] == "mdi:music"
    assert len(seen[SAVE]) == 1
    assert sent_types(spy).count(EDIT) == 1


async def test_a_component_that_does_not_advertise_the_edit_is_not_sent_it(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older component has other commands but not ``dashboard_edit``:
    Core's save writes the patch, and the component is never asked."""
    older_component(monkeypatch, "dashboard_edit")
    await lovelace(hass, component_core_client)

    with received(hass, EDIT, SAVE) as seen:
        result = await set_dashboard(
            component_core_client, patch=EDITS["patch"], config_hash=DOCUMENT_HASH
        )

    assert result["write_committed"] is True
    assert await stored(component_core_client) == {"views": []}
    assert (seen[EDIT], len(seen[SAVE])) == ([], 1)


async def test_a_conflict_after_a_title_change_reports_the_title_as_written(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    client = component_core_client
    await lovelace(hass, client)

    with received(hass, EDIT, SAVE) as seen:
        body = await refused(
            client, title="After", config={"views": []}, config_hash="stale"
        )

    assert body["reason"] == "conflict"
    assert (body["metadata_updated"], body["dashboard_created"]) == (True, False)
    assert (body["write_committed"], body["config_write_committed"]) == (True, False)
    assert (await registry_row(client, URL) or {})["title"] == "After"
    assert await stored(client) == DOCUMENT
    assert (len(seen[EDIT]), seen[SAVE]) == (1, [])


async def test_a_failed_connection_after_creating_reports_the_dashboard_created(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    client = component_core_client
    await lovelace(hass, client)

    with (
        patch.object(
            component_dashboard_edit,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("unavailable")),
        ),
        received(hass, SAVE) as seen,
    ):
        body = await refused(client, "new-dashboard", title="New", config={"views": []})

    assert (body["dashboard_created"], body["metadata_updated"]) == (True, False)
    assert (body["write_committed"], body["config_write_committed"]) == (True, False)
    assert await registry_row(client, "new-dashboard") is not None
    assert await stored(client, "new-dashboard") is None
    assert seen[SAVE] == []


@pytest.mark.parametrize(
    ("backend", "exists"),
    [("native", True), ("native", False), ("absent", False)],
)
async def test_a_hash_guards_a_saved_config_but_not_a_new_dashboard(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    exists: bool,
) -> None:
    """A dashboard Core holds no config for conflicts with any hash; creating
    one has nothing to compare, so the hash is not sent."""
    use_backend(hass, monkeypatch, backend)
    await lovelace(hass, core_client, None)
    url_path = URL if exists else "new-dashboard"
    config = {"views": [{"title": "First config"}]}
    kwargs: dict[str, Any] = {
        "title": "New",
        "config": config,
        "config_hash": "hash-from-another-dashboard",
    }
    if exists:
        kwargs.pop("title")

    with received(hass, *([EDIT] if backend == "native" else [])) as seen:
        if exists:
            body = await refused(core_client, url_path, **kwargs)
        else:
            result = await set_dashboard(core_client, url_path, **kwargs)

    if exists:
        assert body["error"]["message"] == "Dashboard has no saved config"
        assert (body["reason"], body["write_committed"]) == ("conflict", False)
        assert "omit config_hash" in advice(body)
        assert await stored(core_client, url_path) is None
    else:
        assert result["dashboard_created"] is True
        assert result["config_hash"] == compute_config_hash(config)
        assert await stored(core_client, url_path) == config
    if backend == "native":
        (frame,) = seen[EDIT]
        assert ("expected_hash" in frame) is exists


async def test_a_replacement_without_a_hash_is_not_told_to_omit_one(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The dashboard is deleted and created again while the component loads
    it: the conflict advice names no hash the caller never sent."""
    client = component_core_client
    dashboard_id = await lovelace(hass, client)

    async def replace() -> None:
        for message in (
            {"type": "lovelace/dashboards/delete", "dashboard_id": dashboard_id},
            {"type": "lovelace/dashboards/create", "url_path": URL, "title": "Test"},
        ):
            reply = await client.send_websocket_message(message)
            assert reply["success"], reply

    with after_load(hass, replace), received(hass, EDIT) as seen:
        body = await refused(client, config={"views": []})

    assert (body["reason"], body["write_committed"]) == ("conflict", False)
    assert "omit config_hash" not in advice(body)
    assert "expected_hash" not in seen[EDIT][0]


async def test_a_user_no_longer_admin_is_refused_without_a_fallback_save(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    hass_admin_user: User,
) -> None:
    """The capabilities were read as an admin; Core refuses the edit to the
    demoted user before running it, so it is known unwritten."""
    client = component_core_client
    await lovelace(hass, client)
    assert await get_component_caps(client) is not None
    await hass.auth.async_update_user(hass_admin_user, group_ids=[GROUP_ID_READ_ONLY])

    with received(hass, SAVE) as seen:
        body = await refused(client, patch=EDITS["patch"], config_hash=DOCUMENT_HASH)

    assert body["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
    assert (body["reason"], body["write_committed"]) == ("unauthorized", False)
    assert seen[SAVE] == []
    assert await stored(client) == DOCUMENT


@pytest.mark.parametrize(
    ("failure", "read_first", "mode"),
    [
        ("connection", False, "patch"),
        ("connection", True, "patch"),
        ("refused", False, "patch"),
        ("refused", True, "patch"),
        ("connection", False, "config"),
    ],
)
async def test_failed_discovery_refuses_the_edit_instead_of_saving_without_it(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    read_only_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    read_first: bool,
    mode: str,
) -> None:
    """Whether the component is there is unknown, so a save past it could
    bypass it. Also when a read tool met the failure first; a repeat keeps
    the cached failure and probes no more."""
    await lovelace(hass, component_core_client)
    # Core refuses the capability read of a user who is not an admin.
    client = read_only_client if failure == "refused" else component_core_client
    fault = (
        nullcontext(None)
        if failure == "refused"
        else patch.object(
            component_api,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
        )
    )
    with fault as connect, received(hass, INFO, EDIT, SAVE) as seen:
        if read_first:
            assert await get_component_caps(client) is None
        for _ in range(2):
            body = await refused(
                client, config_hash=DOCUMENT_HASH, **{mode: EDITS[mode]}
            )
            assert body["reason"] == "load_failed"
            assert body.get("config_write_committed", body["write_committed"]) is False
            assert body["post_write_verified"] is False

    assert await stored(component_core_client) == DOCUMENT
    assert (seen[EDIT], seen[SAVE]) == ([], [])
    assert (len(seen[INFO]) if connect is None else connect.await_count) == 1
