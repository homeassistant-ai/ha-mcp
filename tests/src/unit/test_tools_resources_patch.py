"""Unit tests for search/replace patching and hashing of inline dashboard resources."""

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest

from ha_mcp import backup_manager
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import auto_backup, resource_patch
from ha_mcp.tools.resource_codec import (
    _PREVIEW_LENGTH,
    MAX_CONTENT_SIZE,
    _data_uri_for,
    _decode_data_uri,
)
from ha_mcp.tools.resource_patch import (
    MAX_EDITS,
    RESOURCE_LOCK_REGISTRY,
    resource_write_lock,
)
from ha_mcp.tools.tools_resources import ResourceTools, register_resources_tools
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .test_resource_codec import _legacy_url

RESOURCE_ID = "res-0001"
CARD = "const color = 'red';\nconst speed = 1.5;\nconst bounds = [10, 20, 30, 40];\n"


class FakeLovelace:
    """Minimal lovelace/resources WebSocket API that stores and records writes.

    Every call yields to the event loop first, as a real round-trip does, so
    concurrent tool calls can interleave between a read and a write.
    """

    def __init__(self, resources: list[dict[str, Any]]) -> None:
        self.resources = resources
        self.updates: list[dict[str, Any]] = []
        # What HA stores for a written URL; Core returns the stored item.
        self.store: Callable[[str], str] = lambda url: url
        self.reject_updates = False

    async def send_websocket_message(self, message: dict[str, Any]) -> Any:
        await asyncio.sleep(0)
        kind = message["type"]
        if kind == "lovelace/resources":
            return {"success": True, "result": self.resources}
        if kind == "lovelace/info":
            return {"success": True, "result": {"resource_mode": "storage"}}
        if kind == "lovelace/resources/update":
            self.updates.append(message)
            if self.reject_updates:
                return {
                    "success": False,
                    "error_code": "not_found",
                    "error": {"message": "Unable to find resource"},
                }
            item = {
                "id": message["resource_id"],
                "type": message["res_type"],
                "url": self.store(message["url"]),
            }
            self.resources = [
                item if row["id"] == item["id"] else row for row in self.resources
            ]
            return {"success": True, "result": item}
        raise AssertionError(f"unexpected WS command {kind}")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _inline(content: str, res_type: str = "module") -> dict[str, Any]:
    return {
        "id": RESOURCE_ID,
        "type": res_type,
        "url": _data_uri_for(content, res_type),
    }


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))


async def _patch(fake: FakeLovelace, **kwargs: Any) -> dict[str, Any]:
    tool = ResourceTools(fake).ha_config_set_dashboard_resource
    return await tool(resource_id=RESOURCE_ID, **kwargs)


async def _patch_error(fake: FakeLovelace, **kwargs: Any) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc_info:
        await _patch(fake, **kwargs)
    assert fake.updates == [], "a failed patch must not write anything"
    return _error(exc_info)


class TestPatchSuccess:
    async def test_two_edits_saved_with_size_and_hash_of_stored_content(self):
        fake = FakeLovelace([_inline(CARD, "css")])

        result = await _patch(
            fake,
            edits=[
                {"old": "[10, 20, 30, 40]", "new": "[10, 20, 32, 40]"},
                {"old": "speed = 1.5", "new": "speed = 2.5"},
            ],
            config_hash=_sha(CARD).upper(),
        )

        (update,) = fake.updates
        stored = _decode_data_uri(update["url"])
        assert stored == CARD.replace("30, 40", "32, 40").replace("1.5", "2.5")
        assert update["res_type"] == "css", "patching must keep the stored type"
        assert result["action"] == "patched"
        assert result["applied"] == 2
        assert result["size"] == len(stored.encode("utf-8"))
        assert result["config_hash"] == _sha(stored)

    async def test_later_edit_can_target_text_an_earlier_edit_wrote(self):
        fake = FakeLovelace([_inline(CARD)])

        await _patch(
            fake,
            edits=[
                {"old": "'red'", "new": "'blue'"},
                {"old": "color = 'blue'", "new": "colour = 'blue'"},
            ],
        )

        assert "colour = 'blue'" in _decode_data_uri(fake.updates[0]["url"])

    async def test_legacy_worker_resource_is_patched_into_a_data_uri(self):
        fake = FakeLovelace(
            [{"id": RESOURCE_ID, "type": "module", "url": _legacy_url(CARD)}]
        )

        await _patch(fake, edits=[{"old": "'red'", "new": "'green'"}])

        url = fake.updates[0]["url"]
        assert url.startswith("data:")
        assert _decode_data_uri(url) == CARD.replace("'red'", "'green'")

    async def test_suffix_deletion_leaving_preview_length_is_not_a_preview(self):
        head = "/* " + "x" * 144 + " */"
        assert len(head) == _PREVIEW_LENGTH
        tail = "\nconst extra = 1;\n"
        fake = FakeLovelace([_inline(head + tail)])

        await _patch(fake, edits=[{"old": tail, "new": ""}])

        assert _decode_data_uri(fake.updates[0]["url"]) == head


