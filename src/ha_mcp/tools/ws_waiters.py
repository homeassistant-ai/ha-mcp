"""WebSocket-event-driven wait helpers for config writes and service calls.

Each helper confirms that an operation reached the entity registry or state
machine before the calling tool returns.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)

logger = logging.getLogger(__name__)


# --- WS-event-driven wait helpers (#1152) -----------------------------------
#
# Background: every config write tool (`ha_config_set_helper`, set_automation,
# set_script, …) calls one of these three helpers after the API write returns,
# to confirm the operation reached the entity registry / state machine before
# the tool itself returns. Until #1152, those checks polled REST every 300ms
# up to a 10s budget. On a slow HA instance the poll could time out before
# the entity hydrated, surfacing a "Helper created but … not yet queryable"
# soft-failure warning even though the write succeeded — see #1152 for the
# agent-misattribution failure mode.
#
# The new pattern is WS-event-driven with a REST sample after subscribe and a
# slow REST backstop, falling back to pure REST polling when the WebSocket is
# unavailable:
#
#   1. Open a `state_changed` (and, for registry-add/remove waits, an
#      `entity_registry_updated`) subscription via `subscribe_events`. The
#      subscription must be live BEFORE we look at the world so we don't miss
#      the event the write triggered.
#   2. Take a single REST sample. This closes the "the event fired between
#      the write returning and our subscribe landing" window — if the entity
#      is already in the desired shape, we return immediately and never
#      touch the event loop.
#   3. Await events for our entity_id, then re-sample. A
#      ``_POLLING_BACKSTOP_INTERVAL`` REST sample also runs every few seconds
#      independently of events, so a silent-broken subscription degrades to
#      a slow-polling REST loop rather than a 10s hang.
#   4. Drop the subscription and event handler in `finally`.
#
# Connection-drop awareness: if `get_websocket_client()` or `subscribe_events`
# fails, we fall through to ``_legacy_poll_until`` (the pre-#1152 REST loop)
# transparently, so the helpers still work on REST-only deployments and during
# HA-mid-restart windows. The legacy loop is also what we call when the WS
# subscription itself fails to set up — the helpers' contract (return bool or
# state dict, never raise on the happy path) is identical to before.

_POLLING_BACKSTOP_INTERVAL = 2.0
"""Seconds between independent REST samples while a WS subscription is open.

Bounded slow-poll backstop so a silent-broken WS subscription still
resolves within the helper's timeout. A 10s budget with a 2s backstop
costs at most ~6 REST calls per wait (one post-subscribe sample plus
~5 backstop samples), vs. ~33 calls for the previous 300ms loop."""


async def _legacy_poll_until(
    identifier: str,
    sample: Callable[[], Awaitable[Any]],
    *,
    timeout: float,
    poll_interval: float,
    description: str,
) -> Any:
    """REST-polling waiter used as the WS-subscription fallback path.

    ``sample`` is the same callable the WS path runs after each event /
    backstop tick — it returns a truthy value when the wait should
    succeed, ``None`` otherwise. Connection / auth errors propagate
    (callers care about those); other transient errors raised inside
    ``sample`` are swallowed there. ``identifier`` is the human-readable
    name used in log lines — usually an entity_id but may be a
    descriptor like ``automation[unique_id=...]`` for discovery waits
    that don't know the entity_id up front.
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            result = await sample()
            if result is not None:
                logger.debug(
                    f"REST waiter: {description} for {identifier} resolved "
                    f"after {time.monotonic() - start:.2f}s"
                )
                return result
        except (HomeAssistantConnectionError, HomeAssistantAuthError):
            raise
        await asyncio.sleep(poll_interval)
    logger.warning(
        f"REST fallback: {description} for {identifier} timed out after {timeout}s"
    )
    return None


