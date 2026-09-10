"""Complete recreation keeps native submission knowledge and every snapshot field."""

import asyncio
import logging
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.config_entry_flow import CreationFlowError, create_flow_helper


def _form() -> dict[str, Any]:
    return {
        "type": "form",
        "flow_id": "create-flow",
        "step_id": "sensor",
        "data_schema": [
            {"name": "name", "required": True},
            {"name": "state", "required": True},
            {"name": "additional_options", "schema": [{"name": "availability"}]},
        ],
    }


def _config() -> dict[str, Any]:
    return {
        "name": "Example",
        "next_step_id": "sensor",
        "state": "{{ 12 }}",
        "additional_options": {"availability": "{{ false }}"},
    }


@pytest.fixture
def client() -> SimpleNamespace:
    return SimpleNamespace(
        start_config_flow=AsyncMock(
            return_value={
                "type": "menu",
                "flow_id": "create-flow",
                "menu_options": ["sensor"],
            }
        ),
        submit_config_flow_step=AsyncMock(),
        abort_config_flow=AsyncMock(),
    )


async def test_complete_recreation_submits_nested_options_unchanged(
    client: SimpleNamespace,
) -> None:
    client.submit_config_flow_step.side_effect = [
        _form(),
        {"type": "create_entry", "result": {"entry_id": "new-entry"}},
    ]
    result = await create_flow_helper(
        client, "template", _config(), complete_snapshot=True
    )
    assert result["entry_id"] == "new-entry"
    payload = deepcopy(_config())
    payload.pop("next_step_id")
    assert client.submit_config_flow_step.await_args_list[1].args == (
        "create-flow",
        payload,
    )
    client.abort_config_flow.assert_not_awaited()


@pytest.mark.parametrize(
    ("scenario", "status", "reason", "aborted"),
    [
        ("initial_create", "applied", "unsupported_form", False),
        ("initial_create_no_flow", "applied", "unsupported_form", False),
        ("menu_create", "applied", "unsupported_form", False),
        ("missing_flow", "not_applied", None, False),
        ("initial_unknown", "unknown", None, False),
        ("form_followup", "unknown", None, False),
        ("form_validation", "not_applied", "validation_failed", True),
        ("invalid_reply", "unknown", None, False),
        ("invalid_created_result", "applied", None, False),
    ],
)
async def test_complete_recreation_reports_native_outcomes_without_retry(
    client: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
    status: str,
    reason: str | None,
    aborted: bool,
) -> None:
    created = {
        "type": "create_entry",
        "flow_id": "create-flow",
        "result": {"entry_id": "new-entry"},
    }
    replies = [_form(), created]
    if scenario.startswith("initial_create"):
        client.start_config_flow.return_value = created
        if scenario.endswith("no_flow"):
            created.pop("flow_id")
    elif scenario == "menu_create":
        replies = [created]
    elif scenario == "missing_flow":
        client.start_config_flow.return_value = {"type": "form", "data_schema": []}
    elif scenario == "initial_unknown":
        client.start_config_flow.return_value = {
            "type": "private-step",
            "flow_id": "create-flow",
        }
    elif scenario == "form_followup":
        replies[1] = {"type": "menu", "menu_options": ["private-step"]}
    elif scenario == "form_validation":
        replies[1] = {
            **_form(),
            "errors": {
                "additional_options.availability": "private-value",
                "private_key.state": "private-value",
            },
        }
    elif scenario == "invalid_reply":
        replies[1] = None
    else:
        replies[1] = {"type": "create_entry", "result": []}
    client.submit_config_flow_step.side_effect = replies

    with caplog.at_level(logging.WARNING), pytest.raises(CreationFlowError) as caught:
        await create_flow_helper(client, "template", _config(), complete_snapshot=True)

    assert caught.value.apply_status == status
    assert caught.value.reason == reason
    assert "private" not in str(caught.value) + caplog.text
    assert client.abort_config_flow.await_count == int(aborted)
    assert client.submit_config_flow_step.await_count <= 2
    expected_identity = (
        "new-entry"
        if scenario in {"initial_create", "initial_create_no_flow", "menu_create"}
        else ""
    )
    assert caught.value.entry_id == expected_identity
    if scenario == "form_validation":
        assert caught.value.fields == ("additional_options.availability",)


@pytest.mark.parametrize("during_start", [False, True])
async def test_complete_recreation_propagates_cancellation_without_aborting(
    client: SimpleNamespace, during_start: bool
) -> None:
    if during_start:
        client.start_config_flow.side_effect = asyncio.CancelledError()
    else:
        client.submit_config_flow_step.side_effect = [_form(), asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await create_flow_helper(client, "template", _config(), complete_snapshot=True)
    client.abort_config_flow.assert_not_awaited()
