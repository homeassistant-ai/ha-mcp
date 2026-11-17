"""Known secret values never leave the server in a tool's answer (#2157).

The server is the real one, built with ``REDACT_SECRETS`` set as an operator
sets it, and called over MCP as a client calls it. ``ha_get_state`` reads a
real ``hass`` through the server's own client; the secret is one the server
learned earlier (an app option or an options form it read).
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.redaction import REDACTED_KNOWN, register_known_secret_values

from ._real_server import build_server
from ._redaction_registry import clean_registry as clean_registry


async def _get_state(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    entity_id: str,
    *,
    redact: bool = True,
) -> tuple[str, Any]:
    """``ha_get_state`` over MCP: the text the client reads, and the
    structured content."""
    monkeypatch.setenv("REDACT_SECRETS", "true" if redact else "false")
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    server = build_server(monkeypatch, core_client)
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "ha_get_state", {"entity_id": entity_id}, raise_on_error=False
        )
    (block,) = result.content
    return block.text, result.structured_content


@pytest.mark.parametrize("redact", [True, False], ids=["on", "off"])
async def test_a_known_secret_in_a_state_is_shown_only_with_redaction_off(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    redact: bool,
) -> None:
    hass.states.async_set("sensor.vault", "on", {"token": "SECRETVALUE1", "ok": "x"})
    register_known_secret_values(["SECRETVALUE1"])

    text, structured = await _get_state(
        core_client, monkeypatch, "sensor.vault", redact=redact
    )

    shown = REDACTED_KNOWN if redact else "SECRETVALUE1"
    assert structured["data"]["attributes"] == {"token": shown, "ok": "x"}
    assert f'"attributes":{{"token":"{shown}","ok":"x"}}' in text


@pytest.mark.parametrize("redact", [True, False], ids=["on", "off"])
async def test_a_known_secret_in_a_tool_error_is_shown_only_with_redaction_off(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    redact: bool,
) -> None:
    register_known_secret_values(["secretvalue1"])

    text, _ = await _get_state(
        core_client, monkeypatch, "sensor.secretvalue1", redact=redact
    )

    shown = REDACTED_KNOWN if redact else "secretvalue1"
    assert f'"message": "Entity \'sensor.{shown}\' not found"' in text


async def test_a_secret_with_quotes_is_scrubbed_from_the_json_text_too(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The text block is the answer's JSON, where a quote or backslash in
    the secret is escaped."""
    secret = 'pass"word\\x'
    hass.states.async_set("sensor.vault", "on", {"token": secret})
    register_known_secret_values([secret])

    text, structured = await _get_state(core_client, monkeypatch, "sensor.vault")

    assert structured["data"]["attributes"] == {"token": REDACTED_KNOWN}
    assert f'"attributes":{{"token":"{REDACTED_KNOWN}"}}' in text


async def test_a_secret_inside_a_longer_one_leaves_no_fragment_in_the_answer(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass.states.async_set("sensor.vault", "on", {"token": "SECRET-xLONGER"})
    register_known_secret_values(["SECRET-x", "SECRET-xLONGER"])

    text, structured = await _get_state(core_client, monkeypatch, "sensor.vault")

    assert structured["data"]["attributes"] == {"token": REDACTED_KNOWN}
    assert f'"attributes":{{"token":"{REDACTED_KNOWN}"}}' in text
