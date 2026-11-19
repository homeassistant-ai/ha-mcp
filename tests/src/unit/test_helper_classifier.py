"""The structured tool error follows Home Assistant's answer across wording changes."""

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandError
from ha_mcp.tools.helpers import exception_to_structured_error

from ._command_faults import pooled_socket


@pytest.mark.parametrize("entity_id", [None, "sensor.missing"])
async def test_missing_dashboard_preserves_native_code_and_context(
    hass: HomeAssistant, core_client: HomeAssistantClient, entity_id: str | None
) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    ws = await pooled_socket(core_client)
    with pytest.raises(HomeAssistantCommandError) as missing:
        await ws.send_command("lovelace/config", url_path="missing-dashboard")

    context = {"entity_id": entity_id} if entity_id else None
    result = exception_to_structured_error(
        missing.value, context=context, raise_error=False
    )

    assert result["ha_error_code"] == "config_not_found"
    assert result["error"]["code"] == "RESOURCE_NOT_FOUND"
    if entity_id:
        assert result["entity_id"] == entity_id
    assert "missing-dashboard" in str(result["error"])


@pytest.mark.parametrize(
    "detail",
    ["connection timeout", "not found", "unauthorized", "extra keys not allowed"],
)
async def test_a_service_refusal_is_not_reclassified_by_its_detail(
    hass: HomeAssistant, core_client: HomeAssistantClient, detail: str
) -> None:
    async def refuse(call: ServiceCall) -> None:
        raise HomeAssistantError(detail)

    hass.services.async_register("counter", "refuse", refuse)
    ws = await pooled_socket(core_client)
    with pytest.raises(HomeAssistantCommandError) as refused:
        await ws.send_command("call_service", domain="counter", service="refuse")
    result = exception_to_structured_error(refused.value, raise_error=False)

    assert result["error"]["code"] == "SERVICE_CALL_FAILED"
    assert result["ha_error_code"] == refused.value.code
    assert detail in result["error"]["message"]


def test_unclassified_exception_keeps_its_own_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    problem = RuntimeError("connection details not found")
    with caplog.at_level(logging.ERROR, logger="ha_mcp.tools.helpers"):
        result: dict[str, Any] = exception_to_structured_error(
            problem, raise_error=False
        )

    assert result["error"]["code"] == "INTERNAL_ERROR"
    records = [r for r in caplog.records if r.exc_info]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].exc_info[1] is problem
