"""An auth failure during a mandatory snapshot is reported as what it is.

The snapshot of a file or YAML write goes through the same admin-only
``ha_mcp_tools`` services as the write, so a refused token fails there first.
Retrying the backup cannot help, and the write stays blocked.

Each case runs on a real ``hass`` with the File & YAML Tools entry set up, and
Core refuses the server's own client: a read-only user's token, a token Core
never issued, and a token Core revoked after the caller token was fetched (a
REST 401 whose WebSocket retry is refused as well).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import get_backup_manager
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.errors import TOOL_ERROR_LOG_LEVEL
from ha_mcp.tools.auto_backup import with_auto_backup
from ha_mcp.tools.backup_on_demand import capture_on_demand
from ha_mcp.tools.tools_filesystem import call_mcp_tools_service

from ._edit_yaml import make_edit_yaml

_FILE = ("file", "www/x.txt")


async def _revoked_after_bootstrap(
    hass: HomeAssistant, client: HomeAssistantClient
) -> HomeAssistantClient:
    """``client`` once it holds the caller token and Core has revoked its own."""
    await call_mcp_tools_service(client, "get_allowed_paths", {})
    refresh_token = hass.auth.async_validate_access_token(client.token)
    assert refresh_token is not None
    hass.auth.async_remove_refresh_token(refresh_token)
    return client


@pytest.fixture(
    params=[
        ("read_only_user", "AUTH_INSUFFICIENT_PERMISSIONS", _FILE),
        ("unknown_token", "AUTH_INVALID_TOKEN", _FILE),
        ("revoked_token", "AUTH_INVALID_TOKEN", _FILE),
        (
            "read_only_user",
            "AUTH_INSUFFICIENT_PERMISSIONS",
            ("yaml", "configuration.yaml::template"),
        ),
    ],
    ids=["non_admin", "rejected", "rejected_on_retry", "non_admin_yaml"],
)
async def refusal(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_read_only_access_token: str,
) -> AsyncIterator[tuple[HomeAssistantClient, str, tuple[str, str]]]:
    """A client Core refuses, the code that must surface, and the target."""
    caller, code, target = request.param
    await make_edit_yaml(hass)  # the tools entry, so the bootstrap service exists
    if caller == "revoked_token":
        yield await _revoked_after_bootstrap(hass, core_client), code, target
        return
    token = hass_read_only_access_token if caller == "read_only_user" else "not-a-token"
    client = HomeAssistantClient(
        base_url=core_client.base_url, token=token, verify_ssl=False
    )
    try:
        yield client, code, target
    finally:
        await client.close()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Auto-backup on, unthrottled, into ``tmp_path``."""
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path / "backups"),
    )
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_global_settings", lambda: settings
    )
    return settings


def _code(exc: pytest.ExceptionInfo[ToolError]) -> str:
    return json.loads(str(exc.value))["error"]["code"]


def _tool(ran: list[str], client: Any, domain: str) -> Any:
    @with_auto_backup(
        domain=domain,
        id_param="path",
        mandatory=True,
        client=client,
    )
    async def tool(*, path: str) -> str:
        ran.append(path)
        return "wrote"

    return tool


# Core closes a socket whose token it refused; the client's close reply then
# re-arms aiohttp's heartbeat timer on that closed server socket, which nothing
# cancels (as in test_component_todo_items.py).
_REFUSED_SOCKET_TIMER = pytest.mark.parametrize("expected_lingering_timers", [True])


@_REFUSED_SOCKET_TIMER
@pytest.mark.usefixtures("settings")
async def test_mandatory_write_reports_the_failure_and_stays_blocked(
    refusal: tuple[HomeAssistantClient, str, tuple[str, str]],
) -> None:
    client, expected_code, (domain, entity_id) = refusal
    ran: list[str] = []

    with pytest.raises(ToolError) as exc:
        await _tool(ran, client, domain)(path=entity_id)

    assert _code(exc) == expected_code
    assert exc.value.log_level == TOOL_ERROR_LOG_LEVEL
    assert ran == []


async def test_on_demand_capture_reports_a_missing_tools_entry(
    core_client: HomeAssistantClient, settings: SimpleNamespace
) -> None:
    # No tools entry is set up, so Core lists no ha_mcp_tools services.
    mgr = get_backup_manager(core_client, settings)

    with pytest.raises(ToolError) as exc:
        await capture_on_demand(mgr, *_FILE)

    assert _code(exc) == "COMPONENT_NOT_INSTALLED"


@_REFUSED_SOCKET_TIMER
async def test_on_demand_capture_reports_the_failure(
    refusal: tuple[HomeAssistantClient, str, tuple[str, str]],
    settings: SimpleNamespace,
) -> None:
    client, expected_code, (domain, entity_id) = refusal
    mgr = get_backup_manager(client, settings)

    with pytest.raises(ToolError) as exc:
        await capture_on_demand(mgr, domain, entity_id)

    assert _code(exc) == expected_code
