"""Story verification — automated HA checks after agent run."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from fastmcp import Client


async def _retry(fn, attempts: int = 3, delay: float = 2.0) -> Any | None:
    """Call fn() up to `attempts` times, returning first non-None result."""
    for i in range(attempts):
        result = await fn()
        if result is not None:
            return result
        if i < attempts - 1:
            await asyncio.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# State checks — use /api/states (REST, with retry for HA registration lag)
# ---------------------------------------------------------------------------


async def _check_entity_exists(client: httpx.AsyncClient, check: dict) -> dict:
    entity_id = check["entity_id"]

    async def attempt():
        r = await client.get(f"/api/states/{entity_id}")
        if r.status_code == 200:
            return {"passed": True, "detail": f"Found {entity_id}"}
        return None

    result = await _retry(attempt)
    if result is not None:
        return {**check, "type": "entity_exists", **result}
    return {**check, "type": "entity_exists", "passed": False, "detail": f"{entity_id} not found"}


async def _check_entity_state(client: httpx.AsyncClient, check: dict) -> dict:
    entity_id = check["entity_id"]
    expected = check["state"]

    async def attempt():
        r = await client.get(f"/api/states/{entity_id}")
        if r.status_code == 200 and r.json().get("state") == expected:
            return {"passed": True, "detail": f"state={expected}"}
        return None

    result = await _retry(attempt)
    if result is not None:
        return {**check, "type": "entity_state", **result}

    # Diagnostic read to get current actual state for error reporting.
    r = await client.get(f"/api/states/{entity_id}")
    try:
        actual = r.json().get("state") if r.status_code == 200 else "not found"
    except Exception:
        actual = "not found"
    return {**check, "type": "entity_state", "passed": False, "detail": f"expected={expected}, actual={actual}"}


async def _find_in_states(client: httpx.AsyncClient, domain: str, alias: str) -> dict | None:
    """Search /api/states for entity in domain whose friendly_name contains alias. Returns state dict or None."""
    r = await client.get("/api/states")
    if r.status_code != 200:
        return None
    try:
        states = r.json()
    except Exception:
        return None
    for state in states:
        if state["entity_id"].startswith(f"{domain}."):
            name = state["attributes"].get("friendly_name", "")
            if alias.lower() in name.lower():
                return state
    return None


async def _check_domain_entity_exists(
    client: httpx.AsyncClient, check: dict, domain: str, check_type: str
) -> dict:
    alias = check["alias"]

    async def attempt():
        state = await _find_in_states(client, domain, alias)
        if state:
            return {"passed": True, "detail": f"Found {state['entity_id']}"}
        return None

    result = await _retry(attempt)
    if result is not None:
        return {**check, "type": check_type, **result}
    return {**check, "type": check_type, "passed": False, "detail": f"No {domain} matching '{alias}'"}


async def _check_automation_exists(client: httpx.AsyncClient, check: dict) -> dict:
    return await _check_domain_entity_exists(client, check, "automation", "automation_exists")


async def _check_script_exists(client: httpx.AsyncClient, check: dict) -> dict:
    return await _check_domain_entity_exists(client, check, "script", "script_exists")


# ---------------------------------------------------------------------------
# Config checks — use /api/config/automation/config (REST, no retry)
# ---------------------------------------------------------------------------


async def _find_in_automation_config(client: httpx.AsyncClient, alias: str) -> dict | None:
    """Return the automation config dict whose alias matches, or None."""
    # Find entity via states; unique_id is in the 'id' attribute of the same state dict.
    state = await _find_in_states(client, "automation", alias)
    if not state:
        return None
    unique_id = state.get("attributes", {}).get("id")
    if not unique_id:
        return None
    r = await client.get(f"/api/config/automation/config/{unique_id}")
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


async def _check_automation_has_condition(client: httpx.AsyncClient, check: dict) -> dict:
    alias = check["alias"]
    auto = await _find_in_automation_config(client, alias)
    if auto is None:
        return {**check, "type": "automation_has_condition", "passed": False, "detail": f"Automation '{alias}' not found"}
    conditions = auto.get("condition", auto.get("conditions", []))
    if conditions:
        return {**check, "type": "automation_has_condition", "passed": True, "detail": f"{len(conditions)} condition(s)"}
    return {**check, "type": "automation_has_condition", "passed": False, "detail": "No conditions found"}


async def _check_automation_has_trigger(client: httpx.AsyncClient, check: dict) -> dict:
    alias = check["alias"]
    auto = await _find_in_automation_config(client, alias)
    if auto is None:
        return {**check, "type": "automation_has_trigger", "passed": False, "detail": f"Automation '{alias}' not found"}
    triggers = auto.get("trigger", auto.get("triggers", []))
    if triggers:
        return {**check, "type": "automation_has_trigger", "passed": True, "detail": f"{len(triggers)} trigger(s)"}
    return {**check, "type": "automation_has_trigger", "passed": False, "detail": "No triggers found"}


# ---------------------------------------------------------------------------
# Registry / dashboard checks — use shared in-process MCP client
# ---------------------------------------------------------------------------


async def _mcp_call(mcp_client, tool_name: str, args: dict | None = None) -> str:
    """Call an MCP tool and return concatenated text output."""
    result = await mcp_client.call_tool(tool_name, args or {})
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


async def _check_area_exists(mcp_client, check: dict) -> dict:
    name = check["name"]
    try:
        text = await _mcp_call(mcp_client, "ha_config_list_areas")
        if name.lower() in text.lower():
            return {**check, "type": "area_exists", "passed": True, "detail": f"Found area '{name}'"}
    except Exception as e:
        return {**check, "type": "area_exists", "passed": False, "detail": f"Error: {e}"}
    return {**check, "type": "area_exists", "passed": False, "detail": f"Area '{name}' not found"}


async def _check_label_exists(mcp_client, check: dict) -> dict:
    name = check["name"]
    try:
        text = await _mcp_call(mcp_client, "ha_config_get_label")
        if name.lower() in text.lower():
            return {**check, "type": "label_exists", "passed": True, "detail": f"Found label '{name}'"}
    except Exception as e:
        return {**check, "type": "label_exists", "passed": False, "detail": f"Error: {e}"}
    return {**check, "type": "label_exists", "passed": False, "detail": f"Label '{name}' not found"}


async def _check_dashboard_exists(mcp_client, check: dict) -> dict:
    url_path = check["url_path"]
    try:
        text = await _mcp_call(mcp_client, "ha_config_get_dashboard", {"list_only": True})
        if url_path in text:
            return {**check, "type": "dashboard_exists", "passed": True, "detail": f"Found dashboard '{url_path}'"}
    except Exception as e:
        return {**check, "type": "dashboard_exists", "passed": False, "detail": f"Error: {e}"}
    return {**check, "type": "dashboard_exists", "passed": False, "detail": f"No dashboard with url_path='{url_path}'"}


# ---------------------------------------------------------------------------
# Response checks — string/regex on agent output (no HA call needed)
# ---------------------------------------------------------------------------


def _check_response_contains(check: dict, agent_output: str) -> dict:
    value = check["value"]
    if value.lower() in agent_output.lower():
        return {**check, "type": "response_contains", "passed": True, "detail": f"Found '{value}'"}
    return {**check, "type": "response_contains", "passed": False, "detail": f"'{value}' not in response"}


def _check_response_matches(check: dict, agent_output: str) -> dict:
    pattern = check["pattern"]
    if re.search(pattern, agent_output):
        return {**check, "type": "response_matches", "passed": True, "detail": f"Pattern matched: {pattern}"}
    return {**check, "type": "response_matches", "passed": False, "detail": f"Pattern not matched: {pattern}"}


# ---------------------------------------------------------------------------
# Check registries
# ---------------------------------------------------------------------------

SYNC_CHECKS = {
    "entity_exists": _check_entity_exists,
    "entity_state": _check_entity_state,
    "automation_exists": _check_automation_exists,
    "script_exists": _check_script_exists,
    "automation_has_condition": _check_automation_has_condition,
    "automation_has_trigger": _check_automation_has_trigger,
}

ASYNC_CHECKS = {
    "area_exists": _check_area_exists,
    "label_exists": _check_label_exists,
    "dashboard_exists": _check_dashboard_exists,
}

RESPONSE_CHECKS = {
    "response_contains": _check_response_contains,
    "response_matches": _check_response_matches,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def verify_ha_checks(
    ha_url: str,
    ha_token: str,
    checks: list[dict],
    agent_output: str,
    mcp_client: Client,
) -> list[dict]:
    """Run all checks concurrently and return results list [{type, passed, detail, ...}].

    Caller owns the ``mcp_client`` lifecycle so one in-process server can be
    shared across many verify_ha_checks calls.
    """
    headers = {"Authorization": f"Bearer {ha_token}"}

    async def run_check(check: dict, http) -> dict:
        check_type = check["type"]
        if check_type in SYNC_CHECKS:
            return await SYNC_CHECKS[check_type](http, check)
        if check_type in ASYNC_CHECKS:
            return await ASYNC_CHECKS[check_type](mcp_client, check)
        if check_type in RESPONSE_CHECKS:
            return RESPONSE_CHECKS[check_type](check, agent_output)
        return {**check, "passed": False, "detail": f"Unknown check type: {check_type}"}

    async with httpx.AsyncClient(base_url=ha_url, headers=headers, timeout=10) as http:
        return list(await asyncio.gather(*[run_check(c, http) for c in checks]))
