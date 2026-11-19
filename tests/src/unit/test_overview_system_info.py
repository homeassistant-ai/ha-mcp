"""``ha_get_overview``: system_info, ``fields=`` projection and server metadata.

The tool runs over the server's own client against a real ``hass``; every
config, notification and repairs answer comes from Core.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.const import __version__
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.setup import async_setup_component

from ha_mcp import settings_ui, update_check
from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.mcp.types import Implementation
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.config import reset_global_settings
from ha_mcp.tool_catalog import CatalogMode, catalog_endpoint
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools, register_search_tools

from ._real_server import build_server
from ._update_check_sources import PyPI
from ._update_check_sources import pypi as pypi

SIDECAR_URL = "http://127.0.0.1:8099/private_abc/settings"


@pytest.fixture
async def answering(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Load the integrations that answer the registry, notification and
    repairs reads of the overview."""
    for domain in ("config", "persistent_notification", "repairs"):
        assert await async_setup_component(hass, domain, {})


@pytest.fixture
def smart(core_client: HomeAssistantClient) -> SmartSearchTools:
    return SmartSearchTools(client=core_client)


@pytest.fixture
def tools(core_client: HomeAssistantClient, smart: SmartSearchTools) -> SearchTools:
    return SearchTools(core_client, smart)


@pytest.fixture
def full_collection(smart: SmartSearchTools) -> Iterator[AsyncMock]:
    """A spy on the entity-wide collection; it still runs."""
    with patch.object(
        smart, "get_system_overview", wraps=smart.get_system_overview
    ) as spy:
        yield spy


@pytest.fixture
def config_read(core_client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """A spy on the config read; Core still answers it."""
    with patch.object(core_client, "get_config", wraps=core_client.get_config) as spy:
        yield spy


@pytest.fixture
def config_unanswered(core_client: HomeAssistantClient) -> Iterator[None]:
    """The config read gets no answer at all."""
    with patch.object(
        core_client,
        "get_config",
        side_effect=HomeAssistantConnectionError("config unavailable"),
    ):
        yield


def _no_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ha_mcp.stdio_settings_sidecar.live_sidecar_url", lambda: None)


def _sidecar_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ha_mcp.stdio_settings_sidecar.live_sidecar_url", lambda: SIDECAR_URL
    )


def _http_mounted(monkeypatch: pytest.MonkeyPatch, prefix: str | None) -> None:
    _no_sidecar(monkeypatch)
    monkeypatch.setattr("ha_mcp.settings_ui.get_http_settings_prefix", lambda: prefix)


# system_info


async def test_full_detail_reports_the_configured_allowlist(
    hass: HomeAssistant, tools: SearchTools, tmp_path: Path
) -> None:
    """An agent reasoning about which paths it may touch sees the external
    directories Home Assistant is configured to allow."""
    await async_process_ha_core_config(
        hass, {"allowlist_external_dirs": [str(tmp_path)]}
    )

    result = await tools.ha_get_overview(detail_level="full", fields=["system_info"])

    allowlist = result["system_info"]["allowlist_external_dirs"]
    assert str(tmp_path) in allowlist
    assert set(allowlist) == hass.config.allowlist_external_dirs


async def test_minimal_detail_leaves_out_the_allowlist(
    hass: HomeAssistant, tools: SearchTools, tmp_path: Path
) -> None:
    await async_process_ha_core_config(
        hass, {"allowlist_external_dirs": [str(tmp_path)]}
    )

    result = await tools.ha_get_overview(fields=["system_info"])

    assert "allowlist_external_dirs" not in result["system_info"]


# fields= projection


@pytest.mark.parametrize("mode", ["full", "search"])
async def test_tool_discovery_hint_follows_the_connection_catalog(
    tools: SearchTools, answering: None, mode: CatalogMode
) -> None:
    """Only a search connection is told to discover tools with
    ha_search_tools; a full-catalog connection has no such tool."""
    with catalog_endpoint(mode):
        result = await tools.ha_get_overview()
    assert ("tool_discovery" in result) is (mode == "search")


async def test_unprojected_overview_returns_every_section(
    tools: SearchTools, answering: None
) -> None:
    result = await tools.ha_get_overview()

    assert result["success"] is True
    assert "system_info" in result
    assert "domain_stats" in result


