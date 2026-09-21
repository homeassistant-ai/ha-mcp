"""Test the Home Assistant event surface of the approval queue.

Pins the payload an automation is expected to read, and the rule that a
failure to deliver it never turns a held tool call into a failed one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import anyio
import pytest

from ha_mcp.policy.approval_queue import PendingApproval
from ha_mcp.policy.events import (
    APPROVAL_REQUESTED_EVENT,
    build_requested_payload,
    emit_approval_requested,
)
from ha_mcp.policy.model import Predicate, Rule


def make_entry(tool_name: str = "ha_call_service") -> PendingApproval:
    now = datetime.now(UTC)
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
    assert "matched_rule" not in payload


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


@pytest.mark.anyio
async def test_emit_fires_the_event_on_the_client():
    client = AsyncMock()

    await emit_approval_requested(client, make_entry())

    client.fire_event.assert_awaited_once()
    event_type, payload = client.fire_event.await_args.args
    assert event_type == APPROVAL_REQUESTED_EVENT
    assert payload["token"] == "tok-1"


@pytest.mark.anyio
async def test_emit_swallows_a_delivery_failure_but_logs_it(caplog):
    """The call is already held; a missing notification must not fail it.

    Without the log line the user relying on this event as their only
    approval signal would see nothing at all — neither event nor reason.
    """
    client = AsyncMock()
    client.fire_event.side_effect = RuntimeError("HA unreachable")

    with caplog.at_level(logging.WARNING, logger="ha_mcp.policy.events"):
        await emit_approval_requested(client, make_entry())

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


def test_short_values_pass_through_unchanged():
    """Control: the cap must not rewrite ordinary arguments."""
    entry = make_entry()

    payload = build_requested_payload(entry)

    assert payload["args"] == {"domain": "lock", "service": "unlock"}


@pytest.mark.anyio
async def test_emit_gives_up_on_a_client_that_never_returns(caplog, monkeypatch):
    """The caller is already blocked; the notification must not eat its window."""
    monkeypatch.setattr("ha_mcp.policy.events.EMIT_TIMEOUT_SECONDS", 0.05)
    client = AsyncMock()

    async def never_returns(*_args, **_kwargs):
        await anyio.sleep(30)

    client.fire_event.side_effect = never_returns

    with caplog.at_level(logging.WARNING, logger="ha_mcp.policy.events"):
        with anyio.fail_after(5):
            await emit_approval_requested(client, make_entry())

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
