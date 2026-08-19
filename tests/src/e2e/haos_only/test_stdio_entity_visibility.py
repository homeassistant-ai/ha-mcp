"""Full stdio visibility sequence against a real HAOS backend."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import requests

from ha_mcp.visibility.persistence import load_visibility_config

from ..utilities.assertions import MCPAssertions, safe_call_tool

pytestmark = pytest.mark.haos_stdio_only

logger = logging.getLogger(__name__)

_HIDDEN_ENTITY = "light.bed_light"
_SIDECAR_URL_TIMEOUT_SECONDS = 15.0


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
    """Wait for the detached sidecar to atomically publish its settings URL."""
    url_file = config_dir / "ui.url"
    deadline = asyncio.get_running_loop().time() + _SIDECAR_URL_TIMEOUT_SECONDS
    last_missing: FileNotFoundError | None = None
    last_observation = "not checked"
    while asyncio.get_running_loop().time() < deadline:
        try:
            url = url_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            last_missing = exc
            url = ""
            last_observation = repr(exc)
            logger.debug("Waiting for stdio sidecar URL at %s: %s", url_file, exc)
        if url:
            return url
        if last_missing is None:
            last_observation = "file exists but is empty"
        await asyncio.sleep(0.1)
    raise AssertionError(
        "stdio sidecar did not publish a non-empty URL at "
        f"{url_file} within {_SIDECAR_URL_TIMEOUT_SECONDS:.0f}s; "
        f"last observation: {last_observation}"
    )


async def test_stdio_sidecar_config_drives_real_visibility_middleware(
    mcp_client: Any,
    haos_stdio_config_dir: Path,
) -> None:
    """Prove UI persistence, HA REST/WS resolution, and report-tool policy."""
    settings_url: str | None = None
    original_config: dict[str, Any] | None = None
    config_changed = False

    try:
        settings_url = await _wait_for_settings_url(haos_stdio_config_dir)
        async with MCPAssertions(mcp_client) as mcp:
            # Seed the subprocess usage log with the entity id. report_issue's
            # recent_logs field makes the later outbound-policy assertion
            # deterministic without creating or mutating any HA entity.
            await mcp.call_tool_success("ha_get_state", {"entity_id": _HIDDEN_ENTITY})

            current = await asyncio.to_thread(_visibility_request, settings_url, "GET")
            original_config = dict(current)
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
            config_changed = True
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
                {"tool_call_count": 16, "fields": ["recent_logs"]},
            )
            assert _HIDDEN_ENTITY in json.dumps(report), report

            unrestricted_report_config = await asyncio.to_thread(
                _visibility_request, settings_url, "GET"
            )
            assert unrestricted_report_config["enabled"] is True
            assert unrestricted_report_config["enforce"] is True
            assert unrestricted_report_config["deny_entity_ids"] == [_HIDDEN_ENTITY]

            await asyncio.to_thread(
                _visibility_request,
                settings_url,
                "PUT",
                {
                    **unrestricted_report_config,
                    "restrict_report_issue": True,
                },
            )
            restricted_report = await safe_call_tool(
                mcp_client,
                "ha_report_issue",
                {"tool_call_count": 16, "fields": ["recent_logs"]},
            )
            assert restricted_report["error"]["code"] == "ENTITY_VISIBILITY_ENFORCED", (
                restricted_report
            )
            assert (
                "would include an entity restricted"
                in restricted_report["error"]["message"]
            ), restricted_report
    finally:
        # Session-scoped stdio clients are shared by many tests on this worker;
        # restore the exact prior config through the same version-checked sidecar
        # API the test exercises, so cleanup cannot rewind the on-disk version.
        if settings_url is not None and original_config is not None and config_changed:
            latest = await asyncio.to_thread(_visibility_request, settings_url, "GET")
            await asyncio.to_thread(
                _visibility_request,
                settings_url,
                "PUT",
                {
                    **original_config,
                    "version": latest["version"],
                },
            )