async def test_single_field_projection_drops_every_other_section(
    tools: SearchTools, answering: None
) -> None:
    result = await tools.ha_get_overview(fields=["system_info"])

    assert result["success"] is True
    assert result["system_info"]["version"] == __version__
    for key in (
        "domain_stats",
        "area_analysis",
        "domains",
        "entity_summary",
        "total_entities",
        "repair_count",
    ):
        assert key not in result, f"unexpected key {key!r} survived projection"


async def test_system_info_projection_reads_only_the_config(
    tools: SearchTools,
    answering: None,
    config_read: AsyncMock,
    sent: AsyncMock,
    full_collection: AsyncMock,
) -> None:
    """A narrow response must also produce a narrow Home Assistant workload."""
    result = await tools.ha_get_overview(fields=["system_info"])

    assert result["system_info"]["version"] == __version__
    config_read.assert_awaited_once()
    sent.assert_not_awaited()
    full_collection.assert_not_awaited()


async def test_independent_sections_read_only_their_own_commands(
    hass: HomeAssistant,
    tools: SearchTools,
    answering: None,
    config_read: AsyncMock,
    sent: AsyncMock,
    full_collection: AsyncMock,
) -> None:
    persistent_notification.async_create(
        hass, "Body", title="Test", notification_id="test"
    )

    result = await tools.ha_get_overview(
        fields=["system_info", "notifications", "repair_count"]
    )

    assert [n["notification_id"] for n in result["notifications"]] == ["test"]
    assert result["repair_count"] == 0
    assert "system_info" in result
    config_read.assert_awaited_once()
    assert sorted(call.args[0]["type"] for call in sent.await_args_list) == [
        "persistent_notification/get",
        "repairs/list_issues",
    ]
    full_collection.assert_not_awaited()


async def test_entity_field_projection_runs_the_full_collection(
    hass: HomeAssistant,
    tools: SearchTools,
    answering: None,
    full_collection: AsyncMock,
) -> None:
    for name in ("one", "two", "three"):
        hass.states.async_set(f"light.{name}", "on")

    result = await tools.ha_get_overview(fields=["domain_stats"])

    assert result["domain_stats"]["light"]["count"] == 3
    full_collection.assert_awaited_once()


async def test_mixed_projection_keeps_only_the_requested_sections(
    hass: HomeAssistant, tools: SearchTools, answering: None
) -> None:
    hass.states.async_set("light.one", "on")

    result = await tools.ha_get_overview(fields=["system_info", "domain_stats"])

    assert "system_info" in result
    assert result["domain_stats"]["light"]["count"] == 1
    assert "area_analysis" not in result


async def test_domains_field_warns_and_skips_the_full_collection(
    tools: SearchTools, full_collection: AsyncMock
) -> None:
    """``domains`` is not a response key; the warning names the sections a
    narrow request can ask for instead of collecting every entity."""
    result = await tools.ha_get_overview(fields=["domains"])

    assert result["success"] is True
    assert "domains" not in result
    warning = result["warnings"][0]
    for name in ("domains", "system_info", "notifications", "repairs"):
        assert name in warning
    full_collection.assert_not_awaited()


async def test_requested_notifications_warn_when_disabled(
    tools: SearchTools, sent: AsyncMock, full_collection: AsyncMock
) -> None:
    result = await tools.ha_get_overview(
        fields=["notifications"], include_notifications=False
    )

    assert result["success"] is True
    assert "notifications" not in result
    assert result["warnings"] == ["notifications omitted: include_notifications=False"]
    sent.assert_not_awaited()
    full_collection.assert_not_awaited()


async def test_warnings_projection_runs_the_full_collection(
    tools: SearchTools, answering: None, full_collection: AsyncMock
) -> None:
    """A caller asking only for ``warnings`` gets the entity collection's
    warnings, not only the narrow sections'."""
    await tools.ha_get_overview(fields=["warnings"])

    full_collection.assert_awaited_once()


async def test_unknown_field_is_absent_and_skips_the_full_collection(
    tools: SearchTools, full_collection: AsyncMock
) -> None:
    result = await tools.ha_get_overview(fields=["nonexistent_key"])

    assert result["success"] is True
    assert "nonexistent_key" not in result
    full_collection.assert_not_awaited()


async def test_unanswered_config_read_degrades_system_info_with_warning(
    tools: SearchTools, config_unanswered: None, full_collection: AsyncMock
) -> None:
    result = await tools.ha_get_overview(fields=["system_info"])

    assert result["success"] is True
    assert "system_info" not in result
    assert result["warnings"] == ["system info unavailable: config unavailable"]
    full_collection.assert_not_awaited()


