"""Home Assistant's own flow strings, as the frontend reads them."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.flow_translations import fetch_flow_strings, flow_text

_ABORT = "component.sun.config.abort.single_instance_allowed"
_NOT_SUPPORTED = "component.demo.config.abort.not_supported"


_STRINGS = {
    _ABORT: "Only one.",
    "component.demo.options.error.cannot_connect": "Cannot reach {host}:{port}.",
    _NOT_SUPPORTED: "{model} is not supported by {integration}.",
}


@pytest.mark.parametrize(
    ("domain", "key", "placeholders", "expected"),
    [
        ("sun", "config.abort.single_instance_allowed", None, "Only one."),
        (
            "demo",
            "options.error.cannot_connect",
            {"host": "10.0.0.2", "port": "8080"},
            "Cannot reach 10.0.0.2:8080.",
        ),
        ("sun", "config.abort.unknown", None, None),
        ("demo", "config.abort.single_instance_allowed", None, None),  # other domain
        (
            "demo",
            "config.abort.not_supported",
            {"model": "X1"},
            "X1 is not supported by {integration}.",
        ),
    ],
)
def test_flow_text(
    domain: str, key: str, placeholders: dict[str, str] | None, expected: str | None
) -> None:
    assert flow_text(_STRINGS, domain, key, placeholders) == expected


@pytest.mark.asyncio
async def test_fetch_flow_strings_sends_one_english_request() -> None:
    client = AsyncMock()
    client.send_websocket_message.return_value = {
        "success": True,
        "result": {"resources": {_ABORT: "Already configured."}},
    }

    strings = await fetch_flow_strings(client, "sun", "config")

    assert strings == {_ABORT: "Already configured."}
    client.send_websocket_message.assert_awaited_once_with(
        {
            "type": "frontend/get_translations",
            "language": "en",
            "category": "config",
            "integration": ["sun"],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(RuntimeError("ws down"), id="call-fails"),
        pytest.param({"success": False, "error": {"code": "x"}}, id="core-error"),
    ],
)
async def test_fetch_flow_strings_is_none_when_unreadable(answer: Any) -> None:
    # None, not {}: a failed read must stay distinguishable from no strings.
    client = AsyncMock()
    if isinstance(answer, Exception):
        client.send_websocket_message.side_effect = answer
    else:
        client.send_websocket_message.return_value = answer

    assert await fetch_flow_strings(client, "sun", "config") is None


@pytest.mark.asyncio
async def test_fetch_flow_strings_is_empty_when_core_has_no_strings() -> None:
    client = AsyncMock()
    client.send_websocket_message.return_value = {
        "success": True,
        "result": {"resources": {}},
    }

    assert await fetch_flow_strings(client, "sun", "config") == {}


@pytest.mark.asyncio
async def test_same_named_fields_in_sections_keep_their_own_help() -> None:
    from ha_mcp.tools.config_helpers.describe import describe_form

    prefix = "component.demo.config.step.user."
    client = AsyncMock()
    client.send_websocket_message.return_value = {
        "success": True,
        "result": {
            "resources": {
                f"{prefix}data_description.port": "Top-level port",
                f"{prefix}sections.a.data_description.port": "Port in a",
                f"{prefix}sections.b.data_description.port": "Port in b",
            }
        },
    }
    port = {"name": "port", "type": "integer"}
    step = {
        "step_id": "user",
        "data_schema": [
            port,
            {"name": "a", "type": "expandable", "schema": [port]},
            {"name": "b", "type": "expandable", "schema": [port]},
        ],
    }

    fields = (await describe_form(client, "demo", "config", step))["fields"]

    assert fields[0]["description"] == "Top-level port"
    assert fields[1]["fields"][0]["description"] == "Port in a"
    assert fields[2]["fields"][0]["description"] == "Port in b"
