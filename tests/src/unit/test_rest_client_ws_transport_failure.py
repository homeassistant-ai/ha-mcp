"""``send_websocket_message`` on a real ``hass``: what raises, what returns.

No answer raises ``HomeAssistantConnectionError``, or
``HomeAssistantCommandNotSent`` when no connection carried the command; an
answer Home Assistant sent back, or a fault on this side, returns a failure
envelope (#1947). A command goes out once: Home Assistant may have run it, so
nothing that fails after it was sent is sent again. Core and its WebSocket API
are real; the connection is dropped, held or refused, or the token revoked.
How ``ha_eval_template`` reads a render's verdict is in
``test_tools_utility_eval_template.py``.
"""

from __future__ import annotations

import pytest
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError

from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)

from ._command_faults import dropped_on, pooled_socket, received, unanswered
from ._receipt_faults import revoke

_ENTRIES = {"type": "config_entries/get"}


async def test_a_connection_dropped_after_core_received_the_command_raises(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """It may have run, so the error does not say it was never sent."""
    socket = await pooled_socket(core_client)

    with (
        dropped_on(hass, "config_entries/get", socket),
        pytest.raises(HomeAssistantConnectionError) as exc,
    ):
        await core_client.send_websocket_message(_ENTRIES)

    assert not isinstance(exc.value, HomeAssistantCommandNotSent)


async def test_an_unanswered_command_raises_with_its_timeout(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An open socket that stopped answering leaves the caller as blind as a
    closed one."""
    async with unanswered(hass, "config_entries/get", monkeypatch):
        with pytest.raises(HomeAssistantConnectionError) as exc:
            await core_client.send_websocket_message(_ENTRIES)

    assert isinstance(exc.value.__cause__, HomeAssistantCommandTimeout)


async def test_a_refused_connection_sent_nothing(
    unreachable_client: HomeAssistantClient,
) -> None:
    with pytest.raises(HomeAssistantCommandNotSent):
        await unreachable_client.send_websocket_message(_ENTRIES)


# Revoking the token closes the server socket, leaving its heartbeat timer.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_refused_token_sent_nothing_and_keeps_its_cause(
    hass: HomeAssistant, core_client: HomeAssistantClient, hass_access_token: str
) -> None:
    revoke(hass, hass_access_token)

    with pytest.raises(HomeAssistantCommandNotSent) as exc:
        await core_client.send_websocket_message(_ENTRIES)

    assert isinstance(exc.value.__cause__, HomeAssistantAuthError)


async def test_cores_rejection_returns_an_envelope(
    core_client: HomeAssistantClient,
) -> None:
    result = await core_client.send_websocket_message({"type": "no_such/command"})

    assert result["success"] is False
    assert "Unknown command" in result["error"]
    assert result["error_code"] == "unknown_command"


async def test_a_fault_on_this_side_returns_an_envelope(
    core_client: HomeAssistantClient,
) -> None:
    """A message the client cannot encode never reaches Home Assistant."""
    result = await core_client.send_websocket_message(
        {"type": "config_entries/get", "domain": {"not", "json"}}
    )

    assert result["success"] is False
    assert "JSON serializable" in result["error"]


async def test_a_403_answer_is_returned_and_not_sent_again(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A service relaying an upstream 403 ran before it failed."""
    calls: list[ServiceCall] = []

    async def _relay(call: ServiceCall) -> ServiceResponse:
        calls.append(call)
        raise HomeAssistantError("upstream answered 403 Forbidden")

    hass.services.async_register(
        "counter", "relay", _relay, supports_response=SupportsResponse.OPTIONAL
    )

    result = await core_client.send_websocket_message(
        {"type": "call_service", "domain": "counter", "service": "relay"}
    )

    assert len(calls) == 1
    assert result["success"] is False
    assert "403 Forbidden" in result["error"]
    assert result["suggestions"]


async def test_a_render_sends_its_timeout_and_releases_its_subscription(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The template's timeout is a field of the command; the client's own wait
    is not sent. The subscription is released, or Core keeps re-rendering."""
    with received(hass, "render_template", "unsubscribe_events") as seen:
        result = await core_client.send_websocket_message(
            {"type": "render_template", "template": "{{ 1 + 1 }}", "timeout": 1}
        )

    assert (result["success"], result["result"]) == (True, 2)
    (sent,) = seen["render_template"]
    assert (sent["timeout"], sent["report_errors"]) == (1, True)
    assert "wait_timeout" not in sent
    (released,) = seen["unsubscribe_events"]
    assert released["subscription"] == sent["id"]


async def test_an_error_ends_the_render_with_the_class_named_message(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core reports the error bare and with its class; the class-named one is
    kept, and the subscription is released."""
    with received(hass, "unsubscribe_events") as seen:
        result = await core_client.send_websocket_message(
            {
                "type": "render_template",
                "template": "{{ d.split('-') }}",
                "variables": {"d": {}},
            }
        )

    assert result["success"] is False
    assert result["error"].startswith("UndefinedError: ")
    assert len(seen["unsubscribe_events"]) == 1


async def test_a_render_whose_connection_drops_raises(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    socket = await pooled_socket(core_client)

    with (
        dropped_on(hass, "render_template", socket),
        pytest.raises(HomeAssistantConnectionError),
    ):
        await core_client.send_websocket_message(
            {"type": "render_template", "template": "{{ 1 }}"}
        )
