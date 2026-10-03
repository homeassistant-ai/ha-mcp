"""The ``call_service`` write command: dispatch and state confirmation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .ws_constants import CALL_SERVICE_DEFAULT_TIMEOUT
from .ws_registry import _state_as_dict, _state_get

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(f"{__package__}.websocket_api")


# =============================================================================
# ha_mcp_tools/call_service  (the first WRITE capability — Phase 3, issue #1813)
# =============================================================================
def _do_call_service(
    hass: HomeAssistant, params: dict[str, Any], *, result: dict[str, Any]
) -> dict[str, Any]:
    """Pure sync formatter for ``call_service``.

    ALL of the work — the authoritative domain block, the ``ServiceNotFound``
    check, the pre-state capture, the expected-aware register-before-fire listener,
    the single ``async_call`` dispatch, the immediate-match, and the bounded
    confirmation wait — happens in the async :func:`_call_service_prep`, which hands
    the finished result dict in as ``result``. This function only returns that
    envelope (mirroring every other ``_do_*``: the WS wrapper's ``send_result`` adds
    the outer success frame), so no awaiting / blocking work ever runs in a ``_do_*``
    step (D2).
    """
    return result


async def _call_service_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Do all of ``call_service``'s async work; return ``{"result": <envelope>}``.

    The order is load-bearing:

    1. **D1 — authoritative domain block (security-critical).** Refuse
       ``domain == "ha_mcp_tools"`` (case/whitespace-normalized) BEFORE any
       ``has_service`` / dispatch. This is enforced HERE, in the component that
       fires the call, independent of (and in addition to) the server-side guard:
       a component ``call_service`` that skipped it would let a caller invoke the
       admin-gated ``ha_mcp_tools.get_caller_token`` in-process (the server IS
       admin) and then every file/YAML service. The block keys off the RESOLVED
       domain, so it holds no matter which path reaches this function.
    2. **ServiceNotFound** before dispatch, so an unknown service is a clean
       ``SERVICE_NOT_FOUND`` and never a landed-but-unreported write.
    3. Pre-state capture for each ``entity_id`` (a synchronous in-memory read). A
       target whose captured state is ``None`` — absent from the state machine, so
       it structurally cannot ever emit a ``state_changed`` for this dispatch — is
       excluded from the wait entirely (:func:`_confirmable_entity_ids`); waiting on
       it would only burn the full ``timeout`` to learn what the pre-state already
       proved. An ``"unavailable"`` target stays IN the wait (unlike a nonexistent
       one, it can legitimately reconnect and transition mid-dispatch — excluding it
       too would silently miss that), so ``should_confirm`` itself stays keyed off
       the full ``entity_ids`` (intent to confirm), not the narrower confirmable
       subset — a ``validate_first=False`` caller that intentionally skips the
       not-found/unavailable error mapping still needs ``partial=True`` on a
       genuinely-excluded target, not a bare unconfirmed-but-not-partial result.
    4. Register the expected-aware ``EVENT_STATE_CHANGED`` waiter BEFORE the dispatch
       (D5) so a fast entity's event can't arrive before the listener exists, scoped
       to only the confirmable targets from step 3. The waiter confirms only on
       reaching the server's ``expected_state`` hint (skipping intermediate/noise
       events); a ``None`` hint keeps any-first-event confirmation.
    5. Fire exactly ONE ``async_call`` (``blocking=True``); flip ``dispatched``
       immediately after so a post-dispatch problem is never retried as a failed
       call (D3/D9).
    6. Immediate-match (:func:`_match_immediate`) for an idempotent no-op — a target
       whose CURRENT state already equals its hint confirms with NO wait — then a
       bounded wait for whatever is still unconfirmed (D4); expiry is ``partial``.
    7. Diff pre→post into the real transition(s) — the REAL observed transition, never
       the hint value.

    Raised exceptions PROPAGATE — the WS handler turns them into a command error the
    server maps (D7). Two distinct classes propagate: the D1 domain block and
    ``ServiceNotFound`` are PRE-dispatch (they raise before ``async_call``, so nothing
    landed); an ``async_call`` / ``return_response`` validation error is MID-dispatch
    (the handler may mutate state and THEN raise — the documented D9 at-most-once
    residual, NOT pre-dispatch). A confirmation timeout is caught and reported as
    ``partial`` (never re-raised): the call already landed.

    The whole POST-dispatch section — the immediate-match re-read, the wait, and the
    pre→post diff — runs inside ONE ``try`` that, once ``dispatched`` is True, never
    lets a raise escape (I1): a raise mapped to a command error would re-POST an
    already-landed write. A pre-confirmation ``async_call`` failure (``dispatched``
    still False) re-raises so the server can map it; any post-dispatch failure degrades
    to a minimal dispatched-but-unconfirmed envelope. The immediate-match re-read is
    itself raise-proof (:func:`_match_immediate`), the attribute diff is raise-proofed
    (:func:`_values_differ`), and ``unsub`` always runs in the ``finally``.
    Serialization residual (bounded, no sanitize pass): the transition embeds each
    state's ``as_dict()`` and the WS transport re-encodes it; HA core enforces
    JSON-serializable state attributes for its own REST/WS/recorder APIs, so a state
    that reached the component already serializes — re-encoding it here is safe.
    """
    domain = msg["domain"]
    service = msg["service"]

    # 1./2. Pre-dispatch guards (D1 domain block + ServiceNotFound) — both raise
    # BEFORE any listener registration or dispatch, so a refused call is never a
    # landed-but-unreported write and ``async_call`` is provably never reached.
    _guard_call_service_target(hass, domain, service)

    service_data = msg.get("service_data") or {}
    entity_ids = list(msg.get("entity_ids") or [])
    wait = msg.get("wait", True)
    timeout = msg.get("timeout", CALL_SERVICE_DEFAULT_TIMEOUT)
    return_response = msg.get("return_response", False)
    # The server's confirmation HINT (``_SERVICE_TO_STATE.get(service)``), applied to
    # every confirmation target. Absent / None keeps any-first-event confirmation.
    expected_state = msg.get("expected_state")
    expected_by_entity = dict.fromkeys(entity_ids, expected_state)

    # should_confirm stays keyed off the full entity_ids (intent to confirm) — see
    # the docstring's step 3 for why this must NOT narrow to the confirmable subset.
    should_confirm = bool(wait and entity_ids)

    # 3. Pre-state capture (synchronous in-memory reads, guarded against drift).
    pre = {eid: _state_as_dict(_state_get(hass, eid)) for eid in entity_ids}
    # Only a target whose pre-dispatch state proves it can possibly report a
    # confirming event is worth actually waiting on (see
    # ``_confirmable_entity_ids``) — a target absent from the state machine can
    # never emit one, so waiting on it is certain to burn the full ``timeout`` for
    # no new information; the server reads the certain ``None`` old_state straight
    # off the transition instead.
    confirmable_entity_ids = _confirmable_entity_ids(entity_ids, pre)

    # 4. Register-before-fire (D5): only when there is something worth confirming.
    evt: Any = None
    captured: dict[str, Any] = {}
    unsub: Any = None
    if should_confirm and confirmable_entity_ids:
        evt, captured, unsub = _register_transition_waiter(
            hass, set(confirmable_entity_ids), expected_by_entity
        )

    # 5. Dispatch exactly once. 6. Immediate-match + bounded wait. 7. Build the diff.
    # Everything after ``dispatched = True`` is inside this ONE try so no post-dispatch
    # raise (a drifted re-read, an exotic-attribute diff) escapes (I1) — that would be
    # mapped to legacy and re-POST an already-landed write. ``unsub`` always runs.
    response: Any = None
    dispatched = False
    result: dict[str, Any]
    try:
        response = await hass.services.async_call(
            domain,
            service,
            dict(service_data),
            blocking=True,
            return_response=return_response,
        )
        dispatched = True
        # ``evt`` is None when nothing was worth waiting on (should_confirm was
        # True but every target was excluded as unconfirmable) — there is then
        # nothing that could ever confirm, so skip the wait outright rather than
        # awaiting an event that was never registered to fire.
        if should_confirm and evt is not None:
            await _await_confirmation(
                hass, confirmable_entity_ids, expected_by_entity, captured, evt, timeout
            )
        result = _build_call_service_result(
            hass,
            domain,
            service,
            entity_ids,
            pre,
            captured,
            should_confirm=should_confirm,
            dispatched=dispatched,
            return_response=return_response,
            response=response,
        )
    except Exception:
        # PRE-confirmation ``async_call`` failure (never dispatched) → re-raise so the
        # server maps it (D7/D9 MID-dispatch residual). Any POST-dispatch failure →
        # degrade to a minimal dispatched-but-unconfirmed envelope (never re-POSTed).
        if not dispatched:
            raise
        _LOGGER.exception(
            "call_service post-dispatch step failed after dispatch; returning "
            "dispatched-but-unconfirmed envelope (%s.%s)",
            domain,
            service,
        )
        result = _dispatched_unconfirmed_result(domain, service)
    finally:
        if unsub is not None:
            unsub()
    return {"result": result}