class TestPatchRefusals:
    async def test_failing_second_edit_saves_nothing(self):
        fake = FakeLovelace([_inline(CARD)])

        error = await _patch_error(
            fake,
            edits=[
                {"old": "'red'", "new": "'blue'"},
                {"old": "not in the card", "new": "x"},
            ],
        )

        assert error["reason"] == "old_not_found"
        assert error["edit_index"] == 1

    async def test_ambiguous_old_reports_match_count_including_overlaps(self):
        fake = FakeLovelace([_inline("aaa;\n")])

        error = await _patch_error(fake, edits=[{"old": "aa", "new": "b"}])

        assert error["reason"] == "old_not_unique"
        assert error["edit_index"] == 0
        assert error["matches"] == 2

    async def test_empty_or_no_op_edits_are_all_reported(self):
        fake = FakeLovelace([_inline(CARD)])

        error = await _patch_error(
            fake,
            edits=[
                {"old": "'red'", "new": "'blue'"},
                {"old": "", "new": "x"},
                {"old": "speed", "new": "speed"},
            ],
        )

        assert error["reason"] == "invalid_edit"
        assert error["invalid_edit_indexes"] == [1, 2]

    async def test_changed_content_is_refused_by_expected_hash(self):
        fake = FakeLovelace([_inline(CARD)])

        error = await _patch_error(
            fake,
            edits=[{"old": "'red'", "new": "'blue'"}],
            config_hash=_sha("an older version"),
        )

        assert error["reason"] == "hash_mismatch"
        assert error["current_config_hash"] == _sha(CARD)

    async def test_non_inline_resource_is_refused(self):
        url = "/local/x.js"
        fake = FakeLovelace([{"id": RESOURCE_ID, "type": "module", "url": url}])

        error = await _patch_error(fake, edits=[{"old": "a", "new": "b"}])

        assert error["reason"] == "not_inline"
        assert error["url"] == url

    async def test_legacy_row_of_non_inline_type_is_refused(self):
        fake = FakeLovelace(
            [{"id": RESOURCE_ID, "type": "js", "url": _legacy_url(CARD)}]
        )

        error = await _patch_error(fake, edits=[{"old": "'red'", "new": "'blue'"}])

        assert error["reason"] == "unsupported_type"
        assert error["resource_type"] == "js"

    async def test_oversized_edit_payload_is_refused_before_applying(self, monkeypatch):
        fake = FakeLovelace([_inline(CARD)])
        half = "x" * (MAX_CONTENT_SIZE // 2 + 1)
        calls: list[str] = []
        count = resource_patch._count_occurrences

        def recording_count(haystack: str, needle: str) -> int:
            calls.append(needle)
            return count(haystack, needle)

        monkeypatch.setattr(resource_patch, "_count_occurrences", recording_count)

        # The final result would fit; the combined 'new' texts do not.
        error = await _patch_error(
            fake,
            edits=[
                {"old": "'red'", "new": half},
                {"old": half, "new": half[:-1]},
            ],
        )

        assert error["reason"] == "edits_too_large"
        assert error["size"] > error["limit"]
        assert calls == [], "no edit may be applied before the size check"

    async def test_unpaired_surrogate_in_new_is_an_invalid_edit(self):
        fake = FakeLovelace([_inline(CARD)])

        error = await _patch_error(
            fake,
            edits=[
                {"old": "'red'", "new": "'blue'"},
                {"old": "1.5", "new": "\ud800"},
            ],
        )

        assert error["reason"] == "invalid_edit"
        assert error["edit_index"] == 1

    async def test_result_over_the_size_limit_is_refused(self):
        near_limit = "/*" + "x" * (MAX_CONTENT_SIZE - 20) + "*/\nconst c = 'red';\n"
        fake = FakeLovelace([_inline(near_limit)])

        # The 'new' text is small; only the patched content crosses the limit.
        error = await _patch_error(
            fake, edits=[{"old": "'red'", "new": "'r" + "e" * 40 + "d'"}]
        )

        assert "Content too large" in error["error"]["message"]
        assert error["size"] > error["limit"]

    async def test_rejected_update_is_reported_and_leaves_the_resource(self):
        fake = FakeLovelace([_inline(CARD)])
        fake.reject_updates = True

        with pytest.raises(ToolError) as exc_info:
            await _patch(fake, edits=[{"old": "'red'", "new": "'blue'"}])

        assert _error(exc_info)["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert _decode_data_uri(fake.resources[0]["url"]) == CARD

    async def test_unknown_resource_id_is_not_found(self):
        fake = FakeLovelace([_inline(CARD) | {"id": "other"}])

        error = await _patch_error(fake, edits=[{"old": "'red'", "new": "'blue'"}])

        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"


class TestWriteSerialization:
    async def test_concurrent_patches_on_one_resource_keep_both_edits(self):
        fake = FakeLovelace([_inline(CARD)])

        await asyncio.gather(
            _patch(fake, edits=[{"old": "'red'", "new": "'blue'"}]),
            _patch(fake, edits=[{"old": "speed = 1.5", "new": "speed = 2.5"}]),
        )

        stored = _decode_data_uri(fake.resources[0]["url"])
        assert stored == CARD.replace("'red'", "'blue'").replace("1.5", "2.5")

    async def test_backup_capture_runs_inside_the_write_lock(self, monkeypatch):
        held: list[bool] = []

        async def capture(*_args: Any, **_kwargs: Any) -> None:
            held.append(
                registry_update_lock(RESOURCE_LOCK_REGISTRY, RESOURCE_ID).locked()
            )

        settings = type("Settings", (), {"enable_auto_backup": True})()
        monkeypatch.setattr(auto_backup, "get_global_settings", lambda: settings)
        monkeypatch.setattr(auto_backup, "_capture_pre_write_snapshot", capture)

        await _patch(
            FakeLovelace([_inline(CARD)]), edits=[{"old": "'red'", "new": "'blue'"}]
        )

        assert held == [True]

    async def test_restore_waits_for_the_tool_write_lock(self, monkeypatch):
        sent: list[dict[str, Any]] = []

        async def fake_send(_client: Any, message: dict[str, Any]) -> Any:
            sent.append(message)
            return {"id": RESOURCE_ID}

        monkeypatch.setattr(backup_manager, "_ws_send", fake_send)
        config = {"id": RESOURCE_ID, "type": "module", "url": "/local/x.js"}

        async with resource_write_lock(RESOURCE_ID):
            restore = asyncio.create_task(
                backup_manager._restore_dashboard_resource(None, RESOURCE_ID, config)
            )
            for _ in range(5):
                await asyncio.sleep(0)
            assert sent == [], "restore must not write while a tool write runs"
        assert await restore == {"id": RESOURCE_ID}
        assert [m["type"] for m in sent] == ["lovelace/resources/update"]


class TestPatchModeSelection:
    async def test_edits_combined_with_content_is_refused(self):
        fake = FakeLovelace([_inline(CARD)])

        await _patch_error(fake, edits=[{"old": "a", "new": "b"}], content="x")

    async def test_edits_without_resource_id_is_refused(self):
        fake = FakeLovelace([_inline(CARD)])
        tool = ResourceTools(fake).ha_config_set_dashboard_resource

        with pytest.raises(ToolError):
            await tool(edits=[{"old": "'red'", "new": "'blue'"}])

        assert fake.updates == []

    async def test_expected_hash_without_edits_is_refused_instead_of_ignored(self):
        fake = FakeLovelace([_inline(CARD)])

        await _patch_error(fake, content="const x = 1;", config_hash=_sha(CARD))

    async def test_registered_schema_bounds_the_number_of_edits(self):
        mcp = FastMCP("test")
        register_resources_tools(mcp, FakeLovelace([]))
        tool = await mcp.get_tool("ha_config_set_dashboard_resource")

        schema = tool.parameters["properties"]["edits"]
        (array,) = [s for s in schema["anyOf"] if s.get("type") == "array"]
        assert (array["minItems"], array["maxItems"]) == (1, MAX_EDITS)


class TestHashReporting:
    async def test_content_write_returns_hash_of_stored_content(self):
        fake = FakeLovelace([_inline(CARD)])

        result = await _patch(fake, content="const x = 'ü';")

        assert result["config_hash"] == _sha(_decode_data_uri(fake.updates[0]["url"]))

    async def test_write_hash_is_taken_from_the_item_ha_returns(self):
        fake = FakeLovelace([_inline(CARD)])
        fake.store = lambda _url: _data_uri_for("const normalized = 1;", "module")

        result = await _patch(fake, edits=[{"old": "'red'", "new": "'blue'"}])

        assert result["config_hash"] == _sha("const normalized = 1;")

    async def test_include_hash_adds_config_hash_only_when_requested(self):
        external = {"id": "ext", "type": "module", "url": "/local/x.js"}
        fake = FakeLovelace([_inline(CARD), external])
        list_tool = ResourceTools(fake).ha_config_list_dashboard_resources

        hashed = (await list_tool(include_hash=True))["resources"]
        plain = (await list_tool())["resources"]

        assert hashed[0]["_config_hash"] == _sha(CARD)
        assert "_config_hash" not in hashed[1], (
            "external resources have no content to hash"
        )
        assert all("_config_hash" not in row for row in plain)
