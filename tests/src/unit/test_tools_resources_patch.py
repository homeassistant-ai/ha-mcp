"""Search/replace patching and hashing of inline dashboard resources on Core's lovelace."""

import asyncio
import base64
import contextlib
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.tools.resource_codec import (
    LEGACY_WORKER_BASE_URL,
    MAX_CONTENT_SIZE,
    content_sha256,
)
from ha_mcp.tools.resource_patch import MAX_EDITS, resource_write_lock
from ha_mcp.tools.tools_resources import ResourceTools, register_resources_tools

from ._held_command import held_command
from ._tool_errors import error_body, error_of

CARD = "const color = 'red';\nconst speed = 1.5;\nconst bounds = [10, 20, 30, 40];\n"
PREVIEW_LENGTH = 150
MIME = {"module": "text/javascript", "css": "text/css;charset=utf-8"}


def _inline_url(content: str, res_type: str = "module") -> str:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"data:{MIME[res_type]};base64,{encoded}"


def _content_of(url: str) -> str:
    return base64.b64decode(url.split(",", 1)[1], validate=True).decode("utf-8")


def _legacy_url(content: str) -> str:
    encoded = base64.urlsafe_b64encode(content.encode()).decode()
    return f"{LEGACY_WORKER_BASE_URL}/{encoded}?type=module"


async def _rows(client: HomeAssistantClient) -> list[dict[str, Any]]:
    reply = await client.send_websocket_message({"type": "lovelace/resources"})
    assert reply["success"] is True, reply
    rows: list[dict[str, Any]] = reply["result"]
    return rows


async def _create(client: HomeAssistantClient, res_type: str, url: str) -> str:
    reply = await client.send_websocket_message(
        {"type": "lovelace/resources/create", "res_type": res_type, "url": url}
    )
    assert reply["success"] is True, reply
    resource_id: str = reply["result"]["id"]
    return resource_id


async def _create_inline(
    client: HomeAssistantClient, content: str, res_type: str = "module"
) -> str:
    return await _create(client, res_type, _inline_url(content, res_type))


async def _stored(client: HomeAssistantClient, resource_id: str) -> str:
    (row,) = [r for r in await _rows(client) if r["id"] == resource_id]
    return _content_of(row["url"])


@pytest.fixture(autouse=True)
async def _lovelace(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})