async def _get_waiter_ws_client(client: Any) -> Any:
    """Return a connected WS client to use for waiter subscriptions, or None.

    Returning ``None`` triggers REST-only fallback in
    ``_ws_wait_for_condition``. Localised import avoids a top-level cycle
    (websocket_client → rest_client → ws_waiters → websocket_client).
    """
    try:
        from ..client.websocket_client import get_websocket_client
    except ImportError as e:  # pragma: no cover - import-time defence
        logger.debug("WS waiter import failed: %s", e)
        return None

    base_url = getattr(client, "base_url", None)
    token = getattr(client, "token", None)
    # Per-client credentials are only meaningful when both are strings.
    # If the caller is a test rig passing a ``MagicMock`` client (which
    # returns ``MagicMock`` for any attribute), forwarding those into the
    # WS pool trips URL-parsing TypeErrors deep inside ``WebSocketManager``.
    # Treat any non-string credential as "no WS available" and fall
    # through to REST polling — production callers always have a real
    # string ``base_url`` and ``token``, so this only matters for tests.
    if not (isinstance(base_url, str) and isinstance(token, str)):
        return None
    try:
        ws_client = await get_websocket_client(
            url=base_url,
            token=token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
    except HomeAssistantAuthError:
        # Auth failures must reach the caller — a bad token should surface
        # as a real error, not as a 10s "timed out" via REST fallback.
        # silent-failure-hunter #1382.
        raise
    except (HomeAssistantConnectionError, OSError, TimeoutError) as e:
        logger.debug("WS waiter could not obtain ws client: %s", e)
        return None

    if not getattr(ws_client, "is_connected", False):
        return None
    return ws_client


async def _ws_subscribe_all(
    ws_client: Any,
    event_types: tuple[str, ...],
    handler: Any,
    attached_handlers: list[str],
    sub_ids: list[int],
    description: str,
    identifier: str,
) -> bool:
    """Attach event handler and subscribe to all event_types.

    Populates attached_handlers and sub_ids in-place.
    Returns True on success, False if a non-auth error triggers REST fallback.
    """
    for et in event_types:
        ws_client.add_event_handler(et, handler)
        attached_handlers.append(et)
    for et in event_types:
        try:
            sub_ids.append(await ws_client.subscribe_events(et))
        except HomeAssistantAuthError:
            raise
        except (
            HomeAssistantConnectionError,
            HomeAssistantCommandError,
            OSError,
            TimeoutError,
        ) as e:
            logger.debug(
                "subscribe_events(%s) failed during %s for %s: %s — falling back to REST polling",
                et,
                description,
                identifier,
                e,
            )
            return False
    return True


async def _ws_post_subscribe_check(
    ws_client: Any,
    sample: Callable[[], Awaitable[Any]],
    start: float,
    timeout: float,
    poll_interval: float,
    description: str,
    identifier: str,
) -> tuple[Any, bool]:
    """Run post-subscribe sample and connection check.

    Returns (result, is_done). When is_done=True the caller should return result
    immediately (either an early success or a REST-poll fallback).
    """
    try:
        result = await sample()
        if result is not None:
            logger.debug(
                f"WS waiter: {description} for {identifier} resolved by "
                f"post-subscribe sample after {time.monotonic() - start:.2f}s"
            )
            return result, True
    except (HomeAssistantConnectionError, HomeAssistantAuthError):
        raise

    if not ws_client.is_connected:
        logger.debug(
            "WS connection dropped before wait loop for %s on %s — completing via REST polling",
            description,
            identifier,
        )
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            return None, True
        return await _legacy_poll_until(
            identifier,
            sample,
            timeout=remaining,
            poll_interval=poll_interval,
            description=description,
        ), True

    return None, False


async def _ws_run_wait_loop(
    ws_client: Any,
    sample: Callable[[], Awaitable[Any]],
    nudge: asyncio.Event,
    start: float,
    timeout: float,
    poll_interval: float,
    description: str,
    identifier: str,
) -> Any:
    """Event-driven wait loop: nudge on event, backstop polling, REST fallback on disconnect."""
    while time.monotonic() - start < timeout:
        remaining = timeout - (time.monotonic() - start)
        wait_budget = min(remaining, _POLLING_BACKSTOP_INTERVAL)
        try:
            await asyncio.wait_for(nudge.wait(), timeout=wait_budget)
            nudge.clear()
        except TimeoutError:
            pass  # polling backstop expired — loop continues to check connection and sample

        if not ws_client.is_connected:
            logger.debug(
                "WS connection dropped during %s for %s — completing wait via REST polling",
                description,
                identifier,
            )
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                return None
            return await _legacy_poll_until(
                identifier,
                sample,
                timeout=remaining,
                poll_interval=poll_interval,
                description=description,
            )

        try:
            result = await sample()
            if result is not None:
                logger.debug(
                    f"WS waiter: {description} for {identifier} resolved "
                    f"after {time.monotonic() - start:.2f}s"
                )
                return result
        except (HomeAssistantConnectionError, HomeAssistantAuthError):
            raise

    logger.warning(
        f"WS waiter: {description} for {identifier} timed out after {timeout}s"
    )
    return None


async def _ws_cleanup(
    ws_client: Any,
    attached_handlers: list[str],
    sub_ids: list[int],
    handler: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    for et in attached_handlers:
        ws_client.remove_event_handler(et, handler)
    for sub_id in sub_ids:
        try:
            await ws_client.unsubscribe_events(sub_id)
        except (HomeAssistantConnectionError, OSError, TimeoutError) as e:
            logger.warning(
                "unsubscribe_events(%s) cleanup failed (subscription "
                "may leak until WS pool reconnects): %s",
                sub_id,
                e,
            )
        except HomeAssistantCommandTimeout:
            logger.warning(
                "unsubscribe_events(%s) cleanup timed out on WS "
                "round-trip; subscription may leak until WS pool "
                "reconnects",
                sub_id,
            )


async def _ws_wait_for_condition(
    client: Any,
    identifier: str,
    sample: Callable[[], Awaitable[Any]],
    *,
    event_types: tuple[str, ...],
    timeout: float,
    poll_interval: float,
    description: str,
    event_filter: Callable[[dict[str, Any]], bool] | None = None,
) -> Any:
    """Subscribe to ``event_types``, sample after subscribe, wait on event.

    Implements the standard "subscribe → sample → wait" pattern from #1152:

    - The handler nudges a single ``asyncio.Event`` whenever HA pushes an
      event matching ``event_filter``. The main loop wakes on that nudge
      or on the polling-backstop timeout, then re-runs ``sample`` (the
      REST source-of-truth check) to decide whether the wait succeeded.
    - Sample-after-subscribe (not before) closes the gap between the
      caller's write returning and our subscription landing on the HA
      side. The event for the write may have already fired by the time we
      subscribe; the post-subscribe sample catches that.
    - If the WS path fails to set up (no WS client, no subscription, …)
      we fall back to ``_legacy_poll_until``. The helpers' contract is
      identical to the pre-#1152 REST loop in that case.

    ``identifier`` is used only for log lines — usually an entity_id but
    may be a descriptor like ``automation[unique_id=...]`` for discovery
    waits (#1395) that don't know the entity_id up front. When
    ``event_filter`` is None the default predicate matches events whose
    ``data["entity_id"]`` equals ``identifier`` — i.e. the standard
    "watch this entity_id" shape used by ``wait_for_entity_*`` /
    ``wait_for_state_change``. Callers that need a different match shape
    (e.g. "any automation with attributes.id == unique_id") pass a custom
    ``event_filter``.

    Returns ``sample``'s truthy return value, or ``None`` on timeout.
    """
    ws_client = await _get_waiter_ws_client(client)
    if ws_client is None:
        return await _legacy_poll_until(
            identifier,
            sample,
            timeout=timeout,
            poll_interval=poll_interval,
            description=description,
        )

    nudge = asyncio.Event()

    def _default_filter(event: dict[str, Any]) -> bool:
        # HA nests ``entity_id`` under ``event["data"]`` for both
        # state_changed and entity_registry_updated. The top-level fallback
        # is defensive only — it lets a future schema drift degrade to a
        # missed nudge rather than an AttributeError.
        data = event.get("data") or {}
        evt_entity = data.get("entity_id") or event.get("entity_id")
        return bool(evt_entity == identifier)

    filter_fn = event_filter if event_filter is not None else _default_filter

    async def handler(event: dict[str, Any]) -> None:
        if filter_fn(event):
            nudge.set()

    # Track which handlers / subscriptions we actually attached so cleanup
    # is exact even if subscribe_events raises partway through.
    attached_handlers: list[str] = []
    sub_ids: list[int] = []
    try:
        if not await _ws_subscribe_all(
            ws_client,
            event_types,
            handler,
            attached_handlers,
            sub_ids,
            description,
            identifier,
        ):
            return await _legacy_poll_until(
                identifier,
                sample,
                timeout=timeout,
                poll_interval=poll_interval,
                description=description,
            )

        start = time.monotonic()
        # Sample-after-subscribe: covers the "event fired before subscribe
        # landed" race. This is where most happy-path waits resolve.
        early_result, is_done = await _ws_post_subscribe_check(
            ws_client, sample, start, timeout, poll_interval, description, identifier
        )
        if is_done:
            return early_result

        return await _ws_run_wait_loop(
            ws_client,
            sample,
            nudge,
            start,
            timeout,
            poll_interval,
            description,
            identifier,
        )
    finally:
        await _ws_cleanup(ws_client, attached_handlers, sub_ids, handler)


async def wait_for_entity_registered(
    client: Any,
    entity_id: str,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
) -> bool:
    """
    Wait until an entity is registered and accessible via the state API.

    Used after config create/update operations to confirm the entity is queryable.
    Listens to ``state_changed`` and ``entity_registry_updated`` events on the
    WebSocket and falls back to REST polling (every ``poll_interval`` seconds)
    when the WebSocket is unavailable. See the module-level note above for the
    subscribe→sample→wait pattern and the failure mode it addresses (#1152).

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity ID to wait for (e.g., 'automation.morning_routine')
        timeout: Maximum time to wait in seconds
        poll_interval: REST poll interval used for the WS-unavailable fallback

    Returns:
        True if entity became accessible, False if timed out
    """

    async def sample() -> bool | None:
        try:
            state = await client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                return None
            logger.warning(f"Unexpected API error sampling {entity_id}: {e}")
            return None
        return True if state else None

    result = await _ws_wait_for_condition(
        client,
        entity_id,
        sample,
        # entity_registry_updated fires when the registry row is added,
        # state_changed when the state machine row hydrates. We watch both
        # so the post-event sample lands as soon as either side completes.
        event_types=("state_changed", "entity_registry_updated"),
        timeout=timeout,
        poll_interval=poll_interval,
        description="entity registration",
    )
    if result is True:
        return True
    logger.warning(f"Entity {entity_id} not registered within {timeout}s")
    return False


async def wait_for_entity_removed(
    client: Any,
    entity_id: str,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
) -> bool:
    """
    Wait until an entity is no longer accessible via the state API.

    Used after config delete operations to confirm the entity is gone. Listens
    to ``state_changed`` and ``entity_registry_updated`` removal events on the
    WebSocket and falls back to REST polling (every ``poll_interval`` seconds)
    when the WebSocket is unavailable. See #1152 for context.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity ID to wait for removal
        timeout: Maximum time to wait in seconds
        poll_interval: REST poll interval used for the WS-unavailable fallback

    Returns:
        True if entity was removed, False if timed out (entity still exists)
    """

    async def sample() -> bool | None:
        try:
            state = await client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                return True
            logger.warning(f"Unexpected API error sampling {entity_id} removal: {e}")
            return None
        # Falsy state == entity is gone from the state machine.
        return True if not state else None

    result = await _ws_wait_for_condition(
        client,
        entity_id,
        sample,
        event_types=("state_changed", "entity_registry_updated"),
        timeout=timeout,
        poll_interval=poll_interval,
        description="entity removal",
    )
    if result is True:
        return True
    logger.warning(f"Entity {entity_id} still exists after {timeout}s")
    return False


async def _sample_state_change(
    client: Any,
    entity_id: str,
    expected_state: str | None,
    baseline: dict[str, str | None],
) -> dict[str, Any] | None:
    """Sample entity state for wait_for_state_change; returns state dict on match."""
    try:
        raw = await client.get_entity_state(entity_id)
    except HomeAssistantAPIError as e:
        logger.debug(f"API error sampling {entity_id} state: {e}")
        return None
    if not isinstance(raw, dict):
        return None
    current = raw.get("state")
    if expected_state is not None and current == expected_state:
        return raw
    if (
        expected_state is None
        and baseline["state"] is not None
        and current != baseline["state"]
    ):
        return raw
    if expected_state is None and baseline["state"] is None and current is not None:
        baseline["state"] = current
    return None


async def wait_for_state_change(
    client: Any,
    entity_id: str,
    expected_state: str | None = None,
    timeout: float = 10.0,
    poll_interval: float = 0.3,
    initial_state: str | None = None,
) -> dict[str, Any] | None:
    """
    Wait until an entity's state changes (optionally to a specific value).

    Used after service calls to verify the operation took effect. Listens to
    ``state_changed`` events on the WebSocket and falls back to REST polling
    (every ``poll_interval`` seconds) when the WebSocket is unavailable. See
    #1152 for context.

    Args:
        client: HomeAssistantClient instance
        entity_id: Entity to monitor
        expected_state: If set, wait for this specific state value.
                        If None, wait for any change from initial_state.
        timeout: Maximum time to wait in seconds
        poll_interval: REST poll interval used for the WS-unavailable fallback
        initial_state: The state before the operation. If None, it will be
                       fetched automatically.

    Returns:
        The entity state dict if the change was detected, None if timed out
    """
    if initial_state is None:
        try:
            raw_initial = await client.get_entity_state(entity_id)
            if isinstance(raw_initial, dict):
                initial_state = raw_initial.get("state")
        except HomeAssistantAPIError:
            logger.debug(
                f"Could not fetch initial state for {entity_id} — will detect any change"
            )
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
            logger.warning(
                f"Connection/auth error fetching initial state for {entity_id}: {e}"
            )
            raise

    # Mutable closure cell so the sampler can adopt the first observed state
    # as the baseline when the initial fetch failed — matches the original
    # REST-loop semantics.
    baseline: dict[str, str | None] = {"state": initial_state}

    async def sample() -> dict[str, Any] | None:
        return await _sample_state_change(client, entity_id, expected_state, baseline)

    result = await _ws_wait_for_condition(
        client,
        entity_id,
        sample,
        event_types=("state_changed",),
        timeout=timeout,
        poll_interval=poll_interval,
        description="state change",
    )
    if isinstance(result, dict):
        return result
    logger.warning(f"Entity {entity_id} state did not change within {timeout}s")
    return None


async def _discover_automation_sample(
    client: Any,
    unique_id: str,
    captured: dict[str, str | None],
) -> str | None:
    """Sample get_states() looking for an automation whose attributes.id matches unique_id."""
    if captured["entity_id"] is not None:
        return captured["entity_id"]
    try:
        states = await client.get_states()
    except HomeAssistantAPIError as e:
        logger.debug(f"API error sampling get_states() for unique_id {unique_id}: {e}")
        captured["last_api_error"] = str(e)
        return None
    for state in states:
        entity_id = state.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith("automation."):
            continue
        if state.get("attributes", {}).get("id") == unique_id:
            return entity_id
    return None


def _automation_event_filter(
    event: dict[str, Any],
    unique_id: str,
    captured: dict[str, str | None],
) -> bool:
    """Filter state_changed events to those matching an automation by unique_id.

    Defensive isinstance guards mirror the sample() callback — the WS dispatcher
    swallows handler exceptions broadly, so a malformed payload reaching
    .startswith would silently fail to nudge and the wait would time out.
    """
    data = event.get("data") or {}
    evt_entity = data.get("entity_id")
    if not isinstance(evt_entity, str) or not evt_entity.startswith("automation."):
        return False
    new_state = data.get("new_state") or {}
    attrs = new_state.get("attributes") if isinstance(new_state, dict) else None
    if not isinstance(attrs, dict) or attrs.get("id") != unique_id:
        return False
    # Guard against last-writer-wins collision (HA forbids duplicate unique_id,
    # but don't coin-flip silently if it ever happens).
    if captured["entity_id"] is None:
        captured["entity_id"] = evt_entity
    elif captured["entity_id"] != evt_entity:
        logger.warning(
            "Duplicate automation match for unique_id %s: %s already captured, ignoring %s",
            unique_id,
            captured["entity_id"],
            evt_entity,
        )
    return True


async def wait_for_automation_entity_by_unique_id(
    client: Any,
    unique_id: str,
    timeout: float = 6.0,
    poll_interval: float = 0.3,
) -> str | None:
    """
    Discover the entity_id assigned to a newly-created automation by unique_id.

    Used after ``POST /config/automation/config/{unique_id}`` to resolve the
    ``automation.<slug>`` entity_id Home Assistant assigned. Listens to
    ``state_changed`` events filtered to ``automation.*`` entities whose
    ``new_state.attributes.id`` equals ``unique_id`` — HA's
    ``BaseAutomationEntity.capability_attributes`` exposes ``unique_id`` as
    ``CONF_ID`` on every emit, so the first state event for a fresh
    automation carries the match. Falls back to REST polling of
    ``get_states()`` when the WebSocket is unavailable. See #1152 / #1395.

    Args:
        client: HomeAssistantClient instance
        unique_id: The unique_id passed to ``POST /config/automation/config/{unique_id}``
        timeout: Maximum time to wait in seconds (preserves the legacy 6s budget)
        poll_interval: REST poll interval used for the WS-unavailable fallback

    Returns:
        The discovered entity_id (e.g. ``"automation.morning_routine"``)
        or ``None`` on timeout.
    """
    # Mutable cells shared between sample and event_filter.
    # ``entity_id``: stashes the discovered entity_id when filter sees a match.
    # ``last_api_error``: tracks REST failures for the timeout warning.
    captured: dict[str, str | None] = {"entity_id": None, "last_api_error": None}

    async def sample() -> str | None:
        return await _discover_automation_sample(client, unique_id, captured)

    def event_filter(event: dict[str, Any]) -> bool:
        return _automation_event_filter(event, unique_id, captured)

    result = await _ws_wait_for_condition(
        client,
        identifier=f"automation[unique_id={unique_id}]",
        sample=sample,
        event_types=("state_changed",),
        timeout=timeout,
        poll_interval=poll_interval,
        description="automation entity discovery",
        event_filter=event_filter,
    )
    if isinstance(result, str):
        return result
    # `_ws_wait_for_condition` / `_legacy_poll_until` already logged the
    # generic "timed out" warning before returning None; just surface the
    # discovery-specific signal when REST sampling was wedged the whole
    # budget so operators can distinguish "automation never published"
    # from "REST channel down."
    if captured["last_api_error"] is not None:
        logger.warning(
            "Automation discovery for unique_id %s timed out with every "
            "REST sample failing; last error: %s",
            unique_id,
            captured["last_api_error"],
        )
    return None