async def test_repairs_still_read_after_system_info_failure(
    tools: SearchTools,
    answering: None,
    config_unanswered: None,
    sent: AsyncMock,
    full_collection: AsyncMock,
) -> None:
    result = await tools.ha_get_overview(fields=["system_info", "repair_count"])

    assert result["success"] is True
    assert result["repair_count"] == 0
    assert result["warnings"] == ["system info unavailable: config unavailable"]
    sent.assert_awaited_once_with({"type": "repairs/list_issues"})
    full_collection.assert_not_awaited()


async def test_rejected_notifications_read_warns_instead_of_reporting_none(
    tools: SearchTools, full_collection: AsyncMock
) -> None:
    """Without the persistent_notification integration Core rejects the
    read; the overview says so rather than reporting no notifications."""
    result = await tools.ha_get_overview(fields=["notifications"])

    assert result["success"] is True
    assert result["notifications"] == []
    [warning] = result["warnings"]
    assert warning.startswith("notifications unavailable: ")
    assert "Unknown command" in warning
    full_collection.assert_not_awaited()


async def test_rejected_repairs_read_is_returned_by_repairs_error_projection(
    hass: HomeAssistant, tools: SearchTools, full_collection: AsyncMock
) -> None:
    assert await async_setup_component(hass, "config", {})

    result = await tools.ha_get_overview(fields=["repairs_error"])

    assert result["success"] is True
    assert result["repairs_error"].startswith("Could not fetch repairs: ")
    assert "Unknown command" in result["repairs_error"]
    assert "warnings" not in result
    full_collection.assert_not_awaited()


async def test_non_list_fields_is_rejected_as_validation_failure(
    tools: SearchTools,
) -> None:
    fields: Any = 123

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_get_overview(fields=fields)

    error = json.loads(str(exc_info.value))
    assert error["error"]["code"] == "VALIDATION_FAILED"
    assert error.get("parameter") == "fields"


# system_summary version


async def test_system_summary_carries_the_running_version(
    tools: SearchTools, answering: None
) -> None:
    result = await tools.ha_get_overview()

    assert result["system_summary"]["version"] == __version__


async def test_system_summary_version_unknown_when_config_unanswered(
    tools: SearchTools, answering: None, config_unanswered: None
) -> None:
    result = await tools.ha_get_overview()

    assert result["system_summary"]["version"] == "unknown"


# settings page (issues #863, #1458)


