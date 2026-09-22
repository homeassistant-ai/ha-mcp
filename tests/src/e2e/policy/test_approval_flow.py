"""Real e2e test for the tool security policies middleware (#966).

Drives the FULL block → approve → re-call loop against a live
testcontainer HA + ha-mcp server with ``ENABLE_TOOL_SECURITY_POLICIES=true``,
using a function-scoped policy-enabled server fixture distinct from the
session-scoped ``mcp_client`` (which boots without policies).

The /api/policy/* HTTP routes are mounted on the same FastMCP Starlette
app as the MCP endpoint; the in-memory ``mcp_client`` transport bypasses
that app, so we drive the policy handlers (returned by
``build_policy_handlers``) via the same async ``Request`` -> ``JSONResponse``
contract the HTTP routes use. This still exercises the production handler
factory + ``ApprovalQueue`` + persistence path end-to-end — only the
Starlette routing layer is short-circuited. The MCP transport / tool
dispatch / middleware pipeline are exercised exactly as a real client
would see them.

Requires Docker (testcontainers); runs in CI.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_constants import TEST_TOKEN

from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.policy.handlers import build_policy_handlers
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.utils.data_paths import get_data_dir

from ..utilities.assertions import parse_mcp_result, tool_error_to_result
from ..utilities.wait_helpers import wait_for_ha_event


async def _expect_blocked(client: Client, args: dict[str, Any]) -> dict[str, Any]:
    """Call ``ha_call_service`` and return the parsed USER_APPROVAL_REQUIRED body.

    FastMCP clients normalize middleware-raised ``ToolError`` to either
    a raised ``ToolError`` (older transport behavior) or a result with
    ``isError=True`` carrying the JSON body in ``content[0].text`` (newer
    transport). Accept both so the test isn't pinned to a specific
    FastMCP version.
    """
    try:
        result = await client.call_tool("ha_call_service", args)
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    assert body.get("error", {}).get("code") == "USER_APPROVAL_REQUIRED", body
    return body


def _make_request(body: dict[str, Any] | None = None) -> MagicMock:
    """Build a minimal Starlette ``Request`` mock for direct handler calls.

    The /api/policy/* handlers only need ``await request.json()``; mock just
    that surface rather than wiring a full ASGI scope.
    """
    request = MagicMock()
    request.json = AsyncMock(return_value=body or {})
    return request


@pytest.fixture
async def policy_enabled_mcp(ha_container_with_fresh_config, monkeypatch, tmp_path):
    """Spin up a fresh policy-enabled MCP server bound to the testcontainer HA.

    Function-scoped so each test gets a clean ``ApprovalQueue`` and an
    isolated ``tool_policy.json`` (no cross-test bleed via the lru-cached
    ``get_data_dir``). The session-scoped ``mcp_server`` / ``mcp_client``
    fixtures boot without ``ENABLE_TOOL_SECURITY_POLICIES`` so they can't
    be reused here.

    Yields ``(client, server, policy_handlers)``:
      * ``client`` — in-memory ``fastmcp.Client`` bound to the policy-enabled MCP
      * ``server`` — the underlying ``HomeAssistantSmartMCPServer`` (exposes
        ``approval_queue``)
      * ``policy_handlers`` — dict of policy_get_config / policy_put_config /
        policy_post_approve / etc. closures, equivalent to what the HTTP
        routes mount.
    """
    container_info = ha_container_with_fresh_config
    if container_info.get("backend") == "haos_inaddon":
        pytest.skip(
            "Inaddon backend uses the addon's own MCP endpoint; this test "
            "needs an in-process server with ENABLE_TOOL_SECURITY_POLICIES=true."
        )

    monkeypatch.setenv("ENABLE_TOOL_SECURITY_POLICIES", "true")
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()

    # Reset cached settings so the new server picks up the env var.
    import ha_mcp.config

    monkeypatch.setattr(ha_mcp.config, "_settings", None)

    base_url = container_info["base_url"]
    token = container_info.get("token", TEST_TOKEN)
    ha_client = HomeAssistantClient(base_url=base_url, token=token)

    server = HomeAssistantSmartMCPServer(client=ha_client)
    assert getattr(server, "approval_queue", None) is not None, (
        "ENABLE_TOOL_SECURITY_POLICIES=true did not register an ApprovalQueue; "
        "verify _apply_tool_security_policies ran successfully."
    )

    handlers = build_policy_handlers(
        data_dir=tmp_path,
        queue=server.approval_queue,
    )

    client = Client(server.mcp)
    async with client:
        yield client, server, handlers

    await ha_client.close()
    get_data_dir.cache_clear()


@pytest.mark.asyncio
async def test_blocked_call_then_approve_then_recall(policy_enabled_mcp):
    """Block, approve, re-call succeeds; mutated args re-block (#966).

    Exercises the full middleware loop through the real MCP transport:
    1. ``PUT /api/policy/config`` enables a rule on ``ha_call_service``
       when ``args.domain == "light"``.
    2. First ``ha_call_service`` raises ``ToolError`` carrying
       ``USER_APPROVAL_REQUIRED`` + an approval token.
    3. ``POST /api/policy/approve`` consumes the token.
    4. Re-call with SAME args succeeds (queue lookup hits approved entry).
    5. Re-call with DIFFERENT args re-blocks (strict args-hash binding;
       approval does not blanket-permit future calls).
    """
    client, server, handlers = policy_enabled_mcp

    # 1. Install a rule that gates light service calls.
    current_resp = await handlers["policy_get_config"](_make_request())
    current = json.loads(current_resp.body)
    new_policy = {
        "wait_seconds": 5,
        "approval_ttl_minutes": 5,
        "rules": [
            {
                "tool_name": "ha_call_service",
                "when": [{"path": "args.domain", "op": "eq", "value": "light"}],
                "remember_minutes": 0,
            }
        ],
        "version": current["version"],
    }
    put_resp = await handlers["policy_put_config"](_make_request(new_policy))
    assert put_resp.status_code == 200, put_resp.body

    # 2. First call: middleware gates → USER_APPROVAL_REQUIRED.
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.bed_light"}
    await _expect_blocked(client, args)
    pending = server.approval_queue.list_pending()
    assert len(pending) == 1, f"expected exactly one pending entry, got {pending!r}"
    token = pending[0].token

    # 3. Approve via the same handler the HTTP route would call.
    approve_resp = await handlers["policy_post_approve"](
        _make_request({"token": token})
    )
    assert approve_resp.status_code == 200, approve_resp.body

    # 4. Re-call with SAME args: middleware sees approved entry → proceeds.
    result = await client.call_tool("ha_call_service", args)
    assert not result.is_error, result

    # 5. Re-call with DIFFERENT args: strict args-hash binding → new gate.
    other_args = {
        "domain": "light",
        "service": "turn_off",
        "entity_id": "light.bed_light",
    }
    await _expect_blocked(client, other_args)


async def _install_rule(handlers, rule: dict[str, Any]) -> None:
    current_resp = await handlers["policy_get_config"](_make_request())
    current = json.loads(current_resp.body)
    body = {
        "wait_seconds": 5,
        "approval_ttl_minutes": 5,
        "rules": [rule],
        "version": current["version"],
    }
    put_resp = await handlers["policy_put_config"](_make_request(body))
    assert put_resp.status_code == 200, put_resp.body


async def _install_rules(handlers, rules: list[dict[str, Any]]) -> None:
    current_resp = await handlers["policy_get_config"](_make_request())
    current = json.loads(current_resp.body)
    body = {
        "wait_seconds": 5,
        "approval_ttl_minutes": 5,
        "rules": rules,
        "version": current["version"],
    }
    put_resp = await handlers["policy_put_config"](_make_request(body))
    assert put_resp.status_code == 200, put_resp.body


@pytest.mark.asyncio
async def test_any_of_multiple_conditions_gates(policy_enabled_mcp):
    """Each policy condition is its own rule; a call matching ANY of them is
    gated (OR across same-tool rules) — end-to-end proof of the ALL->ANY editor
    change (PR #1993). A call matching the SECOND condition still blocks."""
    client, server, handlers = policy_enabled_mcp
    await _install_rules(
        handlers,
        [
            {
                "tool_name": "ha_call_service",
                "when": [{"path": "args.domain", "op": "eq", "value": "lock"}],
                "remember_minutes": 0,
            },
            {
                "tool_name": "ha_call_service",
                "when": [
                    {"path": "args.domain", "op": "eq", "value": "alarm_control_panel"}
                ],
                "remember_minutes": 0,
            },
        ],
    )
    # Matches the SECOND condition only → still gated (OR, not AND).
    await _expect_blocked(
        client,
        {
            "domain": "alarm_control_panel",
            "service": "alarm_disarm",
            "entity_id": "alarm_control_panel.home",
        },
    )
    assert server.approval_queue.list_pending()


@pytest.mark.asyncio
async def test_wildcard_path_gates_when_any_arg_matches(policy_enabled_mcp):
    """`args.*` fans out: blocks when ANY arg equals the gated value."""
    client, server, handlers = policy_enabled_mcp
    await _install_rule(
        handlers,
        {
            "tool_name": "ha_call_service",
            "when": [{"path": "args.*", "op": "eq", "value": "light"}],
            "remember_minutes": 0,
        },
    )
    # domain="light" → matches via wildcard
    await _expect_blocked(
        client,
        {"domain": "light", "service": "turn_on", "entity_id": "light.bed_light"},
    )
    assert server.approval_queue.list_pending()


@pytest.mark.asyncio
async def test_wildcard_path_passes_when_no_arg_matches(policy_enabled_mcp):
    """`args.*` does NOT gate when no arg satisfies the condition."""
    client, server, handlers = policy_enabled_mcp
    await _install_rule(
        handlers,
        {
            "tool_name": "ha_call_service",
            "when": [{"path": "args.*", "op": "eq", "value": "lock"}],
            "remember_minutes": 0,
        },
    )
    # No arg equals "lock" → call must pass through to the real tool.
    result = await client.call_tool(
        "ha_call_service",
        {"domain": "light", "service": "turn_on", "entity_id": "light.bed_light"},
    )
    assert not result.is_error, result
    assert server.approval_queue.list_pending() == []


@pytest.mark.asyncio
async def test_case_insensitive_match_gates_regardless_of_caller_casing(
    policy_enabled_mcp,
):
    """Rule value 'lock' should gate calls with 'LOCK', 'Lock', etc."""
    client, server, handlers = policy_enabled_mcp
    await _install_rule(
        handlers,
        {
            "tool_name": "ha_call_service",
            "when": [{"path": "args.domain", "op": "eq", "value": "lock"}],
            "remember_minutes": 0,
        },
    )
    # Caller capitalises — CI matching must still gate.
    await _expect_blocked(
        client, {"domain": "LOCK", "service": "unlock", "entity_id": "lock.front"}
    )
    pending = server.approval_queue.list_pending()
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_ws_command_gated_when_policy_scopes_call_service(policy_enabled_mcp):
    """ws_command escape hatch fail-safe: a domain-keyed rule can't match a
    ws_command call (no ``domain``/``service`` args), but scoping ANY rule to
    ``ha_call_service`` still forces approval on it rather than letting it
    slip through the fail-open default (see ``evaluate()`` in
    ``ha_mcp/policy/evaluator.py``)."""
    client, server, handlers = policy_enabled_mcp
    await _install_rule(
        handlers,
        {
            "tool_name": "ha_call_service",
            "when": [{"path": "args.domain", "op": "eq", "value": "light"}],
            "remember_minutes": 0,
        },
    )
    # No domain/service — the rule's predicate can't match this call — but
    # the fail-safe clause must still gate it because a ha_call_service rule
    # exists in the policy.
    await _expect_blocked(client, {"ws_command": "repairs/ignore_issue"})
    pending = server.approval_queue.list_pending()
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_deny_raises_user_denied(policy_enabled_mcp):
    """POST /deny → middleware raises USER_DENIED, never calls the tool."""
    client, server, handlers = policy_enabled_mcp
    await _install_rule(
        handlers,
        {
            "tool_name": "ha_call_service",
            "when": [{"path": "args.domain", "op": "eq", "value": "light"}],
            "remember_minutes": 0,
        },
    )
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.bed_light"}
    await _expect_blocked(client, args)
    token = server.approval_queue.list_pending()[0].token
    deny_resp = await handlers["policy_post_deny"](_make_request({"token": token}))
    assert deny_resp.status_code == 200, deny_resp.body
    # Re-call with same args: middleware sees the denied entry → USER_DENIED.
    try:
        result = await client.call_tool("ha_call_service", args)
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    assert body.get("error", {}).get("code") == "USER_DENIED", body


@pytest.mark.asyncio
async def test_remember_minutes_skips_approval_within_window(policy_enabled_mcp):
    """remember_minutes>0: a second call within the window bypasses gating."""
    client, server, handlers = policy_enabled_mcp
    await _install_rule(
        handlers,
        {
            "tool_name": "ha_call_service",
            "when": [{"path": "args.domain", "op": "eq", "value": "light"}],
            "remember_minutes": 5,
        },
    )
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.bed_light"}
    await _expect_blocked(client, args)
    token = server.approval_queue.list_pending()[0].token
    approve_resp = await handlers["policy_post_approve"](
        _make_request({"token": token})
    )
    assert approve_resp.status_code == 200
    # First post-approval call consumes the pending entry AND seeds the
    # remember-cache. Second call within the 5-minute window should skip
    # the queue entirely.
    result_a = await client.call_tool("ha_call_service", args)
    assert not result_a.is_error
    result_b = await client.call_tool("ha_call_service", args)
    assert not result_b.is_error
    # Pending must be empty — neither call left an entry behind.
    assert server.approval_queue.list_pending() == []


async def _install_event_decision_rule(handlers, *, wait_seconds: int = 30) -> None:
    """Gate light service calls and open the event-bus decision channel."""
    current_resp = await handlers["policy_get_config"](_make_request())
    current = json.loads(current_resp.body)
    body = {
        "wait_seconds": wait_seconds,
        "approval_ttl_minutes": 5,
        "event_decisions_enabled": True,
        "rules": [
            {
                "tool_name": "ha_call_service",
                "when": [{"path": "args.domain", "op": "eq", "value": "light"}],
                "remember_minutes": 0,
            }
        ],
        "version": current["version"],
    }
    put_resp = await handlers["policy_put_config"](_make_request(body))
    assert put_resp.status_code == 200, put_resp.body


def _announced_by(
    server: HomeAssistantSmartMCPServer,
) -> Callable[[dict[str, Any]], bool]:
    """Match only the announcements this server issued.

    One Home Assistant serves every test in the lane, so an
    ``ha_mcp_approval_requested`` event on its bus is not necessarily ours:
    another test's server announces its own held call on the same bus, and
    a response to that token is refused here as ``unknown_token`` because
    this queue never issued it. The queue is the discriminator -- arguments
    can coincide between tests, an issued token cannot.
    """

    def issued_here(event: dict[str, Any]) -> bool:
        announced = (event.get("data") or {}).get("token")
        return isinstance(announced, str) and (
            server.approval_queue.get(announced) is not None
        )

    return issued_here


async def _respond_and_wait_for_result(
    responder: HomeAssistantClient,
    payload: dict[str, Any],
    *,
    base_url: str,
    token: str,
) -> dict[str, Any] | None:
    """Fire one response event and return the result event it produces.

    The listener is subscribed inside the server process, so the only way
    to observe what it made of a response is the answer it fires back --
    which is the point of that answer existing. Started before the
    response goes out, because the round trip can complete first.

    Filtered by the token being answered, for the reason ``_announced_by``
    gives: a result naming a token this call did not send belongs to
    another test sharing the bus.
    """
    fired: asyncio.Task | None = None

    def fire_the_response() -> None:
        nonlocal fired
        fired = asyncio.create_task(
            responder.fire_event("ha_mcp_approval_response", payload)
        )

    result = await wait_for_ha_event(
        "ha_mcp_approval_result",
        fire_the_response,
        predicate=lambda ev: (ev.get("data") or {}).get("token") == payload["token"],
        timeout=20.0,
        ha_url=base_url,
        token=token,
    )
    if fired is not None:
        await fired
    return result


@pytest.mark.asyncio
async def test_a_real_event_round_trip_decides_a_held_call(
    policy_enabled_mcp, ha_container_with_fresh_config
):
    """The whole transport, over the real bus: announce → respond → dispatch.

    Every other test of this feature stops at a seam — the HTTP handlers,
    a mocked subscription, or the listener called directly — so none of
    them exercises the path a user actually uses: Home Assistant carries
    the announcement out, and carries the response back in over a
    WebSocket subscription this server opened itself. A wrong PIN first,
    because the interesting property is not that a response decides the
    request but that only the right one does.
    """
    client, server, handlers = policy_enabled_mcp
    base_url = ha_container_with_fresh_config["base_url"]
    token = ha_container_with_fresh_config.get("token", TEST_TOKEN)

    pin_resp = await handlers["policy_post_decision_pin"](
        _make_request({"pin": "2468"})
    )
    assert pin_resp.status_code == 200, pin_resp.body
    await _install_event_decision_rule(handlers)

    args = {"domain": "light", "service": "turn_on", "entity_id": "light.bed_light"}
    call_task: asyncio.Task | None = None

    def start_the_gated_call() -> None:
        nonlocal call_task
        call_task = asyncio.create_task(client.call_tool("ha_call_service", args))

    announcement = await wait_for_ha_event(
        "ha_mcp_approval_requested",
        start_the_gated_call,
        predicate=_announced_by(server),
        timeout=20.0,
        ha_url=base_url,
        token=token,
    )
    assert announcement is not None, "the held call was never announced on the bus"
    assert call_task is not None
    approval_token = announcement["data"]["token"]

    responder = HomeAssistantClient(base_url=base_url, token=token)
    try:
        # A wrong PIN decides nothing, and the call keeps waiting -- and
        # the responder is told so, which is the only way an automation
        # can distinguish a refusal from an event nobody received.
        refusal = await _respond_and_wait_for_result(
            responder,
            {"token": approval_token, "decision": "approve", "pin": "9999"},
            base_url=base_url,
            token=token,
        )
        assert refusal is not None, "a refused response produced no result event"
        assert refusal["data"]["applied"] is False
        assert refusal["data"]["reason"] == "wrong_pin"
        assert refusal["data"]["token"] == approval_token
        # The PIN itself, not the word: ``reason`` says ``wrong_pin``, so a
        # substring search for "pin" answers a different question than the
        # one that matters -- whether the refusal handed the guess back.
        assert "9999" not in json.dumps(refusal["data"])
        assert not [key for key in refusal["data"] if "pin" in key.lower()]
        assert not call_task.done(), (
            "a wrong PIN released the held call; the PIN is the only thing "
            "standing between an agent-fired event and its own approval"
        )
        assert server.approval_queue.get(approval_token) is not None

        applied = await _respond_and_wait_for_result(
            responder,
            {"token": approval_token, "decision": "approve", "pin": "2468"},
            base_url=base_url,
            token=token,
        )
        assert applied is not None, "an applied response produced no result event"
        assert applied["data"]["applied"] is True
        assert applied["data"]["reason"] == "applied"
        assert applied["data"]["tool_name"] == "ha_call_service"
        result = await asyncio.wait_for(call_task, timeout=20)
    finally:
        if not call_task.done():
            call_task.cancel()
        await responder.close()

    assert not result.is_error, result
    # Consumed exactly once: the entry is gone, so a replayed response
    # event carrying the same token cannot dispatch the tool again.
    assert server.approval_queue.get(approval_token) is None


@pytest.mark.asyncio
async def test_a_real_event_round_trip_denies_a_held_call(
    policy_enabled_mcp, ha_container_with_fresh_config
):
    """Deny travels the same path and produces the denial error."""
    client, server, handlers = policy_enabled_mcp
    base_url = ha_container_with_fresh_config["base_url"]
    token = ha_container_with_fresh_config.get("token", TEST_TOKEN)

    pin_resp = await handlers["policy_post_decision_pin"](
        _make_request({"pin": "2468"})
    )
    assert pin_resp.status_code == 200, pin_resp.body
    await _install_event_decision_rule(handlers)

    args = {"domain": "light", "service": "turn_off", "entity_id": "light.bed_light"}
    call_task: asyncio.Task | None = None

    def start_the_gated_call() -> None:
        nonlocal call_task
        call_task = asyncio.create_task(client.call_tool("ha_call_service", args))

    announcement = await wait_for_ha_event(
        "ha_mcp_approval_requested",
        start_the_gated_call,
        predicate=_announced_by(server),
        timeout=20.0,
        ha_url=base_url,
        token=token,
    )
    assert announcement is not None, "the held call was never announced on the bus"
    assert call_task is not None

    responder = HomeAssistantClient(base_url=base_url, token=token)
    try:
        denial = await _respond_and_wait_for_result(
            responder,
            {
                "token": announcement["data"]["token"],
                "decision": "deny",
                "pin": "2468",
            },
            base_url=base_url,
            token=token,
        )
        assert denial is not None, "an applied denial produced no result event"
        assert denial["data"]["decision"] == "deny"
        assert denial["data"]["applied"] is True
        assert denial["data"]["reason"] == "applied"
        try:
            result = await asyncio.wait_for(call_task, timeout=20)
        except ToolError as exc:
            body = tool_error_to_result(exc)
        else:
            body = parse_mcp_result(result)
    finally:
        if not call_task.done():
            call_task.cancel()
        await responder.close()

    assert body.get("error", {}).get("code") == "USER_DENIED", body
