"""Restore diagnostics identify refusals without echoing snapshot or HA values."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.config_entry_flow import OptionsFlowError, update_config_entry_options


@pytest.fixture
def flow_client():
    return SimpleNamespace(
        get_config_entry=AsyncMock(return_value={"domain": "template"}),
        start_options_flow=AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "restore-flow",
                "step_id": "sensor",
                "data_schema": [{"name": "state", "required": True}],
            }
        ),
        submit_options_flow_step=AsyncMock(),
        abort_options_flow=AsyncMock(),
    )


@pytest.mark.parametrize(
    ("scenario", "reason", "fields", "submitted"),
    [
        ("unsupported_fields", "unsupported_fields", ("unit_of_measurement",), False),
        ("unsupported_form", "unsupported_form", (), False),
        ("initial_validation", "validation_failed", ("state",), False),
        ("submit_validation", "validation_failed", ("state",), True),
        ("abort", "flow_aborted", (), True),
        ("connection", None, (), True),
    ],
)
async def test_restore_reason_never_echoes_values_or_unknown_keys(
    flow_client, scenario, reason, fields, submitted
):
    config = {"state": "secret-marker-template"}
    if scenario == "unsupported_fields":
        config.update(
            unit_of_measurement="secret-marker-unit",
            secret_marker_key="secret-marker-value",
        )
    elif scenario == "unsupported_form":
        flow_client.start_options_flow.return_value = {
            "type": "menu",
            "flow_id": "restore-flow",
            "menu_options": ["secret-marker-menu"],
        }
    elif scenario in {"initial_validation", "submit_validation"}:
        reply = deepcopy(flow_client.start_options_flow.return_value)
        reply["errors"] = {
            "state": "secret-marker-error",
            "secret_marker_key": "secret-marker-value",
        }
        if scenario == "initial_validation":
            flow_client.start_options_flow.return_value = reply
        else:
            flow_client.submit_options_flow_step.return_value = reply
    elif scenario == "abort":
        flow_client.submit_options_flow_step.return_value = {
            "type": "abort",
            "reason": "secret-marker-abort",
        }
    else:
        flow_client.submit_options_flow_step.side_effect = HomeAssistantConnectionError(
            "secret-marker-transport"
        )

    with pytest.raises(OptionsFlowError) as caught:
        await update_config_entry_options(
            flow_client,
            "entry",
            config,
            expected_domain="template",
            keep_current_values=False,
        )

    error = caught.value
    assert error.reason == reason
    assert error.fields == fields
    assert "secret-marker" not in str(error)
    assert "secret_marker_key" not in str(error)
    assert error.apply_status == (
        "unknown" if scenario == "connection" else "not_applied"
    )
    assert flow_client.submit_options_flow_step.await_count == int(submitted)
    if scenario == "connection":
        flow_client.abort_options_flow.assert_not_awaited()
    else:
        flow_client.abort_options_flow.assert_awaited_once_with("restore-flow")
