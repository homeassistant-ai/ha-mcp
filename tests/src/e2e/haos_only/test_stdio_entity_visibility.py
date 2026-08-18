"""Full stdio visibility sequence against a real HAOS backend."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import requests

from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import (
    load_visibility_config,
    save_visibility_config,
)

from ..utilities.assertions import MCPAssertions, safe_call_tool

pytestmark = pytest.mark.haos_stdio_only

_HIDDEN_ENTITY = "light.bed_light"


def _visibility_request(
    settings_url: str,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the secret-path sidecar API without consulting host proxies."""
    endpoint = settings_url.removesuffix("/settings") + "/api/visibility/config"
    session = requests.Session()
    session.trust_env = False
    response = session.request(method, endpoint, json=payload, timeout=10)
    response.raise_for_status()
    body = response.json()
    assert isinstance(body, dict), body
    return body


async def _wait_for_settings_url(config_dir: Path) -> str:
    url_file = config_dir / "ui.url"
    for _attempt in range(50):
        try:
            url = url_file.read_text(encoding="utf-8").strip()
        except OSError:
            url = ""
        if url:
            return url
        await asyncio.sleep(0.1)
    raise AssertionError(f"stdio sidecar did not publish {url_file.name}")


async def test_stdio_sidecar_config_drives_real_visibility_middleware(
    mcp_client: Any,
    haos_stdio_config_dir: Path,
) -> None:
    """Prove UI persistence, HA REST/WS resolution, and report-tool policy."""
    settings_url = await _wait_for_settings_url(haos_stdio_config_dir)

    try:
        async with MCPAssertions(mcp_client) as mcp:
            # Seed the subprocess usage log with the entity id. report_issue's
            # recent_logs field makes the later outbound-policy assertion
            # deterministic without creating or mutating any HA entity.
            await mcp.call_tool_success("ha_get_state", {"entity_id": _HIDDEN_ENTITY})

            current = await asyncio.to_thread(_visibility_request, settings_url, "GET")
            saved = await asyncio.to_thread(
                _visibility_request,
                settings_url,
                "PUT",
                {
                    "version": current["version"],
                    "enabled": True,
                    "enforce": True,
                    "exclude_categories": [],
                    "deny_entity_ids": [_HIDDEN_ENTITY],
                    "restrict_report_issue": False,
                },
            )
            persisted = load_visibility_config(haos_stdio_config_dir)
            assert persisted.version == saved["version"]
            assert persisted.deny_entity_ids == [_HIDDEN_ENTITY]
            assert persisted.restrict_report_issue is False

            # A clean tool call forces middleware resolution of the hidden set.
            # That resolver reads live states over REST and the entity registry
            # over WebSocket before allowing this entity-independent result.
            await mcp.call_tool_success("ha_config_get_label", {})

            denied = await safe_call_tool(
                mcp_client, "ha_get_state", {"entity_id": _HIDDEN_ENTITY}
            )
            assert denied["error"]["code"] == "ENTITY_NOT_FOUND", denied

            report = await mcp.call_tool_success(
                "ha_report_issue",
                {"tool_call_count": 2, "fields": ["recent_logs"]},
            )
            assert _HIDDEN_ENTITY in json.dumps(report), report

            restricted_current = await asyncio.to_thread(
                _visibility_request, settings_url, "GET"
            )
            assert restricted_current["enabled"] is True
            assert restricted_current["enforce"] is True
            assert restricted_current["deny_entity_ids"] == [_HIDDEN_ENTITY]

            await asyncio.to_thread(
                _visibility_request,
                settings_url,
                "PUT",
                {
                    **restricted_current,
                    "restrict_report_issue": True,
                },
            )
            restricted_report = await safe_call_tool(
                mcp_client,
                "ha_report_issue",
                {"tool_call_count": 2, "fields": ["recent_logs"]},
            )
            assert restricted_report["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED", (
                restricted_report
            )
    finally:
        # Session-scoped stdio clients are shared by many tests on this worker;
        # always restore no-op visibility even when an API assertion fails.
        save_visibility_config(
            haos_stdio_config_dir,
            VisibilityConfig(enabled=False, enforce=False, exclude_categories=[]),
        )