def _confirmable_entity_ids(entity_ids: list[str], pre: Mapping[str, Any]) -> list[str]:
    """Targets whose pre-dispatch state proves they can possibly confirm.

    A target absent from the state machine (``pre[eid] is None``) can never emit a
    confirming ``state_changed`` for this dispatch — HA no-ops a service call for
    an entity id that matches nothing, and nothing will register that id mid-call
    either. Excluding it from the wait lets the server read the certain outcome
    straight off the transition's ``None`` ``old_state`` instead of burning the
    full timeout to learn nothing new.

    Deliberately NOT excluded: a target whose captured state is ``"unavailable"``.
    Unlike a nonexistent id, an unavailable entity can legitimately reconnect and
    transition during the blocking dispatch (the very case ``ENTITY_UNAVAILABLE``
    exists to distinguish from a real failure would itself go undetected if the
    listener were never registered) — so it stays in the wait and is judged by
    whether it actually confirmed, not excluded upfront.
    """
    return [eid for eid in entity_ids if pre.get(eid) is not None]


def _guard_call_service_target(hass: HomeAssistant, domain: str, service: str) -> None:
    """Pre-dispatch refusals for ``call_service`` — raise BEFORE any dispatch.

    * **D1 (security-critical)** — refuse ``domain == "ha_mcp_tools"``
      (case/whitespace-normalized). Enforced HERE, in the component that fires the
      call, independent of (and in addition to) the server-side guard: a component
      ``call_service`` that skipped it would let a caller invoke the admin-gated
      ``ha_mcp_tools.get_caller_token`` in-process (the server IS admin) and then
      every file/YAML service. The block keys off the RESOLVED domain, so it holds
      no matter which path reaches this function.
    * **ServiceNotFound** — an unknown service is a clean ``SERVICE_NOT_FOUND``, not
      a phantom write. Both raises propagate to the WS handler (D7).
    """
    if str(domain).strip().lower() == DOMAIN:
        from homeassistant.exceptions import HomeAssistantError

        raise HomeAssistantError(
            "the ha_mcp_tools domain is not callable through call_service; "
            "use the dedicated ha_* tools"
        )
    if not hass.services.has_service(domain, service):
        from homeassistant.exceptions import ServiceNotFound

        raise ServiceNotFound(domain, service)


