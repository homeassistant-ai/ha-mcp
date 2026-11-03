"""ha_set_integration(reconfigure=True) across calls: one flow step per call."""

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.config_entry_flow_errors import ReconfigureStatus
from ha_mcp.tools.tools_integrations import IntegrationTools

from .test_integration_reconfigure import (
    _legacy_registry_reads,
    reconfig_entry,
    reconfigure_client,
)

# Fixtures, imported to apply here: the entry, and the registry doubles.
__all__ = ["_legacy_registry_reads", "reconfig_entry"]


def _form(step_id: str) -> dict[str, Any]:
    return {
        "flow_id": "flow-multi",
        "type": "form",
        "step_id": step_id,
        "data_schema": [{"name": step_id, "required": True}],
    }


@pytest.mark.asyncio
async def test_a_step_that_shows_another_form_hands_the_same_token_back(
    reconfig_entry: dict[str, object],
) -> None:
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(return_value=_form("host"))
    client._request = AsyncMock(return_value=_form("host"))
    client.submit_config_flow_step = AsyncMock(return_value=_form("port"))
    tools = IntegrationTools(client)

    preview = await tools.ha_set_integration(entry_id="entry-123", reconfigure=True)
    result = await tools.ha_set_integration(
        entry_id="entry-123",
        reconfigure=True,
        flow_id=preview["flow_id"],
        confirm_token=preview["confirm_token"],
        config={"host": "192.0.2.10"},
    )

    assert result["status"] == ReconfigureStatus.PREVIEW
    assert result["flow_id"] == "flow-multi"
    assert result["step_id"] == "port"
    assert result["confirm_token"] == preview["confirm_token"]
    client.submit_config_flow_step.assert_awaited_once_with(
        "flow-multi", {"host": "192.0.2.10"}
    )
    client.abort_config_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_abort_without_flow_id_is_refused_and_opens_nothing(
    reconfig_entry: dict[str, object],
) -> None:
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            entry_id="entry-123", reconfigure=True, abort=True
        )

    error = json.loads(str(exc_info.value))
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "flow_id" in error["error"]["message"]
    client.start_reconfigure_flow.assert_not_awaited()
    client.abort_config_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_flow_id_alone_reads_the_step_again_and_submits_nothing(
    reconfig_entry: dict[str, object],
) -> None:
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(return_value=_form("host"))
    client._request = AsyncMock(return_value=_form("host"))
    tools = IntegrationTools(client)

    preview = await tools.ha_set_integration(entry_id="entry-123", reconfigure=True)
    again = await tools.ha_set_integration(
        entry_id="entry-123",
        reconfigure=True,
        flow_id=preview["flow_id"],
        confirm_token=preview["confirm_token"],
    )

    assert again["status"] == ReconfigureStatus.PREVIEW
    assert again["step_id"] == "host"
    assert again["confirm_token"] == preview["confirm_token"]
    client._request.assert_awaited_once_with(
        "GET", "/config/config_entries/flow/flow-multi"
    )
    client.submit_config_flow_step.assert_not_called()


@pytest.mark.asyncio
async def test_menu_choice_with_config_is_refused_before_anything_is_read(
    reconfig_entry: dict[str, object],
) -> None:
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            flow_id="flow-multi",
            confirm_token="sha256:x",
            menu_choice="host",
            config={"host": "192.0.2.10"},
        )

    error = json.loads(str(exc_info.value))
    assert "not both" in error["error"]["message"]
    assert "still open" in error["error"]["suggestion"]
    client.submit_config_flow_step.assert_not_called()


@pytest.mark.asyncio
async def test_the_first_call_says_a_menu_choice_was_not_submitted(
    reconfig_entry: dict[str, object],
) -> None:
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(return_value=_form("host"))

    preview = await IntegrationTools(client).ha_set_integration(
        entry_id="entry-123", reconfigure=True, menu_choice="host"
    )

    assert any("were not submitted" in w for w in preview["warnings"])
    client.submit_config_flow_step.assert_not_called()


_SUCCESS_ABORT = {
    "flow_id": "flow-multi",
    "type": "abort",
    "reason": "reconfigure_successful",
}


@pytest.mark.asyncio
async def test_a_flow_that_finishes_when_it_is_opened_is_unverified(
    reconfig_entry: dict[str, object],
) -> None:
    """The first call submits nothing, but Core's first step may already have
    changed the entry: a success abort is not a preview."""
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(return_value=_SUCCESS_ABORT)

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            entry_id="entry-123", reconfigure=True
        )

    error = json.loads(str(exc_info.value))
    assert error["status"] == ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    assert error["flow_id"] == "flow-multi"
    assert "without a submit" in error["error"]["message"]
    client.submit_config_flow_step.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_again", "says"),
    [
        pytest.param(
            AsyncMock(return_value=_SUCCESS_ABORT), "without a submit", id="finished"
        ),
        # Core answers the GET by running the step again, which on a
        # reconfigure flow may write the entry: a 5xx there is classified like
        # a submit's.
        pytest.param(
            AsyncMock(side_effect=HomeAssistantAPIError("boom", status_code=500)),
            None,
            id="http-500",
        ),
    ],
)
async def test_reading_the_step_again_may_have_applied(
    reconfig_entry: dict[str, object], read_again: AsyncMock, says: str | None
) -> None:
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(return_value=_form("host"))
    client._request = read_again
    tools = IntegrationTools(client)

    preview = await tools.ha_set_integration(entry_id="entry-123", reconfigure=True)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_set_integration(
            entry_id="entry-123",
            reconfigure=True,
            flow_id=preview["flow_id"],
            confirm_token=preview["confirm_token"],
        )

    error = json.loads(str(exc_info.value))
    assert error["status"] == ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    if says:
        assert says in error["error"]["message"]
    client.submit_config_flow_step.assert_not_called()


@pytest.mark.asyncio
async def test_a_menu_is_answered_with_flow_id_token_and_menu_choice(
    reconfig_entry: dict[str, object],
) -> None:
    menu = {
        "flow_id": "flow-multi",
        "type": "menu",
        "step_id": "choose_transport",
        "menu_options": ["network", "cloud"],
    }
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)
    client.start_reconfigure_flow = AsyncMock(return_value=menu)
    client._request = AsyncMock(return_value=menu)
    client.submit_config_flow_step = AsyncMock(return_value=_form("network"))
    tools = IntegrationTools(client)

    preview = await tools.ha_set_integration(entry_id="entry-123", reconfigure=True)
    result = await tools.ha_set_integration(
        entry_id="entry-123",
        reconfigure=True,
        flow_id=preview["flow_id"],
        confirm_token=preview["confirm_token"],
        menu_choice="network",
    )

    assert preview["flow_type"] == "menu"
    client.submit_config_flow_step.assert_awaited_once_with(
        "flow-multi", {"next_step_id": "network"}
    )
    assert result["status"] == ReconfigureStatus.PREVIEW
    assert result["step_id"] == "network"
    assert result["confirm_token"] == preview["confirm_token"]


@pytest.mark.asyncio
async def test_describe_cannot_be_combined_with_reconfigure(
    reconfig_entry: dict[str, object],
) -> None:
    client = reconfigure_client()
    client.get_config_entry = AsyncMock(return_value=reconfig_entry)

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            entry_id="entry-123", reconfigure=True, describe=True
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "drop describe" in error["message"]
    client.start_reconfigure_flow.assert_not_awaited()
