"""Admin calls to ``ha_mcp_tools`` component services over HA REST.

The E2E suites drive these services directly with the admin token, the way the
ha-mcp server drives them. Each helper asserts that the call succeeded: the
services answer a non-admin caller, and the setters a rejected caller token,
with HTTP 200 and ``success: false``.
"""

from __future__ import annotations

from typing import Any

import httpx

from tests.test_constants import TEST_TOKEN


async def _call_admin_service(
    base_url: str, service: str, data: dict[str, Any]
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/api/services/ha_mcp_tools/{service}",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "Content-Type": "application/json",
            },
            params={"return_response": ""},
            json=data,
        )
    assert resp.status_code == 200, f"{service} failed: {resp.text!r}"
    sr = resp.json().get("service_response", {})
    assert sr.get("success") is True, f"{service} failed: {resp.text!r}"
    return sr


async def bootstrap_caller_token(base_url: str) -> str:
    """Fetch the caller token via the admin-gated bootstrap service."""
    sr = await _call_admin_service(base_url, "get_caller_token", {})
    assert sr.get("token"), f"get_caller_token returned no token: {sr!r}"
    return sr["token"]


async def set_allowed_paths(
    base_url: str, caller_token: str, paths: list[str]
) -> dict[str, Any]:
    """Call set_allowed_paths; assert success, return the response."""
    return await _call_admin_service(
        base_url,
        "set_allowed_paths",
        {"_ha_mcp_token": caller_token, "paths": paths},
    )


async def set_extra_yaml_keys(
    base_url: str, caller_token: str, keys: list[str]
) -> dict[str, Any]:
    """Call set_extra_yaml_keys; assert success, return the response."""
    return await _call_admin_service(
        base_url,
        "set_extra_yaml_keys",
        {"_ha_mcp_token": caller_token, "keys": keys},
    )
