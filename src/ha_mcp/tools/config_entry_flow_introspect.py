"""Read-only introspection of a helper's config flow.

Starts a flow only to read its first form (or its menu, or the branch form a
``menu_choice`` picks) and always aborts it. Feeds the ``data_schema`` and
``menu_options`` attached to helper flow errors; split out of
``config_entry_flow_walker``, which drives flows to completion. Also holds the
shielded flow aborts.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import anyio

from ..client.rest_client import HomeAssistantAPIError

logger = logging.getLogger(__name__)

# How long cleanup waits for Home Assistant to abort a flow.
_ABORT_TIMEOUT_S = 5.0


def _flow_gone(err: BaseException) -> bool:
    """Whether ``err`` is Core's 404 for a flow that no longer exists: it
    already finished (abort, create_entry)."""
    return getattr(err, "status_code", None) == 404


async def shielded_abort(
    abort: Callable[[str], Awaitable[Any]],
    flow_id: str,
    *,
    failure: str,
    level: int = logging.WARNING,
) -> None:
    """Abort ``flow_id``.

    Shielded: a cancellation arrives through an anyio cancel scope, which
    would cancel the abort itself at its first await. A failure is logged as
    ``failure`` at ``level``, at debug when the flow is already gone.
    """
    try:
        with anyio.CancelScope(shield=True):
            await asyncio.wait_for(abort(flow_id), timeout=_ABORT_TIMEOUT_S)
    except Exception as abort_err:
        logger.log(
            logging.DEBUG if _flow_gone(abort_err) else level,
            "%s: %s",
            failure,
            abort_err,
            exc_info=True,
        )


async def abort_probe_flow(
    abort: Callable[[str], Awaitable[Any]], flow_id: str, *, quiet: bool = False
) -> None:
    """Abort a flow opened only to read it, shielded: a cancelled read still
    closes it. A failure logs at warning, at debug when ``quiet``."""
    await shielded_abort(
        abort,
        flow_id,
        failure=f"flow {flow_id} abort failed",
        level=logging.DEBUG if quiet else logging.WARNING,
    )


async def abort_failed_flow(
    abort: Callable[[str], Awaitable[Any]], flow_id: str, *, kind: str = "flow"
) -> None:
    """Abort a still-pending flow without hiding the original failure.
    ``kind`` names the flow in the log."""
    await shielded_abort(
        abort, flow_id, failure=f"Failed to abort {kind} {flow_id} after error"
    )


async def abort_failed_flow_type_only(
    abort: Callable[[str], Awaitable[Any]], flow_id: str, msg: str, *args: object
) -> None:
    """Abort a flow after a failed write.

    A failure logs ``msg % (*args, error_type)`` at warning, at debug when the
    flow is already gone: the error's type only, never its text, which may
    echo the values the flow carried.
    """
    try:
        with anyio.CancelScope(shield=True):  # see shielded_abort
            await asyncio.wait_for(abort(flow_id), timeout=_ABORT_TIMEOUT_S)
    except Exception as abort_err:  # noqa: BLE001
        logger.log(
            logging.DEBUG if _flow_gone(abort_err) else logging.WARNING,
            msg,
            *args,
            type(abort_err).__name__,
        )


class _FlowType(StrEnum):
    """HA config flow result type strings."""

    FORM = "form"
    MENU = "menu"
    ABORT = "abort"
    CREATE_ENTRY = "create_entry"


def _form_info(step: dict[str, Any]) -> dict[str, Any]:
    """A FORM step's schema, else {}."""
    schema = step.get("data_schema")
    if step.get("type") != _FlowType.FORM or not isinstance(schema, list):
        return {}
    return {"schema": schema}


async def _process_menu_flow_result(
    flow_result: dict[str, Any],
    client: Any,
    intro_flow_id: str | None,
    menu_choice: str | None,
) -> dict[str, Any]:
    """Return schema or menu_options dict for a MENU-type flow result."""
    info: dict[str, Any] = {}
    if menu_choice:
        if not intro_flow_id:
            return info
        try:
            step = await asyncio.wait_for(
                client.submit_config_flow_step(
                    intro_flow_id, {"next_step_id": menu_choice}
                ),
                timeout=10.0,
            )
        except (HomeAssistantAPIError, TimeoutError):
            return info
        return _form_info(step)

    if choices := menu_choices(flow_result):
        info["menu_options"] = choices
    return info


def menu_choices(step: dict[str, Any]) -> list[str]:
    """A MENU step's branch names: a list of step ids, or the keys of a
    ``{step_id: label}`` dict (HA allows both); malformed ones drop."""
    options = step.get("menu_options")
    if isinstance(options, dict):
        options = list(options)
    if not isinstance(options, list):
        return []
    return [opt for opt in options if isinstance(opt, str)]


async def fetch_helper_flow_info(
    client: Any,
    helper_type: str | None,
    menu_choice: str | None = None,
) -> dict[str, Any]:
    """Best-effort introspection of a helper's config-entry flow.

    Starts a fresh introspection flow (always aborted) and returns a dict
    with optional keys ``"schema"`` and ``"menu_options"`` so a single HA
    round-trip serves both the schema-attach path (used by
    ``_raise_flow_api_error`` and the pre-flow validation gates in
    ``_handle_flow_helper``) and the menu-sub-types path (used when a
    menu-rooted helper has no branch chosen yet — issue #1186).

    Behaviour:

    - FORM at top: ``{"schema": [...]}``
    - MENU at top with ``menu_choice``: submits and returns the branch form
      in the same shape (no ``menu_options`` since the caller already picked
      a branch)
    - MENU at top without ``menu_choice``: ``{"menu_options": [...]}``
    - any failure or unparseable shape, including a ``menu_choice`` HA
      rejects: ``{}`` (callers branch on ``"schema" in info`` /
      ``"menu_options" in info``)
    """
    info: dict[str, Any] = {}
    if not helper_type or client is None:
        return info
    intro_flow_id: str | None = None
    try:
        flow_result = await client.start_config_flow(helper_type)
        intro_flow_id = flow_result.get("flow_id")
        flow_type = flow_result.get("type")

        if flow_type == _FlowType.MENU:
            return await _process_menu_flow_result(
                flow_result, client, intro_flow_id, menu_choice
            )
        return _form_info(flow_result)
    except Exception:  # noqa: BLE001
        return info
    finally:
        if intro_flow_id:
            await abort_probe_flow(client.abort_config_flow, intro_flow_id, quiet=True)