def _event_state_value(state: Any) -> Any:
    """The primary state string from a ``State`` object OR an ``as_dict()`` mapping.

    Raise-proof: an exotic/stub shape (or a ``.state`` accessor that raises) degrades
    to ``None`` so the expected-aware waiter and the post-dispatch immediate-match can
    never propagate past the dispatch (I1). ``None`` never equals a (str) expected
    hint, so an unreadable state simply does not confirm — it keeps waiting.
    """
    try:
        if isinstance(state, Mapping):
            return state.get("state")
        return getattr(state, "state", None)
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive; exotic/stub shapes
        return None


def _register_transition_waiter(
    hass: HomeAssistant, target_set: set[str], expected_by_entity: Mapping[str, Any]
) -> tuple[Any, dict[str, Any], Any]:
    """Register the ``EVENT_STATE_CHANGED`` listener BEFORE the dispatch (D5).

    Returns ``(evt, captured, unsub)``: ``evt`` is set once every id in
    ``target_set`` has reported a CONFIRMING ``new_state``; ``captured`` maps each id
    to its raw new_state; ``unsub`` tears the listener down. Registering before the
    dispatch closes the race where a fast entity's event arrives before the
    listener exists.

    ``expected_by_entity`` supplies each target's server-computed expected-state HINT
    (``_SERVICE_TO_STATE``). With a hint, ONLY the event that reaches that state
    confirms — a multi-phase service's intermediate states (``lock``:
    unlocked→locking→locked) and attribute-only noise (a ``media_player`` position
    tick while ``state`` stays "playing") are skipped, NOT captured. With no hint
    (``None``, e.g. ``set_temperature``) any first ``new_state`` confirms — today's
    unchanged behavior.
    """
    import asyncio

    from homeassistant.const import EVENT_STATE_CHANGED

    evt = asyncio.Event()
    captured: dict[str, Any] = {}

    def _on_change(event: Any) -> None:
        data = getattr(event, "data", None) or {}
        eid = data.get("entity_id")
        new = data.get("new_state")
        # M-newstate-none: a state_changed with new_state=None means the entity was
        # REMOVED mid-wait — that is not a confirmed transition, so do NOT capture it
        # (leaving the target uncaptured keeps the op ``partial``, not falsely
        # confirmed). ``_post_state`` still re-reads a best-available current state.
        if eid in target_set and new is not None:
            exp = expected_by_entity.get(eid)
            # Hint present → confirm ONLY on reaching the expected state (skip
            # intermediate/noise events). Hint None → any first event confirms.
            if exp is None or _event_state_value(new) == exp:
                captured[eid] = new
                if target_set <= set(captured):
                    evt.set()

    # Mark the listener a HA callback so ``EventBus.async_listen`` classifies its
    # ``HassJob`` as ``HassJobType.Callback`` and runs it INLINE on the event loop
    # (``is_callback`` reads exactly ``getattr(func, "_hass_callback", False)``).
    # Without this a plain function is ``HassJobType.Executor``: every instance-wide
    # ``state_changed`` gets thrown at the thread pool, and ``evt.set()`` then runs
    # cross-thread on a non-thread-safe ``asyncio.Event`` → delayed/spurious
    # ``partial`` confirmations and an ``InvalidStateError`` race with the
    # timeout-cancel. We set the attribute directly rather than using ``@callback``:
    # the unit-test harness MagicMock-stubs ``homeassistant.core``, so the decorator
    # would be a MagicMock and break the listener, whereas the plain attribute set is
    # exactly what ``callback`` does (``func.__dict__["_hass_callback"] = True``) and
    # is inert under the stub.
    _on_change._hass_callback = True  # type: ignore[attr-defined]

    unsub = hass.bus.async_listen(EVENT_STATE_CHANGED, _on_change)
    return evt, captured, unsub


