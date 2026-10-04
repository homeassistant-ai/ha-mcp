"""Complete options restores preserve payload and submission knowledge."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.config_entry_flow import OptionsFlowError, update_config_entry_options
from ha_mcp.tools.config_entry_flow_form import _handle_form_step, _ReuseState


def _step() -> dict[str, Any]:
    return {
        "type": "form",
        "last_step": True,
        "flow_id": "restore-flow",
        "step_id": "button",
        "data_schema": [
            {"name": "press", "required": False, "selector": {"action": {}}},
            {
                "name": "additional_options",
                "type": "expandable",
                "required": False,
                "schema": [
                    {
                        "name": "availability",
                        "required": False,
                        "description": {"suggested_value": "{{ false }}"},
                        "selector": {"template": {}},
                    }
                ],
            },
        ],
    }


def _client(step: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        get_config_entry=AsyncMock(return_value={"domain": "template"}),
        start_options_flow=AsyncMock(return_value=_step() if step is None else step),
        submit_options_flow_step=AsyncMock(
            return_value={"type": "create_entry", "result": {}}
        ),
        abort_options_flow=AsyncMock(),
    )


async def _restore(client: Any, config: dict[str, Any]) -> dict[str, Any]:
    return await update_config_entry_options(
        client, "entry", config, expected_domain="template", keep_current_values=False
    )


async def test_explicit_empty_section_completes_without_false_key_error() -> None:
    client = _client()
    result = await _restore(client, {"additional_options": {}})
    assert result["success"] is True
    client.submit_options_flow_step.assert_awaited_once_with(
        "restore-flow", {"additional_options": {}}
    )
    client.abort_options_flow.assert_not_awaited()


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, {"additional_options": {"availability": "{{ false }}"}}),
        (
            {"additional_options": {}},
            {"additional_options": {"availability": "{{ false }}"}},
        ),
        ({"additional_options": {"availability": None}}, {}),
    ],
)
def test_ordinary_edit_section_backfill_and_clear_unchanged(values, expected) -> None:
    payload = _handle_form_step(
        "restore-flow",
        _step(),
        values,
        reuse_state=_ReuseState(),
        keep_current_values=True,
    )
    assert payload == expected


@pytest.mark.parametrize(
    "config",
    [
        {"unknown": 1},
        {"additional_options": {"typo": 1}},
        {"step_values": {"button": {"press": []}}},
        {"next_step_id": "button"},
    ],
)
async def test_unsupported_snapshot_fields_are_refused_before_submit(config) -> None:
    client = _client()
    with pytest.raises(Exception) as caught:
        await _restore(client, config)
    assert getattr(caught.value, "apply_status", None) == "not_applied"
    client.submit_options_flow_step.assert_not_awaited()
    client.abort_options_flow.assert_awaited_once_with("restore-flow")


@pytest.mark.parametrize(
    "step",
    [
        {"type": "menu", "flow_id": "restore-flow", "menu_options": ["button"]},
        {
            "type": "form",
            "last_step": True,
            "flow_id": "restore-flow",
            "step_id": "button",
        },
    ],
)
async def test_restore_requires_authoritative_single_form_schema(step) -> None:
    client = _client(step)
    with pytest.raises(Exception) as caught:
        await _restore(client, {"press": []})
    assert getattr(caught.value, "apply_status", None) == "not_applied"
    client.submit_options_flow_step.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        HomeAssistantConnectionError("lost reply"),
        TimeoutError("lost reply"),
        HomeAssistantAPIError("proxy timeout", status_code=504),
    ],
)
async def test_lost_submit_reply_preserves_unknown_and_does_not_abort(error) -> None:
    client = _client()
    client.submit_options_flow_step.side_effect = error
    with pytest.raises(Exception) as caught:
        await _restore(client, {"press": []})
    assert getattr(caught.value, "apply_status", None) == "unknown"
    assert getattr(caught.value, "entry_id", None) == "entry"
    assert getattr(caught.value, "flow_id", None) == "restore-flow"
    assert "may have been applied" in str(caught.value)
    client.abort_options_flow.assert_not_awaited()


async def test_acknowledged_completion_survives_local_response_error() -> None:
    client = _client()
    client.submit_options_flow_step.return_value = {
        "type": "create_entry",
        "result": [],
    }
    with pytest.raises(Exception) as caught:
        await _restore(client, {"press": []})
    assert getattr(caught.value, "apply_status", None) == "applied"
    assert "completed" in str(caught.value)
    client.abort_options_flow.assert_not_awaited()


@pytest.mark.parametrize(
    ("status_code", "apply_status", "error_type"),
    [
        (401, "not_applied", HomeAssistantAuthError),
        (403, "not_applied", HomeAssistantAPIError),
        (504, "unknown", HomeAssistantAPIError),
    ],
)
async def test_http_submit_failure_preserves_application_knowledge(
    status_code: int, apply_status: str, error_type: type[Exception]
) -> None:
    requests: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(status_code, json={"message": "Rejected"})

    async with httpx.AsyncClient(
        base_url="http://offline.invalid/api", transport=httpx.MockTransport(respond)
    ) as http_client:
        native = HomeAssistantClient.__new__(HomeAssistantClient)
        native.httpx_client = http_client
        native._is_admin = True
        native._admin_route_refused = False
        client = _client()
        client.submit_options_flow_step = AsyncMock(
            wraps=native.submit_options_flow_step
        )
        with pytest.raises(OptionsFlowError) as caught:
            await _restore(client, {"press": []})

    assert isinstance(caught.value.__cause__, error_type)
    assert caught.value.apply_status == apply_status
    assert caught.value.entry_id == "entry"
    assert caught.value.flow_id == "restore-flow"
    client.submit_options_flow_step.assert_awaited_once_with(
        "restore-flow", {"press": []}
    )
    assert requests == [
        ("POST", "/api/config/config_entries/options/flow/restore-flow")
    ]
    if apply_status == "not_applied":
        client.abort_options_flow.assert_awaited_once_with("restore-flow")
    else:
        client.abort_options_flow.assert_not_awaited()


@pytest.mark.parametrize(
    "reply",
    [
        {
            "type": "form",
            "last_step": True,
            "flow_id": "restore-flow",
            "data_schema": [],
        },
        {"type": "menu", "flow_id": "restore-flow", "menu_options": ["next"]},
    ],
)
async def test_unexpected_followup_does_not_submit_another_restore_step(reply) -> None:
    """A form after the one HA called the last, or any menu, is not filled."""
    client = _client({**_step(), "last_step": True})
    client.submit_options_flow_step.return_value = reply
    with pytest.raises(OptionsFlowError) as caught:
        await _restore(client, {"press": []})
    assert caught.value.apply_status == "not_applied"
    assert caught.value.reason == "unsupported_form"
    assert "authoritative options form" in str(caught.value)
    client.submit_options_flow_step.assert_awaited_once_with(
        "restore-flow", {"press": []}
    )
    client.abort_options_flow.assert_awaited_once_with("restore-flow")


def _two_forms() -> SimpleNamespace:
    """generic_thermostat's options: init, then presets (#2632)."""
    init = {
        "type": "form",
        "flow_id": "restore-flow",
        "step_id": "init",
        "data_schema": [{"name": "cold_tolerance", "required": False}],
        "last_step": False,
    }
    presets = {
        **init,
        "step_id": "presets",
        "data_schema": [{"name": "away_temp", "required": False}],
        "last_step": True,
    }
    client = _client(init)
    client.submit_options_flow_step.side_effect = [
        presets,
        {"type": "create_entry", "result": {}},
    ]
    return client


