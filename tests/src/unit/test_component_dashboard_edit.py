"""Native dashboard writes against Core's live-cache and async-save lifecycle.

The dashboards are Core's own lovelace storage dashboards on a real ``hass``,
created and saved through Core's ``lovelace/*`` commands; Core's
``LovelaceStorage`` replaces its cached config and fires ``lovelace_updated``
before it awaits persistence. Where a test needs a failure or a concurrent
writer at one point of that lifecycle, it wraps the dashboard's own load or
save, which still runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.components.lovelace.dashboard import LovelaceYAML
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_capture_events
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools import dashboard_edit
from ha_mcp.utils.config_hash import compute_config_hash

from ._component_core import ws_call, ws_result

URL = "home-dashboard"
BEFORE = {"title": "Before", "views": [{"cards": []}]}


async def _dashboard(
    hass: HomeAssistant, ws: MockHAClientWebSocket, config: Any = BEFORE
) -> str:
    """``home-dashboard`` saved with ``config`` (None: never saved); its id."""
    assert await async_setup_component(hass, "lovelace", {})
    created = await ws_result(
        ws, "lovelace/dashboards/create", url_path=URL, title="Home", mode="storage"
    )
    if config is not None:
        await ws_result(ws, "lovelace/config/save", url_path=URL, config=config)
    dashboard_id: str = created["id"]
    return dashboard_id


def _live(hass: HomeAssistant, url_path: str | None = URL) -> Any:
    return hass.data[LOVELACE_DATA].dashboards[url_path]


async def _stored(ws: MockHAClientWebSocket, url_path: str | None = URL) -> Any:
    return await ws_result(ws, "lovelace/config", url_path=url_path)


def _patch_request(
    body: Any, patch_ops: list[dict[str, Any]] | None = None, **kwargs: Any
) -> dict[str, Any]:
    return {
        "url_path": URL,
        "expected_hash": compute_config_hash(body),
        "patch": patch_ops
        if patch_ops is not None
        else [{"op": "replace", "path": "/title", "value": "After"}],
        **kwargs,
    }


def _wrap(
    hass: HomeAssistant,
    method: str,
    before: Callable[[], Awaitable[None]] | None = None,
    after: Callable[[], Awaitable[None]] | None = None,
) -> Any:
    """Run ``before`` / ``after`` around the live dashboard's own ``method``."""
    dashboard = _live(hass)
    real = getattr(dashboard, method)

    async def wrapped(*args: Any) -> Any:
        if before is not None:
            await before()
        result = await real(*args)
        if after is not None:
            await after()
        return result

    return patch.object(dashboard, method, side_effect=wrapped)


