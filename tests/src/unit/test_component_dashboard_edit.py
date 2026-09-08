"""Native dashboard writes against Core's live-cache and async-save lifecycle."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ha_mcp.utils.config_hash import compute_config_hash

from .test_component_ws_search import (
    _REAL_VOL,
    FakeHass,
    _FakeConnection,
    _FakeWSApi,
    _Unauthorized,
    wsapi,
)


class ConfigNotFound(Exception):
    """An existing storage dashboard without a saved config."""


class LiveDashboard:
    """Core returns its live config and replaces it before awaiting persistence."""

    mode = "storage"

    def __init__(self, config):
        self.body = config
        self.loads = 0
        self.saves = []
        self.events = []
        self.on_load = None
        self.on_persist = None

    async def async_load(self, force):
        assert force is False
        self.loads += 1
        if self.on_load is not None:
            await self.on_load(self)
        if self.body is None:
            raise ConfigNotFound()
        return self.body

    async def async_save(self, config):
        self.saves.append(config)
        self.body = config
        self.events.append("lovelace_updated")
        if self.on_persist is not None:
            await self.on_persist(self)


@pytest.fixture
def edit(monkeypatch):
    """Use existing component import stubs, with real serialization behavior."""
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.lovelace",
        SimpleNamespace(LOVELACE_DATA="lovelace"),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.lovelace.const",
        SimpleNamespace(ConfigNotFound=ConfigNotFound),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.helpers.json",
        SimpleNamespace(
            json_bytes=lambda value: json.dumps(
                value,
                default=lambda leaf: (
                    leaf.isoformat()
                    if isinstance(leaf, datetime)
                    else _unsupported_json(leaf)
                ),
                allow_nan=False,
            ).encode()
        ),
    )
    return importlib.import_module("custom_components.ha_mcp_tools.dashboard_edit")


def _unsupported_json(value):
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def hass_for(dashboard, key="home-dashboard"):
    return FakeHass(data={"lovelace": SimpleNamespace(dashboards={key: dashboard})})


def patch_request(body, patch=None, **kwargs):
    return {
        "url_path": "home-dashboard",
        "expected_hash": compute_config_hash(body),
        "patch": patch
        if patch is not None
        else [{"op": "replace", "path": "/title", "value": "After"}],
        **kwargs,
    }


@pytest.mark.asyncio
async def test_patch_saves_detached_config_and_returns_authoritative_hash(edit):
    original = {"title": "Before", "views": [{"cards": []}]}
    dashboard = LiveDashboard(original)
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), patch_request(original)
    )
    assert result["success"] is True
    assert result["write_committed"] is True
    assert result["post_write_verified"] is True
    assert result["config"] == {"title": "After", "views": [{"cards": []}]}
    assert result["config_hash"] == compute_config_hash(result["config"])
    assert original == {"title": "Before", "views": [{"cards": []}]}
    assert dashboard.saves == [result["config"]]
    assert dashboard.events == ["lovelace_updated"]
    result["config"]["views"].append({"title": "client-only"})
    assert dashboard.body["views"] == [{"cards": []}]


@pytest.mark.asyncio
@pytest.mark.parametrize("url_path", [None, "lovelace"])
@pytest.mark.parametrize("stored_key", [None, "lovelace"])
async def test_default_dashboard_lookup(edit, url_path, stored_key):
    dashboard = LiveDashboard({"title": "Before"})
    result = await edit.async_edit_dashboard(
        hass_for(dashboard, stored_key),
        patch_request(dashboard.body, url_path=url_path),
    )
    assert result["success"] is True
    assert dashboard.body == {"title": "After"}


@pytest.mark.asyncio
async def test_default_prefers_lovelace_key_over_none(edit):
    canonical = LiveDashboard({"title": "Before"})
    older = LiveDashboard({"title": "Other"})
    hass = hass_for(canonical, "lovelace")
    hass.data["lovelace"].dashboards[None] = older
    result = await edit.async_edit_dashboard(
        hass, patch_request(canonical.body, url_path=None)
    )
    assert result["success"] is True
    assert canonical.body == {"title": "After"}
    assert older.saves == []


@pytest.mark.asyncio
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
async def test_invalid_requests_never_write(edit, message):
    original = {"title": "Before"}
    dashboard = LiveDashboard(original)
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), {"url_path": "home-dashboard", **message}
    )
    assert result["success"] is False
    assert result["write_committed"] is False
    assert result["error"]["code"] == "validation_failed"
    assert dashboard.body is original
    assert dashboard.saves == dashboard.events == []


@pytest.mark.asyncio
async def test_failed_later_patch_operation_leaves_live_nested_config_untouched(edit):
    original = {"title": "Before", "views": [{"cards": []}]}
    dashboard = LiveDashboard(original)
    result = await edit.async_edit_dashboard(
        hass_for(dashboard),
        patch_request(
            original,
            [
                {
                    "op": "add",
                    "path": "/views/0/cards/-",
                    "value": {"type": "markdown"},
                },
                {"op": "remove", "path": "/missing"},
            ],
        ),
    )
    assert result["success"] is False
    assert result["write_committed"] is False
    assert dashboard.body is original
    assert original["views"][0]["cards"] == []
    assert dashboard.saves == dashboard.events == []


@pytest.mark.asyncio
async def test_stale_hash_rejects_without_saving(edit):
    dashboard = LiveDashboard({"title": "Concurrent"})
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), patch_request({"title": "Before"})
    )
    assert result["error"]["code"] == "conflict"
    assert result["write_committed"] is False
    assert dashboard.body == {"title": "Concurrent"}
    assert dashboard.saves == dashboard.events == []


@pytest.mark.asyncio
async def test_dashboard_replaced_during_initial_load_rejects(edit):
    dashboard = LiveDashboard({"title": "Before"})
    replacement = LiveDashboard({"title": "Before"})
    hass = hass_for(dashboard)

    async def swap(_dashboard):
        hass.data["lovelace"].dashboards["home-dashboard"] = replacement

    dashboard.on_load = swap
    result = await edit.async_edit_dashboard(hass, patch_request(dashboard.body))
    assert result["error"]["code"] == "conflict"
    assert dashboard.saves == replacement.saves == []


@pytest.mark.asyncio
async def test_racing_writes_from_same_hash_only_one_commits(edit):
    original = {"title": "Before"}
    dashboard = LiveDashboard(original)

    async def persist(_dashboard):
        await asyncio.sleep(0)

    dashboard.on_persist = persist
    hass = hass_for(dashboard)
    first, second = await asyncio.gather(
        edit.async_edit_dashboard(hass, patch_request(original)),
        edit.async_edit_dashboard(hass, patch_request(original)),
    )
    assert first["success"] is True
    assert second["error"]["code"] == "conflict"
    assert len(dashboard.saves) == 1


@pytest.mark.asyncio
async def test_noop_patch_does_not_save_or_emit_event(edit):
    dashboard = LiveDashboard({"title": "Before"})
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), patch_request(dashboard.body, [])
    )
    assert result["success"] is True
    assert result["write_committed"] is False
    assert result["post_write_verified"] is True
    assert result["config_hash"] == compute_config_hash(dashboard.body)
    assert dashboard.saves == dashboard.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config", [{"views": []}, {"strategy": {"type": "original-states"}}]
)
async def test_empty_storage_dashboard_allows_full_replacement(edit, config):
    dashboard = LiveDashboard(None)
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), {"url_path": "home-dashboard", "config": config}
    )
    assert result["success"] is True
    assert dashboard.saves == [config]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["config", "patch"])
@pytest.mark.parametrize("missing", [False, True])
async def test_empty_config_hash_conflicts_only_for_existing_full_replacement(
    edit, mode, missing
):
    dashboard = LiveDashboard(None)
    result = await edit.async_edit_dashboard(
        FakeHass() if missing else hass_for(dashboard),
        {
            "url_path": "home-dashboard",
            "expected_hash": "stale",
            mode: {"views": []} if mode == "config" else [],
        },
    )
    assert result["error"]["code"] == (
        "conflict" if mode == "config" and not missing else "not_found"
    )
    assert result["write_committed"] is False
    assert dashboard.saves == dashboard.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_strategy", [False, True])
async def test_strategy_dashboard_policy_matches_existing_tool(edit, keep_strategy):
    original = {"strategy": {"type": "original-states"}}
    dashboard = LiveDashboard(original)
    config = {"views": []}
    if keep_strategy:
        config["strategy"] = {"type": "custom:another-strategy"}
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), {"url_path": "home-dashboard", "config": config}
    )
    assert result["success"] is keep_strategy
    if not keep_strategy:
        assert result["error"]["code"] == "strategy_conversion"
        assert dashboard.body is original
        assert dashboard.saves == []


@pytest.mark.asyncio
async def test_yaml_dashboard_is_rejected_before_loading(edit):
    dashboard = LiveDashboard({"secret": "must-not-read"})
    dashboard.mode = "yaml"
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), {"url_path": "home-dashboard", "config": {}}
    )
    assert result["error"]["code"] == "yaml_not_supported"
    assert result["write_committed"] is False
    assert dashboard.loads == 0
    assert dashboard.saves == []


@pytest.mark.asyncio
async def test_missing_dashboard_is_clean_failure(edit):
    result = await edit.async_edit_dashboard(
        FakeHass(), {"url_path": "missing-dashboard", "config": {}}
    )
    assert result["error"]["code"] == "not_found"
    assert result["write_committed"] is False


@pytest.mark.asyncio
async def test_hash_uses_core_json_normalization(edit):
    stamp = datetime(2026, 9, 7, tzinfo=UTC)
    dashboard = LiveDashboard({"title": "Before", "stamp": stamp})
    wire_config = {"title": "Before", "stamp": stamp.isoformat()}
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), patch_request(wire_config)
    )
    assert result["success"] is True
    assert result["config"]["stamp"] == stamp.isoformat()
    assert result["config_hash"] == compute_config_hash(result["config"])


@pytest.mark.asyncio
async def test_unserializable_candidate_is_rejected_before_save(edit):
    dashboard = LiveDashboard({"title": "Before"})
    result = await edit.async_edit_dashboard(
        hass_for(dashboard),
        {"url_path": "home-dashboard", "config": {"unsupported": object()}},
    )
    assert result["success"] is False
    assert result["write_committed"] is False
    assert dashboard.saves == dashboard.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("save failed"), asyncio.CancelledError()]
)
async def test_save_exception_is_unknown_without_retry_or_rollback(edit, failure):
    dashboard = LiveDashboard({"title": "Before"})

    async def fail(_dashboard):
        raise failure

    dashboard.on_persist = fail
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), patch_request(dashboard.body)
    )
    assert result["success"] is False
    assert result["error"]["code"] == "write_outcome_unknown"
    assert result["write_committed"] is None
    assert dashboard.body == {"title": "After"}
    assert len(dashboard.saves) == 1
    assert dashboard.events == ["lovelace_updated"]


@pytest.mark.asyncio
async def test_readback_failure_preserves_committed_save(edit):
    dashboard = LiveDashboard({"title": "Before"})

    async def break_readback(current):
        async def fail(_dashboard):
            raise RuntimeError("readback failed")

        current.on_load = fail

    dashboard.on_persist = break_readback
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), patch_request(dashboard.body)
    )
    assert result["success"] is True
    assert result["write_committed"] is True
    assert result["post_write_verified"] is False
    assert result["config"] == {"title": "After"}
    assert result["config_hash"] is None
    assert result["warnings"]
    assert len(dashboard.saves) == 1


@pytest.mark.asyncio
async def test_another_writer_during_persistence_returns_latest_readback(edit):
    dashboard = LiveDashboard({"title": "Before"})

    async def other_writer(current):
        current.body = {"title": "Later writer"}

    dashboard.on_persist = other_writer
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), patch_request(dashboard.body)
    )
    assert result["success"] is True
    assert result["config"] == {"title": "Later writer"}
    assert result["config_hash"] == compute_config_hash(result["config"])
    assert result["write_committed"] is True
    assert result["post_write_verified"] is True
    assert len(dashboard.saves) == 1


def test_new_command_is_registered_and_admin_gated(edit, monkeypatch):
    transport = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", transport)
    monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
    wsapi.async_register_commands(FakeHass())
    command = "ha_mcp_tools/dashboard_edit"
    assert "dashboard_edit" in wsapi._do_info(FakeHass())["capabilities"]
    handler = transport.registered[command]
    dashboard = LiveDashboard({"title": "Before"})
    msg = {"id": 1, "type": command, **patch_request(dashboard.body)}
    with pytest.raises(_Unauthorized):
        handler(hass_for(dashboard), _FakeConnection(is_admin=False), msg)
    assert dashboard.saves == []
    connection = _FakeConnection()
    handler(hass_for(dashboard), connection, msg)
    assert connection.results[1]["success"] is True
    assert dashboard.body == {"title": "After"}


@pytest.mark.asyncio
async def test_identical_full_replacement_still_saves(edit):
    original = {"title": "Before"}
    dashboard = LiveDashboard(original)
    result = await edit.async_edit_dashboard(
        hass_for(dashboard),
        {"url_path": "home-dashboard", "config": original},
    )
    assert result["success"] is True
    assert result["write_committed"] is True
    assert result["previous_config_size"] == len(json.dumps(original))
    assert len(dashboard.saves) == 1
    assert dashboard.body is not original


@pytest.mark.asyncio
async def test_patch_cannot_remove_strategy(edit):
    original = {"strategy": {"type": "original-states"}}
    dashboard = LiveDashboard(original)
    result = await edit.async_edit_dashboard(
        hass_for(dashboard),
        patch_request(original, [{"op": "remove", "path": "/strategy"}]),
    )
    assert result["error"]["code"] == "strategy_conversion"
    assert result["write_committed"] is False
    assert dashboard.body is original
    assert dashboard.saves == []


@pytest.mark.asyncio
async def test_hash_required_replacement_cannot_initialize_empty_storage(edit):
    dashboard = LiveDashboard(None)
    result = await edit.async_edit_dashboard(
        hass_for(dashboard),
        {
            "url_path": "home-dashboard",
            "config": {"views": []},
            "expected_hash": compute_config_hash({}),
        },
    )
    assert result["error"]["code"] == "conflict"
    assert result["write_committed"] is False
    assert dashboard.saves == []


@pytest.mark.asyncio
async def test_readback_replacement_with_yaml_never_loads_yaml(edit):
    dashboard = LiveDashboard({"title": "Before"})
    yaml_dashboard = LiveDashboard({"secret": "must-not-read"})
    yaml_dashboard.mode = "yaml"
    hass = hass_for(dashboard)

    async def replace_dashboard(_dashboard):
        hass.data["lovelace"].dashboards["home-dashboard"] = yaml_dashboard

    dashboard.on_persist = replace_dashboard
    result = await edit.async_edit_dashboard(hass, patch_request(dashboard.body))
    assert result["write_committed"] is True
    assert result["post_write_verified"] is False
    assert result["config_hash"] is None
    assert result["config"] == {"title": "After"}
    assert yaml_dashboard.loads == 0


@pytest.mark.parametrize("phase", ["prepare", "save", "verify"])
async def test_unexpected_edit_failure_logs_traceback(edit, caplog, phase):
    dashboard = LiveDashboard({"views": []})

    async def fail_load(current):
        if phase == "prepare" or current.loads > 1:
            raise ImportError("Core API moved")

    async def fail_save(current):
        raise RuntimeError("Persistence failed")

    if phase == "save":
        dashboard.on_persist = fail_save
    else:
        dashboard.on_load = fail_load
    result = await edit.async_edit_dashboard(
        hass_for(dashboard), {"url_path": "home-dashboard", "config": {"views": []}}
    )
    records = [r for r in caplog.records if r.name == edit.__name__]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert (
        result["write_committed"]
        is ({"prepare": False, "save": None, "verify": True}[phase])
    )
    assert "Core API moved" not in str(result)
    assert "Persistence failed" not in str(result)
