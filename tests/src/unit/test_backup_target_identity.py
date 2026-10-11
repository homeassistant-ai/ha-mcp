"""A backup snapshots the helper the write changes, and says when it cannot.

``ha_config_set_helper`` and ``ha_remove_helpers_integrations`` resolve a
storage helper through the entity registry: ``<type>.<id>`` names an entity,
whose ``unique_id`` is the helper's storage id. A renamed entity can carry
another helper's id as its object id, so the snapshot must ask the registry
first, as the writes do (#2632).
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_helper_target import note_no_safety_backup
from ha_mcp.backup_manager import SCHEMA_VERSION
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
)
from ha_mcp.tools.backup import _edits_restore
from ha_mcp.tools.tools_integrations import IntegrationTools

from .test_backup_manager import _mk_handler, _mk_manager, _StubClient
from .test_backup_restore_consumers import response_for, run_restore
from .test_backup_restore_consumers import settings_script as settings_script

# Helper "hall" was renamed to input_boolean.porch; helper "porch" now has
# the entity input_boolean.porch_2.
HELPERS = [{"id": "hall", "name": "Hall"}, {"id": "porch", "name": "Porch"}]
REGISTRY = {
    "input_boolean.porch": {"unique_id": "hall"},
    "input_boolean.porch_2": {"unique_id": "porch"},
}


def _helper_ws(registry: dict[str, Any]) -> Any:
    async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
        if msg["type"] == "input_boolean/list":
            return HELPERS
        if msg["type"] == "config/entity_registry/get":
            if msg["entity_id"] in registry:
                return registry[msg["entity_id"]]
            raise HomeAssistantCommandError(
                "Command failed: Entity not found", "not_found"
            )
        raise AssertionError(f"unexpected ws message: {msg}")

    return fake_ws


class TestHelperSnapshotTarget:
    async def test_a_renamed_entity_snapshots_the_helper_it_belongs_to(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write changes "hall", so a snapshot of "porch" leaves no rollback."""
        monkeypatch.setattr(bm, "_ws_send", _helper_ws(REGISTRY))

        got = await bm._fetch_helper(_StubClient(), "porch", "input_boolean")

        assert got["id"] == "hall"

    async def test_an_entity_of_no_listed_helper_snapshots_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write fails on the registry's unique_id; the object id names
        another helper, which a snapshot of it would mislabel."""
        registry = {"input_boolean.porch": {"unique_id": "yaml_only"}}
        monkeypatch.setattr(bm, "_ws_send", _helper_ws(registry))

        assert await bm._fetch_helper(_StubClient(), "porch", "input_boolean") is None

    async def test_an_entity_id_of_another_type_snapshots_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write takes counter.porch for input_boolean.counter.porch and
        fails; a snapshot of helper "porch" would back up a helper not written."""
        monkeypatch.setattr(bm, "_ws_send", _helper_ws({}))

        got = await bm._fetch_helper(_StubClient(), "counter.porch", "input_boolean")

        assert got is None

    async def test_a_helper_without_a_registry_entry_is_found_by_its_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The removal falls back to the storage id when the registry has none."""
        monkeypatch.setattr(bm, "_ws_send", _helper_ws({}))

        got = await bm._fetch_helper(_StubClient(), "porch", "input_boolean")

        assert got["id"] == "porch"


def _tag_ws(registry: dict[str, Any]) -> Any:
    """Core rejects a tag id with a hyphen as an entity_id (invalid_format)."""

    async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
        if msg["type"] == "tag/list":
            return [{"id": "front"}, {"id": "xyz", "name": "Front"}, {"id": "abc-1"}]
        if msg["type"] == "config/entity_registry/list":
            return []
        if msg["type"] == "config/entity_registry/get":
            if "-" in msg["entity_id"]:
                raise HomeAssistantCommandError("Command failed", "invalid_format")
            if msg["entity_id"] in registry:
                return registry[msg["entity_id"]]
            raise HomeAssistantCommandError("Command failed", "not_found")
        raise AssertionError(f"unexpected ws message: {msg}")

    return fake_ws


class TestTagSnapshotTarget:
    """ha_config_set_helper (tag_item_id) and ha_remove_helpers_integrations take
    a bare tag id as the stored id; a tag's entity_id follows its name, so
    tag.front can belong to the tag named "Front"."""

    async def test_a_bare_tag_id_is_the_stored_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = {"tag.front": {"unique_id": "xyz"}}
        monkeypatch.setattr(bm, "_ws_send", _tag_ws(registry))

        got = await bm._fetch_helper(_StubClient(), "front", "tag")

        assert got["id"] == "front"

    async def test_a_tag_entity_id_is_the_tag_it_belongs_to(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = {"tag.front": {"unique_id": "xyz"}}
        monkeypatch.setattr(bm, "_ws_send", _tag_ws(registry))

        got = await bm._fetch_helper(_StubClient(), "tag.front", "tag")

        assert got["id"] == "xyz"

    async def test_a_tag_id_that_cannot_name_an_entity_is_still_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tag.abc-1 is how the tag tools accept the id abc-1."""
        monkeypatch.setattr(bm, "_ws_send", _tag_ws({}))

        got = await bm._fetch_helper(_StubClient(), "tag.abc-1", "tag")

        assert got["id"] == "abc-1"