def _match_immediate(
    hass: HomeAssistant,
    entity_ids: list[str],
    expected_by_entity: Mapping[str, Any],
    captured: dict[str, Any],
) -> None:
    """Capture a target whose CURRENT state already equals its expected hint.

    Mirrors legacy's "sample current state first": for each not-yet-captured target
    with a known expected state, re-read the live state right after ``async_call``
    returns and, if it already equals the expected value, capture it as confirmation
    so NO wait is needed — a ``turn_on`` on an already-on light confirms instantly
    (pre == expected == "on") instead of timing out to a false ``partial``. No-hint
    targets (``exp is None``) are left for the any-first-event waiter — unchanged.

    Called AFTER the dispatch, so it MUST be raise-proof (I1): a re-read that raised
    and reached the WS handler would be mapped to legacy and re-POST an already-landed
    write. ``_state_get`` is guarded (``None`` on drift) and ``_event_state_value`` is
    raise-proof, so this never propagates past dispatch. ``captured`` is mutated in
    place with the raw ``State`` (``_post_state``/``_state_as_dict`` normalize it, the
    same shape the waiter captures).
    """
    for eid in entity_ids:
        exp = expected_by_entity.get(eid)
        if exp is None or eid in captured:
            continue
        cur = _state_get(hass, eid)
        if cur is not None and _event_state_value(cur) == exp:
            captured[eid] = cur


async def _await_confirmation(
    hass: HomeAssistant,
    entity_ids: list[str],
    expected_by_entity: Mapping[str, Any],
    captured: dict[str, Any],
    evt: Any,
    timeout: float,
) -> None:
    """Immediate-match the idempotent no-ops, then bounded-wait whatever remains (D4).

    Runs AFTER the single ``async_call``: :func:`_match_immediate` captures a target
    whose current state already equals its hint (a ``turn_on`` on an already-on light)
    with NO wait; the bounded wait runs only if some target is still unconfirmed and
    its expiry is swallowed (``partial``, never a failure). Kept as its own helper so
    :func:`_call_service_prep` stays under the complexity gate; raise-proof re-read
    (``_match_immediate``), so nothing here escapes past the dispatch (I1).
    """
    import asyncio

    _match_immediate(hass, entity_ids, expected_by_entity, captured)
    if set(entity_ids) <= set(captured):
        return  # every target confirmed by the immediate-match — no wait needed
    try:
        await asyncio.wait_for(evt.wait(), timeout)
    except TimeoutError:
        pass  # partial confirmation, not a failure (D4)


