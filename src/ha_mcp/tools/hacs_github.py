"""``ha_manage_hacs(action="connect_github")``: the Marketplace's GitHub sign-in.

``marketplace/github/connect`` opens a reconfigure flow of the Marketplace
entry, which the panel then continues in the config-flow dialog. Nobody else
can continue it (``ha_set_integration`` refuses this entry), so this action
answers it one step per call through the shared flow-step helpers: an empty
form, then a progress step whose placeholders carry GitHub's URL and the code
the user enters there. Once GitHub confirms the code, Core holds the flow at
its last step until it is read; that read ends it, and the Marketplace's
``github_connected`` tells how, as the panel asks it.
"""

from typing import Any

from ..client.rest_client import HomeAssistantCommandError
from ..errors import ErrorCode, create_error_response
from .config_entry_flow_progress import PROGRESS_STEP_TYPES
from .config_entry_reload_watch import watch_entry_reload
from .hacs_backend import marketplace_entry, store_command
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
    entry = await marketplace_entry(ws_client, _ACTION)
    return FlowTarget("reconfigure", "marketplace", entry["entry_id"])


async def _outcome(ws_client: Any, flow_id: str) -> dict[str, Any]:
    """The flow's last step was read; the Marketplace says whether GitHub is
    connected."""
    opened_flows.pop(flow_id, None)
    answer: dict[str, Any] = {**_ACTION, "flow_id": flow_id, "flow_open": False}
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
        "message": "The sign-in flow ended, and the Marketplace reports GitHub "
        "as connected.",
    }


def _step_answer(step_answer: dict[str, Any]) -> dict[str, Any]:
    """A flow-step helper's answer as this action's data: the tool adds its
    own ``success``."""
    return {**_ACTION, **{k: v for k, v in step_answer.items() if k != "success"}}


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


async def connect_github(
    ws_client: Any, *, client: Any, flow_id: str | None, abort: bool | None
) -> dict[str, Any]:
    target = await _target(ws_client)
    if abort:
        return _step_answer(await abort_open_flow(client, target, flow_id))
    if flow_id is None:
        started = await store_command(ws_client, "marketplace/github/connect", _ACTION)
        flow_id = str(started["flow_id"])
        opened_flows[flow_id] = target.anchor
        step = await read_step(client, target, flow_id)
        return {
            **_step_answer(await open_step_answer(client, target, flow_id, step)),
            "message": (
                "The flow is open at its first step. Call again with flow_id to "
                "get the code the user enters on GitHub, or end it with flow_id "
                "and abort=True."
            ),
        }
    # Subscribed before the flow's last step is read: Core reloads the entry
    # with the new token as the flow ends, and only a stream opened first
    # sees that reload settle.
    async with watch_entry_reload(client, str(target.entry_id)) as reloaded:
        step = await read_step(client, target, flow_id)
        if step.get("type") == "form":
            step = await submit_step(
                client, target, flow_id, config={}, menu_choice=None, current=step
            )
            if step.get("type") in PROGRESS_STEP_TYPES:
                # The user needs the code before anything can finish.
                return _progress_answer(flow_id, step)
        settled = await settle(client, target, flow_id, step)
        if settled.get("type") in ("form", "menu"):
            return _step_answer(
                await open_step_answer(client, target, flow_id, settled)
            )
        await reloaded()
    return await _outcome(ws_client, flow_id)
