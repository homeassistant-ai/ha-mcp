"""Home Assistant's own system health and repair data for ``ha_get_system_health``.

The baseline is Core's ``system_health/info`` stream in the shape Core serves
it, labelled with Core's ``system_health`` strings; the repair texts are Core's
own ``issues`` strings. The other sections read what the System information
dialog, the Integration startup dialog and the Matter panel read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
    HomeAssistantError,
)
from .flow_translations import configured_language, fetch_flow_strings, flow_text

logger = logging.getLogger(__name__)

# Core sets no overall limit on pending system health checks; its reachability
# checks give up after 5 seconds.
HEALTH_DEADLINE_SECONDS = 15.0
MATTER_TOPOLOGY_LIMIT = 50
SUPERVISOR_ENDPOINTS = {
    "resolution": "/resolution/info",
    "core_stats": "/core/stats",
    "supervisor_stats": "/supervisor/stats",
}


def reraise_if_fatal(exc: BaseException) -> None:
    """Re-raise what must unwind instead of becoming a section error.

    Cancellation and interpreter exit, ``ToolError`` (the MCP ``isError``
    contract), and ``HomeAssistantConnectionError``: once the transport is dead
    the other sections fail too, so the root cause surfaces once (#1624).
    """
    if (
        isinstance(exc, ToolError)
        or not isinstance(exc, Exception)
        or isinstance(exc, HomeAssistantConnectionError)
    ):
        raise exc


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
    untitled = []
    for issue in issues:
        key = f"issues.{issue.get('translation_key') or issue['issue_id']}"
        placeholders = issue.get("translation_placeholders")
        for field in ("title", "description"):
            text = flow_text(strings, issue["domain"], f"{key}.{field}", placeholders)
            if text is not None:
                issue[field] = text
        if "title" not in issue:
            untitled.append(f"{issue['domain']}.{issue['issue_id']}")
    if untitled:
        warnings.append(
            "Home Assistant has no repair strings for these repairs, so they "
            "carry no title: " + ", ".join(untitled)
        )
    return warnings


async def add_repair_data(ws_client: Any, issues: list[dict[str, Any]]) -> list[str]:
    """Give each issue the ``data`` the Repairs page reads with
    ``repairs/get_issue_data``, such as a reauth issue's ``flow_id``.

    Returns a warning naming the issues whose data could not be read.
    """
    replies = await asyncio.gather(
        *(
            ws_client.send_command(
                "repairs/get_issue_data",
                domain=issue["domain"],
                issue_id=issue["issue_id"],
            )
            for issue in issues
        ),
        return_exceptions=True,
    )
    unread = []
    for issue, reply in zip(issues, replies, strict=True):
        if isinstance(reply, BaseException):
            reraise_if_fatal(reply)
            unread.append(f"{issue['domain']}.{issue['issue_id']}: {reply}")
        else:
            issue["data"] = (reply.get("result") or {}).get("issue_data")
    if not unread:
        return []
    return ["The data of these repairs could not be read: " + "; ".join(unread)]


async def add_health_labels(client: Any, result: dict[str, Any]) -> None:
    """Give each domain in ``result["health_info"]`` the ``name`` and key
    ``labels`` the System information dialog shows.

    The name is Core's ``title`` string for the domain and each label is
    Core's ``component.<domain>.system_health.info.<key>`` string, in the
    configured language. Strings that could not be read become warnings in
    ``result``.
    """
    health_info = result["health_info"]
    if not health_info:
        return
    language, warning = await configured_language(client, "system health labels")
    warnings = [warning] if warning else []
    domains = sorted(health_info)
    labels, titles = await asyncio.gather(
        fetch_flow_strings(client, domains, "system_health", language=language),
        fetch_flow_strings(client, domains, "title", language=language),
    )
    if labels is None or titles is None:
        warnings.append(
            "Home Assistant's system health labels could not be read, so the "
            "health info carries raw keys only"
        )
    else:
        for domain, entry in health_info.items():
            if name := flow_text(titles, domain, "title", None):
                entry["name"] = name
            entry_labels = {
                key: text
                for key in entry.get("info") or {}
                if (
                    text := flow_text(labels, domain, f"system_health.info.{key}", None)
                )
            }
            if entry_labels:
                entry["labels"] = entry_labels
    if warnings:
        result.setdefault("warnings", []).extend(warnings)


async def read_integration_startup(ws_client: Any, client: Any) -> dict[str, Any]:
    """Core's ``integration/setup_info``, slowest first, with each integration's
    name, as the Integration startup dialog shows it."""
    try:
        reply = await ws_client.send_command("integration/setup_info")
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        return {"error": f"Integration startup times not available: {exc}"}
    setups = sorted(
        reply.get("result") or [],
        key=lambda setup: setup.get("seconds") or 0,
        reverse=True,
    )
    section: dict[str, Any] = {"integrations": setups, "count": len(setups)}
    if not setups:
        return section
    language, warning = await configured_language(client, "integration names")
    titles = await fetch_flow_strings(
        client, [setup["domain"] for setup in setups], "title", language=language
    )
    if titles is None:
        warning = "Home Assistant's integration names could not be read"
    else:
        for setup in setups:
            if name := flow_text(titles, setup["domain"], "title", None):
                setup["name"] = name
    if warning:
        section["_warnings"] = [warning]
    return section


async def read_supervisor(client: Any) -> dict[str, Any]:
    """Supervisor's resolution info and the Core and Supervisor container
    stats, which the System information dialog reads on a Supervisor install."""
    from .tools_addons import _supervisor_api_call

    try:
        config = await client.get_config()
    except HomeAssistantError as exc:
        reraise_if_fatal(exc)
        return {"error": f"Supervisor data not available: {exc}"}
    if "hassio" not in (config.get("components") or []):
        return {"error": "Supervisor is not available on this installation"}
    replies = await asyncio.gather(
        *(_supervisor_api_call(client, path) for path in SUPERVISOR_ENDPOINTS.values()),
        return_exceptions=True,
    )
    section: dict[str, Any] = {}
    for key, reply in zip(SUPERVISOR_ENDPOINTS, replies, strict=True):
        if isinstance(reply, BaseException):
            if not isinstance(reply, ToolError):
                reraise_if_fatal(reply)
            section[key] = {"error": f"Not available: {reply}"}
        else:
            section[key] = reply.get("result")
    return section


async def read_matter_network(ws_client: Any) -> dict[str, Any]:
    """What the Matter panel reads: Core's Matter config entry and its
    ``matter/network_topology``, each as Core serves it.

    The entry is the first enabled one that is not ignored, as on the panel.
    It is read with ``config_entries/get``, not from the component's snapshot,
    whose rows carry the identity fields only. Only ``nodes`` and
    ``connections`` are replaced, each capped at ``MATTER_TOPOLOGY_LIMIT``,
    with ``node_count`` and ``connection_count``
    giving the full sizes.
    """
    try:
        entries = await ws_client.send_command("config_entries/get", domain="matter")
    except Exception as exc:  # noqa: BLE001
        reraise_if_fatal(exc)
        logger.warning("Failed to fetch Matter network data: %s", exc)
        return {"error": f"Matter integration not available or error: {exc}"}
    entry = next(
        (
            e
            for e in entries.get("result") or []
            if e.get("domain") == "matter"
            and e.get("disabled_by") is None
            and e.get("source") != "ignore"
        ),
        None,
    )
    if entry is None:
        return {"error": "No enabled Matter config entry found"}
    section: dict[str, Any] = {"config_entry": entry}
    if entry.get("state") != "loaded":
        return section
    try:
        reply = await ws_client.send_command("matter/network_topology")
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        section["topology"] = {"error": f"Matter network topology not available: {exc}"}
        return section
    graph = reply.get("result") or {}
    nodes = graph.get("nodes") or []
    connections = graph.get("connections") or []
    section["topology"] = {
        **graph,
        "nodes": nodes[:MATTER_TOPOLOGY_LIMIT],
        "connections": connections[:MATTER_TOPOLOGY_LIMIT],
        "node_count": len(nodes),
        "connection_count": len(connections),
    }
    if max(len(nodes), len(connections)) > MATTER_TOPOLOGY_LIMIT:
        section["topology"]["truncated"] = True
    return section
