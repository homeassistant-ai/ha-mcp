"""Test the Home Assistant event surface of the approval queue.

Pins the payload an automation is expected to read, and the rule that a
failure to deliver it never turns a held tool call into a failed one.

The events are fired through the server's own client into a real ``hass``,
whose bus is where an automation would read them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Event, HomeAssistant, callback

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.policy import events
from ha_mcp.policy.approval_queue import PendingApproval
from ha_mcp.policy.events import (
    APPROVAL_REQUESTED_EVENT,
    APPROVAL_RESULT_EVENT,
    build_requested_payload,
    build_result_payload,
    emit_approval_requested,
    emit_approval_result,
)
from ha_mcp.policy.model import Predicate, Rule

_CREATED = datetime(2026, 1, 1, 12, tzinfo=UTC)


def make_entry(tool_name: str = "ha_call_service") -> PendingApproval:
    now = _CREATED
    return PendingApproval(
        token="tok-1",
        tool_name=tool_name,
        args_hash="hash-1",
        args={"domain": "lock", "service": "unlock"},
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )


def test_payload_carries_token_tool_and_args():
    payload = build_requested_payload(make_entry())

    assert payload["token"] == "tok-1"
    assert payload["tool_name"] == "ha_call_service"
    assert payload["args"] == {"domain": "lock", "service": "unlock"}
    assert payload["created_at"] and payload["expires_at"]


def test_payload_carries_the_matched_rule_when_there_is_one():
    rule = Rule(
        tool_name="ha_call_service",
        when=[Predicate(path="args.domain", op="in", value=["lock"])],
    )

    payload = build_requested_payload(make_entry(), rule)

    assert payload["matched_rule"] == {
        "tool_name": "ha_call_service",
        "when": [{"path": "args.domain", "op": "in", "value": ["lock"]}],
    }


def _record(hass: HomeAssistant, event_type: str) -> list[Event]:
    """Every ``event_type`` event Core's bus delivers, as an automation sees it."""
    fired: list[Event] = []

    @callback
    def record(event: Event) -> None:
        fired.append(event)

    hass.bus.async_listen(event_type, record)
    return fired


