"""Unit tests for applying a label onto areas via ``ha_config_set_label``.

Issue #2455: agents can create a label and stick it on rooms in one call.
Assignment is additive (existing area labels are kept) — replace-the-set
lives on ``ha_set_area_or_floor(kind="area", labels=...)``.
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError


@pytest.fixture
def mock_client():
    return MagicMock()


@pytest.fixture
def register_tools(mock_client):
    from ha_mcp.tools.tools_labels import register_label_tools

    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mock_mcp = MagicMock()
    mock_mcp.add_tool = capture_add_tool
    register_label_tools(mock_mcp, mock_client)
    return registered


def _ws_handler(
    *,
    labels: list[dict[str, Any]] | None = None,
    areas: list[dict[str, Any]] | None = None,
):
    """Answer label + area registry list/create/update."""
    label_rows = labels if labels is not None else []
    area_rows = areas if areas is not None else []

    async def ws_handler(msg: dict) -> dict:
        msg_type = msg.get("type", "")
        if msg_type == "config/label_registry/list":
            return {"success": True, "result": label_rows}
        if msg_type == "config/label_registry/create":
            derived = msg.get("name", "").lower().replace(":", "_").replace(" ", "_")
            return {
                "success": True,
                "result": {
                    "label_id": derived,
                    **{k: v for k, v in msg.items() if k != "type"},
                },
            }
        if msg_type == "config/label_registry/update":
            return {
                "success": True,
                "result": {k: v for k, v in msg.items() if k != "type"},
            }
        if msg_type == "config/area_registry/list":
            return {"success": True, "result": area_rows}
        if msg_type == "config/area_registry/update":
            return {
                "success": True,
                "result": {k: v for k, v in msg.items() if k != "type"},
            }
        return {"success": True, "result": {}}

    return ws_handler


def _sent_types(mock_client) -> list[str]:
    return [
        call.args[0].get("type")
        for call in mock_client.send_websocket_message.call_args_list
    ]


def _area_updates(mock_client) -> list[dict]:
    return [
        call.args[0]
        for call in mock_client.send_websocket_message.call_args_list
        if call.args[0].get("type") == "config/area_registry/update"
    ]


class TestSetLabelAssignsAreas:
    async def test_create_with_areas_adds_label_and_keeps_existing(
        self, register_tools, mock_client
    ):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(
                areas=[
                    {
                        "area_id": "kitchen",
                        "name": "Kitchen",
                        "labels": ["existing"],
                    }
                ]
            )
        )

        result = await register_tools["ha_config_set_label"](
            name="Site Home", areas=["kitchen"]
        )

        assert result["success"] is True
        assert result["label_id"] == "site_home"
        assert result["assigned_areas"] == ["kitchen"]
        updates = _area_updates(mock_client)
        assert len(updates) == 1
        assert updates[0]["area_id"] == "kitchen"
        assert updates[0]["labels"] == ["existing", "site_home"]
        assert "config/label_registry/create" in _sent_types(mock_client)

    async def test_unknown_area_rejected_before_label_create(
        self, register_tools, mock_client
    ):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(areas=[{"area_id": "kitchen"}])
        )

        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](
                name="Site Home", areas=["ghost_room"]
            )

        err = json.loads(str(excinfo.value))
        assert err["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert err["unknown_area_ids"] == ["ghost_room"]
        assert err["areas"] == ["ghost_room"]
        # The shared single-area helper suggests area_id="" to clear; this tool
        # has no area_id parameter, so that advice must not leak in here.
        assert all("area_id=" not in s for s in err["error"].get("suggestions", [])), (
            err["error"].get("suggestions")
        )
        assert "config/label_registry/create" not in _sent_types(mock_client)

    async def test_all_unknown_areas_reported_at_once(
        self, register_tools, mock_client
    ):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(areas=[{"area_id": "kitchen"}])
        )

        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](
                name="Site Home", areas=["ghost_room", "kitchen", "phantom"]
            )

        err = json.loads(str(excinfo.value))
        assert err["unknown_area_ids"] == ["ghost_room", "phantom"]

    async def test_empty_area_id_rejected(self, register_tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(side_effect=_ws_handler())

        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](name="X", areas=[""])

        assert "VALIDATION_INVALID_PARAMETER" in str(excinfo.value)
        mock_client.send_websocket_message.assert_not_called()

    async def test_already_labeled_area_skips_update(self, register_tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(
                labels=[{"label_id": "site_home", "name": "Site Home"}],
                areas=[
                    {
                        "area_id": "kitchen",
                        "name": "Kitchen",
                        "labels": ["site_home"],
                    }
                ],
            )
        )

        result = await register_tools["ha_config_set_label"](
            name="Site Home", label_id="site_home", areas=["kitchen"]
        )

        assert result["success"] is True
        assert result["assigned_areas"] == ["kitchen"]
        assert _area_updates(mock_client) == []

    async def test_omitted_areas_does_not_touch_area_registry(
        self, register_tools, mock_client
    ):
        mock_client.send_websocket_message = AsyncMock(side_effect=_ws_handler())

        result = await register_tools["ha_config_set_label"](name="Site Home")

        assert result["success"] is True
        assert "assigned_areas" not in result
        assert all(
            t not in ("config/area_registry/list", "config/area_registry/update")
            for t in _sent_types(mock_client)
        )

    async def test_empty_areas_list_is_noop(self, register_tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(side_effect=_ws_handler())

        result = await register_tools["ha_config_set_label"](name="Site Home", areas=[])

        assert result["success"] is True
        assert result["assigned_areas"] == []
        assert "config/area_registry/list" not in _sent_types(mock_client)
        assert "config/area_registry/update" not in _sent_types(mock_client)

    async def test_two_areas_validated_from_one_list(self, register_tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(
                areas=[
                    {"area_id": "kitchen", "name": "Kitchen", "labels": []},
                    {"area_id": "living_room", "name": "Living", "labels": []},
                ]
            )
        )

        result = await register_tools["ha_config_set_label"](
            name="Site Home", areas=["kitchen", "living_room"]
        )

        assert result["success"] is True
        assert result["assigned_areas"] == ["kitchen", "living_room"]
        lists_before_create = []
        for call in mock_client.send_websocket_message.call_args_list:
            msg_type = call.args[0].get("type")
            if msg_type == "config/label_registry/create":
                break
            if msg_type == "config/area_registry/list":
                lists_before_create.append(msg_type)
        assert lists_before_create == ["config/area_registry/list"]

    async def test_partial_area_assign_reports_completed(
        self, register_tools, mock_client
    ):
        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/label_registry/list":
                return {"success": True, "result": []}
            if msg_type == "config/label_registry/create":
                return {
                    "success": True,
                    "result": {"label_id": "site_home", "name": "Site Home"},
                }
            if msg_type == "config/area_registry/list":
                return {
                    "success": True,
                    "result": [
                        {"area_id": "kitchen", "name": "Kitchen", "labels": []},
                        {"area_id": "living_room", "name": "Living", "labels": []},
                    ],
                }
            if msg_type == "config/area_registry/update":
                if msg.get("area_id") == "living_room":
                    return {"success": False, "error": "boom"}
                return {
                    "success": True,
                    "result": {k: v for k, v in msg.items() if k != "type"},
                }
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)

        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](
                name="Site Home", areas=["kitchen", "living_room"]
            )

        err = json.loads(str(excinfo.value))
        assert err["error"]["code"] == "SERVICE_CALL_FAILED"
        assert err["partial"] is True
        assert err["assigned_areas"] == ["kitchen"]
        assert err["label_id"] == "site_home"
        assert err["area_id"] == "living_room"
        updates = _area_updates(mock_client)
        assert [u["area_id"] for u in updates] == ["kitchen", "living_room"]

    async def test_transport_failure_after_first_area_is_partial(
        self, register_tools, mock_client
    ):
        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/label_registry/list":
                return {"success": True, "result": []}
            if msg_type == "config/label_registry/create":
                return {
                    "success": True,
                    "result": {"label_id": "site_home", "name": "Site Home"},
                }
            if msg_type == "config/area_registry/list":
                return {
                    "success": True,
                    "result": [
                        {"area_id": "kitchen", "name": "Kitchen", "labels": []},
                        {"area_id": "living_room", "name": "Living", "labels": []},
                    ],
                }
            if msg_type == "config/area_registry/update":
                if msg.get("area_id") == "living_room":
                    raise HomeAssistantConnectionError("ws dropped")
                return {
                    "success": True,
                    "result": {k: v for k, v in msg.items() if k != "type"},
                }
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)

        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](
                name="Site Home", areas=["kitchen", "living_room"]
            )

        err = json.loads(str(excinfo.value))
        # A dropped connection stays a connection error — collapsing it into
        # SERVICE_CALL_FAILED would hide the one recovery that helps.
        assert err["error"]["code"] == "CONNECTION_FAILED"
        assert err["partial"] is True
        assert err["assigned_areas"] == ["kitchen"]
        assert err["label_id"] == "site_home"
        assert err["area_id"] == "living_room"
        assert "ws dropped" in err["error"]["message"]
        assert any(
            "do not recreate the label" in s
            for s in err["error"].get("suggestions", [])
        ), err["error"].get("suggestions")

    async def test_timeout_during_assign_keeps_timeout_classification(
        self, register_tools, mock_client
    ):
        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/label_registry/list":
                return {"success": True, "result": []}
            if msg_type == "config/label_registry/create":
                return {
                    "success": True,
                    "result": {"label_id": "site_home", "name": "Site Home"},
                }
            if msg_type == "config/area_registry/list":
                return {
                    "success": True,
                    "result": [{"area_id": "kitchen", "name": "Kitchen", "labels": []}],
                }
            if msg_type == "config/area_registry/update":
                raise TimeoutError("area update timed out")
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)

        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](
                name="Site Home", areas=["kitchen"]
            )

        err = json.loads(str(excinfo.value))
        assert err["error"]["code"] == "TIMEOUT_OPERATION"
        assert err["partial"] is True
        assert err["assigned_areas"] == []

    async def test_reread_confirms_label_when_update_omits_labels(
        self, register_tools, mock_client
    ):
        """HA may ack the update without echoing labels — re-read decides."""
        stored: list[str] = []

        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/label_registry/list":
                return {"success": True, "result": []}
            if msg_type == "config/label_registry/create":
                return {
                    "success": True,
                    "result": {"label_id": "site_home", "name": "Site Home"},
                }
            if msg_type == "config/area_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "area_id": "kitchen",
                            "name": "Kitchen",
                            "labels": list(stored),
                        }
                    ],
                }
            if msg_type == "config/area_registry/update":
                stored.extend(msg.get("labels", []))
                # Success envelope with no labels echoed back.
                return {"success": True, "result": {"area_id": "kitchen"}}
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)

        result = await register_tools["ha_config_set_label"](
            name="Site Home", areas=["kitchen"]
        )

        assert result["success"] is True
        assert result["assigned_areas"] == ["kitchen"]
        types = _sent_types(mock_client)
        last_update = len(types) - 1 - types[::-1].index("config/area_registry/update")
        assert "config/area_registry/list" in types[last_update + 1 :]

    async def test_cancellation_during_assign_is_not_swallowed(
        self, register_tools, mock_client
    ):
        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/label_registry/list":
                return {"success": True, "result": []}
            if msg_type == "config/label_registry/create":
                return {
                    "success": True,
                    "result": {"label_id": "site_home", "name": "Site Home"},
                }
            if msg_type == "config/area_registry/list":
                return {
                    "success": True,
                    "result": [
                        {"area_id": "kitchen", "name": "Kitchen", "labels": []},
                    ],
                }
            if msg_type == "config/area_registry/update":
                raise asyncio.CancelledError
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)

        with pytest.raises(asyncio.CancelledError):
            await register_tools["ha_config_set_label"](
                name="Site Home", areas=["kitchen"]
            )

    async def test_create_without_label_id_rejects_area_assign(
        self, register_tools, mock_client
    ):
        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")
            if msg_type == "config/area_registry/list":
                return {
                    "success": True,
                    "result": [{"area_id": "kitchen", "name": "Kitchen"}],
                }
            if msg_type == "config/label_registry/create":
                return {"success": True, "result": {"name": "Site Home"}}
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=ws_handler)

        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](
                name="Site Home", areas=["kitchen"]
            )

        err = json.loads(str(excinfo.value))
        assert err["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "label_id" in err["error"]["message"]
        assert _area_updates(mock_client) == []

    async def test_each_area_is_listed_before_update(self, register_tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(
                areas=[
                    {"area_id": "kitchen", "name": "Kitchen", "labels": []},
                    {"area_id": "living_room", "name": "Living", "labels": []},
                ]
            )
        )

        await register_tools["ha_config_set_label"](
            name="Site Home", areas=["kitchen", "living_room"]
        )

        types = _sent_types(mock_client)
        first_update = types.index("config/area_registry/update")
        second_update = types.index("config/area_registry/update", first_update + 1)
        assert types[first_update - 1] == "config/area_registry/list"
        assert types[second_update - 1] == "config/area_registry/list"

    async def test_snapshots_target_areas_before_assign(
        self, register_tools, mock_client, monkeypatch
    ):
        snaps: list[tuple[str, str]] = []

        class FakeMgr:
            async def maybe_snapshot(self, domain, entity_id, **kwargs):
                snaps.append((domain, entity_id))

        monkeypatch.setattr(
            "ha_mcp.tools.tools_labels.get_backup_manager",
            lambda *_a, **_k: FakeMgr(),
        )
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_ws_handler(
                areas=[{"area_id": "kitchen", "name": "Kitchen", "labels": []}]
            )
        )

        await register_tools["ha_config_set_label"](name="Site Home", areas=["kitchen"])

        assert ("area_or_floor", "area:kitchen") in snaps
