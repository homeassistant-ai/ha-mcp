"""Options restore diagnostics preserve safe paths and identify unaborted flows."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.config_entry_flow import OptionsFlowError, update_config_entry_options


@pytest.fixture
def flow_client() -> SimpleNamespace:
    return SimpleNamespace(
        get_config_entry=AsyncMock(return_value={"domain": "template"}),
        start_options_flow=AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "restore-flow",
                "step_id": "sensor",
                "data_schema": [
                    {"name": "state", "required": True},
                    {"name": "additional_options", "schema": []},
                    {"name": "secret_marker_section", "schema": []},
                ],
            }
        ),
        submit_options_flow_step=AsyncMock(),
        abort_options_flow=AsyncMock(),
    )


@pytest.mark.parametrize("scenario", ["unsupported", "validation"])
async def test_nested_restore_fields_preserve_only_fully_safe_paths(
    flow_client: SimpleNamespace, caplog: pytest.LogCaptureFixture, scenario: str
) -> None:
    config = {"state": "secret-marker-template"}
    if scenario == "unsupported":
        config.update(
            additional_options={
                "availability": "secret-marker-template",
                "secret_marker_leaf": "secret-marker-value",
            },
            secret_marker_section={"availability": "secret-marker-template"},
        )
    else:
        flow_client.start_options_flow.return_value["errors"] = {
            "additional_options.availability": "secret-marker-error",
            "additional_options.secret_marker_leaf": "secret-marker-error",
            "secret_marker_section.availability": "secret-marker-error",
        }

    with caplog.at_level(logging.WARNING), pytest.raises(OptionsFlowError) as caught:
        await update_config_entry_options(
            flow_client,
            "entry",
            config,
            expected_domain="template",
            keep_current_values=False,
        )

    assert caught.value.fields == ("additional_options.availability",)
    assert "additional_options.availability" in str(caught.value)
    assert "secret_marker" not in str(caught.value) + caplog.text
    assert "secret-marker" not in str(caught.value) + caplog.text
    flow_client.submit_options_flow_step.assert_not_awaited()
    flow_client.abort_options_flow.assert_awaited_once_with("restore-flow")


async def test_restore_abort_cleanup_logs_safe_failure_context(
    flow_client: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    flow_client.abort_options_flow.side_effect = HomeAssistantConnectionError(
        "PRIVATE_TEST_PAYLOAD"
    )

    with caplog.at_level(logging.WARNING), pytest.raises(OptionsFlowError) as caught:
        await update_config_entry_options(
            flow_client,
            "entry",
            {"state": "secret-marker-template", "unit_of_measurement": "secret-marker"},
            expected_domain="template",
            keep_current_values=False,
        )

    assert caught.value.reason == "unsupported_fields"
    assert caught.value.apply_status == "not_applied"
    flow_client.submit_options_flow_step.assert_not_awaited()
    flow_client.abort_options_flow.assert_awaited_once_with("restore-flow")
    assert "PRIVATE_TEST_PAYLOAD" not in caplog.text
    assert "secret-marker" not in caplog.text
    assert "restore-flow" in caplog.text
    assert "entry" in caplog.text
    assert "stage=abort_cleanup" in caplog.text
    assert "reason=unsupported_fields" in caplog.text
    assert "error_type=HomeAssistantConnectionError" in caplog.text


@pytest.mark.parametrize(
    ("scenario", "apply_status", "error_type"),
    [
        ("connection", "unknown", OptionsFlowError),
        ("unknown_step", "unknown", OptionsFlowError),
        ("applied_response", "applied", OptionsFlowError),
        ("applied_initial", "applied", OptionsFlowError),
        ("cancelled", "unknown", asyncio.CancelledError),
    ],
)
async def test_unaborted_restore_logs_reconciliation_context_without_values(
    flow_client: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
    apply_status: str,
    error_type: type[BaseException],
) -> None:
    if scenario == "connection":
        flow_client.submit_options_flow_step.side_effect = HomeAssistantConnectionError(
            "secret-marker-transport"
        )
    elif scenario == "cancelled":
        flow_client.submit_options_flow_step.side_effect = asyncio.CancelledError(
            "secret-marker-cancellation"
        )
    elif scenario == "unknown_step":
        flow_client.submit_options_flow_step.return_value = {
            "type": "secret-marker-unknown-step",
        }
    elif scenario == "applied_response":
        flow_client.submit_options_flow_step.return_value = {
            "type": "create_entry",
            "result": [],
        }
    else:
        flow_client.start_options_flow.return_value = {
            "type": "create_entry",
            "flow_id": "restore-flow",
            "result": {"secret_marker_key": "secret-marker-value"},
        }

    with caplog.at_level(logging.WARNING), pytest.raises(error_type):
        await update_config_entry_options(
            flow_client,
            "entry",
            {"state": "secret-marker-template"},
            expected_domain="template",
            keep_current_values=False,
        )

    flow_client.abort_options_flow.assert_not_awaited()
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "restore-flow" in message
    assert "entry" in message
    assert f"apply_status={apply_status}" in message
    assert "not aborted" in message
    assert "reconcile" in message.lower()
    assert "secret_marker" not in caplog.text
    assert "secret-marker" not in caplog.text
