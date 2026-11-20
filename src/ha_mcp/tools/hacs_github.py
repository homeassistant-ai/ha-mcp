"""``ha_manage_hacs(action="connect_github")``: the Marketplace's GitHub sign-in.

``marketplace/github/connect`` opens a reconfigure flow of the Marketplace
entry, which the panel then continues in the config-flow dialog. Nobody else
can continue it (``ha_set_integration`` refuses this entry), so this action
answers it one step per call through the shared flow-step helpers: an empty
form, then a progress step whose placeholders carry GitHub's URL and the code
the user enters there. Core finishes the flow by itself once GitHub confirms
the code, so a flow that is no longer open has ended, and the Marketplace's
``github_connected`` tells how, as the panel asks it.
"""

import json
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import HomeAssistantCommandError
from ..errors import ErrorCode, create_error_response
from .config_entry_flow_errors import ReconfigureStatus
from .config_entry_flow_progress import PROGRESS_STEP_TYPES
from .hacs_backend import store_command
from .helpers import raise_tool_error
from .integration_flow_steps import (
    FlowTarget,
    abort_open_flow,
    open_step_answer,
    opened_flows,
    read_step,
    settle,
    submit_step,
)

_ACTION = {"action": "connect_github"}


async def _target(ws_client: Any) -> FlowTarget:
    """The Marketplace entry's reconfigure flow."""
    reply = await ws_client.send_command("config_entries/get", domain="marketplace")
    entries = reply.get("result") or []
    if not entries:
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "Home Assistant has no Marketplace entry to connect GitHub to.",
                suggestions=[
                    "The Marketplace comes with Home Assistant 2026.11 and newer."
                ],
                context=_ACTION,
            )
        )
    return FlowTarget("reconfigure", "marketplace", entries[0]["entry_id"])


def _flow_gone(failure: ToolError) -> bool:
    """The flow-step helpers' refusal of a flow Home Assistant no longer holds:
    not open when read, or gone between two polls of its progress step."""
    try:
        body: dict[str, Any] = json.loads(str(failure))
    except ValueError:
        return False
    return bool(
        body.get("error", {}).get("code") == ErrorCode.RESOURCE_NOT_FOUND
        or body.get("status") == ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    )


async def _outcome(ws_client: Any, flow_id: str) -> dict[str, Any]:
    """The flow has ended; the Marketplace says whether GitHub is connected."""
    opened_flows.pop(flow_id, None)
    answer: dict[str, Any] = {**_ACTION, "flow_id": flow_id, "finished": True}
    try:
        info = await ws_client.send_command("marketplace/info")
    except HomeAssistantCommandError as err:
        # Core reloads the entry with the new token as the flow ends.
        return {
            **answer,
            "github_connected": None,
            "warnings": [
                f"The Marketplace did not answer yet ({err}); read "
                "store.github_connected with ha_get_hacs_info(action='search')."
            ],
        }
    connected = bool((info.get("result") or {}).get("github_connected"))
    if not connected:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "The GitHub sign-in ended without connecting an account.",
                suggestions=[
                    "Start it again with ha_manage_hacs(action='connect_github')."
                ],
                context={**answer, "github_connected": False},
            )
        )
    return {
        **answer,
        "github_connected": True,
        "message": "GitHub is connected to the Marketplace.",
    }


def _progress_answer(flow_id: str, step: dict[str, Any]) -> dict[str, Any]:
    placeholders = step.get("description_placeholders") or {}
    return {
        **_ACTION,
        "flow_id": flow_id,
        "flow_type": step.get("type"),
        "step_id": step.get("step_id"),
        "progress_action": step.get("progress_action"),
        "description_placeholders": placeholders,
        "message": (
            f"Ask the user to open {placeholders.get('url')} and enter the code "
            f"{placeholders.get('code')}. Then call again with flow_id to wait "
            "for the outcome, or end it with flow_id and abort=True."
        ),
    }


async def _wait(
    client: Any, ws_client: Any, target: FlowTarget, flow_id: str, step: dict[str, Any]
) -> dict[str, Any]:
    """Wait out the progress step; Core may end the flow meanwhile, and a poll
    then finds it gone."""
    try:
        settled = await settle(client, target, flow_id, step)
    except ToolError as failure:
        if not _flow_gone(failure):
            raise
        return await _outcome(ws_client, flow_id)
    if settled.get("type") in ("form", "menu"):
        return {**_ACTION, **await open_step_answer(client, target, flow_id, settled)}
    return await _outcome(ws_client, flow_id)


async def connect_github(
    ws_client: Any, *, client: Any, flow_id: str | None, abort: bool | None
) -> dict[str, Any]:
    target = await _target(ws_client)
    if abort:
        return {**_ACTION, **await abort_open_flow(client, target, flow_id)}
    if flow_id is None:
        started = await store_command(ws_client, "marketplace/github/connect", _ACTION)
        flow_id = str(started["flow_id"])
        opened_flows[flow_id] = target.anchor
        step = await read_step(client, target, flow_id)
        return {
            **_ACTION,
            **await open_step_answer(client, target, flow_id, step),
            "message": (
                "The flow is open at its first step. Call again with flow_id to "
                "get the code the user enters on GitHub, or end it with flow_id "
                "and abort=True."
            ),
        }
    try:
        step = await read_step(client, target, flow_id)
    except ToolError as failure:
        if not _flow_gone(failure):
            raise
        return await _outcome(ws_client, flow_id)
    if step.get("type") == "form":
        step = await submit_step(
            client, target, flow_id, config={}, menu_choice=None, current=step
        )
        if step.get("type") in PROGRESS_STEP_TYPES:
            # The user needs the code before anything can finish.
            return _progress_answer(flow_id, step)
    return await _wait(client, ws_client, target, flow_id, step)
