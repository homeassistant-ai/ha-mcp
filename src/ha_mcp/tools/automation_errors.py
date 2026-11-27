"""Errors for an automation or script Home Assistant rejected.

Core's own text leads every rejection, behind a prefix naming the kind; this module
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
from typing import Any, Literal, NoReturn, cast

from ha_mcp._vendor.websockets.exceptions import ConnectionClosed

from ..client.rest_client import HomeAssistantAPIError, HomeAssistantError
from ..client.websocket_client import get_websocket_client
from ..error_wording import (
    config_key_malformed,
    config_view_rejection,
    invalid_platform_key,
    unexpected_condition_value,
    variables_not_a_mapping,
)
from ..errors import ErrorCode, create_error_response
from .component_automation import automation_describe
from .helpers import bp_warnings_suggestion, raise_tool_error

logger = logging.getLogger(__name__)


# Bounds the wait for a platform subscription's first event, which Core sends
# right after the subscription result.
DESCRIPTIONS_TIMEOUT_S = 5.0


def section_from_status(status: str | None) -> str | None:
    """The config section a component ``validate`` status names."""
    return (
        status.removeprefix("failed_")
        if status and status.startswith("failed_")
        else None
    )


def core_rejection_text(exc: HomeAssistantAPIError) -> str | None:
    """The config view's verdict ("Key malformed: " / "Message malformed: ")
    from a write's 400; None for any other error or 400 body."""
    if exc.status_code != 400 or not isinstance(exc.response_data, dict):
        return None
    message = exc.response_data.get("message")
    if isinstance(message, str) and config_view_rejection(message):
        return message
    return None


async def failed_section_from_core(
    client: Any, config: dict[str, Any], domain: str = "automation"
) -> str | None:
    """Name the failed section with Core's ``validate_config``; best effort.

    A script's ``sequence`` is checked as ``actions``. When every section Core
    checks is valid, the root schema (or the blueprint) is what failed.
    """
    sections = {}
    if domain == "script":
        if config.get("sequence") is not None:
            sections["actions"] = config["sequence"]
    else:
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
                return "sequence" if domain == "script" else section
    return "blueprint" if "use_blueprint" in config else "schema"


async def raise_if_core_rejected(
    client: Any,
    exc: HomeAssistantAPIError,
    config: dict[str, Any],
    *,
    context: dict[str, Any],
    bp_warnings: list[str],
    domain: str = "automation",
) -> None:
    """Raise a write's 400 that Core answered as its rejection; return otherwise."""
    core_text = core_rejection_text(exc)
    if core_text is None:
        return
    if config_key_malformed(core_text):
        # Core refused the key before it judged the config.
        failed_section: str | None = "id"
    else:
        failed_section = await failed_section_from_core(client, config, domain)
    await raise_core_rejection(
        client,
        core_text,
        config,
        failed_section=failed_section,
        context=context,
        bp_warnings=bp_warnings,
        domain=domain,
    )


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
        message = await asyncio.wait_for(queue.get(), timeout=DESCRIPTIONS_TIMEOUT_S)
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
    """Registered keys nearest to ``key``.

    Searched within the key's domain when Core registers one: the domain is
    the part a caller has right, and a renamed key's replacement lives there
    (``schedule.turned_on`` is now ``schedule.block_started``, which no
    lookalike from another domain would surface). Across every domain
    otherwise, which also serves a misspelt domain. A key nothing comes close
    to gets no hint: the domain's other keys are not corrections.
    """
    domain, dot, _ = key.partition(".")
    if dot and (same := [c for c in candidates if c.startswith(f"{domain}.")]):
        return difflib.get_close_matches(key, same, n=3, cutoff=0.5)
    return difflib.get_close_matches(key, candidates, n=3, cutoff=0.5)


def _platform_condition(config: dict[str, Any]) -> bool:
    conditions = config.get("conditions", config.get("condition"))
    items = conditions if isinstance(conditions, list) else [conditions]
    return any(
        isinstance(c, dict) and "platform" in c and "condition" not in c for c in items
    )


def text_suggestions(core_text: str, config: dict[str, Any]) -> list[str]:
    """Targeted hints for Core rejections agents commonly cause."""
    suggestions = []
    if variables_not_a_mapping(core_text):
        suggestions.append(
            "variables must be a dict mapping names to values, "
            'e.g. {"variables": {"my_var": 42}}'
        )
    if unexpected_condition_value(core_text) and _platform_condition(config):
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
    domain: str = "automation",
) -> NoReturn:
    """Raise VALIDATION_FAILED with Core's text and the hints it supports."""
    error_context: dict[str, Any] = {**context, "failed_section": failed_section}
    if failed_section == "id":
        # Nothing in the config is at fault, so no config hint applies.
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                f"Home Assistant rejected the {domain}'s id: {core_text}",
                suggestions=[f"Use the {domain} id Core suggests in its message"],
                context=error_context,
            )
        )
    suggestions = text_suggestions(core_text, config)
    match = invalid_platform_key(core_text)
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
    if failed_section == "blueprint":
        suggestions.append(
            f"Use ha_manage_blueprints(action='list', domain='{domain}') to list "
            "available blueprints"
        )
    suggestions.append(f"Use ha_get_skill_guide for {domain} examples")
    if bp_warnings:
        suggestions.append(bp_warnings_suggestion(bp_warnings))
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_FAILED,
            f"Home Assistant rejected the {domain}: {core_text}",
            suggestions=suggestions,
            context=error_context,
        )
    )
