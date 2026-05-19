"""
Async waiting utilities for E2E testing.

This module provides helper functions for waiting for state changes, operations
to complete, and other asynchronous conditions in Home Assistant.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from typing import Any

import websockets
from fastmcp.exceptions import ClientError, FastMCPError
from mcp import McpError

from .assertions import parse_mcp_result, safe_call_tool

logger = logging.getLogger(__name__)

# Transient errors expected during async polling of MCP tools or HTTP endpoints.
# Bugs (TypeError, AttributeError, KeyError, AssertionError, ...) MUST propagate
# out of polling loops so they fail tests with a clear stack trace instead of
# being swallowed and retried until timeout. See issue #1266.
_POLLING_TRANSIENT_ERRORS = (
    McpError,
    FastMCPError,
    ClientError,
    RuntimeError,
    OSError,
    TimeoutError,
)


async def wait_for_entity_state(
    mcp_client,
    entity_id: str,
    expected_state: str,
    timeout: int = 10,
    poll_interval: float = 0.5,
) -> bool:
    """
    Wait for entity to reach expected state.

    Args:
        mcp_client: FastMCP client instance
        entity_id: Entity to monitor
        expected_state: State to wait for
        timeout: Maximum wait time in seconds
        poll_interval: Time between checks in seconds

    Returns:
        True if state reached, False if timeout
    """
    start_time = time.monotonic()

    logger.info(
        f"⏳ Waiting for {entity_id} to reach state '{expected_state}' (timeout: {timeout}s)"
    )

    while time.monotonic() - start_time < timeout:
        try:
            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if "data" in state_data and state_data["data"] is not None:
                current_state = state_data.get("data", {}).get("state")
                logger.debug(f"🔍 {entity_id} current state: {current_state}")

                if current_state == expected_state:
                    elapsed = time.monotonic() - start_time
                    logger.info(
                        f"✅ {entity_id} reached state '{expected_state}' after {elapsed:.1f}s"
                    )
                    return True

        except _POLLING_TRANSIENT_ERRORS as e:
            logger.debug(f"⚠️ Error checking state for {entity_id}: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(
        f"⚠️ {entity_id} did not reach state '{expected_state}' within {timeout}s"
    )
    return False


async def wait_for_logbook_entry(
    mcp_client,
    search_text: str,
    timeout: int = 30,
    poll_interval: float = 2.0,
    hours_back: int = 1,
) -> bool:
    """
    Wait for logbook entry containing specific text.

    Args:
        mcp_client: FastMCP client instance
        search_text: Text to search for in logbook
        timeout: Maximum wait time in seconds
        poll_interval: Time between logbook checks in seconds
        hours_back: How many hours of logbook to search

    Returns:
        True if entry found, False if timeout
    """
    start_time = time.monotonic()

    logger.info(
        f"⏳ Waiting for logbook entry containing '{search_text}' (timeout: {timeout}s)"
    )

    while time.monotonic() - start_time < timeout:
        try:
            logbook_result = await mcp_client.call_tool(
                "ha_get_logs", {"hours_back": hours_back}
            )

            logbook_data = parse_mcp_result(logbook_result)

            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if "data" in logbook_data and logbook_data["data"] is not None:
                entries = logbook_data["data"].get("entries", [])

                for entry in entries:
                    entry_text = str(entry).lower()
                    if search_text.lower() in entry_text:
                        elapsed = time.monotonic() - start_time
                        logger.info(
                            f"✅ Found logbook entry with '{search_text}' after {elapsed:.1f}s"
                        )
                        return True

        except _POLLING_TRANSIENT_ERRORS as e:
            logger.debug(f"⚠️ Error checking logbook: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(
        f"⚠️ Logbook entry containing '{search_text}' not found within {timeout}s"
    )
    return False


async def wait_for_condition(
    condition_func: Callable[[], Any],
    timeout: int = 10,
    poll_interval: float = 0.5,
    condition_name: str = "condition",
) -> bool:
    """
    Wait for custom condition function to return truthy value.

    Args:
        condition_func: Function that returns truthy when condition is met
        timeout: Maximum wait time in seconds
        poll_interval: Time between checks in seconds
        condition_name: Name of condition for logging

    Returns:
        True if condition met, False if timeout
    """
    start_time = time.monotonic()

    logger.info(f"⏳ Waiting for {condition_name} (timeout: {timeout}s)")

    while time.monotonic() - start_time < timeout:
        try:
            if (
                await condition_func()
                if asyncio.iscoroutinefunction(condition_func)
                else condition_func()
            ):
                elapsed = time.monotonic() - start_time
                logger.info(f"✅ {condition_name} met after {elapsed:.1f}s")
                return True
        except _POLLING_TRANSIENT_ERRORS as e:
            logger.debug(f"⚠️ Error checking {condition_name}: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(f"⚠️ {condition_name} not met within {timeout}s")
    return False


async def wait_for_state_change(
    mcp_client, entity_id: str, timeout: int = 10, poll_interval: float = 0.5
) -> str | None:
    """
    Wait for entity state to change from current state.

    Args:
        mcp_client: FastMCP client instance
        entity_id: Entity to monitor
        timeout: Maximum wait time in seconds
        poll_interval: Time between checks in seconds

    Returns:
        New state if changed, None if timeout or error
    """
    # Get initial state
    try:
        initial_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": entity_id}
        )
        initial_data = parse_mcp_result(initial_result)

        # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
        if "data" not in initial_data or initial_data["data"] is None:
            logger.warning(f"⚠️ Could not get initial state for {entity_id}")
            return None

        initial_state = initial_data.get("data", {}).get("state")
        logger.info(
            f"⏳ Waiting for {entity_id} to change from '{initial_state}' (timeout: {timeout}s)"
        )

    except Exception as e:
        logger.warning(f"⚠️ Error getting initial state for {entity_id}: {e}")
        return None

    start_time = time.monotonic()

    while time.monotonic() - start_time < timeout:
        try:
            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if "data" in state_data and state_data["data"] is not None:
                current_state = state_data.get("data", {}).get("state")

                if current_state != initial_state:
                    elapsed = time.monotonic() - start_time
                    logger.info(
                        f"✅ {entity_id} changed: '{initial_state}' → '{current_state}' after {elapsed:.1f}s"
                    )
                    return current_state

        except _POLLING_TRANSIENT_ERRORS as e:
            logger.debug(f"⚠️ Error checking state change for {entity_id}: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(
        f"⚠️ {entity_id} did not change from '{initial_state}' within {timeout}s"
    )
    return None


async def wait_for_tool_result(
    mcp_client,
    tool_name: str,
    arguments: dict[str, Any],
    predicate: Callable[[dict[str, Any]], bool],
    timeout: int = 15,
    poll_interval: float = 0.5,
    description: str = "tool result",
) -> dict[str, Any]:
    """
    Poll an MCP tool until the result satisfies a predicate.

    Useful when an entity was just created and needs time to be registered
    in Home Assistant before it becomes visible to search/query tools.

    Args:
        mcp_client: FastMCP client instance
        tool_name: MCP tool to call repeatedly
        arguments: Arguments to pass to the tool
        predicate: Function that receives parsed tool result and returns
                   True when the desired condition is met
        timeout: Maximum wait time in seconds
        poll_interval: Time between calls in seconds
        description: Human-readable description for logging

    Returns:
        The parsed tool result that satisfied the predicate.

    Raises:
        TimeoutError: If the predicate is not satisfied within the timeout.
    """
    start_time = time.monotonic()
    last_data: dict[str, Any] = {}

    logger.info(f"⏳ Waiting for {description} (timeout: {timeout}s)")

    while True:
        # Call the tool — catch tool/network errors to keep polling
        try:
            result = await mcp_client.call_tool(tool_name, arguments)
            last_data = parse_mcp_result(result)
        except _POLLING_TRANSIENT_ERRORS as e:
            logger.debug(f"⚠️ Error calling {tool_name}: {e}")
            if time.monotonic() - start_time >= timeout:
                raise TimeoutError(
                    f"{description}: timed out after {timeout}s (last error: {e})"
                ) from e
            await asyncio.sleep(poll_interval)
            continue

        # Skip MCP error responses — entity may not be registered yet
        if last_data.get("success") is False:
            logger.debug(
                f"⚠️ {tool_name} returned error: {last_data.get('error')}, retrying..."
            )
            if time.monotonic() - start_time >= timeout:
                raise TimeoutError(
                    f"{description}: timed out after {timeout}s "
                    f"(last MCP error: {last_data.get('error')})"
                )
            await asyncio.sleep(poll_interval)
            continue

        # Run predicate OUTSIDE try/except so bugs (TypeError, KeyError) propagate
        if predicate(last_data):
            elapsed = time.monotonic() - start_time
            logger.info(f"✅ {description} satisfied after {elapsed:.1f}s")
            return last_data

        if time.monotonic() - start_time >= timeout:
            raise TimeoutError(
                f"{description}: timed out after {timeout}s (predicate not satisfied)"
            )
        await asyncio.sleep(poll_interval)


async def wait_for_entity_registration(
    mcp_client, entity_id: str, timeout: int = 20
) -> bool:
    """
    Wait for entity to be registered and queryable via API.

    Does not check for a specific state — only that the entity exists and is
    visible to ``ha_get_state``. Useful after ``ha_config_set_helper`` or
    ``ha_set_entity``, where the tool returns success before Home Assistant
    finishes the async entity-registry update.

    Args:
        mcp_client: FastMCP client instance
        entity_id: Entity to wait for
        timeout: Maximum wait time in seconds

    Returns:
        True if entity becomes queryable within timeout, False otherwise.
    """
    start_time = time.monotonic()
    attempt = 0

    async def entity_exists():
        nonlocal attempt
        attempt += 1
        data = await safe_call_tool(
            mcp_client, "ha_get_state", {"entity_id": entity_id}
        )
        # Check if 'data' key exists (not 'success' key)
        success = "data" in data and data["data"] is not None

        # Per-attempt details at debug level so transient misses don't
        # clutter CI logs; sibling wait_for_entity_state follows the same
        # convention. See .gemini/styleguide.md ("Exception Handling in
        # Test Polling Loops").
        elapsed = time.monotonic() - start_time
        logger.debug(
            f"[Attempt {attempt} @ {elapsed:.1f}s] Checking {entity_id}: "
            f"success={success}, data keys={list(data.keys())}"
        )

        if success:
            state = data.get("data", {}).get("state", "N/A")
            logger.info(f"✅ Entity {entity_id} EXISTS with state='{state}'")
        else:
            error = data.get("error", "No error message")
            logger.debug(f"❌ Entity {entity_id} check failed: {error}")

        return success

    return await wait_for_condition(
        entity_exists, timeout=timeout, condition_name=f"{entity_id} registration"
    )


async def _open_ha_ws(ha_url: str | None = None, token: str | None = None):
    """Open + authenticate a fresh HA WebSocket connection.

    Shared setup for the ``*_via_ws`` helpers below. Returns the connected
    websocket; caller is responsible for closing it (use ``async with``).
    """
    if ha_url is None:
        ha_url = os.environ.get("HOMEASSISTANT_URL")
    if not ha_url:
        raise RuntimeError("No ha_url and $HOMEASSISTANT_URL unset")
    if token is None:
        token = os.environ.get("HOMEASSISTANT_TOKEN")
    if not token:
        from test_constants import TEST_TOKEN as _DEFAULT_TOKEN

        token = _DEFAULT_TOKEN

    ws_url = ha_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    ws_url = ws_url.rstrip("/") + "/api/websocket"

    ws = await websockets.connect(ws_url, open_timeout=10.0)
    try:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            await ws.close()
            raise RuntimeError(f"Expected auth_required handshake, got {hello!r}")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_result = json.loads(await ws.recv())
        if auth_result.get("type") != "auth_ok":
            await ws.close()
            raise RuntimeError(f"WS auth failed: {auth_result!r}")
    except Exception:
        await ws.close()
        raise
    return ws


async def wait_for_ha_event(
    event_type: str,
    trigger: Callable[[], Any],
    *,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    timeout: float = 5.0,
    ha_url: str | None = None,
    token: str | None = None,
) -> dict[str, Any] | None:
    """Subscribe to ``event_type``, invoke ``trigger``, return first matching event.

    Subscribes BEFORE running the trigger so events fired during the
    trigger's awaitable cannot be missed. Returns the matching event
    dict (HA's full event payload — ``{"event_type", "data", "time_fired", ...}``)
    or ``None`` on timeout. Predicate lets the caller filter by entity_id,
    context, or any other field of the event.

    Opens a **dedicated** WebSocket connection — independent of the
    MCP client's listener subscriptions and not visible to other
    callers. Each invocation pays one WS handshake + auth round-trip.

    Useful for replacing 10s logbook polls with sub-second event waits —
    e.g. ``automation_triggered`` after a manual ``automation.trigger``
    service call (~10s saved per ``test_basic_automation_lifecycle``).
    """
    ws = await _open_ha_ws(ha_url=ha_url, token=token)
    try:
        sub_id = 1
        await ws.send(
            json.dumps(
                {"id": sub_id, "type": "subscribe_events", "event_type": event_type}
            )
        )
        sub_result = json.loads(await ws.recv())
        if not (
            sub_result.get("type") == "result"
            and sub_result.get("success") is True
            and sub_result.get("id") == sub_id
        ):
            logger.warning(f"subscribe_events({event_type!r}) failed: {sub_result!r}")
            return None

        # Subscription is live; now fire the trigger. Trigger errors are
        # NOT caught — a test's setup crashing (TypeError, AssertionError,
        # KeyError, etc.) is a bug, not a transient, and should surface
        # immediately with a clear traceback rather than being indistinguishable
        # from "no matching event arrived". Matches the
        # _POLLING_TRANSIENT_ERRORS discipline AGENTS.md mandates.
        trigger_result = trigger()
        if asyncio.iscoroutine(trigger_result):
            await trigger_result

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                return None
            payload = json.loads(raw)
            if payload.get("type") != "event":
                continue
            event = payload.get("event") or {}
            if event.get("event_type") != event_type:
                continue
            if predicate is None or predicate(event):
                return event
    except (
        websockets.exceptions.WebSocketException,
        ConnectionError,
        OSError,
        json.JSONDecodeError,
    ) as e:
        # Narrow catch: WS transport / handshake / malformed frame are
        # transients we want to gracefully degrade to timeout. Caller
        # bugs (TypeError, AttributeError, KeyError from a buggy
        # predicate) propagate.
        logger.warning(f"wait_for_ha_event({event_type!r}) transient error: {e!r}")
        return None
    finally:
        try:
            await ws.close()
        except (websockets.exceptions.WebSocketException, OSError):
            # Close errors on an already-broken socket — don't mask the
            # caller's outcome with them.
            pass


async def wait_for_entities_registered_via_ws(
    expected_entity_ids: Iterable[str],
    *,
    timeout: float = 30.0,
    ha_url: str | None = None,
    token: str | None = None,
) -> set[str]:
    """Block until ``state_changed`` arrives for every expected entity_id, via HA WS.

    Opens a fresh WebSocket connection to HA, authenticates with the
    test token, subscribes to ``state_changed``, and resolves as soon as
    each entity_id in ``expected_entity_ids`` has produced at least one
    state_changed event. Avoids the chronic 10s ``ha_list_states`` polling
    burn observed in the HAOS bulk-fixture wait (#1349 audit).

    Pairs with a subsequent ``ha_list_states`` call (or per-entity
    ``ha_get_state``) for a final correctness check — this helper only
    confirms HA has published the entity to the state machine; the
    caller does the authoritative read.

    Args:
        expected_entity_ids: The set of entity_ids whose registration we
            need to observe. Returns as soon as all have fired.
        timeout: Hard ceiling in seconds. Returns the set of entity_ids
            actually seen (may be a strict subset of ``expected_entity_ids``)
            when the timeout fires.
        ha_url: HA base URL. Defaults to ``$HOMEASSISTANT_URL``.
        token: Long-lived access token. Defaults to ``$HOMEASSISTANT_TOKEN``,
            falling back to ``tests.test_constants.TEST_TOKEN``.

    Returns:
        Set of entity_ids that fired ``state_changed`` (and so are
        confirmed registered) before the timeout. Also returns the
        partial seen set on transient WS/parsing failure (caller's
        fallback path picks up the missing ids via REST polling);
        config/auth failures (RuntimeError) propagate so a permanently
        broken WS path fails loudly rather than silently masking
        every CI run.
    """
    expected = set(expected_entity_ids)
    if not expected:
        return set()

    if ha_url is None:
        ha_url = os.environ.get("HOMEASSISTANT_URL")
    if not ha_url:
        raise RuntimeError(
            "wait_for_entities_registered_via_ws: no ha_url and "
            "$HOMEASSISTANT_URL is unset"
        )
    if token is None:
        token = os.environ.get("HOMEASSISTANT_TOKEN")
    if not token:
        # Fall back to the test constant for tiers where the env var
        # isn't set by the fixture (kept centralized in test_constants).
        from test_constants import TEST_TOKEN as _DEFAULT_TOKEN

        token = _DEFAULT_TOKEN

    ws_url = ha_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    ws_url = ws_url.rstrip("/") + "/api/websocket"

    seen: set[str] = set()
    deadline = time.monotonic() + timeout

    logger.info(
        f"⏳ Waiting for {len(expected)} entity registrations via WS "
        f"(timeout={timeout}s): {sorted(expected)[:5]}"
        f"{'…' if len(expected) > 5 else ''}"
    )

    try:
        async with websockets.connect(ws_url, open_timeout=10.0) as ws:
            # HA WS handshake: server sends auth_required, client sends
            # auth with access_token, server replies auth_ok (or auth_invalid).
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError(
                    f"Expected auth_required handshake, got {hello!r}"
                )
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth_result = json.loads(await ws.recv())
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError(f"WS auth failed: {auth_result!r}")

            # Subscribe to state_changed. HA's response carries the
            # subscription id as the request id (not in result).
            sub_msg_id = 1
            await ws.send(
                json.dumps(
                    {
                        "id": sub_msg_id,
                        "type": "subscribe_events",
                        "event_type": "state_changed",
                    }
                )
            )
            sub_result = json.loads(await ws.recv())
            if not (
                sub_result.get("type") == "result"
                and sub_result.get("success") is True
                and sub_result.get("id") == sub_msg_id
            ):
                raise RuntimeError(
                    f"subscribe_events failed: {sub_result!r}"
                )

            while seen != expected:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except TimeoutError:
                    break
                payload = json.loads(raw)
                if payload.get("type") != "event":
                    continue
                event = payload.get("event") or {}
                data = event.get("data") or {}
                entity_id = data.get("entity_id")
                if entity_id in expected:
                    seen.add(entity_id)
    except (
        websockets.exceptions.WebSocketException,
        ConnectionError,
        OSError,
        json.JSONDecodeError,
    ) as e:
        # Narrow catch: same discipline as ``wait_for_ha_event`` above —
        # transient WS / parsing errors degrade gracefully to "missing
        # entities" (caller's fallback path handles it), but
        # ``RuntimeError`` (auth failure, missing url/token, malformed
        # handshake) and ``TypeError``/``AttributeError``/``KeyError``
        # (caller bug) propagate so a permanently broken WS path
        # surfaces loud rather than silently masking every CI run.
        logger.warning(
            f"WS-based entity-registration wait transient error: {e!r}. "
            f"Seen {len(seen)}/{len(expected)} before failure."
        )

    if seen == expected:
        logger.info(f"✅ All {len(expected)} entity registrations observed via WS")
    else:
        logger.warning(
            f"⚠️ WS wait incomplete after {timeout}s — "
            f"saw {len(seen)}/{len(expected)}; missing: "
            f"{sorted(expected - seen)[:10]}"
        )
    return seen
