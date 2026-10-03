"""The ``bulk_call_service`` write command."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .call_service import (
    _call_service_transition,
    _confirmable_entity_ids,
    _guard_call_service_target,
    _match_immediate,
    _post_state,
    _register_transition_waiter,
)
from .constants import CALL_SERVICE_DEFAULT_TIMEOUT
from .registry import _state_as_dict, _state_get

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(__package__)


# =============================================================================
# ha_mcp_tools/bulk_call_service  (the BATCH write capability — Phase 3, D5a)
# =============================================================================
def _do_bulk_call_service(
    hass: HomeAssistant, params: dict[str, Any], *, result: dict[str, Any]
) -> dict[str, Any]:
    """Pure sync formatter for ``bulk_call_service``.

    Like :func:`_do_call_service`, ALL of the work — the per-op D1 domain block,
    the ``ServiceNotFound`` checks, the expected-aware register-before-fire pass, the
    dispatches, the per-op immediate-match, and the one bounded batch wait — happens
    in the async :func:`_bulk_call_service_prep`, which hands the finished envelope in
    as ``result``. This function only returns it, so no awaiting / blocking work runs
    in a ``_do_*`` step (D2).
    """
    return result


async def _bulk_call_service_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Do all of ``bulk_call_service``'s async work; return ``{"result": ...}``.

    Register-before-fire is trivially correct for the batch: every listener is
    registered in one synchronous pass BEFORE any ``async_call`` is issued, so no
    op's confirming event can arrive before its listener exists. The order is
    load-bearing:

    1. **D1 batch fail-closed (security-critical).** Run
       :func:`_guard_call_service_target` for EVERY operation FIRST — before any
       pre-state read, listener, or dispatch. A single op targeting the
       ``ha_mcp_tools`` domain (or an unknown service) makes the WHOLE frame raise:
       no partial batch is dispatched, so no ``ha_mcp_tools.*`` op can ever slip
       through in a batch (register-before-fire + all-guards-first means a refused
       op aborts before any real write lands).
    2. Pre-state capture for every op's ``entity_ids`` (synchronous in-memory).
    3. Register ALL expected-aware confirmation listeners in one pass BEFORE any
       dispatch (each op's ``expected_state`` hint governs its waiter); every
       ``unsub`` is torn down in the ``finally``.
    4. Dispatch: ``parallel`` fans the ``async_call``s out through
       :func:`asyncio.gather` with ``return_exceptions=True`` so one op's failure
       does not abort the others (its exception is recorded on that op, NOT raised —
       UNLIKE the step-1 guards, which DO raise the whole frame pre-dispatch);
       ``parallel=False`` awaits them in order. Each op flips its own ``dispatched``
       flag the moment its ``async_call`` returns.
    5. Immediate-match per dispatched op (:func:`_bulk_match_immediate`) — an
       idempotent no-op whose current state already equals its hint confirms with no
       wait — then ONE shared bounded deadline (D4, not per-op serial timeouts) for
       every op still unconfirmed.
    6. Per-op pre→post diff, reusing the single-call transition/build helpers.
    7. Return every op's result plus batch counts.
    """
    operations = list(msg.get("operations") or [])
    parallel = bool(msg.get("parallel", True))
    wait = bool(msg.get("wait", True))
    timeout = msg.get("timeout", CALL_SERVICE_DEFAULT_TIMEOUT)

    # 1. D1 batch fail-closed: guard EVERY op before ANY pre-state / listener /
    #    dispatch. A refused op (``ha_mcp_tools`` domain or unknown service) raises
    #    the whole frame here, so nothing in the batch is ever dispatched partially.
    for op in operations:
        _guard_call_service_target(hass, op["domain"], op["service"])

    # 2. Normalize + pre-state capture (synchronous in-memory reads) per op.
    ops = [_bulk_op_record(hass, op, wait=wait) for op in operations]

    # 3. Register-before-fire (D5): every confirmable op's listener is registered in
    #    one synchronous pass BEFORE any dispatch; ALL unsubs torn down in finally.
    unsubs = _bulk_register_all(hass, ops)
    try:
        await _bulk_dispatch_all(hass, ops, parallel=parallel)  # 4
        _bulk_match_immediate(hass, ops)  # 5a immediate-match idempotent no-ops
        await _bulk_wait_all(ops, timeout)  # 5b (one shared deadline; expiry=partial)
    finally:
        for unsub in unsubs:
            unsub()

    # 6./7. Per-op pre→post diff + batch counts. Post-dispatch assembly MUST be total
    # (I1): the ops already fired, so a raise here would be mapped to legacy and
    # re-dispatch every landed op (double-fire). On any failure return a minimal
    # envelope reporting each op dispatched-but-unconfirmed (its real dispatched/error
    # preserved) instead of propagating.
    try:
        result = _build_bulk_result(hass, ops)
    except Exception:
        _LOGGER.exception(
            "bulk_call_service post-dispatch assembly failed after dispatch; "
            "returning dispatched-but-unconfirmed batch envelope"
        )
        result = _dispatched_unconfirmed_bulk_result(ops)
    return {"result": result}


