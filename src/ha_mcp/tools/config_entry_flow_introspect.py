"""Read-only introspection of a helper's config flow.

Starts a flow only to read its first form (or its menu) and always aborts it.
Feeds ``describe`` and the ``data_schema`` attached to flow errors; split out
of ``config_entry_flow_walker``, which drives flows to completion.
"""

import asyncio
import logging
from enum import StrEnum
from typing import Any

from ..client.rest_client import HomeAssistantAPIError

logger = logging.getLogger(__name__)


class _FlowType(StrEnum):
    """HA config flow result type strings."""

    FORM = "form"
    MENU = "menu"
    ABORT = "abort"
    CREATE_ENTRY = "create_entry"


def _form_info(step: dict[str, Any]) -> dict[str, Any]:
    """A FORM step's schema, the step_id that keys its help text, and HA's
    ``last_step`` (``False``: a fixed form follows; ``None``: one may, chosen
    from the answers), else {}."""
    schema = step.get("data_schema")
    if step.get("type") != _FlowType.FORM or not isinstance(schema, list):
        return {}
    return {
        "schema": schema,
        "step_id": step.get("step_id"),
        "last_step": step.get("last_step"),
    }


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

    options = flow_result.get("menu_options")
    if isinstance(options, list):
        filtered = [opt for opt in options if isinstance(opt, str)]
        if filtered:
            info["menu_options"] = filtered
    return info


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

    - FORM at top: ``{"schema": [...], "step_id": ...}``
    - MENU at top with ``menu_choice``: submits and returns the branch
      form schema as ``{"schema": [...]}`` (no ``menu_options`` since
      the caller already picked a branch)
    - MENU at top without ``menu_choice``: ``{"menu_options": [...]}``
    - any failure or unparseable shape: ``{}`` (callers branch on
      ``"schema" in info`` / ``"menu_options" in info``)
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
            try:
                await asyncio.wait_for(
                    client.abort_config_flow(intro_flow_id), timeout=5.0
                )
            except Exception as abort_err:  # noqa: BLE001
                logger.debug(
                    f"Failed to abort introspection flow {intro_flow_id}: {abort_err}"
                )
