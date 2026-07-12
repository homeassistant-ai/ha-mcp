"""Unit tests for the deep-search helper name-staleness fix (issue #1794).

HA's storage-level helper record (``<type>/list``) carries the immutable
``id`` (== registry ``unique_id``) and the creation-time entity_id slug + name.
A UI rename updates *only* the entity registry, so a helper searched by its
CURRENT name / entity_id was invisible to the deep-search helper branch, which
scored the storage-derived values. The fix reuses the ha_search orchestrator's
already-fetched entity-registry snapshot (no new request) to map each helper's
``unique_id`` to its current ``(entity_id, name)`` and score/emit those, while
still scoring the storage name so config-body references and un-renamed helpers
never regress.

Two levels:
- **Component**: ``_build_helper_registry_map`` parses the snapshot; a
  renamed helper matches by its current name via ``_search_helper_type``,
  an un-renamed one still matches, the storage name still matches, and the
  no-snapshot path is byte-for-byte today's behaviour.
- **Seam**: a rename driven through the public ``deep_search`` entrypoint with
  a ``prefetched_registry`` snapshot surfaces the current name — and the same
  query without the snapshot does not (proving the snapshot is what fixes it).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools

_INPUT_TYPES = {
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
    "input_button",
}


def _make_tools(client) -> SmartSearchTools:
    """Construct SmartSearchTools without loading global settings."""
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        return SmartSearchTools(client=client)


def _registry(*entries: dict) -> dict:
    """Wrap registry entries in the WS ``entity_registry/list`` success shape."""
    return {"success": True, "result": list(entries)}


# --------------------------------------------------------------------------
# Component: _build_helper_registry_map
# --------------------------------------------------------------------------


class TestBuildHelperRegistryMap:
    def test_none_snapshot_is_empty_map(self) -> None:
        """No snapshot handed down (direct deep_search caller) → empty map."""
        assert SmartSearchTools._build_helper_registry_map(None, _INPUT_TYPES) == {}

    def test_soft_failure_snapshot_is_empty_map(self) -> None:
        """A ``{"success": False}`` snapshot is a soft failure → empty map, so
        the caller degrades to storage-name matching rather than trusting a
        failed registry."""
        assert (
            SmartSearchTools._build_helper_registry_map(
                {"success": False}, _INPUT_TYPES
            )
            == {}
        )

    def test_maps_uid_to_current_entity_id_and_name(self) -> None:
        """A registry ``name`` override maps unique_id → (entity_id, name)."""
        snap = _registry(
            {
                "entity_id": "input_boolean.new_name",
                "platform": "input_boolean",
                "unique_id": "abc123",
                "name": "New Name",
                "original_name": "Old Name",
            }
        )
        out = SmartSearchTools._build_helper_registry_map(snap, _INPUT_TYPES)
        assert out == {"abc123": ("input_boolean.new_name", "New Name")}

    def test_name_falls_back_to_original_name(self) -> None:
        """A null registry ``name`` falls back to ``original_name``."""
        snap = _registry(
            {
                "entity_id": "input_boolean.kitchen_light",
                "platform": "input_boolean",
                "unique_id": "kitchen_light",
                "name": None,
                "original_name": "Kitchen Light",
            }
        )
        out = SmartSearchTools._build_helper_registry_map(snap, _INPUT_TYPES)
        assert out == {
            "kitchen_light": ("input_boolean.kitchen_light", "Kitchen Light")
        }

    def test_missing_both_names_stores_none(self) -> None:
        """Both names absent → name is None so the caller keeps the storage name;
        the corrected entity_id is still captured."""
        snap = _registry(
            {
                "entity_id": "input_boolean.x",
                "platform": "input_boolean",
                "unique_id": "x",
            }
        )
        out = SmartSearchTools._build_helper_registry_map(snap, _INPUT_TYPES)
        assert out == {"x": ("input_boolean.x", None)}

    def test_non_input_platform_and_malformed_entries_skipped(self) -> None:
        """Non-input_* platforms, entries missing unique_id/entity_id, and
        non-dict rows are all skipped."""
        snap = _registry(
            {
                "entity_id": "light.kitchen",
                "platform": "hue",
                "unique_id": "hue-1",
                "name": "Kitchen",
            },
            {"entity_id": "input_boolean.no_uid", "platform": "input_boolean"},
            {"platform": "input_boolean", "unique_id": "no_eid"},
            "not-a-dict",
            {
                "entity_id": "input_number.temp",
                "platform": "input_number",
                "unique_id": "temp",
                "name": "Temp",
            },
        )
        out = SmartSearchTools._build_helper_registry_map(snap, _INPUT_TYPES)
        assert out == {"temp": ("input_number.temp", "Temp")}


# --------------------------------------------------------------------------
# Component: _search_helper_type registry override
# --------------------------------------------------------------------------


def _client_listing(records: list[dict]) -> MagicMock:
    """Client whose ``<type>/list`` returns ``records`` (any input_* type)."""
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": records}
    )
    return client


@pytest.mark.asyncio
class TestSearchHelperTypeRegistryOverride:
    async def test_renamed_helper_matches_by_current_name(self) -> None:
        """A helper renamed in the UI (registry name/entity_id differ from the
        storage record) matches a search for its CURRENT name and emits the
        current name + entity_id."""
        tools = _make_tools(_client_listing([{"id": "abc123", "name": "Old Name"}]))
        registry_by_uid = {"abc123": ("input_boolean.new_name", "New Name")}

        matches, failed = await tools._search_helper_type(
            "input_boolean",
            "new name",
            True,
            asyncio.Semaphore(4),
            registry_by_uid=registry_by_uid,
        )

        assert failed is False
        assert len(matches) == 1
        m = matches[0]
        assert m["entity_id"] == "input_boolean.new_name"
        assert m["name"] == "New Name"
        assert m["match_in_name"] is True

    async def test_unrenamed_helper_still_matches(self) -> None:
        """An un-renamed helper (registry name == storage name) still matches by
        name — no regression."""
        tools = _make_tools(
            _client_listing([{"id": "kitchen_light", "name": "Kitchen Light"}])
        )
        registry_by_uid = {
            "kitchen_light": ("input_boolean.kitchen_light", "Kitchen Light")
        }

        matches, failed = await tools._search_helper_type(
            "input_boolean",
            "kitchen light",
            True,
            asyncio.Semaphore(4),
            registry_by_uid=registry_by_uid,
        )

        assert failed is False
        assert len(matches) == 1
        assert matches[0]["entity_id"] == "input_boolean.kitchen_light"
        assert matches[0]["name"] == "Kitchen Light"
        assert matches[0]["match_in_name"] is True

    async def test_storage_name_still_matches_after_rename(self) -> None:
        """Searching the PRE-rename (storage / config-body) name still finds the
        helper — via the config-body scan — while the emitted name is the
        current one. Guards the "config references must not regress" contract."""
        tools = _make_tools(_client_listing([{"id": "abc123", "name": "Old Name"}]))
        registry_by_uid = {"abc123": ("input_boolean.new_name", "New Name")}

        matches, failed = await tools._search_helper_type(
            "input_boolean",
            "old name",
            True,
            asyncio.Semaphore(4),
            registry_by_uid=registry_by_uid,
        )

        assert failed is False
        assert len(matches) == 1
        assert matches[0]["name"] == "New Name"
        assert matches[0]["match_in_config"] is True

    async def test_no_snapshot_path_is_todays_behaviour(self) -> None:
        """Without a registry map the branch scores the storage-derived
        entity_id + name exactly as before: the current name does NOT match,
        the storage name does."""
        tools = _make_tools(_client_listing([{"id": "abc123", "name": "Old Name"}]))

        # Current name misses (the #1794 bug, unfixed on the no-snapshot path).
        miss, miss_failed = await tools._search_helper_type(
            "input_boolean", "new name", True, asyncio.Semaphore(4)
        )
        assert miss_failed is False
        assert miss == []

        # Storage name still matches, with the storage-derived entity_id/name.
        hit, hit_failed = await tools._search_helper_type(
            "input_boolean", "old name", True, asyncio.Semaphore(4)
        )
        assert hit_failed is False
        assert len(hit) == 1
        assert hit[0]["entity_id"] == "input_boolean.abc123"
        assert hit[0]["name"] == "Old Name"


# --------------------------------------------------------------------------
# Seam: rename surfaces through public deep_search with prefetched_registry
# --------------------------------------------------------------------------


def _seam_client(storage_record: dict) -> MagicMock:
    """Client for a helper-only deep_search: one input_boolean storage record,
    every other input_*/list clean-empty, flow-helper entries clean-empty."""

    async def _ws(msg):
        if msg.get("type") == "input_boolean/list":
            return {"success": True, "result": [storage_record]}
        return {"success": True, "result": []}

    client = MagicMock()
    client.get_states = AsyncMock(return_value=[])
    client.send_websocket_message = AsyncMock(side_effect=_ws)
    client._request = AsyncMock(return_value=[])
    return client


@pytest.mark.asyncio
class TestRenameThroughDeepSearch:
    _REGISTRY = _registry(
        {
            "entity_id": "input_boolean.new_name",
            "platform": "input_boolean",
            "unique_id": "abc123",
            "name": "New Name",
            "original_name": "Old Name",
        }
    )

    async def test_prefetched_registry_surfaces_current_name(self) -> None:
        """A renamed helper is found by its current name when deep_search is
        handed the registry snapshot, with the current entity_id + name."""
        tools = _make_tools(_seam_client({"id": "abc123", "name": "Old Name"}))

        result = await tools.deep_search(
            query="New Name",
            search_types=["helper"],
            limit=10,
            prefetched_registry=self._REGISTRY,
        )

        helpers = result["helpers"]
        assert len(helpers) == 1, f"expected the renamed helper; got {helpers!r}"
        assert helpers[0]["entity_id"] == "input_boolean.new_name"
        assert helpers[0]["name"] == "New Name"

    async def test_without_registry_current_name_is_invisible(self) -> None:
        """The same current-name query with NO snapshot finds nothing — the bug
        the snapshot fixes (storage name is "Old Name", entity slug "abc123")."""
        tools = _make_tools(_seam_client({"id": "abc123", "name": "Old Name"}))

        result = await tools.deep_search(
            query="New Name", search_types=["helper"], limit=10
        )

        assert result["helpers"] == []