_SNAPSHOT = {"name": "Office", "cold_tolerance": 0.5, "away_temp": 16}


async def test_multi_form_restore_fills_each_form_from_the_snapshot() -> None:
    client = _two_forms()
    result = await update_config_entry_options(
        client,
        "entry",
        _SNAPSHOT,
        keep_current_values=False,
        fixed_options={**_SNAPSHOT, "away_temp": 20},
    )
    assert result["success"] is True
    assert [c.args[1] for c in client.submit_options_flow_step.await_args_list] == [
        {"cold_tolerance": 0.5},
        {"away_temp": 16},
    ]


async def test_option_no_form_offers_must_still_match_or_nothing_applies() -> None:
    """A creation-only option (here ``name``) that changed refuses the restore
    before the final form is submitted."""
    client = _two_forms()
    with pytest.raises(OptionsFlowError) as caught:
        await update_config_entry_options(
            client,
            "entry",
            _SNAPSHOT,
            keep_current_values=False,
            fixed_options={**_SNAPSHOT, "name": "Renamed"},
        )
    assert caught.value.reason == "identity_changed"
    assert caught.value.apply_status == "not_applied"
    assert caught.value.fields == ("name",)
    client.submit_options_flow_step.assert_awaited_once()
    client.abort_options_flow.assert_awaited_once_with("restore-flow")


async def test_native_form_validation_rejection_is_not_applied() -> None:
    client = _client()
    client.submit_options_flow_step.return_value = {
        **_step(),
        "errors": {"press": "invalid"},
    }
    with pytest.raises(Exception) as caught:
        await _restore(client, {"press": []})
    assert getattr(caught.value, "apply_status", None) == "not_applied"
    client.submit_options_flow_step.assert_awaited_once()
    client.abort_options_flow.assert_awaited_once_with("restore-flow")


async def test_cancellation_after_submit_is_propagated() -> None:
    submitted = asyncio.Event()

    async def submit(*args):
        submitted.set()
        await asyncio.Future()

    client = _client()
    client.submit_options_flow_step.side_effect = submit
    task = asyncio.create_task(_restore(client, {"press": []}))
    await asyncio.wait_for(submitted.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(task)
    client.abort_options_flow.assert_not_awaited()
