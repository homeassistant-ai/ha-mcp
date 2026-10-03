"""Core's Model Context Protocol integration discovers the app (#2307).

The server posts its URL to the Supervisor's ``/discovery`` when the app starts.
On Core 2026.10+ that raises an ``mcp`` config flow; confirming it makes Core
connect to the announced URL, list the tools and convert every input schema,
so a loaded entry proves the hostname resolves from Core, the URL has no
redirecting trailing slash, and the whole tool surface is accepted. Older
Cores must not be announced to at all.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

import pytest
from haos_runtime import (
    HA_MCP_DEV_ADDON_SLUG,
    HA_MCP_TEST_SECRET_PATH,
    _home_assistant_ws_command,
)

pytestmark = [pytest.mark.haos_only]

_FLOW_TIMEOUT = 60.0
_LOAD_TIMEOUT = 120.0


def _ws(info: dict[str, Any], command: dict[str, Any]) -> Any:
    return _home_assistant_ws_command(info["base_url"], info["token"], command)


def _rest(info: dict[str, Any], method: str, path: str, body: Any = None) -> Any:
    req = urllib.request.Request(
        f"{info['base_url']}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {info['token']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _app_announcements(info: dict[str, Any]) -> list[dict[str, Any]]:
    stored = _ws(
        info, {"type": "supervisor/api", "endpoint": "/discovery", "method": "get"}
    )
    return [
        message
        for message in stored["discovery"]
        if message["addon"] == HA_MCP_DEV_ADDON_SLUG and message["service"] == "mcp"
    ]


def _mcp_discovery_flows(info: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        flow
        for flow in _ws(info, {"type": "config_entries/flow/progress"})
        if flow["handler"] == "mcp" and flow["context"].get("source") == "hassio"
    ]


def _core_handles_discovery(version: str) -> bool:
    year, month = (int(part) for part in version.split(".")[:2])
    return (year, month) >= (2026, 10)


@pytest.mark.inaddon_only
@pytest.mark.timeout(int(_FLOW_TIMEOUT + _LOAD_TIMEOUT + 120))
def test_core_discovers_and_connects_to_the_app(
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    info = ha_container_with_fresh_config
    version = _ws(info, {"type": "get_config"})["version"]

    if not _core_handles_discovery(version):
        # Older Cores would turn the message into a card that drops the URL.
        assert _app_announcements(info) == []
        return

    (message,) = _app_announcements(info)
    url = message["config"]["url"]
    assert url.endswith(HA_MCP_TEST_SECRET_PATH), url

    # The app announced while the test image was baked, before this Core
    # session, and the Supervisor deduplicates its unchanged re-announcements.
    # Deliver the stored message the way the Supervisor does for a new one.
    _rest(info, "POST", f"/api/hassio_push/discovery/{message['uuid']}")
    deadline = time.monotonic() + _FLOW_TIMEOUT
    while not (flows := _mcp_discovery_flows(info)):
        assert time.monotonic() < deadline, f"Core {version} opened no mcp flow"
        time.sleep(3)
    (flow,) = flows
    assert flow["step_id"] == "hassio_confirm"

    result = _rest(
        info, "POST", f"/api/config/config_entries/flow/{flow['flow_id']}", {}
    )
    assert result["type"] == "create_entry", result
    entry_id = result["result"]["entry_id"]
    try:
        deadline = time.monotonic() + _LOAD_TIMEOUT
        while True:
            entry = _ws(
                info, {"type": "config_entries/get_single", "entry_id": entry_id}
            )
            state = entry["config_entry"]["state"]
            if state == "loaded":
                break
            assert time.monotonic() < deadline, f"mcp entry stuck in {state}: {entry}"
            time.sleep(3)
        assert entry["config_entry"]["title"] == "ha-mcp"
    finally:
        _rest(info, "DELETE", f"/api/config/config_entries/entry/{entry_id}")