def _bulk_op_record(
    hass: HomeAssistant, op: Mapping[str, Any], *, wait: bool
) -> dict[str, Any]:
    """A mutable working record for one batch operation (incl. its pre-state).

    Reads the resolved ``{domain, service, service_data?, entity_ids?,
    expected_state?}`` row defensively (the direct-prep tests pass raw dicts that
    never went through the schema, so the mutable defaults are re-applied here).
    ``pre`` is the synchronous in-memory pre-state per target; ``expected_by_entity``
    maps every target to this op's confirmation hint (``_SERVICE_TO_STATE``) so the
    waiter + immediate-match key off it; ``dispatched`` / ``error`` / ``response``
    start empty and are filled during dispatch. ``confirmable_entity_ids`` excludes
    ONLY a target whose captured pre-state is ``None`` (nonexistent) — it can never
    emit a confirming event, so it is never worth the shared wait (see
    ``_confirmable_entity_ids``). Deliberately NOT excluded from it: a target
    already ``"unavailable"``, which can legitimately reconnect and confirm
    mid-dispatch. ``should_confirm`` stays intent-level (``bool(wait and
    entity_ids)`` — the FULL list, not the confirmable subset): a
    ``validate_first=False`` caller that skips the not-found/unavailable error
    mapping still needs ``partial=True`` on a genuinely-excluded target, not a
    bare unconfirmed-but-not-partial result.
    """
    entity_ids = list(op.get("entity_ids") or [])
    expected_state = op.get("expected_state")
    pre = {eid: _state_as_dict(_state_get(hass, eid)) for eid in entity_ids}
    confirmable_entity_ids = _confirmable_entity_ids(entity_ids, pre)
    return {
        "domain": op["domain"],
        "service": op["service"],
        "service_data": op.get("service_data") or {},
        "entity_ids": entity_ids,
        "confirmable_entity_ids": confirmable_entity_ids,
        "expected_by_entity": dict.fromkeys(entity_ids, expected_state),
        # Intent-level (full entity_ids), NOT the confirmable subset — mirrors
        # ``_call_service_prep``'s should_confirm: a validate_first=False caller
        # that intentionally skips the not-found/unavailable error mapping still
        # needs partial=True on a genuinely-excluded target, not a bare
        # unconfirmed-but-not-partial result. confirmable_entity_ids scopes ONLY
        # which targets are actually worth registering a listener / waiting for.
        "should_confirm": bool(wait and entity_ids),
        "pre": pre,
        "evt": None,
        "captured": {},
        "dispatched": False,
        "response": None,
        "error": None,
    }


async def _bulk_dispatch_one(hass: HomeAssistant, op: dict[str, Any]) -> None:
    """Fire exactly one op's ``async_call`` and flip its ``dispatched`` flag.

    Mirrors the single-call dispatch (``blocking=True``), but bulk never requests a
    per-op ``return_response`` (D5a keeps the batch simple — the single
    ``call_service`` covers response-returning calls). ``dispatched`` is set only
    AFTER ``async_call`` returns, so a raise (captured per-op by the caller) leaves
    it ``False`` and the op is never counted as a landed write.
    """
    op["response"] = await hass.services.async_call(
        op["domain"],
        op["service"],
        dict(op["service_data"]),
        blocking=True,
        return_response=False,
    )
    op["dispatched"] = True


