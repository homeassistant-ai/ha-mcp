"""Dashboard resource tools follow Core's lovelace contracts (issue #2632).

Core's ``lovelace/resources/{create,update,delete}`` answer structured codes:
``not_found`` for a missing id (helpers/collection.py) and ``unknown_command``
when resources are YAML-managed, because those commands are only registered in
storage resource mode (lovelace/__init__.py). The tools route on those codes.
Every reply here is Core's own, from ``lovelace`` set up in the mode a test
names.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.tools_resources import ResourceTools

from ._client_spies import sent_messages
from ._tool_errors import error_body, error_of


async def _ws(client: HomeAssistantClient, message: dict[str, Any]) -> Any:
    """Core's result for ``message``; the command must succeed."""
    reply = await client.send_websocket_message(message)
    assert reply["success"] is True, reply
    return reply["result"]


async def _rows(client: HomeAssistantClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = await _ws(client, {"type": "lovelace/resources"})
    return rows


async def _create(client: HomeAssistantClient, res_type: str, url: str) -> str:
    row = await _ws(
        client, {"type": "lovelace/resources/create", "res_type": res_type, "url": url}
    )
    resource_id: str = row["id"]
    return resource_id


@pytest.fixture
async def storage_mode(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})


@pytest.fixture
async def yaml_mode(hass: HomeAssistant) -> None:
    config = {
        "resource_mode": "yaml",
        "resources": [{"type": "module", "url": "/local/card.js"}],
    }
    assert await async_setup_component(hass, "lovelace", {"lovelace": config})