async def test_settings_url_surfaced_when_sidecar_running(
    tools: SearchTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sidecar_running(monkeypatch)

    result = await tools.ha_get_overview()

    assert result.get("settings_url") == SIDECAR_URL


async def test_settings_url_omitted_when_no_sidecar(
    tools: SearchTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict ``not in``: an empty or null ``settings_url`` on every call
    would hand the user a broken link."""
    _no_sidecar(monkeypatch)

    result = await tools.ha_get_overview()

    assert "settings_url" not in result


async def test_settings_url_survives_fields_projection(
    tools: SearchTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that narrows the overview with ``fields=`` can still hand the
    user the settings URL."""
    _sidecar_running(monkeypatch)

    result = await tools.ha_get_overview(fields=["system_info"])

    assert result.get("settings_url") == SIDECAR_URL
    assert "system_info" in result


async def test_settings_url_hint_when_http_mounted(
    tools: SearchTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standalone HTTP cannot know its external host, so it points at the
    mount path instead of guessing a URL."""
    _http_mounted(monkeypatch, "/mcp")

    result = await tools.ha_get_overview()

    assert "settings_url" not in result
    assert "/mcp/settings" in result.get("settings_url_hint", "")


async def test_no_settings_fields_without_sidecar_or_http_mount(
    tools: SearchTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    _http_mounted(monkeypatch, None)

    result = await tools.ha_get_overview()

    assert "settings_url" not in result
    assert "settings_url_hint" not in result


@pytest.mark.parametrize("rejected_secret", ["", "/private_{token}"])
async def test_rejected_registration_clears_stale_http_hint(
    tools: SearchTools,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    rejected_secret: str,
) -> None:
    """A rejected later registration must not advertise an older server."""
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    _no_sidecar(monkeypatch)
    server = build_server(monkeypatch, core_client)
    settings_ui.reset_http_settings_state()
    try:
        settings_ui.register_settings_routes(FastMCP("old"), server, secret_path="/old")
        assert settings_ui.get_http_settings_prefix() == "/old"
        settings_ui.register_settings_routes(
            FastMCP("new"), server, secret_path=rejected_secret
        )

        result = await tools.ha_get_overview()

        assert "settings_url_hint" not in result
    finally:
        settings_ui.reset_http_settings_state()


async def test_settings_url_hint_normalizes_trailing_slash(
    tools: SearchTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MCP_SECRET_PATH`` is not normalized, so ``/mcp/`` must not yield
    ``//settings``."""
    _http_mounted(monkeypatch, "/mcp/")

    result = await tools.ha_get_overview()

    hint = result.get("settings_url_hint", "")
    assert "/mcp/settings" in hint
    assert "/mcp//settings" not in hint


async def test_settings_url_hint_survives_fields_projection(
    tools: SearchTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    _http_mounted(monkeypatch, "/mcp")

    result = await tools.ha_get_overview(fields=["system_info"])

    assert "system_info" in result
    assert "/mcp/settings" in result.get("settings_url_hint", "")


# Read Only Mode (#1569)


async def test_read_only_keys_absent_when_flag_off(tools: SearchTools) -> None:
    """Strict ``not in``, so emitting them on every overview is caught."""
    result = await tools.ha_get_overview()

    assert "read_only_mode" not in result
    assert "read_only_mode_hint" not in result


async def test_read_only_keys_present_and_survive_fields_projection(
    tools: SearchTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A minimized overview still tells the agent writes are blocked."""
    monkeypatch.setenv("READ_ONLY_MODE", "true")
    reset_global_settings()

    result = await tools.ha_get_overview(fields=["system_info"])

    assert "system_info" in result
    assert result.get("read_only_mode") is True
    assert "Read Only Mode is ON" in result.get("read_only_mode_hint", "")


# the server's own update status and the calling client


async def test_ha_mcp_update_present_and_survives_projection(
    tools: SearchTools, monkeypatch: pytest.MonkeyPatch, pypi: PyPI
) -> None:
    monkeypatch.delenv(update_check.DISABLE_ENV, raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(update_check, "get_version", lambda: "7.8.0")
    pypi.publish("ha-mcp", "7.9.0")

    result = await tools.ha_get_overview(fields=["system_info"])

    assert "system_info" in result
    notice = result["ha_mcp_update"]
    assert (notice["current"], notice["latest"], notice["update_available"]) == (
        "7.8.0", "7.9.0", True
    )
    assert pypi.asked == ["ha-mcp"]


async def test_ha_mcp_update_absent_when_check_opted_out(tools: SearchTools) -> None:
    """With ``HA_MCP_DISABLE_UPDATE_CHECK`` set (as every unit test runs) the
    key is omitted entirely, not emitted as null."""
    result = await tools.ha_get_overview()

    assert "ha_mcp_update" not in result


async def test_mcp_client_reports_the_calling_client_through_projection(
    core_client: HomeAssistantClient,
) -> None:
    """Diagnosing a client needs no extra call: even a narrow overview names
    the client and the capabilities it declared."""
    mcp = FastMCP("overview")
    register_search_tools(
        mcp, core_client, smart_tools=SmartSearchTools(client=core_client)
    )

    async def elicit(*_args: Any) -> None:
        return None

    async with Client(
        mcp,
        client_info=Implementation(name="openai-mcp", version="1.0.0"),
        elicitation_handler=elicit,
    ) as client:
        call = await client.call_tool("ha_get_overview", {"fields": ["system_info"]})

    reported = call.structured_content["mcp_client"]
    assert reported["client_info"] == {
        "name": "openai-mcp",
        "version": "1.0.0",
        "title": "",
    }
    assert "elicitation.form" in reported["capabilities"]
    assert reported["protocol_version"] != "unknown"


# keys the fields= docstring promises


async def test_notifications_emitted_as_empty_list_when_none(
    tools: SearchTools, answering: None
) -> None:
    result = await tools.ha_get_overview()

    assert result["notifications"] == []
    assert result["notification_count"] == 0


async def test_fields_projection_returns_empty_lists_without_warning(
    tools: SearchTools, answering: None
) -> None:
    """On a clean instance both keys are present, so the projection's
    "not found in response" warning stays silent."""
    result = await tools.ha_get_overview(fields=["notifications", "repairs"])

    assert result["notifications"] == []
    assert result["repairs"] == []
    joined = " ".join(str(w) for w in result.get("warnings") or [])
    assert "notifications" not in joined
    assert "repairs" not in joined
