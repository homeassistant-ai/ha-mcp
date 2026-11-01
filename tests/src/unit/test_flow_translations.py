"""Home Assistant's own flow strings, as the frontend reads them."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.flow_translations import fetch_flow_strings, flow_text

_ABORT = "component.sun.config.abort.single_instance_allowed"


def test_flow_text_returns_the_translated_string() -> None:
    strings = {_ABORT: "Already configured. Only a single configuration possible."}

    assert (
        flow_text(strings, "sun", "config.abort.single_instance_allowed", None)
        == "Already configured. Only a single configuration possible."
    )


def test_flow_text_fills_the_description_placeholders() -> None:
    key = "component.demo.options.error.cannot_connect"
    strings = {key: "Cannot reach {host} on port {port}."}

    assert (
        flow_text(
            strings,
            "demo",
            "options.error.cannot_connect",
            {"host": "10.0.0.2", "port": "8080"},
        )
        == "Cannot reach 10.0.0.2 on port 8080."
    )


def test_flow_text_is_none_for_a_key_core_does_not_ship() -> None:
    assert flow_text({_ABORT: "x"}, "sun", "config.abort.unknown", None) is None
    # The key is scoped to the domain it was asked for.
    assert (
        flow_text({_ABORT: "x"}, "demo", "config.abort.single_instance_allowed", None)
        is None
    )


def test_flow_text_leaves_a_placeholder_without_a_value_as_written() -> None:
    key = "component.demo.config.abort.not_supported"
    strings = {key: "{model} is not supported by {integration}."}

    assert (
        flow_text(strings, "demo", "config.abort.not_supported", {"model": "X1"})
        == "X1 is not supported by {integration}."
    )


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
async def test_fetch_flow_strings_is_empty_when_the_call_fails() -> None:
    client = AsyncMock()
    client.send_websocket_message.side_effect = RuntimeError("ws down")

    assert await fetch_flow_strings(client, "sun", "config") == {}
