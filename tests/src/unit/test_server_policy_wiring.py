"""Tool security policies are wired into the real server (#966, #2502).

Each test builds ``HomeAssistantSmartMCPServer`` on the ``core_client``
fixture, so the gate announces on, and hears decisions from, a real Home
Assistant. The global settings carry placeholder credentials, as in OAuth
mode: whatever reaches Home Assistant does so as the server's own client.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_capture_events

from ha_mcp.__main__ import OAuthProxyClient
from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client.rest_client import HomeAssistantAuthError, HomeAssistantClient
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.policy.decision_pin import set_pin
from ha_mcp.policy.decisions import APPROVAL_RESPONSE_EVENT
from ha_mcp.policy.events import (
    APPROVAL_REQUESTED_EVENT,
    APPROVAL_RESULT_EVENT,
    emit_approval_result,
)
from ha_mcp.policy.middleware import PolicyMiddleware
from ha_mcp.policy.model import Policy, Rule
from ha_mcp.policy.persistence import POLICY_FILENAME, save_policy
from ha_mcp.policy.server_channels import approval_ws_client
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.tools.renamed_tool_middleware import RenamedToolAliasMiddleware
from ha_mcp.utils.data_paths import get_data_dir

GATED_TOOL = "ha_get_state"
ENTITY = "light.porch"
PIN = "4711-porch"

# A file the pre-#1993 editor wrote: one rule, its conditions AND-ed, no
# schema_version stamp.
UNMIGRATED_POLICY = {
    "rules": [
        {
            "tool_name": "ha_call_service",
            "when": [
                {"path": "args.domain", "op": "eq", "value": "lock"},
                {"path": "args.service", "op": "eq", "value": "unlock"},
            ],
        }
    ]
}


@pytest.fixture
async def policy_client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on a Home Assistant that, like every real one, has
    ``auth`` loaded, so the client can ask whether its token is an admin's."""
    assert await async_setup_component(hass, "auth", {})
    return core_client


def _build_server(
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    *,
    policies: bool,
) -> HomeAssistantSmartMCPServer:
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "oauth-mode-token")
    monkeypatch.setenv("ENABLE_TOOL_SECURITY_POLICIES", str(policies).lower())
    reset_global_settings()
    return HomeAssistantSmartMCPServer(client=core_client)


def _gate_the_tool(*, event_decisions: bool = False) -> None:
    data_dir = get_data_dir()
    save_policy(
        data_dir,
        Policy(
            wait_seconds=10,
            rules=[Rule(tool_name=GATED_TOOL)],
            event_decisions_enabled=event_decisions,
        ),
    )
    if event_decisions:
        set_pin(data_dir, PIN)


def _write_unmigrated_policy() -> None:
    (get_data_dir() / POLICY_FILENAME).write_text(json.dumps(UNMIGRATED_POLICY))


def _stored_policy() -> dict[str, Any]:
    stored: dict[str, Any] = json.loads(
        (get_data_dir() / POLICY_FILENAME).read_text(encoding="utf-8")
    )
    return stored


def _first(hass: HomeAssistant, event_type: str) -> asyncio.Future[Event]:
    """The next ``event_type`` event on Home Assistant's bus."""
    future: asyncio.Future[Event] = hass.loop.create_future()

    @callback
    def _seen(event: Event) -> None:
        if not future.done():
            future.set_result(event)

    hass.bus.async_listen_once(event_type, _seen)
    return future


async def _arrival(future: asyncio.Future[Event]) -> Event:
    return await asyncio.wait_for(future, 10)


def _policy_gates(server: HomeAssistantSmartMCPServer) -> list[PolicyMiddleware]:
    return [mw for mw in server.mcp.middleware if isinstance(mw, PolicyMiddleware)]


async def test_retired_tool_names_are_resolved_before_the_policy_gate(
    monkeypatch: pytest.MonkeyPatch, policy_client: HomeAssistantClient
) -> None:
    """Registration order is inbound order (first added = outermost).

    The gate resolves a retired name itself (``policy/test_middleware.py``);
    the alias still runs ahead of it, which keeps every other name-keyed
    middleware in the chain reading the current name.
    """
    server = _build_server(monkeypatch, policy_client, policies=True)
    chain = server.mcp.middleware
    alias = [
        i for i, mw in enumerate(chain) if isinstance(mw, RenamedToolAliasMiddleware)
    ]
    gate = [i for i, mw in enumerate(chain) if isinstance(mw, PolicyMiddleware)]
    assert alias, "the alias middleware is not registered at all"
    assert gate, "the policy gate is not wired at all"
    assert alias[0] < gate[0]