@pytest.mark.usefixtures("yaml_mode")
class TestYamlResourceMode:
    async def test_write_in_yaml_mode_says_resources_are_yaml_managed(
        self, core_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await ResourceTools(core_client).ha_config_set_dashboard_resource(
                url="/local/other.js"
            )

        data = error_body(exc_info)
        assert "YAML-managed" in data["error"]["message"]
        assert data["resource_mode"] == "yaml"

    async def test_list_reports_yaml_resource_mode_and_that_writes_are_unavailable(
        self, core_client: HomeAssistantClient
    ) -> None:
        result = await ResourceTools(core_client).ha_config_list_dashboard_resources()

        assert result["resource_mode"] == "yaml"
        assert "YAML-managed" in result["note"]


async def test_unknown_command_without_yaml_mode_passes_core_error_through(
    core_client: HomeAssistantClient,
) -> None:
    """The YAML explanation needs Core's own resource_mode, not a guess:
    without ``lovelace`` Core knows no resource command and no mode."""
    with pytest.raises(ToolError) as exc_info:
        await ResourceTools(core_client).ha_config_delete_dashboard_resource(
            resource_id="abc"
        )

    data = error_body(exc_info)
    assert data["ha_error_code"] == "unknown_command"
    assert "YAML-managed" not in data["error"]["message"]
    assert "resource_mode" not in data


@pytest.mark.usefixtures("storage_mode")
class TestCoreErrorCodes:
    async def test_update_of_missing_resource_reports_not_found(
        self, core_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await ResourceTools(core_client).ha_config_set_dashboard_resource(
                url="/local/card.js", resource_id="gone"
            )

        data = error_body(exc_info)
        assert data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert data["resource_id"] == "gone"

    async def test_not_found_wording_without_the_code_is_not_reclassified(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only Core's ``not_found`` code means a missing resource, not its words."""

        async def refuse(_collection: ResourceStorageCollection, item_id: str) -> None:
            raise HomeAssistantError(f"Unable to find resource_id {item_id}")

        monkeypatch.setattr(ResourceStorageCollection, "async_delete_item", refuse)

        with pytest.raises(ToolError) as exc_info:
            await ResourceTools(core_client).ha_config_delete_dashboard_resource(
                resource_id="abc"
            )

        data = error_body(exc_info)
        assert data["ha_error_code"] == "home_assistant_error"
        assert data["error"]["code"] == "SERVICE_CALL_FAILED"

    async def test_a_url_already_registered_is_created_again(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Core has no duplicate-URL check, and the tool invents none."""
        first = await _create(core_client, "module", "/local/card.js")

        result = await ResourceTools(core_client).ha_config_set_dashboard_resource(
            url="/local/card.js"
        )

        assert result["resource_id"] != first
        assert [row["url"] for row in await _rows(core_client)] == [
            "/local/card.js",
            "/local/card.js",
        ]


@pytest.mark.usefixtures("storage_mode")
class TestCoreResourceTypes:
    async def test_by_type_counts_every_type_core_returns(
        self, core_client: HomeAssistantClient
    ) -> None:
        await _create(core_client, "html", "/local/a.html")
        await _create(core_client, "module", "/local/b.js")

        result = await ResourceTools(core_client).ha_config_list_dashboard_resources()

        assert result["by_type"] == {"html": 1, "module": 1}

    async def test_html_url_resource_reaches_core(
        self, core_client: HomeAssistantClient
    ) -> None:
        result = await ResourceTools(core_client).ha_config_set_dashboard_resource(
            url="/local/legacy.html", resource_type="html"
        )

        (row,) = await _rows(core_client)
        assert result["resource_id"] == row["id"]
        assert row["type"] == "html"

    async def test_unknown_url_resource_type_is_left_to_core(
        self, core_client: HomeAssistantClient
    ) -> None:
        """The tool keeps no copy of Core's type list; Core's rejection is relayed."""
        create = {
            "type": "lovelace/resources/create",
            "res_type": "wasm",
            "url": "/local/card.wasm",
        }
        refusal = await core_client.send_websocket_message(create)
        assert refusal["success"] is False

        with pytest.raises(ToolError) as exc_info:
            await ResourceTools(core_client).ha_config_set_dashboard_resource(
                url="/local/card.wasm", resource_type="wasm"
            )

        error = error_of(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error["message"] == refusal["error"]
        assert await _rows(core_client) == []

    async def test_inline_html_is_rejected_before_any_write(
        self, core_client: HomeAssistantClient
    ) -> None:
        with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
            await ResourceTools(core_client).ha_config_set_dashboard_resource(
                content="<p>x</p>", resource_type="html"
            )

        assert "html" in error_of(exc_info)["message"]
        sent.assert_not_awaited()

    async def test_inline_error_does_not_suggest_a_type_core_rejects(
        self, core_client: HomeAssistantClient
    ) -> None:
        with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
            await ResourceTools(core_client).ha_config_set_dashboard_resource(
                content="var x = 1;", resource_type="modul"
            )

        suggestions = error_of(exc_info)["suggestions"]
        assert not any("modul'" in s for s in suggestions)
        sent.assert_not_awaited()


THEME = {"type": "css", "url": "/local/theme.css"}


@pytest.fixture
def manager(core_client: HomeAssistantClient, tmp_path: Path) -> bm.BackupManager:
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path / "backups"),
    )
    return bm.get_backup_manager(core_client, settings)


def _spy_commands() -> Any:
    """A spy on every WebSocket command; each still reaches Core."""
    return patch.object(
        HomeAssistantWebSocketClient,
        "send_command",
        autospec=True,
        wraps=HomeAssistantWebSocketClient.send_command,
    )


def _commands(spy: Any) -> list[dict[str, Any]]:
    """The commands ``spy`` saw, as ``{"type": ..., **fields}``."""
    return [
        {"type": call.args[1], **call.kwargs}
        for call in spy.call_args_list
        if call.args[1] != "auth/current_user"
    ]


async def _snapshot_theme(
    core_client: HomeAssistantClient, manager: bm.BackupManager
) -> tuple[str, str]:
    """Create the theme resource in Core and snapshot it; returns (id, name)."""
    resource_id = await _create(core_client, THEME["type"], THEME["url"])
    path = await manager.maybe_snapshot("dashboard_resource", resource_id)
    assert path is not None
    return resource_id, path.name


async def _delete(core_client: HomeAssistantClient, resource_id: str) -> None:
    await _ws(
        core_client,
        {"type": "lovelace/resources/delete", "resource_id": resource_id},
    )


async def _restore(manager: bm.BackupManager, name: str) -> dict[str, Any]:
    return await manager.restore_snapshot(name, take_safety_backup=False)


@pytest.mark.usefixtures("storage_mode")
class TestRestoreDashboardResource:
    async def test_restore_of_deleted_resource_recreates_it(
        self, core_client: HomeAssistantClient, manager: bm.BackupManager
    ) -> None:
        old, name = await _snapshot_theme(core_client, manager)
        await _delete(core_client, old)

        with _spy_commands() as spy:
            await _restore(manager, name)

        (row,) = await _rows(core_client)
        assert row["id"] != old
        assert {"type": row["type"], "url": row["url"]} == THEME
        sent = _commands(spy)
        assert [c["type"] for c in sent] == [
            "lovelace/resources/update",
            "lovelace/resources",
            "lovelace/resources/create",
        ]
        assert sent[-1] == {
            "type": "lovelace/resources/create",
            "res_type": "css",
            "url": "/local/theme.css",
        }

    async def test_restore_of_a_recreated_resource_writes_nothing(
        self, core_client: HomeAssistantClient, manager: bm.BackupManager
    ) -> None:
        """An earlier restore re-created the resource under a new id."""
        old, name = await _snapshot_theme(core_client, manager)
        await _delete(core_client, old)
        await _restore(manager, name)
        (recreated,) = await _rows(core_client)

        with _spy_commands() as spy:
            result = await _restore(manager, name)

        assert await _rows(core_client) == [recreated]
        assert [c["type"] for c in _commands(spy)] == [
            "lovelace/resources/update",
            "lovelace/resources",
        ]
        assert recreated["id"] in json.dumps(result)

    async def test_restore_leaves_another_resource_with_the_same_url_alone(
        self, core_client: HomeAssistantClient, manager: bm.BackupManager
    ) -> None:
        """Core keeps no url uniqueness: a row of another type is not ours."""
        old, name = await _snapshot_theme(core_client, manager)
        await _delete(core_client, old)
        other = await _create(core_client, "js", THEME["url"])

        await _restore(manager, name)

        rows = await _rows(core_client)
        assert {"id": other, "type": "js", "url": THEME["url"]} in rows
        assert [
            {"type": r["type"], "url": r["url"]} for r in rows if r["id"] != other
        ] == [THEME]

    async def test_restore_in_place_also_restores_the_resource_type(
        self, core_client: HomeAssistantClient, manager: bm.BackupManager
    ) -> None:
        resource_id, name = await _snapshot_theme(core_client, manager)
        await _ws(
            core_client,
            {
                "type": "lovelace/resources/update",
                "resource_id": resource_id,
                "res_type": "module",
                "url": "/local/theme.js",
            },
        )

        await _restore(manager, name)

        assert await _rows(core_client) == [{"id": resource_id, **THEME}]


async def test_restore_does_not_recreate_on_other_core_errors(
    yaml_mode: None, core_client: HomeAssistantClient, manager: bm.BackupManager
) -> None:
    """A snapshot restored where resources are YAML-managed: Core knows no
    update command, which is not a deleted resource to re-create."""
    manager.backup_dir.mkdir(parents=True, exist_ok=True)
    name = "dashboard_resource.old.20200101_000000.yaml"
    (manager.backup_dir / name).write_text(
        yaml.safe_dump(
            {
                "schema_version": bm.SCHEMA_VERSION,
                "domain": "dashboard_resource",
                "entity_id": "old",
                "config": {"id": "old", **THEME},
            }
        ),
        encoding="utf-8",
    )

    with _spy_commands() as spy, pytest.raises(Exception, match="nknown command"):
        await _restore(manager, name)

    assert [c["type"] for c in _commands(spy)] == ["lovelace/resources/update"]


async def test_restore_of_a_row_without_a_type_omits_the_type(
    hass_storage: dict[str, Any],
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    manager: bm.BackupManager,
) -> None:
    """Core's import of a stored ``resources:`` block kept untyped rows, and
    its resource store still holds them."""
    hass_storage["lovelace_resources"] = {
        "version": 1,
        "key": "lovelace_resources",
        "data": {"items": [{"id": "imported", "url": THEME["url"]}]},
    }
    assert await async_setup_component(hass, "lovelace", {})
    (imported,) = await _rows(core_client)
    assert "type" not in imported
    path = await manager.maybe_snapshot("dashboard_resource", imported["id"])
    assert path is not None
    await _ws(
        core_client,
        {
            "type": "lovelace/resources/update",
            "resource_id": imported["id"],
            "url": "/local/moved.css",
        },
    )

    with _spy_commands() as spy:
        await _restore(manager, path.name)

    assert await _rows(core_client) == [imported]
    writes = [c for c in _commands(spy) if c["type"].startswith("lovelace/resources/")]
    assert len(writes) == 1
    assert "res_type" not in writes[0]


@pytest.mark.usefixtures("storage_mode")
async def test_diff_after_a_recreating_restore_compares_against_the_new_row(
    core_client: HomeAssistantClient, manager: bm.BackupManager
) -> None:
    old, name = await _snapshot_theme(core_client, manager)
    await _delete(core_client, old)
    await _create(core_client, "css", "/local/other.css")
    same_url = await _create(core_client, "js", THEME["url"])
    recreated = await _create(core_client, THEME["type"], THEME["url"])

    diff = await manager.diff_snapshot(name)

    assert diff["entity_missing"] is False
    assert diff["unchanged"] is True

    for resource_id, url in ((recreated, "/local/moved.css"), (same_url, "/x.css")):
        await _ws(
            core_client,
            {
                "type": "lovelace/resources/update",
                "resource_id": resource_id,
                "url": url,
            },
        )

    assert (await manager.diff_snapshot(name))["entity_missing"] is True