@pytest.fixture
def auto_backup(
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> bm.BackupManager:
    """Auto-backup on, unthrottled, writing under ``tmp_path``."""
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("AUTO_BACKUP_THROTTLE_MINUTES", "0")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
    reset_global_settings()
    return bm.get_backup_manager(core_client, get_global_settings())


async def _patch(
    client: HomeAssistantClient, resource_id: str, **kwargs: Any
) -> dict[str, Any]:
    return await ResourceTools(client).ha_config_set_dashboard_resource(
        resource_id=resource_id, **kwargs
    )


async def _patch_error(
    client: HomeAssistantClient, resource_id: str, **kwargs: Any
) -> dict[str, Any]:
    before = await _rows(client)
    with pytest.raises(ToolError) as exc_info:
        await _patch(client, resource_id, **kwargs)
    assert await _rows(client) == before, "a failed patch must not write anything"
    return error_body(exc_info)


class TestPatchSuccess:
    async def test_two_edits_saved_with_size_and_hash_of_stored_content(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD, "css")

        result = await _patch(
            core_client,
            resource_id,
            edits=[
                {"old": "[10, 20, 30, 40]", "new": "[10, 20, 32, 40]"},
                {"old": "speed = 1.5", "new": "speed = 2.5"},
            ],
            config_hash=content_sha256(CARD).upper(),
        )

        (row,) = await _rows(core_client)
        stored = _content_of(row["url"])
        assert stored == CARD.replace("30, 40", "32, 40").replace("1.5", "2.5")
        assert row["type"] == "css", "patching must keep the stored type"
        assert result["action"] == "patched"
        assert result["applied"] == 2
        assert result["size"] == len(stored.encode("utf-8"))
        assert result["config_hash"] == content_sha256(stored)

    async def test_later_edit_can_target_text_an_earlier_edit_wrote(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)

        await _patch(
            core_client,
            resource_id,
            edits=[
                {"old": "'red'", "new": "'blue'"},
                {"old": "color = 'blue'", "new": "colour = 'blue'"},
            ],
        )

        assert "colour = 'blue'" in await _stored(core_client, resource_id)

    async def test_legacy_worker_resource_is_patched_into_a_data_uri(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create(core_client, "module", _legacy_url(CARD))

        await _patch(
            core_client, resource_id, edits=[{"old": "'red'", "new": "'green'"}]
        )

        (row,) = await _rows(core_client)
        assert row["url"].startswith("data:")
        assert _content_of(row["url"]) == CARD.replace("'red'", "'green'")

    async def test_suffix_deletion_leaving_preview_length_is_not_a_preview(
        self, core_client: HomeAssistantClient
    ) -> None:
        head = "/* " + "x" * (PREVIEW_LENGTH - 6) + " */"
        tail = "\nconst extra = 1;\n"
        resource_id = await _create_inline(core_client, head + tail)

        await _patch(core_client, resource_id, edits=[{"old": tail, "new": ""}])

        assert await _stored(core_client, resource_id) == head


class TestPatchRefusals:
    async def test_failing_second_edit_saves_nothing(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)

        error = await _patch_error(
            core_client,
            resource_id,
            edits=[
                {"old": "'red'", "new": "'blue'"},
                {"old": "not in the card", "new": "x"},
            ],
        )

        assert error["reason"] == "old_not_found"
        assert error["edit_index"] == 1

    async def test_ambiguous_old_reports_match_count_including_overlaps(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, "aaa;\n")

        error = await _patch_error(
            core_client, resource_id, edits=[{"old": "aa", "new": "b"}]
        )

        assert error["reason"] == "old_not_unique"
        assert error["edit_index"] == 0
        assert error["matches"] == 2

    async def test_empty_or_no_op_edits_are_all_reported(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)

        error = await _patch_error(
            core_client,
            resource_id,
            edits=[
                {"old": "'red'", "new": "'blue'"},
                {"old": "", "new": "x"},
                {"old": "speed", "new": "speed"},
            ],
        )

        assert error["reason"] == "invalid_edit"
        assert error["invalid_edit_indexes"] == [1, 2]

    async def test_changed_content_is_refused_by_expected_hash(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)

        error = await _patch_error(
            core_client,
            resource_id,
            edits=[{"old": "'red'", "new": "'blue'"}],
            config_hash=content_sha256("an older version"),
        )

        assert error["reason"] == "hash_mismatch"
        assert error["current_config_hash"] == content_sha256(CARD)

    async def test_non_inline_resource_is_refused(
        self, core_client: HomeAssistantClient
    ) -> None:
        url = "/local/x.js"
        resource_id = await _create(core_client, "module", url)

        error = await _patch_error(
            core_client, resource_id, edits=[{"old": "a", "new": "b"}]
        )

        assert error["reason"] == "not_inline"
        assert error["url"] == url

    async def test_legacy_row_of_non_inline_type_is_refused(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create(core_client, "js", _legacy_url(CARD))

        error = await _patch_error(
            core_client, resource_id, edits=[{"old": "'red'", "new": "'blue'"}]
        )

        assert error["reason"] == "unsupported_type"
        assert error["resource_type"] == "js"

    async def test_oversized_edit_payload_is_refused_before_applying(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)
        half = "x" * (MAX_CONTENT_SIZE // 2 + 1)

        # The final result would fit; the combined 'new' texts do not.
        error = await _patch_error(
            core_client,
            resource_id,
            edits=[
                {"old": "'red'", "new": half},
                {"old": half, "new": half[:-1]},
            ],
        )

        assert error["reason"] == "edits_too_large"
        assert error["size"] > error["limit"]

    async def test_unpaired_surrogate_in_new_is_an_invalid_edit(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)

        error = await _patch_error(
            core_client,
            resource_id,
            edits=[
                {"old": "'red'", "new": "'blue'"},
                {"old": "1.5", "new": "\ud800"},
            ],
        )

        assert error["reason"] == "invalid_edit"
        assert error["edit_index"] == 1

    async def test_result_over_the_size_limit_is_refused(
        self, core_client: HomeAssistantClient
    ) -> None:
        near_limit = "/*" + "x" * (MAX_CONTENT_SIZE - 20) + "*/\nconst c = 'red';\n"
        resource_id = await _create_inline(core_client, near_limit)

        # The 'new' text is small; only the patched content crosses the limit.
        error = await _patch_error(
            core_client,
            resource_id,
            edits=[{"old": "'red'", "new": "'r" + "e" * 40 + "d'"}],
        )

        assert "Content too large" in error["error"]["message"]
        assert error["size"] > error["limit"]

    async def test_resource_deleted_before_the_write_is_reported_and_left_gone(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)

        async with held_command(hass, "lovelace/resources/update") as held:
            patch = asyncio.create_task(
                _patch(
                    core_client, resource_id, edits=[{"old": "'red'", "new": "'blue'"}]
                )
            )
            await asyncio.wait_for(held.arrived.wait(), timeout=10)
            reply = await core_client.send_websocket_message(
                {"type": "lovelace/resources/delete", "resource_id": resource_id}
            )
            assert reply["success"] is True, reply
            held.release.set()
            with pytest.raises(ToolError) as exc_info:
                await patch

        assert error_of(exc_info)["code"] == "RESOURCE_NOT_FOUND"
        assert await _rows(core_client) == []

    async def test_unknown_resource_id_is_not_found(
        self, core_client: HomeAssistantClient
    ) -> None:
        await _create_inline(core_client, CARD)

        error = await _patch_error(
            core_client, "other", edits=[{"old": "'red'", "new": "'blue'"}]
        )

        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"


class TestWriteSerialization:
    async def test_concurrent_patches_on_one_resource_keep_both_edits(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)

        await asyncio.gather(
            _patch(core_client, resource_id, edits=[{"old": "'red'", "new": "'blue'"}]),
            _patch(
                core_client,
                resource_id,
                edits=[{"old": "speed = 1.5", "new": "speed = 2.5"}],
            ),
        )

        stored = await _stored(core_client, resource_id)
        assert stored == CARD.replace("'red'", "'blue'").replace("1.5", "2.5")

    async def test_second_patch_and_its_backup_wait_for_the_first_write(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        auto_backup: bm.BackupManager,
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)
        red = {"old": "'red'", "new": "'blue'"}
        speed = {"old": "speed = 1.5", "new": "speed = 2.5"}

        async with held_command(
            hass, "lovelace/resources/update", first_only=True
        ) as held:
            first = asyncio.create_task(_patch(core_client, resource_id, edits=[red]))
            await asyncio.wait_for(held.arrived.wait(), timeout=10)
            second = asyncio.create_task(
                _patch(core_client, resource_id, edits=[speed])
            )
            done, _pending = await asyncio.wait({second}, timeout=1)
            assert not done, "the second patch must wait for the first write"
            held.release.set()
            await asyncio.gather(first, second)

        stored = await _stored(core_client, resource_id)
        assert stored == CARD.replace("'red'", "'blue'").replace("1.5", "2.5")
        backups = sorted(
            _content_of(auto_backup.read_snapshot(row["name"])["config"]["url"])
            for row in auto_backup.list_snapshots()
        )
        assert backups == sorted([CARD, CARD.replace("'red'", "'blue'")]), (
            "the second backup must hold the first edit"
        )

    async def test_restore_waits_for_the_tool_write_lock(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        auto_backup: bm.BackupManager,
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)
        path = await auto_backup.maybe_snapshot("dashboard_resource", resource_id)
        assert path is not None
        reply = await core_client.send_websocket_message(
            {
                "type": "lovelace/resources/update",
                "resource_id": resource_id,
                "res_type": "module",
                "url": "/local/moved.js",
            }
        )
        assert reply["success"] is True, reply

        async with held_command(hass, "lovelace/resources/update") as held:
            async with resource_write_lock(resource_id):
                restore = asyncio.create_task(
                    auto_backup.restore_snapshot(path.name, take_safety_backup=False)
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(held.arrived.wait(), timeout=1)
                assert not held.arrived.is_set(), (
                    "restore must not write while a tool write runs"
                )
            await asyncio.wait_for(held.arrived.wait(), timeout=10)
            held.release.set()
            await restore

        assert await _stored(core_client, resource_id) == CARD


class TestPatchModeSelection:
    async def test_edits_combined_with_content_is_refused(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)

        await _patch_error(
            core_client,
            resource_id,
            edits=[{"old": "a", "new": "b"}],
            content="x",
        )

    async def test_edits_without_resource_id_is_refused(
        self, core_client: HomeAssistantClient
    ) -> None:
        await _create_inline(core_client, CARD)
        before = await _rows(core_client)

        with pytest.raises(ToolError):
            await ResourceTools(core_client).ha_config_set_dashboard_resource(
                edits=[{"old": "'red'", "new": "'blue'"}]
            )

        assert await _rows(core_client) == before

    async def test_expected_hash_without_edits_is_refused_instead_of_ignored(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)

        await _patch_error(
            core_client,
            resource_id,
            content="const x = 1;",
            config_hash=content_sha256(CARD),
        )

    async def test_registered_schema_bounds_the_number_of_edits(
        self, core_client: HomeAssistantClient
    ) -> None:
        mcp = FastMCP("test")
        register_resources_tools(mcp, core_client)
        tool = await mcp.get_tool("ha_config_set_dashboard_resource")

        schema = tool.parameters["properties"]["edits"]
        (array,) = [s for s in schema["anyOf"] if s.get("type") == "array"]
        assert (array["minItems"], array["maxItems"]) == (1, MAX_EDITS)


class TestHashReporting:
    async def test_content_write_returns_hash_of_stored_content(
        self, core_client: HomeAssistantClient
    ) -> None:
        resource_id = await _create_inline(core_client, CARD)

        result = await _patch(core_client, resource_id, content="const x = 'ü';")

        assert result["config_hash"] == content_sha256(
            await _stored(core_client, resource_id)
        )

    async def test_write_hash_is_taken_from_the_item_ha_returns(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Core answers with the item it stored; here it stores a normalized URL."""
        resource_id = await _create_inline(core_client, CARD)
        normalized = _inline_url("const normalized = 1;")
        update = ResourceStorageCollection.async_update_item

        async def store_normalized(
            collection: ResourceStorageCollection, item_id: str, updates: dict[str, Any]
        ) -> dict[str, Any]:
            return await update(collection, item_id, {**updates, "url": normalized})

        monkeypatch.setattr(
            ResourceStorageCollection, "async_update_item", store_normalized
        )

        result = await _patch(
            core_client, resource_id, edits=[{"old": "'red'", "new": "'blue'"}]
        )

        assert result["config_hash"] == content_sha256("const normalized = 1;")

    async def test_include_hash_adds_config_hash_only_when_requested(
        self, core_client: HomeAssistantClient
    ) -> None:
        await _create_inline(core_client, CARD)
        await _create(core_client, "module", "/local/x.js")
        list_tool = ResourceTools(core_client).ha_config_list_dashboard_resources

        hashed = (await list_tool(include_hash=True))["resources"]
        plain = (await list_tool())["resources"]

        assert hashed[0]["_config_hash"] == content_sha256(CARD)
        assert "_config_hash" not in hashed[1], (
            "external resources have no content to hash"
        )
        assert all("_config_hash" not in row for row in plain)
