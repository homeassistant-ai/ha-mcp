"""A service call's response data: Core's reply envelope and its refusals."""

from __future__ import annotations

from typing import Any

from .component_api import COMPONENT_INSTALL_HINT

#: Emitted when a ``return_response=true`` reply is not HA's
#: ``{"changed_states": [...], "service_response": ...}`` envelope. Without it an
#: empty ``result`` reads as an affirmative "no entity states changed" rather than
#: "the records could not be separated out". Module-level so the tests that
#: pin the behaviour assert against this exact string instead of a prose fragment
#: that goes tautologically green when the wording is edited.
NON_ENVELOPE_WARNING = (
    "Home Assistant's return_response reply did not match the expected "
    "{changed_states, service_response} envelope, so no changed-state records "
    "could be separated from the response data. An empty 'result' here does NOT "
    "mean nothing changed — the whole reply is reported under 'service_response'."
)

#: Emitted when the component served a ``return_response`` call but returned no
#: ``service_response`` key. The component only sets that key for a non-null
#: response, so an absent key is ambiguous server-side: the service may genuinely
#: have returned nothing, or the component may have discarded the response on its
#: dispatched-but-unconfirmed path. Only raised alongside ``partial`` — the case
#: where the discard is actually possible — so a plain null response stays quiet.
COMPONENT_RESPONSE_UNCONFIRMED_WARNING = (
    "The service was dispatched but its confirmation did not arrive, and no "
    "response data came back with it. A null 'service_response' here does NOT "
    "prove the service returned nothing — the response may have been produced "
    "and lost with the confirmation. Re-read state to confirm the outcome."
)

#: Added when a non-administrator's token sends the call over WebSocket, whose
#: answer lists no changed states.
STATES_NOT_LISTED_WARNING = (
    "This token's user is not a Home Assistant administrator, so the call went "
    "over WebSocket, which lists no changed states: an empty 'result' does not "
    "mean nothing changed. Read the entity's state to confirm."
)

#: Added to a failed ``return_response`` call without the component: Core checks
#: the response only after the service ran, and only the component's answer says
#: whether the service ran before a refusal.
RESPONSE_CALL_FAILED_HINT = (
    "The service may have run before Home Assistant refused its response: check "
    "its effect before calling it again. The ha_mcp_tools component reports a "
    f"refused response as such. {COMPONENT_INSTALL_HINT}"
)


#: Added to the error of a call the component dispatched that then failed.
SERVICE_FAILED_AFTER_DISPATCH_HINT = (
    "The service may have changed something before it failed: check its effect "
    "before calling it again."
)


def note_rejected_response(
    response: dict[str, Any], error: dict[str, Any] | None
) -> None:
    """The service ran and Home Assistant refused the response it returned.

    Core checks the response only after the service ran, so the call is no
    failure and must not be repeated to get its response. No-op without one.
    """
    if error is None:
        return
    response["service_response"] = None
    response["response_error"] = error
    response.setdefault("warnings", []).append(
        "The service ran, but Home Assistant rejected the response it returned "
        f"({error.get('message')}). Do not call it again to get a response."
    )


def split_return_response_envelope(
    result: Any, *, return_response: bool
) -> tuple[Any, Any, bool, list[str]]:
    """Split HA's reply into (changed states, response, present, warnings).

    HA answers ``return_response=true`` with an envelope
    ``{"changed_states": [...], "service_response": ...}``. The response data
    belongs at the top level of ha_call_service's reply exactly ONCE — the
    placement the component path (``_build_component_call_response``) already
    uses — so it is peeled off here, BEFORE projection, leaving only the
    changed states to project into ``result``. Returning it in both places
    shipped it twice, byte-identical, doubling its token cost (issue #2085).

    A legitimately null ``service_response`` is still reported as present
    (``True``) so the caller emits the key.

    With ``return_response`` false there is no envelope to split: HA returns a
    plain changed-states list, so it passes through untouched and no
    ``service_response`` key is emitted (``present`` False). That is the
    overwhelming majority of calls.

    Anything else means a non-conforming responder (HA core always sends both
    keys, with a list) — in practice ``_request``'s ``{}`` for a non-JSON reply
    such as a reverse proxy's redirect. Every such shape takes ONE path: the whole
    reply becomes the response data, ``result`` stays empty, and a warning
    says so. Uniformity is the point — peeling a recognised key out of an
    unrecognised envelope would silently discard whatever else it carried,
    and an empty ``result`` would otherwise read as an affirmative "no entity
    states changed". A caller that asked for response data must never get back
    neither the data nor an explanation.
    """
    if not return_response:
        return result, None, False, []
    if (
        isinstance(result, dict)
        and "service_response" in result
        and isinstance(result.get("changed_states"), list)
    ):
        return result["changed_states"], result["service_response"], True, []
    # Any other shape is non-conforming. Hand the WHOLE reply back as the
    # response data rather than guessing which part is which: splitting one
    # recognised key out of an unrecognised envelope discards the rest (a
    # non-list ``changed_states`` would vanish entirely) and would make the
    # warning's "the whole reply is reported under 'service_response'" a lie.
    # Projecting nothing into ``result`` keeps the records from shipping twice.
    return [], result, True, [NON_ENVELOPE_WARNING]