async def test_a_gated_call_is_announced_and_released_by_a_settings_ui_approval(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    policy_client: HomeAssistantClient,
) -> None:
    """The gate announces through the server's client and waits on the
    queue the settings UI approves on.

    Were the gate holding a queue of its own, ``/api/policy/approve`` would
    approve a request no call is waiting on; were it built without the
    server's client, a held call would be invisible outside the settings UI.
    """
    hass.states.async_set(ENTITY, "on")
    _gate_the_tool()
    server = _build_server(monkeypatch, policy_client, policies=True)
    assert len(_policy_gates(server)) == 1
    requested = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)
    first_request = _first(hass, APPROVAL_REQUESTED_EVENT)

    async with Client(server.mcp) as client:
        call = asyncio.create_task(
            client.call_tool(GATED_TOOL, {"entity_id": ENTITY}, raise_on_error=False)
        )
        announced = await _arrival(first_request)
        assert requested == [announced]
        assert announced.data["tool_name"] == GATED_TOOL
        assert not call.done()

        [pending] = server.approval_queue.list_pending()
        assert pending.token == announced.data["token"]
        server.approval_queue.approve(pending.token)
        result = await call

    assert not result.is_error
    assert ENTITY in result.content[0].text


async def test_with_policies_off_the_server_gates_nothing(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    policy_client: HomeAssistantClient,
) -> None:
    hass.states.async_set(ENTITY, "on")
    _gate_the_tool()
    server = _build_server(monkeypatch, policy_client, policies=False)
    assert _policy_gates(server) == []
    assert getattr(server, "approval_queue", None) is None
    requested = async_capture_events(hass, APPROVAL_REQUESTED_EVENT)

    async with Client(server.mcp) as client:
        result = await client.call_tool(GATED_TOOL, {"entity_id": ENTITY})

    assert ENTITY in result.content[0].text
    assert requested == []


async def test_an_old_policy_file_is_migrated_even_with_policies_off(
    monkeypatch: pytest.MonkeyPatch, policy_client: HomeAssistantClient
) -> None:
    """The file must already read as ANY-match whenever the user later turns
    the feature on, without another restart."""
    _write_unmigrated_policy()

    _build_server(monkeypatch, policy_client, policies=False)

    stored = _stored_policy()
    assert stored["schema_version"] is not None
    assert [len(rule["when"]) for rule in stored["rules"]] == [1, 1]


async def test_a_migration_that_cannot_save_does_not_stop_the_server(
    monkeypatch: pytest.MonkeyPatch, policy_client: HomeAssistantClient
) -> None:
    """A one-time data fixup must never take down every server boot."""
    _write_unmigrated_policy()

    with patch(
        "ha_mcp.policy.persistence.save_policy",
        side_effect=OSError(30, "Read-only file system"),
    ):
        server = _build_server(monkeypatch, policy_client, policies=False)

    assert server.mcp is not None
    assert "schema_version" not in _stored_policy()


async def test_a_decision_from_the_event_bus_releases_the_held_call(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    policy_client: HomeAssistantClient,
) -> None:
    """The response listener is wired in, shares the server's queue, and
    subscribes as the server's own client.

    In OAuth mode the global settings hold only a placeholder token; a
    subscription opened with them would authenticate as nobody, and the
    announcement would go out on REST while no answer could ever come back.
    Here the global settings point at nothing at all.
    """
    hass.states.async_set(ENTITY, "on")
    _gate_the_tool(event_decisions=True)
    server = _build_server(monkeypatch, policy_client, policies=True)
    first_request = _first(hass, APPROVAL_REQUESTED_EVENT)
    first_result = _first(hass, APPROVAL_RESULT_EVENT)

    async with Client(server.mcp) as client:
        call = asyncio.create_task(
            client.call_tool(GATED_TOOL, {"entity_id": ENTITY}, raise_on_error=False)
        )
        token = (await _arrival(first_request)).data["token"]
        hass.bus.async_fire(
            APPROVAL_RESPONSE_EVENT,
            {"token": token, "decision": "approve", "pin": PIN},
        )
        result = await call
        reported = await _arrival(first_result)

    assert not result.is_error
    assert ENTITY in result.content[0].text
    assert reported.data["token"] == token
    assert reported.data["applied"] is True


