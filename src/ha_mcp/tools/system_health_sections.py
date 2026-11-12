"""Home Assistant's own system health and repair data for ``ha_get_system_health``.

The baseline is Core's ``system_health/info`` stream in the shape Core serves
it; the repair texts are Core's own ``issues`` strings.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..client.rest_client import HomeAssistantConnectionError
from .flow_translations import configured_language, fetch_flow_strings, flow_text

# Core sets no overall limit on pending system health checks; its reachability
# checks give up after 5 seconds.
HEALTH_DEADLINE_SECONDS = 15.0


async def read_system_health(ws_client: Any) -> dict[str, Any]:
    """The whole ``system_health/info`` subscription, read until ``finish``.

    Core answers with ``initial`` (every domain, each pending check as
    ``{"type": "pending"}``), one ``update`` per finished check, then
    ``finish``. The updates are merged the way the frontend merges them. A check
    still pending at the deadline stays pending, and releasing the subscription
    makes Core cancel it. Raises ``TimeoutError`` when no ``initial`` arrived.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + HEALTH_DEADLINE_SECONDS
    subscription, queue = await ws_client.subscribe_command(
        "system_health/info", wait_timeout=HEALTH_DEADLINE_SECONDS
    )
    data: dict[str, Any] | None = None
    try:
        while (remaining := deadline - loop.time()) > 0:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                break
            except asyncio.QueueShutDown as exc:
                raise HomeAssistantConnectionError(
                    "WebSocket closed while reading system_health/info"
                ) from exc
            event = message.get("event") or {}
            if event.get("type") == "finish":
                break
            data = _merge_event(data, event)
    finally:
        await ws_client.release_subscription(subscription)
    if data is None:
        raise TimeoutError("system_health/info sent no data before the deadline")
    return _health_result(data)


def _merge_event(
    data: dict[str, Any] | None, event: dict[str, Any]
) -> dict[str, Any] | None:
    """Apply one stream event to the data read so far (``None``: no ``initial``)."""
    if event.get("type") == "initial":
        return event.get("data") or {}
    if event.get("type") == "update" and data is not None:
        info = data.setdefault(event["domain"], {}).setdefault("info", {})
        info[event["key"]] = (
            event.get("data") if event.get("success") else event.get("error")
        )
    return data


def _health_result(data: dict[str, Any]) -> dict[str, Any]:
    """The baseline result, warning about the checks that are still pending."""
    pending = [
        f"{domain}.{key}"
        for domain, entry in data.items()
        for key, value in (entry.get("info") or {}).items()
        if isinstance(value, dict) and value.get("type") == "pending"
    ]
    result: dict[str, Any] = {
        "success": True,
        "health_info": data,
        "component_count": len(data),
        "message": f"Retrieved health info for {len(data)} components",
    }
    if pending:
        result["warnings"] = [
            f"{len(pending)} system health checks did not finish within "
            f"{HEALTH_DEADLINE_SECONDS:g} seconds and still show as pending: "
            + ", ".join(pending)
        ]
    return result


async def add_repair_texts(client: Any, issues: list[dict[str, Any]]) -> list[str]:
    """Give each issue the ``title`` and ``description`` the Repairs page shows.

    The strings are Core's ``issues`` translations in the configured language,
    at ``component.<domain>.issues.<translation_key or issue_id>``, with the
    issue's placeholders filled. Returns the warnings for strings that could not
    be read.
    """
    if not issues:
        return []
    language, warning = await configured_language(
        client, "repair titles and descriptions"
    )
    warnings = [warning] if warning else []
    domains = sorted({issue["domain"] for issue in issues})
    strings = await fetch_flow_strings(client, domains, "issues", language=language)
    if strings is None:
        return [
            *warnings,
            "Home Assistant's repair strings could not be read, so the repairs "
            "carry no title or description",
        ]
    for issue in issues:
        key = f"issues.{issue.get('translation_key') or issue['issue_id']}"
        placeholders = issue.get("translation_placeholders")
        for field in ("title", "description"):
            text = flow_text(strings, issue["domain"], f"{key}.{field}", placeholders)
            if text is not None:
                issue[field] = text
    return warnings
