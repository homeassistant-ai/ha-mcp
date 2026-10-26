"""Real service errors retain their outcome through domain-specific consumers."""

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools
from ha_mcp.tools.tools_system import SystemTools
from ha_mcp.tools.tools_themes import ThemesTools
from ha_mcp.tools.tools_updates import UpdateTools


@pytest.mark.parametrize("status", [400, 500])
async def test_theme_service_error_keeps_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    effects: list[dict[str, str]] = []

    def post(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == (
            "POST",
            "/api/services/frontend/set_theme",
        )
        effects.append(json.loads(request.content))
        return httpx.Response(
            status, json={"message": "theme handler failed after effect"}
        )

    async with HomeAssistantClient(
        "http://theme.test", "test-token", verify_ssl=True, is_admin=True
    ) as client:
        await client.httpx_client.aclose()
        client.httpx_client = httpx.AsyncClient(
            base_url="http://theme.test/api", transport=httpx.MockTransport(post)
        )
        ws = AsyncMock(
            side_effect=AssertionError(
                "Failed writes must not replay or read back implicitly"
            )
        )
        monkeypatch.setattr(client, "send_websocket_message", ws)
        with pytest.raises(ToolError) as caught:
            await ThemesTools(client).ha_manage_theme(
                action="set", theme_name="owned-test", mode="dark"
            )
        ws.assert_not_awaited()
    result = json.loads(str(caught.value))
    assert effects == [{"name": "owned-test", "mode": "dark"}]
    assert result["write_outcome_unknown"] is True
    assert result["http_status"] == status
    assert "theme handler failed after effect" in result["error"]["details"]
    assert result["theme_name"] == "owned-test"
    assert result["mode"] == "dark"
    assert "before retrying" in result["error"]["suggestion"]
    assert any("action='list'" in s for s in result["error"]["suggestions"])


@pytest.mark.parametrize(
    "consumer", ["update", "enabled", "trigger", "scene", "reload"]
)
async def test_partial_service_consumers_keep_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
) -> None:
    effects: list[str] = []

    def post(request: httpx.Request) -> httpx.Response:
        effects.append(request.url.path)
        return httpx.Response(500, json={"message": "not found after effect"})

    async with HomeAssistantClient(
        "http://consumer.test", "test-token", is_admin=True
    ) as client:
        await client.httpx_client.aclose()
        client.httpx_client = httpx.AsyncClient(
            base_url="http://consumer.test/api", transport=httpx.MockTransport(post)
        )
        ws = AsyncMock(side_effect=AssertionError("must not replay"))
        monkeypatch.setattr(client, "send_websocket_message", ws)
        result = {"success": True, "action": "update", "config_hash": "saved"}
        if consumer == "update":
            rows, succeeded = await UpdateTools(client)._call_update_service(
                "install", ["update.test"], False
            )
            assert succeeded == 0
            error = rows[0]
        elif consumer == "reload":
            monkeypatch.setattr(
                "ha_mcp.tools.tools_system.RELOAD_TARGETS",
                {"test": ("automation", "reload")},
            )
            result = await SystemTools(client)._reload_all_targets()
            assert result["reloaded"] == []
            error = result["service_errors"][0]
        else:
            if consumer == "enabled":
                await AutomationConfigTools(client)._apply_enabled_state(
                    result, "automation.test", True, False
                )
                field = "enabled_applied"
            elif consumer == "trigger":
                await AutomationConfigTools(client)._run_automation_actions(
                    result, "automation.test"
                )
                field = "actions_triggered"
            else:
                await ConfigSceneTools(client)._activate_scene(
                    result, "scene.test", standalone=False
                )
                field = "activated"
            assert result["success"] is True
            assert result["config_hash"] == "saved"
            assert result[field] is None
            error = result["service_errors"][0]
        ws.assert_not_awaited()
    assert len(effects) == 1
    assert error["write_outcome_unknown"] is True
    assert error["http_status"] == 500
    assert "not found after effect" in error["error"]["details"]
    assert "before retrying" in error["error"]["suggestion"]


@pytest.mark.parametrize(
    "failure,http_status,native_code,diagnostic",
    [
        ("timeout", None, None, "reply lost after update effect"),
        ("auth", 401, None, "HTTP 401"),
        ("api", 500, None, "API error after update effect"),
        ("ws", None, "not_found", "WS error after update effect"),
    ],
)
async def test_update_batch_retains_prior_results_and_stops_after_unknown_effect(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    http_status: int | None,
    native_code: str | None,
    diagnostic: str,
) -> None:
    effects: list[str] = []
    targets = ["update.first", "update.uncertain", "update.unattempted"]

    def post(request: httpx.Request) -> httpx.Response:
        assert failure != "ws"
        effects.append(json.loads(request.content)["entity_id"])
        if len(effects) == 1:
            return httpx.Response(200, json=[])
        if failure == "timeout":
            raise httpx.ReadTimeout("reply lost after update effect")
        if failure == "api":
            return httpx.Response(
                500, json={"message": "API error after update effect"}
            )
        return httpx.Response(401, json={"message": "unauthorized after update effect"})

    async def websocket(message: dict[str, Any]) -> dict[str, Any]:
        assert failure == "ws"
        effects.append(message["service_data"]["entity_id"])
        if len(effects) == 1:
            return {"success": True, "result": {}}
        return {
            "success": False,
            "error": "WS error after update effect",
            "error_code": "not_found",
        }

    async with HomeAssistantClient(
        "http://update.test", "test-token", is_admin=failure != "ws"
    ) as client:
        await client.httpx_client.aclose()
        client.httpx_client = httpx.AsyncClient(
            base_url="http://update.test/api", transport=httpx.MockTransport(post)
        )
        ws = AsyncMock(side_effect=websocket)
        monkeypatch.setattr(client, "send_websocket_message", ws)
        tools = UpdateTools(client)
        monkeypatch.setattr(
            tools, "_resolve_update_targets", AsyncMock(return_value=targets)
        )
        monkeypatch.setattr(
            tools, "_verify_targets_exist", AsyncMock(return_value=(targets, []))
        )
        result = await tools._execute_batch_action("install", targets, None, False)
        if failure == "ws":
            assert ws.await_count == 2
        else:
            ws.assert_not_awaited()
    assert effects == targets[:2]
    assert result["success"] is False
    assert result["requested"] == 3
    assert result["succeeded"] == 1
    assert result["results"][0] == {"success": True, "entity_id": targets[0]}
    uncertain = result["results"][1]
    assert uncertain["entity_id"] == targets[1]
    assert uncertain["write_outcome_unknown"] is True
    assert uncertain.get("http_status") == http_status
    assert uncertain.get("ha_error_code") == native_code
    assert diagnostic in uncertain["error"]["details"]
    assert "before retrying" in uncertain["error"]["suggestion"]
    assert result["not_attempted"] == [targets[2]]