def _bulk_register_all(hass: HomeAssistant, ops: list[dict[str, Any]]) -> list[Any]:
    """Register every confirmable op's transition listener in one pass (D5).

    One synchronous sweep BEFORE any dispatch, so no op's confirming event can
    arrive before its listener exists. Each confirmable op is handed its own ``evt``
    / ``captured`` (mutating the op record); the returned ``unsub`` list is torn down
    in the prep's ``finally``. Non-confirmable ops register nothing.

    If ``async_listen`` raises mid-sweep (near-impossible in practice), every listener
    already registered in this pass is unsubbed before re-raising — the prep's
    ``try/finally`` has not been entered yet, so those would otherwise leak on the bus.
    """
    unsubs: list[Any] = []
    try:
        for op in ops:
            if op["should_confirm"] and op["confirmable_entity_ids"]:
                evt, captured, unsub = _register_transition_waiter(
                    hass, set(op["confirmable_entity_ids"]), op["expected_by_entity"]
                )
                op["evt"] = evt
                op["captured"] = captured
                unsubs.append(unsub)
    except Exception:
        for unsub in unsubs:
            unsub()
        raise
    return unsubs


async def _bulk_dispatch_all(
    hass: HomeAssistant, ops: list[dict[str, Any]], *, parallel: bool
) -> None:
    """Fire every op's dispatch, recording a per-op failure without aborting the batch.

    ``parallel`` fans the dispatches out through :func:`asyncio.gather` with
    ``return_exceptions=True`` so one op's ``async_call`` raising is captured on THAT
    op (``error`` set, ``dispatched`` left ``False``) and the others still run;
    ``parallel=False`` awaits them in order, catching each op's failure the same way.
    Neither mode propagates a per-op dispatch error — that is the whole point of the
    batch (UNLIKE the pre-dispatch D1/ServiceNotFound guards, which DO raise).
    """
    import asyncio

    if parallel:
        outcomes = await asyncio.gather(
            *(_bulk_dispatch_one(hass, op) for op in ops),
            return_exceptions=True,
        )
        for op, outcome in zip(ops, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                op["error"] = _bulk_op_error(outcome)
    else:
        for op in ops:
            try:
                await _bulk_dispatch_one(hass, op)
            except Exception as err:  # noqa: BLE001
                op["error"] = _bulk_op_error(err)


def _bulk_match_immediate(hass: HomeAssistant, ops: list[dict[str, Any]]) -> None:
    """Immediate-match every dispatched, confirmable op (idempotent no-ops).

    Runs the SAME raise-proof :func:`_match_immediate` per op AFTER the batch
    dispatch and BEFORE the shared wait: an op whose target already sits at its
    expected hint (a ``turn_on`` on an already-on light) is captured here so
    :func:`_bulk_wait_all` skips it — no full-timeout stall for a batch of no-ops.
    Raise-proof (``_match_immediate`` guards each re-read), so it cannot propagate
    past the batch dispatch (I1).
    """
    for op in ops:
        if op["should_confirm"] and op["dispatched"]:
            _match_immediate(
                hass,
                op["confirmable_entity_ids"],
                op["expected_by_entity"],
                op["captured"],
            )


async def _bulk_wait_all(ops: list[dict[str, Any]], timeout: float) -> None:
    """Bounded confirmation wait for the batch: ONE shared deadline (D4).

    Waits up to ``timeout`` for every dispatched, confirmable op's transition on a
    single shared deadline (not per-op serial timeouts). An op already FULLY captured
    by the immediate-match (:func:`_bulk_match_immediate`) is skipped — its ``evt`` was
    never ``set`` (the match populates ``captured`` directly), so waiting on it would
    stall the whole batch to the timeout. Expiry is swallowed — whichever ops did not
    report are ``partial`` (never a failure); the ops that did report stay confirmed.
    A batch with nothing left to confirm returns immediately.
    """
    import asyncio

    waiters = [
        op["evt"].wait()
        for op in ops
        if op["should_confirm"]
        and op["dispatched"]
        # evt is None when nothing was worth waiting on for this op (every
        # target was excluded as unconfirmable) — nothing could ever set it.
        and op["evt"] is not None
        and not (set(op["confirmable_entity_ids"]) <= set(op["captured"]))
    ]
    if not waiters:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*waiters), timeout)
    except TimeoutError:
        pass  # partial confirmation for whichever ops did not report (D4)


