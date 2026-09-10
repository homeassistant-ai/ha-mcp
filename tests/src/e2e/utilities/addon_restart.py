"""Fresh-client probes and process-identity fences for disruptive app tests."""

import asyncio
import logging
import time
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from .assertions import parse_mcp_result
from .wait_helpers import _POLLING_TRANSIENT_ERRORS

LOG = logging.getLogger(__name__)
TRANSIENT_ADDON_ERRORS = (*_POLLING_TRANSIENT_ERRORS, httpx.HTTPError)


def _call_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("App restart phase exhausted its deadline")
    return min(30.0, remaining)


async def call_tool_fresh(
    addon_url: str, tool: str, args: dict[str, Any], *, timeout: float = 30.0
) -> Any:
    """Bound one complete MCP exchange without retaining a restart-broken session."""
    async with asyncio.timeout(timeout):
        async with Client(StreamableHttpTransport(url=addon_url)) as client:
            raw = await client.call_tool(tool, args)
    return parse_mcp_result(raw)


async def get_instance_id(settings_info_url: str, *, timeout: float = 30.0) -> str:
    """Read the settings endpoint's per-process identity."""
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.get(settings_info_url)
    response.raise_for_status()
    data = response.json()
    assert isinstance(data, dict), "Settings info returned non-object JSON"
    instance_id = data.get("instance_id")
    assert isinstance(instance_id, str) and instance_id, (
        "Settings info returned no process instance_id"
    )
    return instance_id


async def post_log_level(
    settings_advanced_url: str, level: str, *, timeout: float = 30.0
) -> None:
    """Write the log level through the same settings API as the web UI."""
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.post(settings_advanced_url, json={"log_level": level})
    response.raise_for_status()
    assert response.status_code == 200, (
        f"Log-level POST returned {response.status_code}"
    )
    assert response.json().get("restart_required") is True, (
        "Settings API response missing restart_required=True"
    )


async def restart_self(settings_restart_url: str, *, timeout: float = 30.0) -> None:
    """Submit the scheduled self-restart; acceptance does not prove replacement."""
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.post(settings_restart_url, json={})
    response.raise_for_status()
    assert response.status_code == 200, (
        f"Self-restart POST returned {response.status_code}"
    )


async def wait_for_addon_replacement(
    settings_info: str,
    addon_url: str,
    baseline_instance_id: str,
    *,
    timeout: float = 180.0,
    poll_interval: float = 3.0,
) -> str:
    """Wait for a different process and a fresh MCP exchange with its endpoint."""
    deadline = time.monotonic() + timeout
    last: object = "replacement not observed"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            async with asyncio.timeout(remaining):
                instance_id = await get_instance_id(
                    settings_info, timeout=_call_timeout(deadline)
                )
                if instance_id != baseline_instance_id:
                    await call_tool_fresh(
                        addon_url,
                        "ha_get_overview",
                        {},
                        timeout=_call_timeout(deadline),
                    )
                    return instance_id
                last = f"still reached pre-restart process {baseline_instance_id}"
        except TRANSIENT_ADDON_ERRORS as error:
            LOG.debug("App replacement probe unavailable: %s", error)
            last = error
        await asyncio.sleep(min(poll_interval, max(deadline - time.monotonic(), 0)))
    raise AssertionError(
        "App replacement never completed a fresh MCP exchange within "
        f"{timeout}s (previous instance={baseline_instance_id}, last={last!r})"
    )


async def restore_info_level(
    settings_advanced: str,
    settings_restart: str,
    settings_info: str,
    addon_url: str,
    *,
    restore_timeout: float = 120.0,
    ready_timeout: float = 180.0,
    poll_interval: float = 3.0,
) -> None:
    """Restore INFO, then finish its replacement before another test connects."""
    deadline = time.monotonic() + restore_timeout
    last: object = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            async with asyncio.timeout(remaining):
                # The baseline belongs to this cleanup restart, not the earlier
                # DEBUG restart. Its old process may keep answering after 200.
                baseline = await get_instance_id(
                    settings_info, timeout=_call_timeout(deadline)
                )
                await post_log_level(
                    settings_advanced, "INFO", timeout=_call_timeout(deadline)
                )
            break
        except TRANSIENT_ADDON_ERRORS as error:
            LOG.debug("INFO restore preparation unavailable: %s", error)
            last = error
            await asyncio.sleep(min(poll_interval, max(deadline - time.monotonic(), 0)))
    else:
        raise AssertionError(
            f"Could not prepare the INFO restore restart within {restore_timeout}s "
            f"(last={last!r})"
        )
    # Resolve the budget before the attempt: expiration here cannot mean the
    # server accepted a restart. Once sent, even a lost response may mean it did.
    submission_timeout = _call_timeout(deadline)
    try:
        await restart_self(settings_restart, timeout=submission_timeout)
    except (httpx.HTTPError, TimeoutError) as error:
        LOG.debug("INFO restore restart outcome uncertain: %s", error)
    # Never replay an attempted restart; its old process may still be serving.
    await wait_for_addon_replacement(
        settings_info,
        addon_url,
        baseline,
        timeout=ready_timeout,
        poll_interval=poll_interval,
    )