async def test_the_result_event_goes_out_on_a_client_it_builds_and_closes(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    policy_client: HomeAssistantClient,
) -> None:
    """One client per event, built as the server's client, and nothing else
    will ever reclaim it.

    The emitter runs in a bus handler rather than a request, so it cannot
    use the shared client in OAuth mode and builds its own from the same
    credentials. That client carries its own connection pool: left open,
    every decided approval adds one. It also starts from what the server's
    client already knows about the token's admin rights, rather than probing
    again or re-sending a refused request.
    """
    hass.states.async_set(ENTITY, "on")
    _gate_the_tool(event_decisions=True)
    server = _build_server(monkeypatch, policy_client, policies=True)
    first_request = _first(hass, APPROVAL_REQUESTED_EVENT)
    first_result = _first(hass, APPROVAL_RESULT_EVENT)

    async with Client(server.mcp) as client:
        call = asyncio.create_task(
            client.call_tool(GATED_TOOL, {"entity_id": ENTITY}, raise_on_error=False)
        )
        token = (await _arrival(first_request)).data["token"]
        # Announcing settled the admin question on the server's client.
        assert policy_client.known_is_admin is True
        with (
            patch.object(
                HomeAssistantClient,
                "__init__",
                autospec=True,
                wraps=HomeAssistantClient.__init__,
            ) as built,
            patch.object(
                HomeAssistantClient,
                "close",
                autospec=True,
                wraps=HomeAssistantClient.close,
            ) as closed,
        ):
            hass.bus.async_fire(
                APPROVAL_RESPONSE_EVENT,
                {"token": token, "decision": "approve", "pin": PIN},
            )
            await call
            await _arrival(first_result)

    [construction] = built.call_args_list
    result_client = construction.args[0]
    assert construction.args[1:3] == (policy_client.base_url, policy_client.token)
    assert construction.kwargs == {
        "verify_ssl": policy_client.verify_ssl,
        "is_admin": True,
        "admin_route_refused": False,
    }
    assert [c.args[0] for c in closed.await_args_list] == [result_client]


async def test_a_channel_without_oauth_credentials_is_refused_not_opened_on_the_pool(
    monkeypatch: pytest.MonkeyPatch, policy_client: HomeAssistantClient
) -> None:
    """OAuth mode with no request context must not read as "no credentials".

    ``OAuthProxyClient`` raises ``HomeAssistantAuthError`` there. Falling
    back to the pooled default connection would authenticate the response
    channel as the placeholder principal, the state this wiring exists to
    prevent; here the pooled connection would even succeed, because the
    global settings point at the real Home Assistant.
    """
    settings = get_global_settings()
    monkeypatch.setattr(settings, "homeassistant_url", policy_client.base_url)
    monkeypatch.setattr(settings, "homeassistant_token", policy_client.token)
    server = SimpleNamespace(client=OAuthProxyClient(policy_client.base_url))

    with pytest.raises(HomeAssistantAuthError):
        await approval_ws_client(server)


async def test_a_result_event_refused_for_a_non_admin_logs_one_line(
    hass: HomeAssistant,
    policy_client: HomeAssistantClient,
    hass_read_only_access_token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-admin token cannot fire events; that is a configuration fact,
    not a crash, so it is one warning and no traceback."""
    results = async_capture_events(hass, APPROVAL_RESULT_EVENT)
    async with HomeAssistantClient(
        policy_client.base_url, hass_read_only_access_token, verify_ssl=False
    ) as client:
        with caplog.at_level(logging.WARNING, logger="ha_mcp.policy.events"):
            await emit_approval_result(
                client, "tok-1", "approve", applied=True, reason="applied"
            )

    [record] = [r for r in caplog.records if r.name == "ha_mcp.policy.events"]
    assert "administrator's token" in record.getMessage()
    assert record.exc_info is None
    assert results == []
