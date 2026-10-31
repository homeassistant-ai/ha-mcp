"""Errors for an automation Home Assistant rejected.

Core's own text leads every rejection, behind one fixed prefix; this module
only names the section that failed and adds suggestions. Closest matches for
an unknown trigger or condition key come from the platforms the running Core
registered (the component's ``describe``, else the first event of Core's
``trigger_platforms/subscribe`` / ``condition_platforms/subscribe``), never
from a list kept here.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from typing import Any, Literal, NoReturn, cast

from ha_mcp._vendor.websockets.exceptions import ConnectionClosed

from ..client.rest_client import HomeAssistantAPIError, HomeAssistantError
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_automation import automation_describe
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

REJECTION_PREFIX = "Home Assistant rejected the automation: "

# Core raises both quote styles (the helpers/condition package uses each once).
_INVALID_KEY = re.compile(r"Invalid (trigger|condition) ['\"]([^'\"]+)['\"] specified")

# Bounds the wait for a platform subscription's first event, which Core sends
# right after the subscription result.
_DESCRIPTIONS_TIMEOUT_S = 5.0


def section_from_status(status: str | None) -> str | None:
    """The config section a component ``validate`` status names."""
    return (
        status.removeprefix("failed_")
        if status and status.startswith("failed_")
        else None
    )


def core_rejection_text(exc: HomeAssistantAPIError) -> str | None:
    """Core's message for a 400 it answered, None for any other error."""
    if exc.status_code != 400 or not isinstance(exc.response_data, dict):
        return None
    message = exc.response_data.get("message")
    return message if isinstance(message, str) and message else None


async def failed_section_from_core(client: Any, config: dict[str, Any]) -> str | None:
    """Name the failed section with Core's ``validate_config``; best effort.

    When every section Core checks is valid, the root schema (or the
    blueprint) is what failed.
    """
    sections = {}
    for plural, singular in (
        ("triggers", "trigger"),
        ("conditions", "condition"),
        ("actions", "action"),
    ):
        value = config.get(plural, config.get(singular))
        if value is not None:
            sections[plural] = value
    if sections:
        try:
            result = await client.send_websocket_message(
                {"type": "validate_config", **sections}
            )
        except HomeAssistantError as exc:
            logger.debug("validate_config got no answer: %s", exc)
            return None
        verdicts = result.get("result") if result.get("success") else None
        if not isinstance(verdicts, dict):
            return None
        for section in sections:
            if (verdicts.get(section) or {}).get("valid") is False:
                return section
    return "blueprint" if "use_blueprint" in config else "schema"


async def _subscription_first_event(client: Any, command: str) -> dict[str, Any] | None:
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        sub_id, queue = await ws.subscribe_command(command)
    except (HomeAssistantError, OSError, ConnectionClosed) as exc:
        logger.debug("%s failed: %s", command, exc)
        return None
    try:
        message = await asyncio.wait_for(queue.get(), timeout=_DESCRIPTIONS_TIMEOUT_S)
    except TimeoutError:
        return None
    finally:
        await ws.release_subscription(sub_id)
    event = message.get("event") if isinstance(message, dict) else None
    return event if isinstance(event, dict) else None


async def platform_descriptions(
    client: Any, kind: Literal["trigger", "condition"]
) -> dict[str, Any] | None:
    """Core's description of every registered trigger or condition, by key."""
    descriptions = await automation_describe(client, kind)
    if descriptions is not None:
        return descriptions
    return await _subscription_first_event(client, f"{kind}_platforms/subscribe")


def closest_matches(key: str, candidates: list[str]) -> list[str]:
    """Registered keys nearest to ``key``, else the keys of its domain."""
    matches = difflib.get_close_matches(key, candidates, n=3, cutoff=0.5)
    domain, dot, _ = key.partition(".")
    if not matches and dot:
        matches = sorted(c for c in candidates if c.startswith(f"{domain}."))[:5]
    return matches


def _platform_condition(config: dict[str, Any]) -> bool:
    conditions = config.get("conditions", config.get("condition"))
    items = conditions if isinstance(conditions, list) else [conditions]
    return any(
        isinstance(c, dict) and "platform" in c and "condition" not in c for c in items
    )


def _text_suggestions(core_text: str, config: dict[str, Any]) -> list[str]:
    """Targeted hints for Core rejections agents commonly cause."""
    lowered = core_text.lower()
    suggestions = []
    if "unexpected keyword argument" in lowered:
        suggestions.append(
            "A root-level field such as alias, triggers or conditions is inside "
            "an action step."
        )
    elif "'variables'" in core_text and "dictionary" in core_text:
        suggestions.append(
            "variables must be a dict mapping names to values, "
            'e.g. {"variables": {"my_var": 42}}'
        )
    if "unexpected value for condition" in lowered and _platform_condition(config):
        suggestions.append(
            "A condition uses 'platform' (trigger syntax); conditions use "
            "'condition', e.g. {'condition': 'state', ...}."
        )
    return suggestions


async def raise_core_rejection(
    client: Any,
    core_text: str,
    config: dict[str, Any],
    *,
    failed_section: str | None,
    context: dict[str, Any],
    bp_warnings: list[str],
) -> NoReturn:
    """Raise VALIDATION_FAILED with Core's text and the hints it supports."""
    error_context: dict[str, Any] = {**context, "failed_section": failed_section}
    suggestions = _text_suggestions(core_text, config)
    match = _INVALID_KEY.search(core_text)
    if match:
        kind = cast(Literal["trigger", "condition"], match.group(1))
        key = match.group(2)
        descriptions = await platform_descriptions(client, kind) or {}
        matches = closest_matches(key, sorted(descriptions))
        if matches:
            error_context["closest_matches"] = matches
            description = descriptions.get(matches[0])
            if isinstance(description, dict):
                error_context["platform_description"] = description
            suggestions.insert(
                0,
                f"Home Assistant has no {kind} '{key}'. Registered {kind}s "
                f"closest to it: {', '.join(matches)}.",
            )
    suggestions.append("Use ha_get_skill_guide for automation examples")
    if bp_warnings:
        suggestions.append(
            "Config had best-practice issues that may be related: "
            + "; ".join(bp_warnings)
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_FAILED,
            f"{REJECTION_PREFIX}{core_text}",
            suggestions=suggestions,
            context=error_context,
        )
    )
