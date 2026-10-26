"""Real service errors retain their outcome through domain-specific consumers."""

import json
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
