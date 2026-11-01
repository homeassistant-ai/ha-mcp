"""Unit tests for search/replace patching and hashing of inline dashboard resources."""

import hashlib
import json
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.resource_codec import (
    MAX_CONTENT_SIZE,
    _data_uri_for,
    _decode_data_uri,
)
from ha_mcp.tools.tools_resources import ResourceTools

from .test_resource_codec import _legacy_url

RESOURCE_ID = "res-0001"
CARD = "const color = 'red';\nconst speed = 1.5;\nconst bounds = [10, 20, 30, 40];\n"


class FakeLovelace:
    """Minimal lovelace/resources WebSocket API that records every write."""

    def __init__(self, resources: list[dict[str, Any]]) -> None:
        self.resources = resources
        self.updates: list[dict[str, Any]] = []

    async def send_websocket_message(self, message: dict[str, Any]) -> Any:
        kind = message["type"]
        if kind == "lovelace/resources":
            return {"success": True, "result": self.resources}
        if kind == "lovelace/info":
            return {"success": True, "result": {"resource_mode": "storage"}}
        if kind == "lovelace/resources/update":
            self.updates.append(message)
            return {"success": True, "result": {"id": message["resource_id"]}}
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
            expected_sha256=_sha(CARD).upper(),
        )

        (update,) = fake.updates
        stored = _decode_data_uri(update["url"])
        assert stored == CARD.replace("30, 40", "32, 40").replace("1.5", "2.5")
        assert update["res_type"] == "css", "patching must keep the stored type"
        assert result["action"] == "patched"
        assert result["applied"] == 2
        assert result["size"] == len(stored.encode("utf-8"))
        assert result["sha256"] == _sha(stored)

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

    async def test_umlauts_and_emoji_survive_the_roundtrip(self):
        content = "const label = 'Wärme 🔥 Grüße';\nconst unit = '°C';\n"
        fake = FakeLovelace([_inline(content)])

        result = await _patch(fake, edits=[{"old": "Wärme 🔥", "new": "Kälte ❄️"}])

        stored = _decode_data_uri(fake.updates[0]["url"])
        assert stored == "const label = 'Kälte ❄️ Grüße';\nconst unit = '°C';\n"
        assert result["sha256"] == _sha(stored)

    async def test_legacy_worker_resource_is_patched_into_a_data_uri(self):
        fake = FakeLovelace(
            [{"id": RESOURCE_ID, "type": "module", "url": _legacy_url(CARD)}]
        )

        await _patch(fake, edits=[{"old": "'red'", "new": "'green'"}])

        url = fake.updates[0]["url"]
        assert url.startswith("data:")
        assert _decode_data_uri(url) == CARD.replace("'red'", "'green'")


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
            expected_sha256=_sha("an older version"),
        )

        assert error["reason"] == "hash_mismatch"
        assert error["actual_sha256"] == _sha(CARD)

    @pytest.mark.parametrize(
        "url", ["/local/x.js", "/hacsfiles/button-card/button-card.js"]
    )
    async def test_non_inline_resource_is_refused(self, url):
        fake = FakeLovelace([{"id": RESOURCE_ID, "type": "module", "url": url}])

        error = await _patch_error(fake, edits=[{"old": "a", "new": "b"}])

        assert error["reason"] == "not_inline"
        assert error["url"] == url

    async def test_result_over_the_size_limit_is_refused(self):
        fake = FakeLovelace([_inline(CARD)])

        error = await _patch_error(
            fake, edits=[{"old": "'red'", "new": "'" + "r" * MAX_CONTENT_SIZE + "'"}]
        )

        assert error["size"] > error["limit"]

    async def test_unknown_resource_id_is_not_found(self):
        fake = FakeLovelace([_inline(CARD) | {"id": "other"}])

        error = await _patch_error(fake, edits=[{"old": "'red'", "new": "'blue'"}])

        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"


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

        await _patch_error(fake, content="const x = 1;", expected_sha256=_sha(CARD))


class TestHashReporting:
    async def test_content_write_returns_hash_of_stored_content(self):
        fake = FakeLovelace([_inline(CARD)])

        result = await _patch(fake, content="const x = 'ü';")

        assert result["sha256"] == _sha(_decode_data_uri(fake.updates[0]["url"]))

    async def test_include_hash_adds_sha256_only_when_requested(self):
        external = {"id": "ext", "type": "module", "url": "/local/x.js"}
        fake = FakeLovelace([_inline(CARD), external])
        list_tool = ResourceTools(fake).ha_config_list_dashboard_resources

        hashed = (await list_tool(include_hash=True))["resources"]
        plain = (await list_tool())["resources"]

        assert hashed[0]["_sha256"] == _sha(CARD)
        assert "_sha256" not in hashed[1], "external resources have no content to hash"
        assert all("_sha256" not in row for row in plain)
