"""Dashboard resource tools follow Core's lovelace contracts (issue #2632).

Core's ``lovelace/resources/{create,update,delete}`` answer structured codes:
``not_found`` for a missing id (helpers/collection.py) and ``unknown_command``
when resources are YAML-managed, because those commands are only registered in
storage resource mode (lovelace/__init__.py). The tools route on those codes.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp import backup_manager
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools.tools_resources import ResourceTools

from .test_backup_manager import _StubSettings


def _failure(code: str, message: str = "Command failed: whatever Core says") -> dict:
    """The envelope ``send_websocket_message`` returns for a rejected command."""
    return {"success": False, "error": message, "error_code": code}


def _tools(replies: dict[str, Any]) -> tuple[ResourceTools, list[dict]]:
    """ResourceTools whose client answers each WS command type from ``replies``."""
    sent: list[dict] = []

    async def _send(msg: dict[str, Any]) -> Any:
        sent.append(msg)
        return replies[msg["type"]]

    client = MagicMock()
    client.send_websocket_message = _send
    return ResourceTools(client), sent


def _error(exc_info: pytest.ExceptionInfo) -> dict:
    return json.loads(str(exc_info.value))


class TestYamlResourceMode:
    async def test_write_in_yaml_mode_says_resources_are_yaml_managed(self) -> None:
        tools, _ = _tools(
            {
                "lovelace/resources/create": _failure("unknown_command"),
                "lovelace/info": {"success": True, "result": {"resource_mode": "yaml"}},
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_dashboard_resource(url="/local/card.js")

        data = _error(exc_info)
        assert "YAML-managed" in data["error"]["message"]
        assert data["resource_mode"] == "yaml"

    async def test_unknown_command_in_storage_mode_passes_core_error_through(
        self,
    ) -> None:
        """The YAML explanation needs Core's own resource_mode, not a guess."""
        tools, _ = _tools(
            {
                "lovelace/resources/delete": _failure("unknown_command", "Core text"),
                "lovelace/info": {
                    "success": True,
                    "result": {"resource_mode": "storage"},
                },
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_delete_dashboard_resource(resource_id="abc")

        data = _error(exc_info)
        assert data["error"]["message"] == "Core text"
        assert data["ha_error_code"] == "unknown_command"

    async def test_list_reports_yaml_resource_mode_and_that_writes_are_unavailable(
        self,
    ):
        tools, _ = _tools(
            {
                "lovelace/resources": {"success": True, "result": []},
                "lovelace/info": {"success": True, "result": {"resource_mode": "yaml"}},
            }
        )

        result = await tools.ha_config_list_dashboard_resources()

        assert result["resource_mode"] == "yaml"
        assert "YAML-managed" in result["note"]


class TestCoreErrorCodes:
    async def test_update_of_missing_resource_reports_not_found(self) -> None:
        tools, _ = _tools({"lovelace/resources/update": _failure("not_found")})

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_dashboard_resource(
                url="/local/card.js", resource_id="gone"
            )

        data = _error(exc_info)
        assert data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert data["resource_id"] == "gone"

    async def test_not_found_wording_without_the_code_is_not_reclassified(self) -> None:
        tools, _ = _tools(
            {
                "lovelace/resources/delete": _failure(
                    "home_assistant_error", "Unable to find resource_id abc"
                )
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_delete_dashboard_resource(resource_id="abc")

        assert _error(exc_info)["error"]["code"] == "SERVICE_CALL_FAILED"

    async def test_create_rejection_keeps_cores_message_not_an_invented_duplicate(
        self,
    ):
        """Core has no duplicate-URL check; its own message must reach the caller."""
        tools, _ = _tools(
            {
                "lovelace/resources/create": _failure(
                    "invalid_format", "url already exists in some future check"
                )
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_dashboard_resource(url="/local/card.js")

        message = _error(exc_info)["error"]["message"]
        assert message == "url already exists in some future check"


class TestCoreResourceTypes:
    async def test_by_type_counts_every_type_core_returns(self) -> None:
        rows = [
            {"id": "1", "type": "html", "url": "/local/a.html"},
            {"id": "2", "type": "module", "url": "/local/b.js"},
        ]
        tools, _ = _tools(
            {
                "lovelace/resources": {"success": True, "result": rows},
                "lovelace/info": {"success": True, "result": {}},
            }
        )

        result = await tools.ha_config_list_dashboard_resources()

        assert result["by_type"] == {"html": 1, "module": 1}

    async def test_html_url_resource_reaches_core(self) -> None:
        tools, sent = _tools(
            {"lovelace/resources/create": {"success": True, "result": {"id": "h1"}}}
        )

        result = await tools.ha_config_set_dashboard_resource(
            url="/local/legacy.html", resource_type="html"
        )

        assert result["resource_id"] == "h1"
        assert sent[0]["res_type"] == "html"

    async def test_unknown_url_resource_type_is_left_to_core(self) -> None:
        """The tool keeps no copy of Core's type list; Core's rejection is relayed."""
        tools, sent = _tools(
            {
                "lovelace/resources/create": _failure(
                    "invalid_format",
                    "value must be one of ['css', 'html', 'js', 'module']",
                )
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_dashboard_resource(
                url="/local/card.wasm", resource_type="wasm"
            )

        assert sent[0]["res_type"] == "wasm"
        error = _error(exc_info)["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert (
            error["message"] == "value must be one of ['css', 'html', 'js', 'module']"
        )

    async def test_inline_html_is_rejected_before_any_write(self) -> None:
        tools, sent = _tools({})

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_dashboard_resource(
                content="<p>x</p>", resource_type="html"
            )

        assert "html" in _error(exc_info)["error"]["message"]
        assert sent == []


_SNAPSHOT = {"id": "old", "type": "css", "url": "/local/theme.css"}


class TestRestoreDashboardResource:
    @pytest.fixture
    def live(self) -> list[dict]:
        """The rows ``lovelace/resources`` lists; the snapshot's id is gone."""
        return []

    @pytest.fixture
    def sent(self, monkeypatch: pytest.MonkeyPatch, live: list[dict]) -> list[dict]:
        calls: list[dict] = []

        async def _fake_ws_send(client: Any, message: dict[str, Any]) -> Any:
            calls.append(message)
            if message["type"] == "lovelace/resources":
                return live
            if (
                message["type"] == "lovelace/resources/update"
                and message["resource_id"] == "old"
            ):
                raise HomeAssistantCommandError(
                    "Command failed: Unable to find resource_id old", "not_found"
                )
            return {"id": "new", "type": "css", "url": "/local/theme.css"}

        monkeypatch.setattr(backup_manager, "_ws_send", _fake_ws_send)
        return calls

    async def test_restore_of_deleted_resource_recreates_it(
        self, sent: list[dict]
    ) -> None:
        result = await backup_manager._restore_dashboard_resource(
            MagicMock(), "old", dict(_SNAPSHOT)
        )

        assert result["id"] == "new"
        assert [m["type"] for m in sent] == [
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
        self, sent: list[dict], live: list[dict]
    ) -> None:
        """An earlier restore re-created the resource under a new id."""
        live.append({"id": "new", "type": "css", "url": "/local/theme.css"})

        result = await backup_manager._restore_dashboard_resource(
            MagicMock(), "old", dict(_SNAPSHOT)
        )

        assert result["id"] == "new"
        assert [m["type"] for m in sent] == [
            "lovelace/resources/update",
            "lovelace/resources",
        ]

    async def test_restore_leaves_another_resource_with_the_same_url_alone(
        self, sent: list[dict], live: list[dict]
    ) -> None:
        """Core keeps no url uniqueness: a row of another type is not ours."""
        live.append({"id": "other", "type": "js", "url": "/local/theme.css"})

        await backup_manager._restore_dashboard_resource(
            MagicMock(), "old", dict(_SNAPSHOT)
        )

        assert sent[-1] == {
            "type": "lovelace/resources/create",
            "res_type": "css",
            "url": "/local/theme.css",
        }
        assert "other" not in [m.get("resource_id") for m in sent]

    async def test_restore_in_place_also_restores_the_resource_type(
        self, sent: list[dict]
    ) -> None:
        await backup_manager._restore_dashboard_resource(
            MagicMock(), "old", dict(_SNAPSHOT)
        )

        assert sent[0] == {
            "type": "lovelace/resources/update",
            "resource_id": "old",
            "res_type": "css",
            "url": "/local/theme.css",
        }

    async def test_restore_of_a_row_without_a_type_omits_the_type(
        self, sent: list[dict]
    ) -> None:
        """Core's import of a stored ``resources:`` block keeps untyped rows."""
        untyped = {"id": "old", "url": "/local/theme.css"}
        await backup_manager._restore_dashboard_resource(MagicMock(), "old", untyped)

        writes = [m for m in sent if "resources/" in m["type"]]
        assert len(writes) == 2
        assert [m for m in writes if "res_type" in m] == []

    async def test_restore_does_not_recreate_on_other_core_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []

        async def _fake_ws_send(client: Any, message: dict[str, Any]) -> Any:
            calls.append(message)
            raise HomeAssistantCommandError("Command failed: bad", "invalid_format")

        monkeypatch.setattr(backup_manager, "_ws_send", _fake_ws_send)

        with pytest.raises(HomeAssistantCommandError):
            await backup_manager._restore_dashboard_resource(
                MagicMock(), "old", dict(_SNAPSHOT)
            )

        assert [c["type"] for c in calls] == ["lovelace/resources/update"]


async def test_diff_after_a_recreating_restore_compares_against_the_new_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = [dict(_SNAPSHOT)]

    async def _fake_ws_send(client: Any, message: dict[str, Any]) -> Any:
        assert message == {"type": "lovelace/resources"}
        return rows

    monkeypatch.setattr(backup_manager, "_ws_send", _fake_ws_send)
    mgr = backup_manager.BackupManager(
        _StubSettings(auto_backup_dir=str(tmp_path)), MagicMock()
    )
    mgr.register(
        backup_manager.DomainHandler(
            "dashboard_resource",
            backup_manager._fetch_dashboard_resource,
            backup_manager._restore_dashboard_resource,
        )
    )
    path = await mgr.maybe_snapshot("dashboard_resource", "old")
    assert path is not None
    rows[:] = [
        {**_SNAPSHOT, "id": "new", "url": "/local/other.css"},
        {**_SNAPSHOT, "id": "same-url", "type": "js"},
        {**_SNAPSHOT, "id": "recreated"},
    ]

    diff = await mgr.diff_snapshot(path.name)

    assert diff["entity_missing"] is False
    assert diff["unchanged"] is True

    rows[2]["url"] = "/local/moved.css"
    rows[1]["url"] = "/local/elsewhere.css"

    assert (await mgr.diff_snapshot(path.name))["entity_missing"] is True


async def test_inline_error_does_not_suggest_a_type_core_rejects() -> None:
    tools, sent = _tools({})

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_dashboard_resource(
            content="var x = 1;", resource_type="modul"
        )

    assert not any("modul'" in s for s in _error(exc_info)["error"]["suggestions"])
    assert sent == []
