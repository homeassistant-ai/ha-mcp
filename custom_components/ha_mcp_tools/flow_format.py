"""Core's own text for a flow step, for ``ha_mcp_tools/flow_format``.

Home Assistant 2026.12 formats a flow step for its LLM tools with
``homeassistant.components.llm.flow.format_result``: the step's title and
description, each field's label and help text, the errors, menu option labels,
and the progress, create-entry and abort texts, with their placeholders
filled. This command runs it in process on the flow's live step, with the
translations Core's config and repairs tools hand it (``_async_finalize_flow_step``
in ``config/llm.py``, the fix-flow formatting in ``repairs/llm.py``), so on
2026.12 the server looks up no flow text itself. Nothing here needs Core's LLM
API to be enabled. An older Core has no formatter and answers
``supported: false``.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .flow_record import MANAGERS, flow_manager

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_FLOW_FORMAT = "ha_mcp_tools/flow_format"
CAPABILITY = "flow_format"
# What the formatter and the string selection below read of a step that has
# ended (abort, create_entry) or is waiting (progress). The server sends the
# step whole, so this is the one list to change when Core reads more.
_ENDED_STEP_KEYS = (
    "flow_id",
    "handler",
    "type",
    "step_id",
    "reason",
    "title",
    "description",
    "description_placeholders",
    "progress_action",
    "translation_domain",
)


def core_formatter() -> ModuleType | None:
    """Core's ``llm.flow`` module, or ``None`` before Home Assistant 2026.12."""
    # 2026.11 has no llm.flow, so it is imported by name and may be missing.
    # Remove once the hacs.json minimum is 2026.12: import it directly and drop the None
    # answers below (``supported: false``, ``format_form``'s None).
    try:
        return importlib.import_module("homeassistant.components.llm.flow")
    except ImportError:
        return None


async def translations(
    hass: HomeAssistant, category: str, domains: set[str]
) -> dict[str, str]:
    from homeassistant.helpers import translation

    return await translation.async_get_translations(
        hass, hass.config.language or "en", category, domains
    )


async def format_form(
    hass: HomeAssistant, domain: str, category: str, step_id: str, schema: Any
) -> list[dict[str, Any]] | None:
    """A declared form's fields as Core formats a form it shows, or ``None``
    without Core's formatter."""
    flow = core_formatter()
    if flow is None:
        return None
    from homeassistant.data_entry_flow import FlowResultType

    formatted = flow.format_result(
        {
            "type": FlowResultType.FORM,
            "flow_id": "",
            "step_id": step_id,
            "data_schema": schema,
        },
        await translations(hass, category, {domain}),
        f"component.{domain}.{category}",
    )
    fields: list[dict[str, Any]] = formatted["fields"]
    return fields


def _live_step(manager: Any, step: dict[str, Any], step_type: Any) -> Any:
    """The form or menu the flow shows now, with its schema, if it is still
    at ``step``. Read from the flow manager as Core's own
    ``llm.flow.async_configure_step`` reads it; that function also advances a
    flow whose current step is a finished progress step, which a read must
    never do, so its step is compared first and it is not called."""
    handler = manager._progress.get(step["flow_id"])
    current = getattr(handler, "cur_step", None)
    if (
        not isinstance(current, dict)
        or current.get("type") != step_type
        or current.get("step_id") != step.get("step_id")
        # Answered again since: its errors would not be the step's.
        or (current.get("errors") or None) != (step.get("errors") or None)
    ):
        return None
    return current


async def _repair_strings(
    hass: HomeAssistant, step: dict[str, Any], msg: dict[str, Any]
) -> tuple[dict[str, str], str, dict[str, Any]]:
    """What Core's repairs tools hand the formatter for a fix flow step."""
    handler, key = step.get("handler"), msg.get("translation_key")
    placeholders = {"extra_placeholders": msg.get("placeholders")}
    if not isinstance(handler, str) or key is None:
        return {}, "", placeholders
    # Read under the issue's own domain, also when a step names another
    # translation_domain, as the Repairs dialog reads a fix flow's texts.
    trans = await translations(hass, "issues", {handler})
    title = trans.get(f"component.{handler}.issues.{key}.title")
    return (
        trans,
        f"component.{handler}.issues.{key}.fix_flow",
        {"fallback_title": title, **placeholders},
    )