TAG_ROWS = [
    {"entity_id": "tag.front", "platform": "tag", "unique_id": "xyz"},
    {"entity_id": "tag.garage", "platform": "tag", "unique_id": "front"},
]


def _tag_client(*, registry_readable: bool = True, stored: bool = True) -> Any:
    """tag.front is the entity of tag xyz, named "Front", not of tag front."""
    client = MagicMock()
    client.get_entity_state = AsyncMock(return_value=None)
    client._request = AsyncMock(return_value=[])
    client.deletes = []

    async def ws(msg: dict[str, Any]) -> dict[str, Any]:
        if msg["type"] == "config/entity_registry/get":
            row = next(
                (r for r in TAG_ROWS if r["entity_id"] == msg["entity_id"]), None
            )
            if row is None:
                return {"success": False, "error": "x", "error_code": "not_found"}
            return {"success": True, "result": row}
        if msg["type"] == "config/entity_registry/list":
            if not registry_readable:
                return {"success": False, "error": "timeout"}
            return {"success": True, "result": TAG_ROWS}
        if msg["type"] == "tag/delete":
            client.deletes.append(msg)
            if not stored:
                return {"success": False, "error": "x", "error_code": "not_found"}
        return {"success": True, "result": {}}

    client.send_websocket_message = AsyncMock(side_effect=ws)
    return client


async def _remove_tag(
    client: Any, target: str, wait: bool = False, waited: Any = None
) -> dict[str, Any]:
    """Without the component: Core's own registry commands answer."""
    with (
        patch(
            "ha_mcp.tools.tools_integrations.resolve_entities_via_component",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.tools.tools_integrations.verify_entity_removed",
            new=waited or AsyncMock(return_value=None),
        ),
    ):
        result: dict[str, Any] = await IntegrationTools(
            client
        ).ha_remove_helpers_integrations(
            target=target, helper_type="tag", confirm=True, wait=wait
        )
    return result


class TestTagRemoval:
    async def test_removing_a_tag_by_its_id_deletes_that_tag(self) -> None:
        client = _tag_client()

        result = await _remove_tag(client, "front")

        assert client.deletes == [{"type": "tag/delete", "tag_id": "front"}]
        assert result["entity_ids"] == ["tag.garage"]

    async def test_an_unreadable_registry_does_not_stop_the_removal(self) -> None:
        """The entity only names the reply; the delete goes by the stored id."""
        client = _tag_client(registry_readable=False)
        waited = AsyncMock(return_value=None)

        result = await _remove_tag(client, "front", wait=True, waited=waited)

        assert client.deletes == [{"type": "tag/delete", "tag_id": "front"}]
        assert result["success"] is True
        # tag.front belongs to tag xyz: neither named nor waited on
        assert result["entity_ids"] == []
        waited.assert_not_awaited()
        assert "registry could not be read" in result["warnings"][0]

    async def test_an_unknown_tag_id_is_not_called_yaml_configured(self) -> None:
        """Tags have no YAML form; the id may be a name or a typo."""
        client = _tag_client(stored=False)

        with pytest.raises(ToolError) as exc_info:
            await _remove_tag(client, "front_door")

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "ENTITY_NOT_FOUND"
        assert "front_door" in error["message"]
        assert "YAML" not in error["message"] + error["suggestion"]


def test_a_removed_helper_is_named_by_its_type() -> None:
    """Its backup domain carries the helper_ prefix; the warning names the type."""
    outcome: dict[str, Any] = {}
    note_no_safety_backup(outcome, "helper_tag", None, {"id": "front_door"})
    warning = outcome["warnings"][0]
    assert "helper_" not in warning
    assert "tag front_door no longer exists" in warning