def _bulk_op_error(exc: BaseException) -> str:
    """A short, stable error string for a per-op dispatch failure.

    A per-op ``async_call`` exception under the batch is recorded here (never
    propagated past the frame — the other ops still return their results), so the
    server can surface which op failed and why without the whole batch aborting.
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _build_bulk_op_result(hass: HomeAssistant, op: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble one op's result envelope from its captured transition.

    Reuses the single-call :func:`_call_service_transition` / :func:`_post_state`
    diff helpers so the per-op transition shape is byte-identical to
    ``call_service``. ``confirmed`` requires the op to have DISPATCHED and every
    target to have reported within the shared wait; ``partial`` is a dispatched-but
    -unconfirmed op (never a failure); an op whose ``async_call`` raised carries
    ``error`` with ``dispatched: false`` and is neither confirmed nor partial.
    """
    entity_ids = list(op["entity_ids"])
    captured = op["captured"]
    should_confirm = op["should_confirm"]
    dispatched = op["dispatched"]
    transitions = [
        _call_service_transition(
            eid, op["pre"].get(eid), _post_state(hass, eid, captured)
        )
        for eid in entity_ids
    ]
    # Against the FULL entity_ids, not just the confirmable subset: an excluded
    # (nonexistent) target can never land in captured, so this naturally stays
    # False whenever one is present — exactly right, since it never confirmed.
    confirmed = bool(should_confirm and dispatched and set(entity_ids) <= set(captured))
    result: dict[str, Any] = {
        "domain": op["domain"],
        "service": op["service"],
        "entity_ids": entity_ids,
        "dispatched": dispatched,
        "confirmed": confirmed,
        "partial": bool(should_confirm and dispatched and not confirmed),
        "transitions": transitions,
    }
    if op["error"] is not None:
        result["error"] = op["error"]
    return result


def _build_bulk_result(
    hass: HomeAssistant, ops: list[dict[str, Any]]
) -> dict[str, Any]:
    """The batch envelope: every op's result plus the batch counts.

    ``dispatched`` counts ops whose single ``async_call`` fired; ``failed`` counts
    ops that recorded a per-op ``error`` (dispatch raised). ``total`` is the batch
    size, so ``total - dispatched`` is the refused/failed-before-landing count.
    """
    op_results = [_build_bulk_op_result(hass, op) for op in ops]
    return {
        "operations": op_results,
        "total": len(op_results),
        "dispatched": sum(1 for r in op_results if r["dispatched"]),
        "failed": sum(1 for r in op_results if r.get("error") is not None),
    }


def _dispatched_unconfirmed_bulk_result(
    ops: list[dict[str, Any]],
) -> dict[str, Any]:
    """Minimal batch envelope when post-dispatch assembly raised (I1 total-formatting).

    Every op already fired (or recorded a pre-landing ``error``); building the rich
    transition rows raised (an exotic attribute value / serialization edge). Report
    each op with empty transitions rather than propagating — a propagated raise would
    become a command error the server maps to legacy and re-dispatch every landed op
    (double-fire). Reads only each record's plain ``domain``/``service``/``entity_ids``
    /``dispatched``/``should_confirm``/``error`` fields (never the rich captured
    state), so it cannot itself raise; the real per-op ``dispatched``/``error`` are
    preserved so a genuinely-failed op is not misreported as landed.
    """
    op_results = [
        {
            "domain": op["domain"],
            "service": op["service"],
            "entity_ids": list(op["entity_ids"]),
            "dispatched": op["dispatched"],
            "confirmed": False,
            "partial": bool(op["should_confirm"] and op["dispatched"]),
            "transitions": [],
            **({"error": op["error"]} if op["error"] is not None else {}),
        }
        for op in ops
    ]
    return {
        "operations": op_results,
        "total": len(op_results),
        "dispatched": sum(1 for r in op_results if r["dispatched"]),
        "failed": sum(1 for r in op_results if r.get("error") is not None),
    }