async def test_emit_fires_the_event_on_home_assistants_bus(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    fired = _record(hass, APPROVAL_REQUESTED_EVENT)

    await emit_approval_requested(core_client, make_entry())
    await hass.async_block_till_done()

    (event,) = fired
    assert event.data["token"] == "tok-1"
    assert event.data["tool_name"] == "ha_call_service"
    assert event.data["args"] == {"domain": "lock", "service": "unlock"}


async def test_emit_swallows_a_delivery_failure_but_logs_it(
    core_client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The call is already held; a missing notification must not fail it.

    Without the log line the user relying on this event as their only
    approval signal would see nothing at all — neither event nor reason.
    """
    with (
        patch.object(
            core_client,
            "fire_event",
            AsyncMock(side_effect=HomeAssistantConnectionError("HA unreachable")),
        ),
        caplog.at_level(logging.WARNING, logger="ha_mcp.policy.events"),
    ):
        await emit_approval_requested(core_client, make_entry())

    assert "failed to fire" in caplog.text
    assert APPROVAL_REQUESTED_EVENT in caplog.text


def test_long_argument_values_are_shortened():
    """The bus reaches every listener; a file write must not be copied onto it."""
    entry = make_entry("ha_write_file")
    entry.args = {"path": "/config/x.yaml", "content": "y" * 5000}

    payload = build_requested_payload(entry)

    assert payload["args"]["path"] == "/config/x.yaml"
    content = payload["args"]["content"]
    assert len(content) < 700
    assert "more characters omitted" in content


def test_large_containers_are_replaced_by_a_marker():
    entry = make_entry("ha_config_set_automation")
    entry.args = {"config": {"triggers": ["t" * 100 for _ in range(50)]}}

    payload = build_requested_payload(entry)

    assert payload["args"]["config"].endswith("of dict omitted>")


@pytest.fixture
async def silent_client(socket_enabled: None) -> AsyncIterator[HomeAssistantClient]:
    """The server's client on a loopback listener that accepts and never answers."""
    connections: list[asyncio.StreamWriter] = []

    async def hold(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(writer)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = HomeAssistantClient(
        base_url=f"http://127.0.0.1:{port}", token="unused", verify_ssl=False
    )
    try:
        yield client
    finally:
        await client.close()
        for writer in connections:
            writer.close()
        server.close()
        await server.wait_closed()


async def test_emit_gives_up_on_a_home_assistant_that_never_answers(
    silent_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller is already blocked; the notification must not eat its window."""
    monkeypatch.setattr(events, "EMIT_TIMEOUT_SECONDS", 0.05)

    with caplog.at_level(logging.WARNING, logger="ha_mcp.policy.events"):
        async with asyncio.timeout(5):
            await emit_approval_requested(silent_client, make_entry())

    assert "timed out" in caplog.text


def test_matched_rule_is_absent_when_no_rule_matched():
    """The policy's fail-safes gate calls no rule matched — none to name."""
    payload = build_requested_payload(make_entry())

    assert "matched_rule" not in payload


def test_a_single_use_request_announces_no_expiry():
    """A dynamic-selector entry dies with its call, well before the TTL.

    Announcing ``expires_at`` there would promise minutes where there are
    seconds, which is exactly why the tool error on that path omits its
    countdown too.
    """
    payload = build_requested_payload(make_entry(), single_use=True)

    assert payload["single_use"] is True
    assert "expires_at" not in payload


def test_an_ordinary_request_announces_its_expiry():
    """Control: the TTL is real for every other request."""
    payload = build_requested_payload(make_entry())

    assert payload["expires_at"]
    assert "single_use" not in payload


def test_the_result_payload_never_carries_the_pin():
    """The two things this event must never grow, asserted two ways.

    The response event that triggers it carries the PIN, so the payload is
    one careless spread away from broadcasting the user's secret to every
    listener on the bus -- and to the frontend's event dev-tools, which is
    where they would first see it. The key set is the stronger half: a new
    field cannot be added without this failing, which is what catches the
    spread nobody meant to write. The value check covers the same field
    being filled from the wrong place.

    ``wrong_pin`` as a reason is not a leak and the test must not pretend
    it is: it says a PIN did not match, which is exactly what the
    responder needs and reveals nothing about either PIN.
    """
    secret = "8213"
    payload = build_result_payload(
        "tok-1",
        "approve",
        applied=False,
        reason="wrong_pin",
        tool_name="ha_call_service",
    )

    assert set(payload) == {"token", "decision", "applied", "reason", "tool_name"}
    flat = json.dumps(payload)
    assert secret not in flat
    assert all(
        key not in payload for key in ("pin", "hash", "digest", "salt", "iterations")
    )


def test_the_result_payload_omits_a_tool_it_cannot_name():
    """Absent rather than null or invented: there is no request to name."""
    payload = build_result_payload(
        "tok-1", "approve", applied=False, reason="unknown_token", tool_name=None
    )

    assert "tool_name" not in payload
    assert payload["applied"] is False


def test_the_result_payload_separates_what_was_asked_from_what_happened():
    """An applied approval is not a claim that the tool then succeeded."""
    payload = build_result_payload(
        "tok-1", "approve", applied=False, reason="expired", tool_name="ha_call_service"
    )

    assert payload["decision"] == "approve"
    assert payload["applied"] is False
    assert payload["reason"] == "expired"


async def test_emit_result_fires_the_event_on_home_assistants_bus(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    fired = _record(hass, APPROVAL_RESULT_EVENT)

    await emit_approval_result(
        core_client, "tok-1", "deny", applied=True, reason="applied"
    )
    await hass.async_block_till_done()

    (event,) = fired
    assert event.data["token"] == "tok-1"
    assert event.data["decision"] == "deny"
    assert event.data["applied"] is True


async def test_emit_result_swallows_a_delivery_failure(
    core_client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The decision has already happened; it cannot be undone from here."""
    with (
        patch.object(
            core_client,
            "fire_event",
            AsyncMock(side_effect=HomeAssistantConnectionError("HA unreachable")),
        ),
        caplog.at_level(logging.WARNING, logger="ha_mcp.policy.events"),
    ):
        await emit_approval_result(
            core_client, "tok-1", "approve", applied=True, reason="applied"
        )

    assert any(APPROVAL_RESULT_EVENT in r.getMessage() for r in caplog.records)