def _build_call_service_result(
    hass: HomeAssistant,
    domain: str,
    service: str,
    entity_ids: list[str],
    pre: Mapping[str, Any],
    captured: Mapping[str, Any],
    *,
    should_confirm: bool,
    dispatched: bool,
    return_response: bool,
    response: Any,
) -> dict[str, Any]:
    """Assemble the ``call_service`` response envelope from the captured transition.

    ``confirmed`` is True only when every target reported within the wait;
    ``partial`` is a confirmation that lapsed (never a failure). ``dispatched`` is
    only ever ``True`` here (a pre-dispatch problem raised out of the prep);
    reporting the flag rather than a literal keeps the D9 at-most-once boundary —
    "reached this shape ⇒ the single async_call fired" — explicit for the server,
    which never retries a dispatched write. ``service_response`` is present only
    when it was both requested AND non-``None``.
    """
    transitions = [
        _call_service_transition(eid, pre.get(eid), _post_state(hass, eid, captured))
        for eid in entity_ids
    ]
    # Against the FULL entity_ids, not just the confirmable subset: an excluded
    # (nonexistent) target can never land in captured, so this naturally stays
    # False whenever one is present — exactly right, since it never confirmed.
    confirmed = bool(should_confirm and set(entity_ids) <= set(captured))
    result: dict[str, Any] = {
        "domain": domain,
        "service": service,
        "dispatched": dispatched,
        "confirmed": confirmed,
        "partial": bool(should_confirm and not confirmed),
        "transitions": transitions,
    }
    if return_response and response is not None:
        result["service_response"] = response
    return result


def _dispatched_unconfirmed_result(domain: str, service: str) -> dict[str, Any]:
    """Minimal ``call_service`` envelope when post-dispatch formatting raised (I1).

    The single ``async_call`` already fired; building the rich transition raised (an
    exotic captured state / serialization edge). Report dispatched-but-unconfirmed with
    no transitions rather than propagating — a propagated raise would become a command
    error the server maps to legacy and re-POST an already-landed write (double-apply).
    Reads only the plain domain/service strings, so it cannot itself raise.
    """
    return {
        "domain": domain,
        "service": service,
        "dispatched": True,
        "confirmed": False,
        "partial": True,
        "transitions": [],
    }


def _post_state(
    hass: HomeAssistant, entity_id: str, captured: Mapping[str, Any]
) -> Any:
    """The post-dispatch state for ``entity_id`` as a plain dict (or ``None``).

    Prefers the listener-captured ``new_state`` (the event that confirmed the
    transition, normalized through the shared :func:`_state_as_dict`); falls back
    to a fresh guarded ``hass.states`` re-read when the target did not report within
    the wait (the ``partial`` case), so a transition row is still populated with the
    best-available current state. Both ``None`` (a vanished/stateless entity) and
    core drift degrade to ``None`` rather than raising.
    """
    if entity_id in captured:
        as_dict = _state_as_dict(captured[entity_id])
        if as_dict is not None:
            return as_dict
    return _state_as_dict(_state_get(hass, entity_id))


def _values_differ(a: Any, b: Any) -> bool:
    """Whether two attribute values differ, raise-proof for array-like values.

    A plain ``a != b`` raises "truth value ... is ambiguous" for numpy arrays and
    other exotic ``__ne__`` results that aren't a bool. Post-dispatch formatting MUST
    NOT raise (a raise here would surface as a command error the server maps to legacy
    → a re-POST of an already-landed write), so fall back to a ``repr`` compare when
    the direct compare's truthiness is not a plain bool.
    """
    try:
        return bool(a != b)
    except Exception:  # noqa: BLE001  # array-like / exotic __ne__ whose result isn't a plain bool
        return repr(a) != repr(b)


def _call_service_transition(
    entity_id: str,
    old_state: dict[str, Any] | None,
    new_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """The real pre→post transition for one target entity.

    ``changed`` compares the top-level ``state`` (always a plain string — safe);
    ``attributes_changed`` lists the attribute keys whose values differ (added/removed
    keys included), compared through :func:`_values_differ` so an array-like attribute
    value cannot raise. Both sides may be ``None`` (a stateless or vanished entity),
    which the ``or {}`` guards fold into an all-``None`` comparison rather than raising.
    """
    old = old_state or {}
    new = new_state or {}
    old_attrs = old.get("attributes") or {}
    new_attrs = new.get("attributes") or {}
    attributes_changed = sorted(
        key
        for key in set(old_attrs) | set(new_attrs)
        if _values_differ(old_attrs.get(key), new_attrs.get(key))
    )
    return {
        "entity_id": entity_id,
        "old_state": old_state,
        "new_state": new_state,
        "changed": old.get("state") != new.get("state"),
        "attributes_changed": attributes_changed,
    }