async def _edit(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    return await dashboard_edit.async_edit_dashboard(hass, msg)


async def test_a_patch_saves_a_detached_config_and_returns_its_hash(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _dashboard(hass, component_ws)
    events = async_capture_events(hass, "lovelace_updated")

    result = await _edit(hass, _patch_request(BEFORE))

    assert (result["success"], result["write_committed"]) == (True, True)
    assert result["post_write_verified"] is True
    assert result["config"] == {"title": "After", "views": [{"cards": []}]}
    assert result["config_hash"] == compute_config_hash(result["config"])
    assert await _stored(component_ws) == result["config"]
    assert len(events) == 1
    result["config"]["views"].append({"title": "client-only"})
    assert await _stored(component_ws) == {"title": "After", "views": [{"cards": []}]}


@pytest.mark.parametrize("url_path", [None, "lovelace"])
async def test_the_default_dashboard_is_found_by_either_name(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, url_path: str | None
) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    await ws_result(component_ws, "lovelace/config/save", config={"title": "Before"})

    result = await _edit(hass, _patch_request({"title": "Before"}, url_path=url_path))

    assert result["success"] is True
    assert await _stored(component_ws, None) == {"title": "After"}


@pytest.mark.parametrize(
    "message",
    [
        {"patch": []},
        {"config": {}, "patch": [], "expected_hash": "any"},
        {},
        {"config": []},
        {"config": {}, "expected_hash": 3},
        {"patch": "[]", "expected_hash": "any"},
    ],
)
async def test_an_invalid_request_never_writes(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, message: dict[str, Any]
) -> None:
    await _dashboard(hass, component_ws)
    events = async_capture_events(hass, "lovelace_updated")

    result = await _edit(hass, {"url_path": URL, **message})

    assert (result["success"], result["write_committed"]) == (False, False)
    assert result["error"]["code"] == "validation_failed"
    assert await _stored(component_ws) == BEFORE
    assert events == []


async def test_a_failed_later_patch_operation_leaves_the_live_config_untouched(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _dashboard(hass, component_ws)
    events = async_capture_events(hass, "lovelace_updated")

    result = await _edit(
        hass,
        _patch_request(
            BEFORE,
            [
                {"op": "add", "path": "/views/0/cards/-", "value": {"type": "x"}},
                {"op": "remove", "path": "/missing"},
            ],
        ),
    )

    assert (result["success"], result["write_committed"]) == (False, False)
    assert await _live(hass).async_load(False) == BEFORE
    assert events == []


async def test_a_stale_hash_is_refused_without_saving(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _dashboard(hass, component_ws, {"title": "Concurrent"})
    events = async_capture_events(hass, "lovelace_updated")

    result = await _edit(hass, _patch_request({"title": "Before"}))

    assert result["error"]["code"] == "conflict"
    assert result["write_committed"] is False
    assert await _stored(component_ws) == {"title": "Concurrent"}
    assert events == []


@pytest.mark.parametrize("mode", ["patch", "guarded_config", "unguarded_config"])
async def test_a_dashboard_replaced_during_the_first_load_is_not_written(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, mode: str
) -> None:
    """Deleted and created again under its url while its config loads: the
    stale object is never written, even with the same config."""
    dashboard_id = await _dashboard(hass, component_ws)
    request = _patch_request(BEFORE)
    if mode != "patch":
        request.pop("patch")
        request["config"] = {"title": "After"}
    if mode == "unguarded_config":
        request.pop("expected_hash")

    async def replace() -> None:
        await ws_result(
            component_ws, "lovelace/dashboards/delete", dashboard_id=dashboard_id
        )
        await ws_result(
            component_ws,
            "lovelace/dashboards/create",
            url_path=URL,
            title="Home",
            mode="storage",
        )

    with _wrap(hass, "async_load", after=replace):
        result = await _edit(hass, request)
    replacement = await ws_call(component_ws, "lovelace/config", url_path=URL)

    assert result["error"]["code"] == "conflict"
    assert result["error"]["message"] == "Dashboard changed while loading its config"
    assert replacement["success"] is False, "the replacement was written"


async def test_racing_writes_from_the_same_hash_commit_only_one(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _dashboard(hass, component_ws)
    events = async_capture_events(hass, "lovelace_updated")

    first, second = await asyncio.gather(
        _edit(hass, _patch_request(BEFORE)), _edit(hass, _patch_request(BEFORE))
    )

    assert first["success"] is True
    assert second["error"]["code"] == "conflict"
    assert len(events) == 1


async def test_a_patch_that_changes_nothing_is_not_saved(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _dashboard(hass, component_ws)
    events = async_capture_events(hass, "lovelace_updated")

    result = await _edit(hass, _patch_request(BEFORE, []))

    assert (result["success"], result["write_committed"]) == (True, False)
    assert result["post_write_verified"] is True
    assert result["config_hash"] == compute_config_hash(BEFORE)
    assert events == []


@pytest.mark.parametrize(
    "config", [{"views": []}, {"strategy": {"type": "original-states"}}]
)
async def test_a_never_saved_dashboard_takes_a_full_replacement(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, config: dict[str, Any]
) -> None:
    await _dashboard(hass, component_ws, None)

    result = await _edit(hass, {"url_path": URL, "config": config})

    assert result["success"] is True
    assert await _stored(component_ws) == config


@pytest.mark.parametrize("mode", ["config", "patch"])
@pytest.mark.parametrize("missing", [False, True])
async def test_a_hash_against_no_saved_config_conflicts_only_for_a_replacement(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    mode: str,
    missing: bool,
) -> None:
    if missing:
        assert await async_setup_component(hass, "lovelace", {})
    else:
        await _dashboard(hass, component_ws, None)
    events = async_capture_events(hass, "lovelace_updated")

    result = await _edit(
        hass,
        {
            "url_path": URL,
            "expected_hash": "stale",
            mode: {"views": []} if mode == "config" else [],
        },
    )

    assert result["error"]["code"] == (
        "conflict" if mode == "config" and not missing else "not_found"
    )
    assert result["write_committed"] is False
    assert events == []


@pytest.mark.parametrize("keep_strategy", [False, True])
async def test_a_strategy_dashboard_is_not_converted_to_a_custom_one(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, keep_strategy: bool
) -> None:
    original = {"strategy": {"type": "original-states"}}
    await _dashboard(hass, component_ws, original)
    config: dict[str, Any] = {"views": []}
    if keep_strategy:
        config["strategy"] = {"type": "custom:another-strategy"}

    result = await _edit(hass, {"url_path": URL, "config": config})

    assert result["success"] is keep_strategy
    if not keep_strategy:
        assert result["error"]["code"] == "strategy_conversion"
        assert await _stored(component_ws) == original


async def test_a_patch_cannot_remove_the_strategy(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    original = {"strategy": {"type": "original-states"}}
    await _dashboard(hass, component_ws, original)

    result = await _edit(
        hass, _patch_request(original, [{"op": "remove", "path": "/strategy"}])
    )

    assert result["error"]["code"] == "strategy_conversion"
    assert result["write_committed"] is False
    assert await _stored(component_ws) == original


async def test_a_yaml_dashboard_is_refused_before_it_is_read(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    assert await async_setup_component(
        hass,
        "lovelace",
        {
            "lovelace": {
                "dashboards": {
                    URL: {"mode": "yaml", "filename": "h.yaml", "title": "Home"}
                }
            }
        },
    )

    with patch.object(_live(hass), "async_load", wraps=_live(hass).async_load) as load:
        result = await _edit(hass, {"url_path": URL, "config": {}})

    assert result["error"]["code"] == "yaml_not_supported"
    assert result["write_committed"] is False
    load.assert_not_called()


async def test_a_missing_dashboard_is_a_clean_failure(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    assert await async_setup_component(hass, "lovelace", {})

    result = await _edit(hass, {"url_path": "missing-dashboard", "config": {}})

    assert result["error"]["code"] == "not_found"
    assert result["write_committed"] is False


async def test_the_hash_uses_cores_json_normalization(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A saved value Core's encoder writes as a string hashes as that string."""
    await _dashboard(hass, component_ws, None)
    stamp = datetime(2026, 9, 7, tzinfo=UTC)
    await _live(hass).async_save({"title": "Before", "stamp": stamp})

    result = await _edit(
        hass, _patch_request({"title": "Before", "stamp": stamp.isoformat()})
    )

    assert result["success"] is True
    assert result["config"]["stamp"] == stamp.isoformat()
    assert result["config_hash"] == compute_config_hash(result["config"])


async def test_an_unserializable_candidate_is_refused_before_saving(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _dashboard(hass, component_ws)
    events = async_capture_events(hass, "lovelace_updated")

    result = await _edit(hass, {"url_path": URL, "config": {"unsupported": object()}})

    assert (result["success"], result["write_committed"]) == (False, False)
    assert events == []


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_a_failed_persistence_is_an_unknown_outcome_without_retry_or_rollback(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    failure: type[BaseException],
) -> None:
    """Core has replaced its cache and fired the event before it persists."""
    await _dashboard(hass, component_ws)
    events = async_capture_events(hass, "lovelace_updated")

    async def fail() -> None:
        raise failure("save failed")

    with _wrap(hass, "async_save", after=fail) as save:
        result = await _edit(hass, _patch_request(BEFORE))

    assert result["error"]["code"] == "write_outcome_unknown"
    assert result["write_committed"] is None
    assert await _live(hass).async_load(False) == {**BEFORE, "title": "After"}
    assert save.await_count == 1
    assert len(events) == 1


async def test_a_failed_readback_keeps_the_committed_save(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _dashboard(hass, component_ws)
    loads = 0

    async def readback_fails() -> None:
        nonlocal loads
        loads += 1
        if loads > 1:
            raise RuntimeError("readback failed")

    with (
        _wrap(hass, "async_load", before=readback_fails),
        _wrap(hass, "async_save") as save,
    ):
        result = await _edit(hass, _patch_request(BEFORE))

    assert (result["success"], result["write_committed"]) == (True, True)
    assert result["post_write_verified"] is False
    assert result["config"] == {**BEFORE, "title": "After"}
    assert result["config_hash"] is None
    assert result["warnings"]
    assert save.await_count == 1


async def test_another_writer_during_persistence_is_what_the_readback_returns(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _dashboard(hass, component_ws)
    dashboard = _live(hass)
    real_save = dashboard.async_save

    async def later_writer() -> None:
        await real_save({"title": "Later writer"})

    with _wrap(hass, "async_save", after=later_writer) as save:
        result = await _edit(hass, _patch_request(BEFORE))

    assert result["config"] == {"title": "Later writer"}
    assert result["config_hash"] == compute_config_hash(result["config"])
    assert (result["write_committed"], result["post_write_verified"]) == (True, True)
    assert save.await_count == 1


async def test_a_dashboard_that_turns_yaml_after_the_save_is_never_read(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The readback must not load a YAML body that took the url meanwhile."""
    await _dashboard(hass, component_ws)
    yaml_dashboard = LovelaceYAML(
        hass, URL, {"mode": "yaml", "filename": "h.yaml", "url_path": URL}
    )

    async def turn_yaml() -> None:
        hass.data[LOVELACE_DATA].dashboards[URL] = yaml_dashboard

    with (
        _wrap(hass, "async_save", after=turn_yaml),
        patch.object(
            yaml_dashboard, "async_load", wraps=yaml_dashboard.async_load
        ) as load,
    ):
        result = await _edit(hass, _patch_request(BEFORE))

    assert result["write_committed"] is True
    assert result["post_write_verified"] is False
    assert result["config_hash"] is None
    assert result["config"] == {**BEFORE, "title": "After"}
    load.assert_not_called()


async def test_an_identical_full_replacement_still_saves(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _dashboard(hass, component_ws)
    events = async_capture_events(hass, "lovelace_updated")

    result = await _edit(hass, {"url_path": URL, "config": BEFORE})

    assert (result["success"], result["write_committed"]) == (True, True)
    assert result["previous_config_size"] == len(json.dumps(BEFORE))
    assert len(events) == 1


async def test_a_replacement_with_a_hash_cannot_initialize_a_never_saved_dashboard(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _dashboard(hass, component_ws, None)

    result = await _edit(
        hass,
        {
            "url_path": URL,
            "config": {"views": []},
            "expected_hash": compute_config_hash({}),
        },
    )

    assert result["error"]["code"] == "conflict"
    assert result["write_committed"] is False


@pytest.mark.parametrize("phase", ["prepare", "save", "verify"])
async def test_an_unexpected_failure_is_logged_with_its_traceback_only(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    caplog: pytest.LogCaptureFixture,
    phase: str,
) -> None:
    """The traceback goes to the log once; the reply never carries it."""
    await _dashboard(hass, component_ws, {"views": []})
    loads = 0

    async def failing_load() -> None:
        nonlocal loads
        loads += 1
        if phase == "prepare" or loads > 1:
            raise ImportError("Core API moved")

    async def failing_save() -> None:
        raise RuntimeError("Persistence failed")

    wrapper = (
        _wrap(hass, "async_save", after=failing_save)
        if phase == "save"
        else _wrap(hass, "async_load", before=failing_load)
    )
    with wrapper, caplog.at_level(logging.WARNING):
        result = await _edit(hass, {"url_path": URL, "config": {"views": []}})

    records = [r for r in caplog.records if r.name == dashboard_edit.__name__]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert (
        result["write_committed"]
        is {"prepare": False, "save": None, "verify": True}[phase]
    )
    assert "Core API moved" not in str(result)
    assert "Persistence failed" not in str(result)


async def test_the_command_is_registered_advertised_and_admin_only(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    component_ws_read_only: MockHAClientWebSocket,
) -> None:
    await _dashboard(hass, component_ws, {"title": "Before"})
    command = "ha_mcp_tools/dashboard_edit"
    request = _patch_request({"title": "Before"})

    refused = await ws_call(component_ws_read_only, command, **request)
    unchanged = await _stored(component_ws)
    allowed = await ws_result(component_ws, command, **request)
    info = await ws_result(component_ws, "ha_mcp_tools/info")

    assert refused["error"]["code"] == "unauthorized"
    assert unchanged == {"title": "Before"}
    assert allowed["success"] is True
    assert await _stored(component_ws) == {"title": "After"}
    assert "dashboard_edit" in info["capabilities"]