def _set_helper_tool(client: Any) -> Any:
    from ha_mcp.tools.tools_config_helpers import register_config_helper_tools

    tools: dict[str, Any] = {}
    mcp = MagicMock()
    mcp.add_tool = lambda method: tools.__setitem__(method.__fastmcp__.name, method)
    register_config_helper_tools(mcp, client)
    return tools["ha_config_set_helper"]


async def test_an_update_by_a_storage_id_that_starts_with_the_type_finds_it() -> None:
    """A helper named "Input Boolean Hall" has the storage id input_boolean_hall;
    without the dot, the prefix check took that id for an entity_id."""
    client = MagicMock()
    updates: list[dict[str, Any]] = []

    async def ws(msg: dict[str, Any]) -> dict[str, Any]:
        if msg["type"] == "config/entity_registry/get":
            if "." not in msg["entity_id"]:
                return {"success": False, "error": "x", "error_code": "invalid_format"}
            return {"success": True, "result": {"unique_id": "input_boolean_hall"}}
        if msg["type"] == "input_boolean/list":
            return {"success": True, "result": [{"id": "input_boolean_hall"}]}
        if msg["type"] == "input_boolean/update":
            updates.append(msg)
        return {"success": True, "result": {"id": "input_boolean_hall"}}

    client.send_websocket_message = AsyncMock(side_effect=ws)
    with patch(
        "ha_mcp.tools.config_helpers.update.wait_for_entity_registered",
        new=AsyncMock(return_value=True),
    ):
        await _set_helper_tool(client)(
            helper_type="input_boolean", helper_id="input_boolean_hall", name="Hall"
        )

    assert updates[0]["input_boolean_id"] == "input_boolean_hall"


def _snapshot(tmp_path: Path, config: dict[str, Any] | None = None) -> str:
    path = tmp_path / "automation.hall_lights.20260521_120000.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": SCHEMA_VERSION,
                "domain": "automation",
                "entity_id": "hall_lights",
                "config": config or {"alias": "Hall lights"},
            }
        )
    )
    return path.name


def _store_ws(
    monkeypatch: pytest.MonkeyPatch,
    registry: Any,
    items: list[dict[str, Any]],
    registry_error: Exception | None = None,
) -> list[dict[str, Any]]:
    """A Core fake for storage items: an update or registry get of an unknown
    id fails with Core's not_found."""
    sent: list[dict[str, Any]] = []
    missing = HomeAssistantCommandError("Command failed: not found", "not_found")

    async def send(_client: Any, message: dict[str, Any]) -> Any:
        sent.append(message)
        kind = message["type"]
        if kind.startswith("config/entity_registry/") and registry_error:
            raise registry_error
        if kind == "config/entity_registry/list":
            return registry
        if kind == "config/entity_registry/get":
            row = next(
                (r for r in registry if r["entity_id"] == message["entity_id"]), None
            )
            if row is None:
                raise missing
            return row
        item_type, _, command = kind.partition("/")
        if command == "list":
            return items
        target = message.get(f"{item_type}_id")
        if command == "update" and target not in {i["id"] for i in items}:
            raise missing
        return {"id": target or "new", "name": message.get("name")}

    monkeypatch.setattr(bm, "_ws_send", AsyncMock(side_effect=send))
    return sent


def _snapshot_files(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.glob("*.yaml"))


def _store_snapshot(tmp_path: Path, domain: str, entity_id: str, item_id: str) -> str:
    name = f"{domain}.{entity_id}.20260521_120000.yaml"
    config = {"id": item_id, "name": item_id.title()}
    (tmp_path / name).write_text(
        yaml.safe_dump(
            {
                "schema_version": SCHEMA_VERSION,
                "domain": domain,
                "entity_id": entity_id,
                "config": config,
            }
        )
    )
    return name


