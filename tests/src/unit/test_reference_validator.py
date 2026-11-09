"""Core's verdict on a config's references reaches the write response.

The component's ``config_references`` validates a proposed automation, script
or scene with Core, checks its references against Core's registries and puts
each entity target to Core's target lookups. These tests pin how the reference
validator reports that answer, and that it says so when nothing was checked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, component_config_reads
from ha_mcp.tools.reference_validator import validate_config_references

from ._component_routing_helpers import make_ws, patch_ws

_CONFIG = {
    "triggers": [{"trigger": "event", "event_type": "arrived"}],
    "actions": [
        {
            "action": "select.select_option",
            "target": {"entity_id": "input_select.mode_of_travel"},
            "data": {"option": "cycling"},
        }
    ],
}
_CAPS = {
    "schema_version": 1,
    "component_version": "9.1.0",
    "capabilities": ["config_references"],
    "limits": {},
}


def _client() -> Any:
    client = AsyncMock()
    client.base_url = "http://ha.local:8123"
    client.token = "tok"
    return client


async def _validate(ws: Any, client: Any | None = None) -> dict[str, Any]:
    with patch_ws(ws, component_config_reads):
        return await validate_config_references(
            client or _client(), _CONFIG, "automation"
        )


def _answer(**answer: Any) -> Any:
    return make_ws(
        "ha_mcp_tools/config_references", info_result=_CAPS, cmd_result=answer
    )


@pytest.mark.anyio
async def test_select_option_on_an_input_select_is_reported_with_cores_offer():
    """#2989: the write saved silently and the helper never changed. The
    warning carries what Core offers for the entity, the right action in it."""
    ws = _answer(
        status="ok",
        missing=[],
        mismatches=[
            {
                "path": "actions[0]",
                "kind": "services",
                "name": "select.select_option",
                "entity_id": "input_select.mode_of_travel",
                "offered": ["input_select.select_next", "input_select.select_option"],
            }
        ],
    )
    result = await _validate(ws)
    (warning,) = result["warnings"]
    assert warning["path"] == "actions[0]"
    assert warning["value"] == "input_select.mode_of_travel"
    assert warning["target_of"] == "select.select_option"
    assert "input_select.select_option" in warning["offered"]
    assert "references_unchecked" not in result
    (call,) = [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/config_references"
    ]
    assert call.kwargs == {"kind": "automation", "config": _CONFIG}


@pytest.mark.anyio
async def test_a_reference_core_does_not_have_is_reported():
    """#940: a hallucinated notify service was accepted silently."""
    ws = _answer(
        status="ok",
        missing=[{"kind": "service", "value": "notify.mobile_app_andrew_phone"}],
        mismatches=[],
    )
    result = await _validate(ws)
    assert [(w["kind"], w["value"]) for w in result["warnings"]] == [
        ("service", "notify.mobile_app_andrew_phone")
    ]


@pytest.mark.anyio
async def test_a_config_core_refuses_leaves_the_references_unchecked():
    ws = _answer(status="invalid", error="Invalid action")
    result = await _validate(ws)
    assert result == {"warnings": [], "references_unchecked": "Invalid action"}


@pytest.mark.anyio
async def test_without_the_component_the_references_are_reported_unchecked():
    """No server-side copy of Core's rules stands in for the component."""
    client = _client()
    client.base_url = None  # no credentials: no component to ask
    result = await _validate(make_ws("unused"), client)
    assert result["warnings"] == []
    assert "not installed" in result["references_unchecked"]


@pytest.mark.parametrize(
    "ws",
    [
        pytest.param(
            make_ws(
                "ha_mcp_tools/config_references",
                info_result=_CAPS | {"capabilities": []},
            ),
            id="capability-not-advertised",
        ),
        pytest.param(
            make_ws(
                "ha_mcp_tools/config_references",
                info_result=_CAPS,
                cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
            ),
            id="command-unknown",
        ),
    ],
)
@pytest.mark.anyio
async def test_an_outdated_component_is_told_apart_from_an_absent_one(ws):
    """An installed component from before the check was told it was missing."""
    client = _client()
    result = await _validate(ws, client)
    assert result["warnings"] == []
    assert "update it" in result["references_unchecked"]


@pytest.mark.anyio
async def test_an_unknown_command_forgets_the_cached_capabilities():
    client = _client()
    ws = make_ws(
        "ha_mcp_tools/config_references",
        info_result=_CAPS,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    await _validate(ws, client)
    assert client not in component_api._CAPS_CACHE


@pytest.mark.parametrize(
    "ws",
    [
        pytest.param(
            make_ws(
                "ha_mcp_tools/config_references",
                info_result=_CAPS,
                cmd_exc=HomeAssistantConnectionError("closed"),
            ),
            id="connection-lost",
        ),
        pytest.param(
            make_ws(
                "ha_mcp_tools/config_references",
                info_result=_CAPS,
                cmd_exc=HomeAssistantCommandTimeout("slow"),
            ),
            id="timeout",
        ),
        pytest.param(_answer(status="ok", missing=None, mismatches=[]), id="malformed"),
        pytest.param(
            _answer(
                status="ok",
                missing=[],
                mismatches=[
                    {
                        "path": "actions[0]",
                        "kind": "services",
                        "name": "x.y",
                        "entity_id": "a.b",
                        "offered": "x.z",
                    }
                ],
            ),
            id="malformed-value",
        ),
        pytest.param(_answer(status="invalid"), id="failed-without-error"),
    ],
)
@pytest.mark.anyio
async def test_a_failed_check_never_aborts_the_write_and_says_why(ws):
    """The check runs before the write; an error escaping it failed the write."""
    client = _client()
    result = await _validate(ws, client)
    assert result["warnings"] == []
    assert result["references_unchecked"]
    assert client in component_api._CAPS_CACHE