def _flow_domain(
    hass: HomeAssistant, manager_name: str, handler: Any
) -> tuple[str, str] | None:
    """The integration a config, options or subentry flow belongs to, and
    the strings category (with a subentry type) its texts live under."""
    if manager_name == "config":
        return (handler, "config") if isinstance(handler, str) else None
    entry_id, category = handler, "options"
    if manager_name == "subentries":
        if not isinstance(handler, list | tuple) or len(handler) != 2:
            return None
        entry_id, category = handler[0], f"config_subentries.{handler[1]}"
    entry = hass.config_entries.async_get_entry(str(entry_id))
    return None if entry is None else (entry.domain, category)


async def _flow_strings(
    hass: HomeAssistant, manager_name: str, step: dict[str, Any], msg: dict[str, Any]
) -> tuple[dict[str, str], str, dict[str, Any]] | None:
    """The translations, base prefix and formatter arguments Core's config
    and repairs tools use for this step; ``None`` when its integration is not
    known."""
    if manager_name == "repairs":
        return await _repair_strings(hass, step, msg)
    found = _flow_domain(hass, manager_name, step.get("handler"))
    if found is None:
        return None
    domain, path = found
    trans_domain = step.get("translation_domain") or domain
    if (
        step.get("translation_domain")
        and step.get("type") == "abort"
        and path.startswith("config_subentries.")
    ):
        # A subentry abort another integration owns is read under that
        # integration's config strings, as the frontend reads it.
        path = "config"
    category = path.partition(".")[0]
    domains = {domain, trans_domain}
    if category == "config":
        domains.add("homeassistant")
    trans = await translations(hass, category, domains)
    base = f"component.{trans_domain}.{path}"
    titles = await translations(hass, "title", {domain})
    kwargs: dict[str, Any] = {"fallback_title": titles.get(f"component.{domain}.title")}
    reason = step.get("reason")
    if step.get("type") == "abort" and not trans.get(f"{base}.abort.{reason}"):
        fallback = trans
        if category != "config":
            fallback = await translations(hass, "config", {"homeassistant"})
        kwargs["fallback_abort_description"] = fallback.get(
            f"component.homeassistant.config.abort.{reason}"
        )
    return trans, base, kwargs


async def async_flow_format(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Core's formatted ``step`` of a flow in ``manager``: a form or menu is
    read from the flow, which must still be at that step; an ended or
    progress step is formatted as Core reported it."""
    flow = core_formatter()
    if flow is None:
        return {"supported": False}
    from homeassistant.data_entry_flow import FlowResultType

    step = msg["step"]
    manager = flow_manager(hass, msg["manager"])
    try:
        step_type = FlowResultType(step.get("type"))
    except ValueError:
        return {"supported": True, "found": False}
    result: Any
    if step_type in (FlowResultType.FORM, FlowResultType.MENU):
        result = None if manager is None else _live_step(manager, step, step_type)
    else:
        result = {k: step[k] for k in _ENDED_STEP_KEYS if k in step}
        result["type"] = step_type
    strings = await _flow_strings(hass, msg["manager"], step, msg)
    if result is None or strings is None:
        return {"supported": True, "found": False}
    trans, base, kwargs = strings
    return {
        "supported": True,
        "found": True,
        "step": flow.format_result(
            result, trans, base, flow_id=step.get("flow_id"), **kwargs
        ),
    }


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return {"result": await async_flow_format(hass, msg)}

    def do_flow_format(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    return [
        (
            {
                vol.Required("type"): WS_FLOW_FORMAT,
                vol.Required("manager"): vol.In(MANAGERS),
                vol.Required("step"): {
                    vol.Required("flow_id"): str,
                    vol.Required("type"): str,
                    vol.Extra: object,
                },
                # A Repairs fix flow: its issue's translation_key and
                # translation_placeholders, as Core's repairs tools pass them.
                vol.Optional("translation_key"): vol.Any(str, None),
                vol.Optional("placeholders"): vol.Any(dict, None),
            },
            do_flow_format,
            prep,
        )
    ]
