"""Submitting one step of a config-entry flow and reading Core's answer.

A submit that Home Assistant rejects (400/422) becomes a structured error; one
whose answer is lost (a timeout, a dropped connection, a gateway 5xx) is
reported as ``applied_but_unverified``, since the step may have committed.
A progress step is polled with the same classification, because Core moves the
flow on inside the GET.
"""

import asyncio
import logging
from typing import Any, NoReturn

from ..client.rest_client import (
    _NO_ANSWER_ERRORS,
    _RETRYABLE_GATEWAY_STATUS,
    HomeAssistantAPIError,
)
from ..errors import ErrorCode, create_error_response
from .config_entry_flow_errors import (
    FlowRef,
    ReconfigureStatus,
    _raise_flow_api_error,
    redacted_step,
)
from .config_entry_flow_progress import wait_for_progress
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

# External steps need a browser (OAuth / cloud authorization), which an MCP
# client cannot drive (issue #1814).
EXTERNAL_STEP_TYPES = frozenset({"external", "external_done"})
RECONFIGURE_SUCCESS_REASONS = frozenset(
    {
        "reauth_successful",
        "reconfigure_successful",
    }
)


async def _submit_step(
    submit_fn: Any,
    flow_id: str,
    payload: dict[str, Any],
    *,
    domain: str | None,
    last_menu_choice: str | None,
    current_step: dict[str, Any],
    is_reconfigure: bool = False,
) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(submit_fn(flow_id, payload), timeout=20.0)
    except HomeAssistantAPIError as api_err:
        if api_err.status_code in (400, 422):
            await _raise_flow_api_error(
                api_err,
                flow_id=flow_id,
                domain=domain,
                menu_choice=last_menu_choice,
                current_step=current_step,
                submitted=payload,
                is_reconfigure=is_reconfigure,
            )
        status = api_err.status_code or 0
        if status in _RETRYABLE_GATEWAY_STATUS or (is_reconfigure and status >= 500):
            # 502/503/504 mean no answer came back: the submit may have reached
            # Home Assistant and committed. On reconfigure any 5xx may follow
            # the step's own write to the entry; other kinds are written by Core
            # only after the step returns (a create flow's async_on_create_entry
            # can still 500 after the save, which this does not tell apart).
            # Anything else re-raises: HA answered.
            _raise_no_answer(
                api_err,
                flow_id=flow_id,
                payload=payload,
                current_step=current_step,
                error_code=ErrorCode.SERVICE_CALL_FAILED,
                detail=f"HTTP {status}",
            )
        raise
    except (TimeoutError, *_NO_ANSWER_ERRORS) as no_answer:
        # Every class here is evidence that NO ANSWER came back — see
        # _NO_ANSWER_ERRORS in rest_client.py — as opposed to Home Assistant
        # answering with a rejection (which the 400/422 branch above handles).
        # The submit is the call that commits (creates the entry, saves the
        # options, or probes and reloads on reconfigure), so losing the answer
        # to it is precisely the applied_but_unverified ambiguity: HA may
        # already have committed, and a retry can repeat the change.
        # rest_client funnels every httpx transport failure into
        # HomeAssistantConnectionError, so a connection reset after the POST
        # reached HA lands here too. The flow is never aborted on this status.
        _raise_no_answer(
            no_answer,
            flow_id=flow_id,
            payload=payload,
            current_step=current_step,
            error_code=ErrorCode.TIMEOUT_OPERATION
            if isinstance(no_answer, TimeoutError)
            else ErrorCode.SERVICE_CALL_FAILED,
            detail=type(no_answer).__name__,
        )
        raise  # not reached; an explicit end of this path for CodeQL


async def _wait_for_progress(
    client: Any,
    flow: FlowRef | None,
    flow_id: str,
    step: dict[str, Any],
    *,
    is_reconfigure: bool,
) -> dict[str, Any]:
    """``wait_for_progress`` with each GET classified like a submit.

    The GET re-runs the flow, so the step after ``progress_done`` may commit.
    Raises ``ValueError`` without a ``flow``: the poll URL depends on it.
    """
    if flow is None:
        raise ValueError("Polling a progress step needs the flow's FlowRef")

    async def poll(url: str, polled: dict[str, Any]) -> dict[str, Any]:
        try:
            return await _submit_step(
                lambda _flow_id, _payload: client._request("GET", url),
                flow_id,
                {},
                domain=flow.domain,
                last_menu_choice=None,
                current_step=polled,
                is_reconfigure=is_reconfigure,
            )
        except HomeAssistantAPIError as err:
            # Core moves a progress flow on only inside the GET. A 404 here is
            # what the client's retry after a gateway 502/503/504 meets when
            # the first attempt finished the flow (or Home Assistant restarted
            # in between).
            if err.status_code == 404:
                _raise_no_answer(
                    err,
                    flow_id=flow_id,
                    payload={},
                    current_step=polled,
                    error_code=ErrorCode.SERVICE_CALL_FAILED,
                    detail="HTTP 404 on a progress poll",
                )
            raise

    return await wait_for_progress(
        client, flow, flow_id, step, is_reconfigure=is_reconfigure, poll=poll
    )


def _raise_no_answer(
    cause: BaseException,
    *,
    flow_id: str,
    payload: dict[str, Any],
    current_step: dict[str, Any],
    error_code: ErrorCode,
    detail: str,
) -> NoReturn:
    """Raise ``applied_but_unverified`` for a submit or poll whose answer was lost."""
    raise_tool_error(
        create_error_response(
            error_code,
            "Flow submission got no answer from Home "
            f"Assistant ({detail}); the change may have been applied",
            suggestions=[
                "Do not retry automatically: a retry can create a duplicate "
                "entry or repeat the change. Check the config entry in Home "
                "Assistant first."
            ],
            context={
                "flow_id": flow_id,
                "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                "submitted_keys": sorted(payload),
                "details": redacted_step(current_step),
            },
        )
    )


def _rejected_on_submit(
    step: dict[str, Any], submitted_form: dict[str, Any] | None
) -> bool:
    """Whether ``step`` is the form just submitted, back with ``errors``.

    A form's first render can carry advisory errors and still accept the
    submit (KNX's routing step opens with ``no_router_discovered``).
    """
    return (
        bool(step.get("errors"))
        and submitted_form is not None
        and submitted_form.get("step_id") == step.get("step_id")
    )


def raise_reconfigure_created_entry(flow_id: str, step: dict[str, Any]) -> NoReturn:
    """A reconfigure flow answered ``create_entry``: never its success."""
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Reconfigure flow created a new config entry instead of "
            "updating the existing entry",
            suggestions=[
                "Inspect Home Assistant for a duplicate entry before "
                "retrying; do not create another entry automatically."
            ],
            context={
                "flow_id": flow_id,
                "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                "details": step,
            },
        )
    )
