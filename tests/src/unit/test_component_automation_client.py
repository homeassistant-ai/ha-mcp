"""The server's client for ``ha_mcp_tools/automation_config``.

Every reply the server cannot use is a fallback to Core's REST/WebSocket API,
never an error; and the names it sends are the ones the component registers.
The client runs against a real ``hass`` with the component's commands, so the
replies are the component's, delivered by Core's WebSocket API.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.automation import config as automation_core_config
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools import automation_config
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_automation, component_config_reads

from ._component_core import ws_call, ws_result


@pytest.fixture
def invalidated(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A spy on the server's capability invalidation; the real one still runs."""
    spy = MagicMock(wraps=component_automation.invalidate_caps)
    monkeypatch.setattr(component_automation, "invalidate_caps", spy)
    return spy


class TestFallbacks:
    async def test_lost_answer_falls_back(
        self,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing was written, so a dropped socket is a fallback, not a
        failed write."""
        monkeypatch.setattr(
            component_core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket closed")),
        )
        assert (
            await component_automation.automation_validate(component_core_client, {})
            is None
        )

    async def test_failed_command_falls_back_and_keeps_the_capability(
        self, component_core_client: HomeAssistantClient, invalidated: MagicMock
    ) -> None:
        """Only ``unknown_command`` means the component lacks the command;
        dropping the cached capabilities on any failure would re-probe on
        every call."""
        # A lookup that names no automation: the component refuses it, and
        # Core answers with an error that is not unknown_command.
        assert (
            await component_automation.automation_lookup(component_core_client) is None
        )
        invalidated.assert_not_called()

    async def test_reply_that_is_not_an_object_falls_back(
        self,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A component of another release answering with a list.
        monkeypatch.setattr(
            automation_config, "async_automation_config", AsyncMock(return_value=["ok"])
        )
        assert (
            await component_automation.automation_validate(component_core_client, {})
            is None
        )

    async def test_lookup_without_a_found_verdict_falls_back(
        self,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Read as "not found", a reply without a boolean ``found`` would
        refuse a write to an automation that exists."""
        monkeypatch.setattr(
            automation_config,
            "async_automation_config",
            AsyncMock(return_value={"found": "yes"}),
        )
        got = await component_automation.automation_lookup(
            component_core_client, entity_id="automation.morning"
        )
        assert got is None

    async def test_unavailable_falls_back_and_logs_why(
        self,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A component that cannot reach part of Core silently lost every
        bridge answer; the log names the part it lacked."""
        # A Core build with neither automation validator.
        for name in ("_async_validate_config_item", "async_validate_config_item"):
            monkeypatch.setattr(automation_core_config, name, None)
        reason = "This Home Assistant has no automation validator"
        with caplog.at_level(logging.WARNING, logger=component_automation.__name__):
            got = await component_automation.automation_validate(
                component_core_client, {}
            )
        assert got is None
        assert any(
            r.levelno == logging.WARNING and reason in r.getMessage()
            for r in caplog.records
        )


class TestCrossSeam:
    async def test_server_gates_on_the_capability_the_component_advertises(
        self, hass: HomeAssistant, component_ws: MockHAClientWebSocket
    ) -> None:
        info: dict[str, Any] = await ws_result(component_ws, "ha_mcp_tools/info")
        assert component_automation.CAPABILITY in info["capabilities"]

    async def test_server_sends_the_command_the_component_registers(
        self, component_ws: MockHAClientWebSocket
    ) -> None:
        reply = await ws_call(
            component_ws,
            component_automation.WS_AUTOMATION_CONFIG,
            action="lookup",
            entity_id="automation.nothing",
        )
        assert reply["success"], reply

    async def test_server_asks_for_references_by_what_the_component_serves(
        self, component_ws: MockHAClientWebSocket
    ) -> None:
        """A renamed flag or command would leave every reference unchecked."""
        info: dict[str, Any] = await ws_result(component_ws, "ha_mcp_tools/info")
        assert component_config_reads.CONFIG_REFERENCES in info["capabilities"]
        reply = await ws_call(
            component_ws,
            component_config_reads.WS_CONFIG_REFERENCES,
            kind="automation",
            config={},
        )
        assert reply.get("error", {}).get("code") != "unknown_command", reply
