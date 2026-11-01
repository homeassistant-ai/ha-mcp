"""The server's client for ``ha_mcp_tools/automation_config``.

Every reply the server cannot use is a fallback to Core's REST/WebSocket API,
never an error; and the names it sends are the ones the component registers.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools import component_automation, component_config_reads
from ha_mcp.tools.component_api import ComponentCaps

from .test_component_todo_items import _Client
from .test_component_ws_search import (
    functional_ws,  # noqa: F401 - pytest fixture
    wsapi,
)


@pytest.fixture
def caps(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    served = ComponentCaps(1, "9.9.9", frozenset({component_automation.CAPABILITY}), {})
    monkeypatch.setattr(
        component_automation, "get_component_caps", AsyncMock(return_value=served)
    )
    invalidate = MagicMock()
    monkeypatch.setattr(component_automation, "invalidate_caps", invalidate)
    return invalidate


class TestFallbacks:
    async def test_lost_answer_falls_back(self, caps: MagicMock) -> None:
        """Nothing was written, so a dropped socket is a fallback, not a
        failed write."""
        client = _Client(raise_=HomeAssistantConnectionError("socket closed"))
        assert await component_automation.automation_validate(client, {}) is None

    async def test_failed_command_falls_back_and_keeps_the_capability(
        self, caps: MagicMock
    ) -> None:
        """Only ``unknown_command`` means the component lacks the command;
        dropping the cached capabilities on any failure would re-probe on
        every call."""
        client = _Client(
            {"success": False, "error": "boom", "error_code": "home_assistant_error"}
        )
        assert await component_automation.automation_validate(client, {}) is None
        caps.assert_not_called()

    async def test_reply_that_is_not_an_object_falls_back(
        self, caps: MagicMock
    ) -> None:
        client = _Client({"success": True, "result": ["ok"]})
        assert await component_automation.automation_validate(client, {}) is None

    async def test_lookup_without_a_found_verdict_falls_back(
        self, caps: MagicMock
    ) -> None:
        """Read as "not found", a reply without a boolean ``found`` would
        refuse a write to an automation that exists."""
        client = _Client({"success": True, "result": {"found": "yes"}})
        got = await component_automation.automation_lookup(
            client, entity_id="automation.morning"
        )
        assert got is None

    async def test_unavailable_falls_back_and_logs_why(
        self, caps: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A component that cannot reach part of Core silently lost every
        bridge answer; the log names the part it lacked."""
        reason = "This Home Assistant has no automation validator"
        client = _Client(
            {"success": True, "result": {"status": "unavailable", "error": reason}}
        )
        with caplog.at_level(logging.WARNING, logger=component_automation.__name__):
            got = await component_automation.automation_validate(client, {})
        assert got is None
        assert any(
            r.levelno == logging.WARNING and reason in r.getMessage()
            for r in caplog.records
        )


class TestCrossSeam:
    def test_server_gates_on_the_capability_the_component_advertises(
        self,
    ) -> None:
        assert component_automation.CAPABILITY in wsapi.CAPABILITIES

    def test_server_sends_the_command_the_component_registers(
        self,
        functional_ws,  # noqa: F811
    ) -> None:
        assert component_automation.WS_AUTOMATION_CONFIG in functional_ws.registered

    def test_server_asks_for_targets_by_the_flag_the_component_advertises(
        self,
    ) -> None:
        """A renamed flag would leave every device, area, floor and label
        target unchecked without an error."""
        assert component_config_reads.REFERENCE_DATA_TARGETS in wsapi.CAPABILITIES