class TestRestoreSafetyBackup:
    async def test_a_target_not_found_under_its_id_is_reported(
        self, tmp_path: Path
    ) -> None:
        """Renamed since the backup, its current state is overwritten unsaved."""
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched=None))

        result = await _edits_restore(mgr, "edits", "restore", _snapshot(tmp_path))

        assert "hall_lights" in result["warnings"][0]
        assert "No safety backup" in result["warnings"][0]

    async def test_a_recent_snapshot_does_not_stop_the_safety_backup(
        self, tmp_path: Path
    ) -> None:
        """The throttle's snapshot holds an older state than the one replaced."""
        mgr = _mk_manager(tmp_path, auto_backup_throttle_minutes=60)
        mgr.register(_mk_handler(fetched={"alias": "Hall lights, edited"}))
        name = _snapshot(tmp_path)
        assert await mgr.maybe_snapshot("automation", "hall_lights") is not None

        result = await _edits_restore(mgr, "edits", "restore", name)

        assert result["data"]["safety_backup"] is not None

    async def test_a_safety_backup_of_another_item_blocks_it(
        self, tmp_path: Path
    ) -> None:
        """Renamed since, the id can name another item: the item the restore
        overwrites would have no safety backup."""
        mgr = _mk_manager(tmp_path)
        written = AssertionError("the restore wrote")
        mgr.register(
            _mk_handler(
                fetched={"id": "other", "alias": "Other"}, raise_on_restore=written
            )
        )
        name = _snapshot(tmp_path, {"id": "hall", "alias": "Hall lights"})

        with pytest.raises(ToolError) as exc_info:
            await _edits_restore(mgr, "edits", "restore", name)

        reply = json.loads(str(exc_info.value))
        assert reply["error"]["code"] == "BACKUP_CAPTURE_FAILED"
        assert "would hold other, not hall" in reply["error"]["message"]
        assert reply["data"]["apply_status"] == "not_applied"
        assert reply["data"]["safety_backup"] is None
        assert _snapshot_files(tmp_path) == [name]

    @pytest.mark.parametrize(
        ("domain", "platform"),
        [("helper_input_boolean", "input_boolean"), ("zone", "zone")],
    )
    async def test_the_safety_backup_follows_a_renamed_item(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        domain: str,
        platform: str,
    ) -> None:
        """The backup's entity_id names item_a now and item_b was renamed; the
        restore writes item_b, so the safety backup holds item_b. A counter
        with the same storage id is another platform's item."""
        registry = [
            {
                "entity_id": "counter.item_b",
                "platform": "counter",
                "unique_id": "item_b",
            },
            {
                "entity_id": f"{platform}.item_b",
                "platform": platform,
                "unique_id": "item_a",
            },
            {
                "entity_id": f"{platform}.renamed",
                "platform": platform,
                "unique_id": "item_b",
            },
        ]
        items = [{"id": "item_a", "name": "A"}, {"id": "item_b", "name": "B2"}]
        sent = _store_ws(monkeypatch, registry, items)
        mgr = _mk_manager(tmp_path)
        bm.register_default_handlers(mgr, None)
        name = _store_snapshot(tmp_path, domain, f"{platform}.item_b", "item_b")

        result = await _edits_restore(mgr, "edits", "restore", name)

        held = mgr.read_snapshot(result["data"]["safety_backup"])
        assert {k: held["config"][k] for k in ("id", "name")} == {
            "id": "item_b",
            "name": "B2",
        }
        assert "restore the safety_backup" in result["warnings"][0]
        assert [
            m[f"{platform}_id"] for m in sent if m["type"] == f"{platform}/update"
        ] == ["item_b"]

    async def test_a_removed_zone_whose_entity_id_names_another_is_recreated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is overwritten, so no backup is taken, of the other zone either."""
        registry = [
            {"entity_id": "zone.hall", "platform": "zone", "unique_id": "porch"}
        ]
        sent = _store_ws(monkeypatch, registry, [{"id": "porch", "name": "Porch"}])
        mgr = _mk_manager(tmp_path)
        bm.register_default_handlers(mgr, None)
        name = _store_snapshot(tmp_path, "zone", "zone.hall", "hall")

        result = await _edits_restore(mgr, "edits", "restore", name)

        assert result["data"]["result"]["restore_mode"] == "recreated"
        assert result["data"]["safety_backup"] is None
        assert "zone hall no longer exists" in result["warnings"][0]
        assert _snapshot_files(tmp_path) == [name]
        assert [m["type"] for m in sent if m.get("zone_id") == "porch"] == []

    async def test_an_unreadable_registry_blocks_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lookup is part of the safety capture: its failure is one."""
        down = HomeAssistantCommandError("Command failed: down", "unknown_error")
        sent = _store_ws(monkeypatch, None, [{"id": "item_b"}], registry_error=down)
        mgr = _mk_manager(tmp_path)
        bm.register_default_handlers(mgr, None)
        name = _store_snapshot(tmp_path, "helper_input_boolean", "x.item_b", "item_b")

        with pytest.raises(ToolError) as exc_info:
            await _edits_restore(mgr, "edits", "restore", name)

        reply = json.loads(str(exc_info.value))
        assert reply["error"]["code"] == "BACKUP_CAPTURE_FAILED"
        assert reply["data"]["apply_status"] == "not_applied"
        assert [m for m in sent if m["type"] == "input_boolean/update"] == []

    async def test_an_earlier_directory_failure_does_not_stop_it(
        self, tmp_path: Path
    ) -> None:
        """A failed directory check sets an error that a later write clears."""
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"alias": "Hall lights"}))
        mgr._init_dir_error = "transient"

        result = await _edits_restore(mgr, "edits", "restore", _snapshot(tmp_path))

        assert result["data"]["safety_backup"] is not None

    async def test_a_failed_apply_names_the_safety_backup(self, tmp_path: Path) -> None:
        """The write may have applied partly; the error keeps its own code."""
        mgr = _mk_manager(tmp_path)
        mgr.register(
            _mk_handler(
                fetched={"alias": "Hall lights"},
                raise_on_restore=HomeAssistantAPIError("Not found", status_code=404),
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await _edits_restore(mgr, "edits", "restore", _snapshot(tmp_path))

        reply = json.loads(str(exc_info.value))
        assert reply["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert reply["data"]["safety_backup"] is not None
        assert reply["data"]["apply_status"] == "unknown"

    async def test_with_auto_backup_off_none_is_promised(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path, enable_auto_backup=False)
        mgr.register(_mk_handler(fetched=None))

        result = await _edits_restore(mgr, "edits", "restore", _snapshot(tmp_path))

        assert "warnings" not in result


def test_settings_shows_the_restore_warnings(settings_script: str) -> None:
    """Settings showed only the safety backup's name, never why it is missing."""
    reply = {
        "status": 200,
        "json": {
            "success": True,
            "data": {
                "safety_backup": None,
                "warnings": ["No safety backup was taken: nothing was found"],
                "result": {"warnings": ["The zone keeps its stored icon"]},
            },
        },
    }

    rendered = run_restore(settings_script, reply)

    assert "nothing was found" in rendered.alerts[0]
    assert "keeps its stored icon" in rendered.alerts[0]


def test_settings_reports_a_failed_write_with_its_warnings(
    settings_script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route answered a failed write with a plain-text HTTP 500."""
    mgr = _mk_manager(tmp_path)
    mgr.register(
        _mk_handler(
            fetched=None,
            raise_on_restore=HomeAssistantAPIError("Not found", status_code=404),
        )
    )
    name = _snapshot(tmp_path)

    response = response_for(monkeypatch, mgr, name)
    rendered = run_restore(
        settings_script, {"status": response.status_code, "json": response.json()}, name
    )

    assert response.status_code == 409
    assert response.json()["data"]["apply_status"] == "unknown"
    assert "nothing was found under hall_lights" in rendered.alerts[0]


def test_settings_names_a_recreated_helper_s_mapping_warning_once(
    settings_script: str,
) -> None:
    """Its outcome message already renders the result's warning."""
    note = "This snapshot has no entity mapping; the recreated helper may have a new entity ID."
    outcome = {
        "restore_mode": "recreated",
        "entity_id": "new-entry",
        "apply_status": "applied",
        "verification_status": "matched",
        "safety_backup": None,
        "result": {
            "entry_id": "new-entry",
            "entity_ids_restored": False,
            "warnings": [note],
        },
    }

    rendered = run_restore(
        settings_script, {"status": 200, "json": {"success": True, "data": outcome}}
    )

    assert rendered.alerts[0].count("may have a new entity ID") == 1


@pytest.mark.parametrize(
    "outcome",
    [
        # A flow helper's recreation carries restore_mode at the top level and
        # today returns no other warning; this pins that one would show.
        {
            "restore_mode": "recreated",
            "apply_status": "applied",
            "result": {"entry_id": "new-entry", "entity_ids_restored": True},
        },
        # A zone's recreation keeps restore_mode inside the result, as sent.
        {"result": {"id": "hall", "restore_mode": "recreated"}},
    ],
    ids=["flow-helper-with-mapping", "zone"],
)
def test_settings_keeps_a_recreated_item_s_own_warnings(
    settings_script: str, outcome: dict[str, Any]
) -> None:
    """Only the no-mapping warning is rendered by the outcome message."""
    outcome = {
        **outcome,
        "safety_backup": None,
        "result": {**outcome["result"], "warnings": ["Created with a warning"]},
    }

    rendered = run_restore(
        settings_script, {"status": 200, "json": {"success": True, "data": outcome}}
    )

    assert "Created with a warning" in rendered.alerts[0]
